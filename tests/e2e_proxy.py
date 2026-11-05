"""Ponta a ponta pelo proxy LiteLLM real, contra um provedor FALSO local que grava o que recebe.

Prova, sem gastar chave nem rodar modelo:
  1. o provedor nunca recebe CPF, placa, nome, token…;
  2. o agente recebe tudo de volta com o valor real (texto e argumento de tool call),
     com e sem streaming, mesmo com o marcador partido entre pedaços do stream;
  3. o cliente não consegue desligar a proteção pela requisição;
  4. a observabilidade grava uma linha por chamada, sem conteúdo.

Uso: .venv/bin/python tests/e2e_proxy.py
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gw"))

RAIZ = Path(__file__).resolve().parent.parent
TOKEN_GITHUB = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
SENSIVEIS = ["529.982.247-25", "BRA2E19", "Carlos Eduardo Mendes", TOKEN_GITHUB, "(31) 98765-4321"]
PERGUNTA = (
    "Cliente Carlos Eduardo Mendes, CPF 529.982.247-25, placa BRA2E19, "
    f"telefone (31) 98765-4321. Token do sistema: {TOKEN_GITHUB}. Consulte o cadastro."
)
RX_MARCADOR = re.compile(r"\[(?:CPF|PLACA|PESSOA|SEGREDO|TELEFONE)_[0-9a-f]{8}\]")

RECEBIDOS: list[dict] = []
BRUTOS: list[tuple[str, bytes]] = []  # (caminho, corpo) de QUALQUER POST que chegar ao provedor falso


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ProvedorFalso(BaseHTTPRequestHandler):
    """Ecoa a última mensagem do usuário e chama uma ferramenta com o primeiro marcador que viu."""

    def log_message(self, *_):
        pass

    def do_POST(self):
        bruto = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        BRUTOS.append((self.path, bruto))
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return
        corpo = json.loads(bruto)
        if str(corpo.get("model", "")).startswith("erro"):
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "falso fora do ar"}')
            return
        RECEBIDOS.append(corpo)
        texto_xml = '<atem:invoke name="buscar"><atem:parameter name="caminho">/tmp</atem:parameter></atem:invoke>'
        if str(corpo.get("model", "")).startswith("xml") and corpo.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(0, len(texto_xml), 5):  # tag partida entre pedaços
                pedaco = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()), "model": "falso",
                          "choices": [{"index": 0, "delta": {"content": texto_xml[i:i + 5]}, "finish_reason": None}]}
                self.wfile.write(f"data: {json.dumps(pedaco)}\n\n".encode())
            fim = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()), "model": "falso",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            self.wfile.write(f"data: {json.dumps(fim)}\n\ndata: [DONE]\n\n".encode())
            self.wfile.flush()
            return
        if str(corpo.get("model", "")).startswith("xml") and not corpo.get("stream"):
            dados = json.dumps({"id": "c1", "object": "chat.completion", "created": int(time.time()), "model": "falso",
                                "choices": [{"index": 0, "finish_reason": "stop",
                                             "message": {"role": "assistant", "content": texto_xml}}],
                                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(dados)))
            self.end_headers()
            self.wfile.write(dados)
            return
        ultima = next(m for m in reversed(corpo["messages"]) if m["role"] == "user")
        texto = ultima["content"] if isinstance(ultima["content"], str) else ultima["content"][0]["text"]
        marcadores = RX_MARCADOR.findall(texto)
        resposta = f"Recebi: {texto}"
        args = json.dumps({"documento": marcadores[0] if marcadores else "nenhum", "obs": " ".join(marcadores)})
        if corpo.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def envia(delta, fim=None):
                pedaco = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()),
                          "model": "falso", "choices": [{"index": 0, "delta": delta, "finish_reason": fim}]}
                self.wfile.write(f"data: {json.dumps(pedaco)}\n\n".encode())
                self.wfile.flush()

            envia({"role": "assistant", "content": ""})
            for i in range(0, len(resposta), 3):  # 3 caracteres por pedaço: marcador sempre partido
                envia({"content": resposta[i:i + 3]})
            envia({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                   "function": {"name": "consulta_cadastro", "arguments": ""}}]})
            for i in range(0, len(args), 4):
                envia({"tool_calls": [{"index": 0, "function": {"arguments": args[i:i + 4]}}]})
            envia({}, "tool_calls")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        saida = {
            "id": "c1", "object": "chat.completion", "created": int(time.time()), "model": "falso",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": resposta,
                "tool_calls": [{"id": "call_1", "type": "function",
                                "function": {"name": "consulta_cadastro", "arguments": args}}],
            }}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
        }
        dados = json.dumps(saida).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)


def checa(cond: bool, msg: str, falhas: list[str]) -> None:
    print(("  ok   " if cond else "  FALHA ") + msg)
    if not cond:
        falhas.append(msg)


def main() -> int:
    falhas: list[str] = []
    p_falso, p_proxy = porta_livre(), porta_livre()
    servidor = ThreadingHTTPServer(("127.0.0.1", p_falso), ProvedorFalso)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()

    config = RAIZ / "config.e2e.yaml"
    base_falso = f"http://127.0.0.1:{p_falso}/v1"
    env_cat = {**os.environ, "GW_CATALOGO": str(RAIZ / "tests/catalogo_e2e.yaml"), "GW_E2E_BASE": base_falso,
               "GW_QUALIDADE": "/nao/existe.json"}
    subprocess.run([str(RAIZ / ".venv/bin/python"), str(RAIZ / "gerar_config.py"), "--saida", str(config)],
                   env=env_cat, check=True, capture_output=True)
    pasta_log = Path(tempfile.mkdtemp(prefix="gw-e2e-"))
    chave = "sk-teste-" + secrets.token_hex(8)
    env = {**env_cat, "GW_MASTER_KEY": chave, "GW_CHAVE_MASCARA": secrets.token_hex(32),
           "GW_LOG_DIR": str(pasta_log), "LITELLM_LOG": "ERROR",
           "GW_AUDIO_CATALOGO": str(RAIZ / "tests/audio_e2e.yaml"), "GW_AUDIO_SAIDAS": str(pasta_log / "audio"),
           "GW_AUDIO_OCIOSO_S": "5",
           # Simula chave de provedor herdada do shell: as rotas pass-through do
           # LiteLLM usariam esta chave e mandariam o pedido sem máscara.
           "OPENAI_API_KEY": "sk-vazada-" + secrets.token_hex(8), "GEMINI_API_KEY": "AIza-vazada",
           "ANTHROPIC_API_KEY": "sk-ant-vazada"}
    proxy = subprocess.Popen(
        [str(RAIZ / ".venv/bin/python"), str(RAIZ / "gw/servidor.py"), "--config", str(config),
         "--host", "127.0.0.1", "--port", str(p_proxy)],
        env=env, cwd=RAIZ, stdout=open(pasta_log / "proxy.log", "w"), stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{p_proxy}"
    try:
        limite = time.time() + 120
        while time.time() < limite:
            if proxy.poll() is not None:
                print((pasta_log / "proxy.log").read_text()[-3000:])
                return 1
            try:
                if httpx.get(f"{base}/health/liveliness", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            print("proxy não subiu");
            return 1

        cab = {"Authorization": f"Bearer {chave}"}
        pedido = {"model": "teste", "user": "agente-e2e", "messages": [
            {"role": "system", "content": "Você é um agente de cadastro."},
            {"role": "user", "content": PERGUNTA},
        ]}

        print("1. sem streaming")
        r = httpx.post(f"{base}/v1/chat/completions", json=pedido, headers=cab, timeout=60)
        checa(r.status_code == 200, f"HTTP 200 (veio {r.status_code})", falhas)
        enviado = json.dumps(RECEBIDOS[-1], ensure_ascii=False)
        for v in SENSIVEIS:
            checa(v not in enviado, f"provedor NÃO recebeu {v[:12]}…", falhas)
        checa(len(set(RX_MARCADOR.findall(enviado))) >= 5, "provedor recebeu os 5 marcadores", falhas)
        checa("Aviso do gateway" in RECEBIDOS[-1]["messages"][0]["content"], "aviso de marcadores no system", falhas)
        msg = r.json()["choices"][0]["message"]
        for v in SENSIVEIS:
            checa(v in msg["content"], f"agente recebeu de volta {v[:12]}…", falhas)
        args = json.loads(msg["tool_calls"][0]["function"]["arguments"])
        checa(args["documento"] in SENSIVEIS, f"argumento da tool com valor real ({args['documento'][:12]}…)", falhas)

        print("2. com streaming (marcador partido de 3 em 3 caracteres)")
        texto, args_txt = "", ""
        with httpx.stream("POST", f"{base}/v1/chat/completions", json={**pedido, "stream": True},
                          headers=cab, timeout=60) as rs:
            checa(rs.status_code == 200, f"HTTP 200 (veio {rs.status_code})", falhas)
            for linha in rs.iter_lines():
                if not linha.startswith("data: ") or linha.endswith("[DONE]"):
                    continue
                for ch in json.loads(linha[6:]).get("choices", []):
                    d = ch.get("delta") or {}
                    texto += d.get("content") or ""
                    for tc in d.get("tool_calls") or []:
                        args_txt += (tc.get("function") or {}).get("arguments") or ""
        enviado = json.dumps(RECEBIDOS[-1], ensure_ascii=False)
        for v in SENSIVEIS:
            checa(v not in enviado, f"provedor NÃO recebeu {v[:12]}… (stream)", falhas)
        checa(not RX_MARCADOR.search(texto), "nenhum marcador sobrou no texto do stream", falhas)
        for v in SENSIVEIS:
            checa(v in texto, f"stream devolveu {v[:12]}…", falhas)
        try:
            args = json.loads(args_txt)
            checa(args["documento"] in SENSIVEIS and not RX_MARCADOR.search(args_txt),
                  "argumentos da tool no stream com valor real", falhas)
        except json.JSONDecodeError:
            checa(False, f"argumentos da tool no stream viraram JSON inválido: {args_txt!r}", falhas)

        print("3. cliente tentando desligar a proteção")
        burla = {**pedido, "metadata": {"disable_global_guardrail": True, "opted_out_global_guardrails": ["*"]},
                 "disable_global_guardrail": True}
        httpx.post(f"{base}/v1/chat/completions", json=burla, headers=cab, timeout=60)
        enviado = json.dumps(RECEBIDOS[-1], ensure_ascii=False)
        checa(all(v not in enviado for v in SENSIVEIS), "proteção continua valendo", falhas)

        print("4. observabilidade")
        time.sleep(2)  # o log de sucesso é assíncrono
        arq = pasta_log / "requisicoes.jsonl"
        linhas = [json.loads(x) for x in arq.read_text().splitlines()] if arq.exists() else []
        checa(len(linhas) >= 3, f"3+ linhas no requisicoes.jsonl (veio {len(linhas)})", falhas)
        if linhas:
            ex = linhas[0]
            print("     exemplo:", json.dumps({k: ex.get(k) for k in
                  ("status", "rota", "provedor", "latencia_ms", "tokens_entrada", "mascarados", "agente")},
                  ensure_ascii=False))
            checa(all(x.get("mascarados") for x in linhas), "toda linha traz a contagem do que foi mascarado", falhas)
            bruto = arq.read_text()
            checa(all(v not in bruto for v in SENSIVEIS), "log não contém dado sensível", falhas)

        print("5. imagem não inspecionável é barrada (padrão GW_IMAGENS=bloquear)")
        antes = len(RECEBIDOS)
        com_imagem = {**pedido, "messages": [{"role": "user", "content": [
            {"type": "text", "text": "lê essa CNH"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
        ]}]}
        r = httpx.post(f"{base}/v1/chat/completions", json=com_imagem, headers=cab, timeout=60)
        checa(r.status_code == 400, f"HTTP 400 com imagem (veio {r.status_code})", falhas)
        checa(len(RECEBIDOS) == antes, "provedor não recebeu nada", falhas)

        print("6. rotas fora da lista (pass-through, Responses, Messages, admin) fechadas")
        brutos_antes = len(BRUTOS)
        cpf = {"model": "teste", "input": PERGUNTA, "messages": [{"role": "user", "content": PERGUNTA}],
               "contents": [{"parts": [{"text": PERGUNTA}]}], "max_tokens": 10}
        for metodo, rota in [
            ("POST", "/v1/responses"), ("POST", "/v1/messages"), ("POST", "/v1/embeddings"),
            ("POST", "/openai/v1/chat/completions"), ("POST", "/openai/chat/completions"),
            ("POST", "/gemini/v1beta/models/gemini-2.5-flash:generateContent"),
            ("POST", "/anthropic/v1/messages"), ("POST", "/v1/completions"),
            ("POST", "/key/generate"), ("GET", "/key/list"), ("GET", "/ui"),
        ]:
            r = httpx.request(metodo, f"{base}{rota}", json=cpf if metodo == "POST" else None, headers=cab, timeout=30)
            checa(r.status_code == 403, f"{metodo} {rota} → 403 (veio {r.status_code})", falhas)
        checa(len(BRUTOS) == brutos_antes, "provedor falso não recebeu nada pelas rotas fechadas", falhas)
        checa(all(v.encode() not in b for _, b in BRUTOS for v in SENSIVEIS),
              "nenhum corpo bruto que chegou ao provedor tem dado sensível", falhas)

        print("7. roteamento inteligente (nota 90 que dá erro × nota 50 que funciona)")
        r = httpx.post(f"{base}/v1/chat/completions", json={**pedido, "model": "inteligente"}, headers=cab, timeout=60)
        checa(r.status_code == 200, f"HTTP 200 (veio {r.status_code})", falhas)
        rot = [json.loads(x) for x in (pasta_log / "roteamento.jsonl").read_text().splitlines()]
        ult = rot[-1]
        checa(ult["escolhido"] == "m:falso/erro-1", f"escolheu primeiro o de maior nota ({ult['escolhido']})", falhas)
        checa(ult["atendeu"] == "m:falso/bom-1" and ult["tentativas"] == 2,
              f"caiu no próximo e registrou quem atendeu ({ult['atendeu']}, {ult['tentativas']} tentativas)", falhas)
        saude = json.loads((pasta_log / "saude.json").read_text())["modelos"]
        checa(saude["m:falso/erro-1"]["ate"] > time.time(), "modelo que falhou ficou em espera", falhas)
        checa(saude["m:falso/bom-1"]["ok"] > 0 and saude["m:falso/bom-1"]["lat_ms"], "sucesso e latência do bom medidos", falhas)
        r = httpx.post(f"{base}/v1/chat/completions", json={**pedido, "model": "inteligente"}, headers=cab, timeout=60)
        ult = json.loads((pasta_log / "roteamento.jsonl").read_text().splitlines()[-1])
        checa(ult["escolhido"] == "m:falso/bom-1" and ult["tentativas"] == 1,
              "na chamada seguinte já escolhe o bom direto (o outro está em espera)", falhas)

        print("7b. pedido com ferramenta só vai para modelo com tool call provada")
        ferramenta = [{"type": "function", "function": {"name": "consulta_cadastro", "parameters": {"type": "object"}}}]
        r = httpx.post(f"{base}/v1/chat/completions", json={**pedido, "model": "inteligente", "tools": ferramenta},
                       headers=cab, timeout=60)
        ult = json.loads((pasta_log / "roteamento.jsonl").read_text().splitlines()[-1])
        checa(r.status_code == 200 and ult["ordem"] == ["m:falso/bom-1"],
              f"rota com tools: plano só com o provado ({r.status_code}, {ult['ordem']})", falhas)
        brutos_antes = len(BRUTOS)
        r = httpx.post(f"{base}/v1/chat/completions", json={**pedido, "model": "so-novo", "tools": ferramenta},
                       headers=cab, timeout=60)
        checa(r.status_code == 503 and "tool call provada" in r.text,
              f"rota sem modelo com tool provada → 503 com o motivo (veio {r.status_code})", falhas)
        checa(len(BRUTOS) == brutos_antes, "provedor não recebeu o pedido recusado", falhas)
        r = httpx.post(f"{base}/v1/chat/completions", json={**pedido, "model": "so-novo"}, headers=cab, timeout=60)
        checa(r.status_code == 200, f"mesma rota sem tools continua atendendo (veio {r.status_code})", falhas)

        print("7c. tool call escrita como texto tira o modelo da fila de tools")
        r = httpx.post(f"{base}/v1/chat/completions", json={**pedido, "model": "m:falso/xml-1", "tools": ferramenta},
                       headers=cab, timeout=60)
        ult = json.loads((pasta_log / "roteamento.jsonl").read_text().splitlines()[-1])
        saude = json.loads((pasta_log / "saude.json").read_text())["modelos"]
        checa(r.status_code == 200 and ult["status"] == "tool_em_texto",
              f"registrado como tool_em_texto ({r.status_code}, {ult['status']})", falhas)
        checa(saude["m:falso/xml-1"]["tools"] is False, "modelo marcado como ferramenta falhou", falhas)
        r = httpx.post(f"{base}/v1/chat/completions", json={**pedido, "model": "m:falso/bom-1", "tools": ferramenta},
                       headers=cab, timeout=60)
        ult = json.loads((pasta_log / "roteamento.jsonl").read_text().splitlines()[-1])
        checa(ult["status"] == "ok", f"tool call de verdade continua ok ({ult['status']})", falhas)
        r = httpx.post(f"{base}/v1/chat/completions",
                       json={**pedido, "model": "m:falso/xml-1", "tools": ferramenta, "stream": True},
                       headers=cab, timeout=60)
        time.sleep(0.5)
        ult = json.loads((pasta_log / "roteamento.jsonl").read_text().splitlines()[-1])
        checa(r.status_code == 200 and ult["status"] == "tool_em_texto" and ult["stream"],
              f"com streaming também ({r.status_code}, {ult['status']})", falhas)

        print("8. painel")
        r = httpx.get(f"{base}/painel", timeout=10)
        checa(r.status_code == 200 and "ai-gateway" in r.text, f"GET /painel → 200 (veio {r.status_code})", falhas)
        r = httpx.get(f"{base}/painel/dados", timeout=10)
        checa(r.status_code == 200, f"GET /painel/dados → 200 (veio {r.status_code})", falhas)
        if r.status_code == 200:
            pd = r.json()
            rk = pd["rotas"]["inteligente"]["ranking"]
            checa(rk[0]["nome"] == "m:falso/bom-1" and "em espera" in (rk[-1]["fora"] or ""),
                  "ranking do painel mostra o bom em 1º e o outro em espera", falhas)
            checa(pd["hoje"]["chamadas"] >= 2 and pd["hoje"]["caiu_para_fallback"] >= 1, "números do dia batem", falhas)
            checa(all(v not in r.text for v in SENSIVEIS) and chave not in r.text,
                  "painel sem dado sensível e sem a chave", falhas)

        print("9. chat: só o próprio painel consegue mandar ação para um harness")
        ok_cab = {"Content-Type": "application/json", "X-GW-Painel": "1", "Origin": base}
        r = httpx.get(f"{base}/painel/chat/estado", timeout=10)
        checa(r.status_code == 200 and "hermes" in r.json(), f"GET estado → 200 (veio {r.status_code})", falhas)
        for nome, cab, corpo in [
            ("sem o header do painel", {"Content-Type": "application/json", "Origin": base}, '{"nome":"hermes"}'),
            ("origem de outro site", {**ok_cab, "Origin": "http://site-malicioso.com"}, '{"nome":"hermes"}'),
            ("text/plain (sem preflight)", {**ok_cab, "Content-Type": "text/plain"}, '{"nome":"hermes"}'),
            ("Host falso (DNS rebinding)", {**ok_cab, "Host": "evil.com:4000"}, '{"nome":"hermes"}'),
        ]:
            r = httpx.post(f"{base}/painel/chat/harness", headers=cab, content=corpo, timeout=10)
            checa(r.status_code == 403, f"{nome} → 403 (veio {r.status_code})", falhas)
        estado_chat = httpx.get(f"{base}/painel/chat/estado", timeout=10).json()
        checa(all(v["status"] == "parado" for v in estado_chat.values()), "nenhum harness foi ligado pelas tentativas", falhas)

        print("10. áudio: texto longo vira um OGG só, com progresso, Range e motor que desliga sozinho")
        texto = " ".join(f"Frase número {i} do teste de áudio longo." for i in range(1, 13)) + "\n\nSegundo parágrafo."
        r = httpx.post(f"{base}/painel/audio/gerar", headers=ok_cab, json={"motor": "falso", "texto": texto, "voz": "teste_a"}, timeout=20)
        checa(r.status_code == 200, f"pedido aceito (veio {r.status_code})", falhas)
        tid = r.json().get("id") if r.status_code == 200 else None
        tarefa = {}
        limite = time.time() + 90
        while tid and time.time() < limite:
            est = httpx.get(f"{base}/painel/audio/estado", timeout=10).json()
            tarefa = next((t for t in est["tarefas"] if t["id"] == tid), {})
            if tarefa.get("status") in ("pronto", "erro", "cancelado"):
                break
            time.sleep(0.5)
        checa(tarefa.get("status") == "pronto", f"tarefa pronta (status {tarefa.get('status')}, erro {tarefa.get('erro')})", falhas)
        checa(tarefa.get("total", 0) > 1 and tarefa.get("feito") == tarefa.get("total"), f"gerou em {tarefa.get('total')} trechos", falhas)
        if tid:
            r = httpx.get(f"{base}/painel/audio/arquivo/{tid}.ogg", timeout=20)
            checa(r.status_code == 200 and r.content[:4] == b"OggS", "arquivo é OGG", falhas)
            import soundfile as sf
            import io as _io
            info = sf.info(_io.BytesIO(r.content))
            esperado = sum(0.05 * len(tr["texto"]) + tr["pausa"] for tr in __import__("audio").dividir(texto, 60))
            checa(info.subtype == "OPUS" and abs(info.duration - esperado) < 0.6,
                  f"Opus com {info.duration:.1f} s (esperado {esperado:.1f} s com as pausas)", falhas)
            r = httpx.get(f"{base}/painel/audio/arquivo/{tid}.ogg", headers={"Range": "bytes=10-99"}, timeout=10)
            checa(r.status_code == 206 and len(r.content) == 90, f"Range → 206 com 90 bytes (veio {r.status_code}, {len(r.content)})", falhas)
        r = httpx.post(f"{base}/painel/audio/gerar", headers={"Content-Type": "application/json"}, json={"motor": "falso", "texto": "x"}, timeout=10)
        checa(r.status_code == 403, f"gerar sem o header do painel → 403 (veio {r.status_code})", falhas)
        limite = time.time() + 40
        while time.time() < limite:
            m = httpx.get(f"{base}/painel/audio/estado", timeout=10).json()["motores"]["falso"]
            if m["status"] == "parado":
                break
            time.sleep(1)
        checa(m["status"] == "parado", f"motor desligou sozinho depois de parado (status {m['status']})", falhas)
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proxy.kill()
        servidor.shutdown()
        config.unlink(missing_ok=True)

    print(f"\n{'TUDO OK' if not falhas else f'{len(falhas)} FALHA(S)'}  (log do proxy: {pasta_log}/proxy.log)")
    return 0 if not falhas else 1


if __name__ == "__main__":
    sys.exit(main())
