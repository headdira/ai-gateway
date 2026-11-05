"""Harness de agente servido pelo gateway: Hermes (Nous Research).

Só um roda por vez: ligar um desliga o outro. Todos usam o próprio gateway como
cérebro (http://127.0.0.1:<GW_PORTA>/v1, rota "geral"), então passam pela máscara de
dado sensível, pelo roteamento inteligente e aparecem no painel.

Cada harness sobe:
- em sessão própria (start_new_session), para encerrar o grupo inteiro (MCPs filhos);
- com ambiente MONTADO aqui (PATH completo, HOME) e nenhuma chave de provedor do
  gateway; o PID vai para logs/harness-<nome>.pid e o parar.sh derruba junto;
- numa porta livre escolhida na hora.

O chat fala com cada um pelo jeito dele e devolve um formato único de eventos:
  texto, raciocinio, ferramenta, aprovacao, pergunta, fim, erro.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import socket
import subprocess
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

RAIZ = Path(__file__).resolve().parent.parent
LOGS = Path(os.environ.get("GW_LOG_DIR", RAIZ / "logs"))
HOME = os.environ.get("HOME", str(Path.home()))
# Onde o hermes-agent está instalado (git clone + `uv sync`). Defina GW_HERMES_DIR no .env.
HERMES_DIR = Path(os.environ.get("GW_HERMES_DIR", Path(HOME) / "hermes-agent")).expanduser()
HERMES_HOME = Path(os.environ.get("GW_HERMES_HOME", RAIZ / "harness/hermes-home"))
SUBIDA_MAX_S = 150

NOMES = {"hermes": "Hermes"}


def _porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _gateway() -> tuple[str, str]:
    porta = os.environ.get("GW_PORTA_EFETIVA") or os.environ.get("GW_PORTA") or "4000"
    return f"http://127.0.0.1:{porta}/v1", os.environ.get("GW_MASTER_KEY", "")


def _env_base() -> dict[str, str]:
    return {
        "PATH": f"/opt/homebrew/bin:/usr/local/bin:{HOME}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": HOME,
        "LANG": os.environ.get("LANG", "pt_BR.UTF-8"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }


class Gerente:
    def __init__(self) -> None:
        self.estado: dict[str, dict[str, Any]] = {n: {"status": "parado"} for n in NOMES}
        self.procs: dict[str, subprocess.Popen] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ ciclo de vida
    def status(self) -> dict:
        for nome, p in list(self.procs.items()):
            if p.poll() is not None and self.estado[nome]["status"] in ("ligando", "no_ar"):
                self.estado[nome].update(status="erro", erro=f"o processo saiu (código {p.returncode}); veja logs/harness-{nome}.log")
                self.procs.pop(nome, None)
        disponivel = {"hermes": (HERMES_DIR / ".venv/bin/hermes").exists()}
        return {n: {**{k: v for k, v in e.items() if k != "chave"}, "nome": NOMES[n], "instalado": disponivel[n]}
                for n, e in self.estado.items()}

    async def ligar(self, nome: str) -> dict:
        if nome not in NOMES:
            raise ValueError("harness desconhecido")
        async with self._lock:
            for outro in NOMES:
                if outro != nome:
                    self.desligar(outro)
            if self.estado[nome]["status"] in ("ligando", "no_ar") and nome in self.procs and self.procs[nome].poll() is None:
                return self.status()
            porta = _porta_livre()
            cmd, cwd, env, chave = self._prepara_hermes(porta)
            LOGS.mkdir(parents=True, exist_ok=True)
            saida = (LOGS / f"harness-{nome}.log").open("ab")
            p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=saida, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
            (LOGS / f"harness-{nome}.pid").write_text(str(p.pid))
            self.procs[nome] = p
            self.estado[nome] = {"status": "ligando", "porta": porta, "pid": p.pid, "desde": time.time(), "chave": chave}
            asyncio.get_running_loop().create_task(self._espera_subir(nome, porta, p))
            return self.status()

    def desligar(self, nome: str) -> None:
        p = self.procs.pop(nome, None)
        arq = LOGS / f"harness-{nome}.pid"
        pid = p.pid if p else (int(arq.read_text()) if arq.exists() and arq.read_text().strip().isdigit() else None)
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)  # o grupo é o PID: subiu com start_new_session
            except ProcessLookupError:
                pass
            limite = time.time() + 10
            while p and p.poll() is None and time.time() < limite:
                time.sleep(0.2)
            if p and p.poll() is None:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        arq.unlink(missing_ok=True)
        self.estado[nome] = {"status": "parado"}

    def desligar_todos(self) -> None:
        for nome in NOMES:
            self.desligar(nome)

    async def _espera_subir(self, nome: str, porta: int, p: subprocess.Popen) -> None:
        limite = time.time() + SUBIDA_MAX_S
        async with httpx.AsyncClient(timeout=2) as c:
            while time.time() < limite:
                if p.poll() is not None:
                    self.estado[nome].update(status="erro", erro=f"o processo saiu (código {p.returncode}); veja logs/harness-{nome}.log")
                    return
                try:
                    if (await c.get(f"http://127.0.0.1:{porta}/health")).status_code == 200:
                        self.estado[nome]["status"] = "no_ar"
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
        self.estado[nome].update(status="erro", erro=f"não respondeu em {SUBIDA_MAX_S} s")

    # ------------------------------------------------------------- preparação
    @staticmethod
    def _prepara_hermes(porta: int):
        base, chave_gw = _gateway()
        HERMES_HOME.mkdir(parents=True, exist_ok=True)
        chave_api = secrets.token_hex(16)
        (HERMES_HOME / "config.yaml").write_text(
            "# Gerado pelo ai-gateway (gw/harness.py) a cada subida. Não editar à mão.\n"
            "model:\n"
            "  provider: custom\n"
            f"  base_url: {base}\n"
            f"  api_key: {chave_gw}\n"
            "  default: geral\n"
            "  default_headers:\n"
            "    X-GW-Agente: hermes-chat\n"
            "# Comando perigoso SEMPRE pergunta para você no chat do painel (o padrão do\n"
            "# Hermes é 'smart': outro LLM decide sozinho).\n"
            "approvals:\n"
            "  mode: manual\n"
            "  timeout: 600\n",
            encoding="utf-8",
        )
        (HERMES_HOME / ".env").write_text(
            f"API_SERVER_ENABLED=true\nAPI_SERVER_KEY={chave_api}\nAPI_SERVER_PORT={porta}\nAPI_SERVER_HOST=127.0.0.1\n",
            encoding="utf-8",
        )
        for arq in ("config.yaml", ".env"):
            os.chmod(HERMES_HOME / arq, 0o600)
        env = {**_env_base(), "HERMES_HOME": str(HERMES_HOME)}
        return [str(HERMES_DIR / ".venv/bin/hermes"), "gateway"], str(HERMES_DIR), env, chave_api

    # ------------------------------------------------------------------ chat
    def _alvo(self, nome: str) -> tuple[int, str | None]:
        e = self.estado.get(nome) or {}
        if e.get("status") != "no_ar":
            raise RuntimeError(f"{NOMES.get(nome, nome)} não está no ar")
        return e["porta"], e.get("chave")

    async def turno(self, nome: str, mensagem: str, sessao: str) -> AsyncIterator[dict]:
        porta, chave = self._alvo(nome)
        async for ev in _turno_hermes(porta, chave or "", mensagem, sessao):
            yield ev

    async def decidir(self, nome: str, corpo: dict) -> dict:
        porta, chave = self._alvo(nome)
        async with httpx.AsyncClient(timeout=20) as c:
            ref = corpo.get("ref") or {}
            cab = {"Authorization": f"Bearer {chave}"}
            if corpo.get("tipo") == "pergunta":
                r = await c.post(f"http://127.0.0.1:{porta}/v1/runs/{ref['run_id']}/clarify", headers=cab, json={
                    "request_id": ref["request_id"], "execution_generation": ref["execution_generation"],
                    "answer": (corpo.get("respostas") or [""])[0]})
            else:
                r = await c.post(f"http://127.0.0.1:{porta}/v1/runs/{ref['run_id']}/approval", headers=cab, json={
                    "request_id": ref["request_id"], "execution_generation": ref["execution_generation"],
                    "choice": "once" if corpo.get("aprovar") else "deny"})
        return {"ok": r.status_code < 300, "http": r.status_code}


# ---------------------------------------------------------------- utilidades
async def _linhas_sse(resp: httpx.Response) -> AsyncIterator[dict]:
    async for ln in resp.aiter_lines():
        if ln.startswith("data:"):
            try:
                yield json.loads(ln[5:].strip())
            except ValueError:
                continue


def _txt(v: Any, n: int = 600) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"


# ------------------------------------------------------------ Hermes (Runs API)
async def _turno_hermes(porta: int, chave: str, mensagem: str, sessao: str) -> AsyncIterator[dict]:
    base = f"http://127.0.0.1:{porta}"
    cab = {"Authorization": f"Bearer {chave}"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10)) as c:
        r = await c.post(f"{base}/v1/runs", headers=cab, json={"input": mensagem, "session_id": sessao})
        if r.status_code >= 300:
            yield {"t": "erro", "msg": f"Hermes respondeu HTTP {r.status_code}: {r.text[:300]}"}
            return
        run_id = r.json()["run_id"]
        vistos: set[str] = set()
        async with c.stream("GET", f"{base}/v1/runs/{run_id}/events", headers=cab) as s:
            async for e in _linhas_sse(s):
                ev = e.get("event")
                if ev == "message.delta" and e.get("delta"):
                    yield {"t": "texto", "d": e["delta"]}
                elif ev in ("reasoning.delta", "thinking.delta") and e.get("delta"):
                    yield {"t": "raciocinio", "d": e["delta"]}
                elif ev in ("tool.started", "tool.completed"):
                    yield {"t": "ferramenta", "id": e.get("tool_call_id") or f"{e.get('tool')}-{e.get('seq')}",
                           "nome": e.get("tool"), "fase": "inicio" if ev == "tool.started" else "fim",
                           "args": _txt(e.get("preview") or "") if ev == "tool.started" else "",
                           "resultado": _txt(e.get("preview") or "", 800) if ev == "tool.completed" else "",
                           "erro": bool(e.get("error"))}
                elif ev in ("approval.request", "clarify.request"):
                    st = (await c.get(f"{base}/v1/runs/{run_id}", headers=cab)).json()
                    for ctl in st.get("pending_controls") or []:
                        pid = ctl.get("prompt_id") or ctl.get("request_id")
                        if not pid or pid in vistos:
                            continue
                        vistos.add(pid)
                        ref = {"run_id": run_id, "request_id": pid, "execution_generation": ctl.get("execution_generation")}
                        texto = ctl.get("description") or ctl.get("question") or ctl.get("command") or ctl.get("title") or ""
                        if ev == "clarify.request" or ctl.get("kind") == "clarify":
                            yield {"t": "pergunta", "id": pid, "perguntas": [texto or "O Hermes precisa de uma resposta"],
                                   "contexto": _txt(ctl, 800), "ref": ref}
                        else:
                            yield {"t": "aprovacao", "id": pid, "descricao": ctl.get("description") or "O Hermes pede aprovação",
                                   "detalhe": _txt(ctl.get("command") or ctl, 1500), "ref": ref}
                elif ev == "run.completed":
                    u = e.get("usage") or {}
                    yield {"t": "fim", "uso": {"in": u.get("input_tokens"), "out": u.get("output_tokens")},
                           "final": e.get("output")}
                    return
                elif ev in ("run.failed", "run.cancelled", "run.interrupted"):
                    yield {"t": "erro", "msg": _txt(e.get("error") or ev, 400)}
                    return


gerente = Gerente()
