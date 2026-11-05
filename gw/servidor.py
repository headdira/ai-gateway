"""Sobe o proxy LiteLLM atrás de um filtro de rotas (lista de permitidas).

Por quê: o LiteLLM expõe dezenas de rotas além do chat — pass-through para
provedores (/openai/*, /gemini/*, /anthropic/*…), Responses, Messages, admin.
A proteção de dados só foi PROVADA em /v1/chat/completions (tests/e2e_proxy.py).
Qualquer outra rota responde 403 antes de chegar no LiteLLM, então nada sai por
um caminho que não passa pela máscara. Restringir rotas no próprio LiteLLM
(allowed_routes) é recurso pago; aqui é um embrulho ASGI de 30 linhas.

Uso: .venv/bin/python gw/servidor.py --config config.gerado.yaml --host 127.0.0.1 --port 4000
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
RAIZ = Path(__file__).resolve().parent.parent

ROTAS_ABERTAS = frozenset({
    ("POST", "/v1/chat/completions"),
    ("POST", "/chat/completions"),
    ("GET", "/v1/models"),
    ("GET", "/models"),
    ("GET", "/health/liveliness"),
    ("GET", "/health/readiness"),
    ("GET", "/painel"),
    ("GET", "/painel/dados"),
    ("GET", "/painel/chat/estado"),
    ("POST", "/painel/chat/harness"),
    ("POST", "/painel/chat/desligar"),
    ("POST", "/painel/chat/enviar"),
    ("POST", "/painel/chat/decidir"),
    ("GET", "/painel/audio/estado"),
    ("POST", "/painel/audio/motor"),
    ("POST", "/painel/audio/desligar"),
    ("POST", "/painel/audio/gerar"),
    ("POST", "/painel/audio/cancelar"),
    ("POST", "/painel/audio/apagar"),
})
RX_ARQUIVO_AUDIO = re.compile(r"^/painel/audio/arquivo/([0-9a-f]{12})\.ogg$")
CORPO_MAX = 256_000


def _cabecalhos(scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}


def _post_do_painel_ok(scope) -> str | None:
    """POST do chat executa coisa (os harnesses têm terminal): só aceita o próprio painel.
    JSON + header próprio forçam preflight (que não respondemos), e Host/Origin barram
    outro site e DNS rebinding. Devolve o motivo da recusa, ou None se ok."""
    h = _cabecalhos(scope)
    porta = (scope.get("server") or ("", 4000))[1]
    permitidos = {f"127.0.0.1:{porta}", f"localhost:{porta}"}
    if h.get("host") not in permitidos:
        return "host não permitido"
    origem = h.get("origin")
    if origem and origem.removeprefix("http://") not in permitidos:
        return "origem não permitida"
    if not h.get("content-type", "").startswith("application/json"):
        return "só JSON"
    if h.get("x-gw-painel") != "1":
        return "falta o header do painel"
    return None


async def _le_corpo(receive) -> dict:
    partes, total = [], 0
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            break
        partes.append(msg.get("body", b""))
        total += len(partes[-1])
        if total > CORPO_MAX:
            raise ValueError("corpo grande demais")
        if not msg.get("more_body"):
            break
    return json.loads(b"".join(partes) or b"{}")


async def _arquivo_audio(scope, send, tid: str) -> None:
    from audio import SAIDAS

    arq = SAIDAS / f"{tid}.ogg"
    if not arq.exists():
        await _responde(send, 404, b'{"erro": "nao achei"}', b"application/json")
        return
    tam = arq.stat().st_size
    ini, fim, status = 0, tam - 1, 200
    m = re.match(r"bytes=(\d*)-(\d*)", _cabecalhos(scope).get("range", ""))
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            ini = int(m.group(1))
            fim = min(int(m.group(2)) if m.group(2) else tam - 1, tam - 1)
        else:  # "bytes=-N": últimos N bytes
            ini = max(0, tam - int(m.group(2)))
        status = 206
    if ini > fim:
        await send({"type": "http.response.start", "status": 416, "headers": [(b"content-range", f"bytes */{tam}".encode())]})
        await send({"type": "http.response.body", "body": b""})
        return
    cab = [(b"content-type", b"audio/ogg"), (b"accept-ranges", b"bytes"), (b"content-length", str(fim - ini + 1).encode()),
           (b"content-disposition", f'inline; filename="audio-{tid}.ogg"'.encode())]
    if status == 206:
        cab.append((b"content-range", f"bytes {ini}-{fim}/{tam}".encode()))
    await send({"type": "http.response.start", "status": status, "headers": cab})
    with arq.open("rb") as f:
        f.seek(ini)
        falta = fim - ini + 1
        while falta > 0:
            bloco = f.read(min(1 << 20, falta))
            if not bloco:
                break
            falta -= len(bloco)
            await send({"type": "http.response.body", "body": bloco, "more_body": falta > 0})


async def _audio(scope, receive, send, caminho: str) -> None:
    from audio import gerente_audio

    if caminho == "/painel/audio/estado":
        await _responde(send, 200, json.dumps(gerente_audio.status(), ensure_ascii=False).encode(), b"application/json")
        return
    motivo = _post_do_painel_ok(scope)
    if motivo:
        await _responde(send, 403, json.dumps({"erro": motivo}).encode(), b"application/json")
        return
    try:
        corpo = await _le_corpo(receive)
        if caminho == "/painel/audio/motor":
            r = await gerente_audio.ligar(str(corpo.get("nome")))
        elif caminho == "/painel/audio/desligar":
            r = gerente_audio.descarregar()
        elif caminho == "/painel/audio/gerar":
            r = await gerente_audio.criar(corpo)
        elif caminho == "/painel/audio/cancelar":
            gerente_audio.cancelar(str(corpo.get("id")))
            r = {"ok": True}
        else:
            gerente_audio.apagar(str(corpo.get("id")))
            r = {"ok": True}
        await _responde(send, 200, json.dumps(r, ensure_ascii=False).encode(), b"application/json")
    except (ValueError, RuntimeError, KeyError, json.JSONDecodeError) as e:
        await _responde(send, 409, json.dumps({"erro": str(e)}, ensure_ascii=False).encode(), b"application/json")


async def _chat(scope, receive, send, caminho: str) -> None:
    from harness import gerente

    if caminho == "/painel/chat/estado":
        await _responde(send, 200, json.dumps(gerente.status()).encode(), b"application/json")
        return
    motivo = _post_do_painel_ok(scope)
    if motivo:
        await _responde(send, 403, json.dumps({"erro": motivo}).encode(), b"application/json")
        return
    try:
        corpo = await _le_corpo(receive)
    except (ValueError, json.JSONDecodeError) as e:
        await _responde(send, 400, json.dumps({"erro": str(e)}).encode(), b"application/json")
        return
    try:
        if caminho == "/painel/chat/harness":
            estado = await gerente.ligar(str(corpo.get("nome")))
            await _responde(send, 200, json.dumps(estado).encode(), b"application/json")
        elif caminho == "/painel/chat/desligar":
            gerente.desligar_todos()
            await _responde(send, 200, json.dumps(gerente.status()).encode(), b"application/json")
        elif caminho == "/painel/chat/decidir":
            r = await gerente.decidir(str(corpo.get("harness")), corpo)
            await _responde(send, 200, json.dumps(r).encode(), b"application/json")
        elif caminho == "/painel/chat/enviar":
            mensagem = str(corpo.get("mensagem") or "").strip()
            if not mensagem:
                await _responde(send, 400, b'{"erro": "mensagem vazia"}', b"application/json")
                return
            await send({"type": "http.response.start", "status": 200, "headers": [
                (b"content-type", b"text/event-stream"), (b"cache-control", b"no-store"), (b"x-accel-buffering", b"no")]})
            try:
                async for ev in gerente.turno(str(corpo.get("harness")), mensagem, str(corpo.get("sessao") or "painel")):
                    await send({"type": "http.response.body", "body": f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode(), "more_body": True})
            except Exception as e:  # noqa: BLE001 - o erro vira evento do chat
                await send({"type": "http.response.body", "body": f"data: {json.dumps({'t': 'erro', 'msg': str(e)[:300]})}\n\n".encode(), "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
    except (ValueError, RuntimeError, KeyError) as e:
        await _responde(send, 409, json.dumps({"erro": str(e)}, ensure_ascii=False).encode(), b"application/json")


def _ultimas_linhas(arq: Path, n: int) -> list[dict]:
    if not arq.exists():
        return []
    with arq.open("rb") as f:
        f.seek(0, 2)
        tam = f.tell()
        f.seek(max(0, tam - 400_000))
        linhas = f.read().decode("utf-8", "ignore").splitlines()[-n:]
    saida = []
    for ln in linhas:
        try:
            saida.append(json.loads(ln))
        except ValueError:
            continue
    return saida


async def _responde(send, status: int, corpo: bytes, tipo: bytes) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", tipo), (b"content-length", str(len(corpo)).encode()), (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": corpo})


_JANELAS: dict[str, int] | None = None


def _janelas_das_rotas() -> dict[str, int]:
    """rota → contexto anunciado (catalogo.contexto_anunciado), calculado uma vez por subida."""
    global _JANELAS
    if _JANELAS is None:
        import catalogo

        cat = catalogo.carrega()
        ativos = catalogo.itens_ativos(cat)
        _JANELAS = {}
        for rota in cat.get("rotas") or {}:
            ctx = catalogo.contexto_anunciado(cat, rota, ativos)
            if ctx:
                _JANELAS[rota] = ctx
    return _JANELAS


def _anuncia_contexto(corpo: bytes) -> bytes:
    """No /v1/models, troca o tamanho das ROTAS pela janela anunciada.

    O LiteLLM mostra a rota com o maior modelo do grupo (1M). O Hermes lê `context_length`
    (e depois `max_input_tokens`) e resume a conversa pela metade disso; com 1M ele nunca
    resumia. Modelos explícitos (m:…) seguem com o tamanho real.
    """
    try:
        dados = json.loads(corpo)
        janelas = _janelas_das_rotas()
        for m in dados.get("data") or []:
            ctx = janelas.get(m.get("id")) if isinstance(m, dict) else None
            if ctx:
                m["context_length"] = ctx
                m["max_input_tokens"] = ctx
        return json.dumps(dados, ensure_ascii=False).encode()
    except Exception:  # noqa: BLE001 - lista de modelos nunca quebra por causa disto
        return corpo


async def _models_com_contexto(app, scope, receive, send) -> None:
    inicio: dict = {}
    partes: list[bytes] = []

    async def captura(msg):
        if msg["type"] == "http.response.start":
            inicio.update(msg)
            return
        if msg["type"] != "http.response.body":
            await send(msg)
            return
        partes.append(msg.get("body", b""))
        if msg.get("more_body"):
            return
        corpo = b"".join(partes)
        cab = list(inicio.get("headers") or [])
        compactado = any(k.lower() == b"content-encoding" for k, _ in cab)
        if inicio.get("status") == 200 and not compactado:
            corpo = _anuncia_contexto(corpo)
        cab = [(k, v) for k, v in cab if k.lower() != b"content-length"] + [(b"content-length", str(len(corpo)).encode())]
        await send({"type": "http.response.start", "status": inicio.get("status", 200), "headers": cab})
        await send({"type": "http.response.body", "body": corpo})

    await app(scope, receive, captura)


class FiltroDeRotas:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":  # realtime etc.: fechado
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] == "http":
            metodo = scope.get("method", "GET").upper()
            caminho = scope.get("path", "").rstrip("/") or "/"
            arquivo = RX_ARQUIVO_AUDIO.match(caminho) if metodo == "GET" else None
            if arquivo:
                await _arquivo_audio(scope, send, arquivo.group(1))
                return
            if (metodo, caminho) not in ROTAS_ABERTAS:
                corpo = json.dumps({"error": {
                    "type": "gateway_rota_fechada",
                    "message": f"Rota fechada pelo gateway: {metodo} {caminho}. "
                               "Abertas: POST /v1/chat/completions, GET /v1/models, GET /health/*, GET /painel.",
                }}).encode()
                await send({"type": "http.response.start", "status": 403, "headers": [
                    (b"content-type", b"application/json"), (b"content-length", str(len(corpo)).encode())]})
                await send({"type": "http.response.body", "body": corpo})
                return
            if metodo == "GET" and caminho in ("/v1/models", "/models"):
                await _models_com_contexto(self.app, scope, receive, send)
                return
            if caminho.startswith("/painel/audio/"):
                await _audio(scope, receive, send, caminho)
                return
            if caminho.startswith("/painel/chat/"):
                await _chat(scope, receive, send, caminho)
                return
            if caminho == "/painel":
                await _responde(send, 200, (RAIZ / "gw/painel.html").read_bytes(), b"text/html; charset=utf-8")
                return
            if caminho == "/painel/dados":
                try:
                    import painel_dados
                    from urllib.parse import parse_qs

                    q = parse_qs((scope.get("query_string") or b"").decode())
                    periodo = (q.get("periodo") or ["24h"])[0]
                    corpo = json.dumps(painel_dados.montar(periodo), ensure_ascii=False, default=str).encode()
                    await _responde(send, 200, corpo, b"application/json")
                except Exception as e:  # noqa: BLE001 - painel não derruba o gateway
                    await _responde(send, 500, json.dumps({"erro": str(e)}).encode(), b"application/json")
                return
        await self.app(scope, receive, send)  # lifespan e rotas abertas


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4000)
    a = ap.parse_args()
    os.environ["CONFIG_FILE_PATH"] = str(Path(a.config).resolve())

    import uvicorn
    from litellm.proxy.proxy_server import app

    import atexit

    from harness import gerente

    from audio import gerente_audio

    atexit.register(gerente.desligar_todos)  # gateway caiu/parou: harness não fica órfão
    atexit.register(gerente_audio.desligar_todos)  # nem motor de áudio
    uvicorn.run(FiltroDeRotas(app), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
