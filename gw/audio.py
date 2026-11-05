"""Gerador de áudio local (voz a partir de texto e música), servido pelo gateway.

- Cada modelo roda num "motor": processo Python próprio (venv própria em audio/motores/<nome>),
  ligado sob demanda, um por vez, e desligado sozinho depois de GW_AUDIO_OCIOSO_S parado.
  Roda com HF_HUB_OFFLINE=1: nada é baixado nem executado da internet na hora de gerar.
- Protocolo do motor (HTTP local): GET /health → {"ok", "tipo", "taxa", "vozes", "max_chars"};
  POST /voz {"texto","voz","velocidade"} → WAV; POST /musica {"prompt","letra","duracao","semente"} → WAV.
- Texto de qualquer tamanho: corta em parágrafos e frases (até max_chars do motor), gera trecho a
  trecho e grava direto num OGG (Opus para voz em 8/12/16/24/48 kHz; Vorbis no resto). Nunca MP3.
- Cada geração é uma tarefa em segundo plano com progresso, cancelamento e histórico em audio/saidas.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml

RAIZ = Path(__file__).resolve().parent.parent
AUDIO = RAIZ / "audio"
SAIDAS = Path(os.environ.get("GW_AUDIO_SAIDAS", AUDIO / "saidas"))
LOGS = Path(os.environ.get("GW_LOG_DIR", RAIZ / "logs"))
CATALOGO = Path(os.environ.get("GW_AUDIO_CATALOGO", AUDIO / "motores.yaml"))
OCIOSO_S = int(os.environ.get("GW_AUDIO_OCIOSO_S", "180"))
SUBIDA_MAX_S = 300
HOME = os.environ.get("HOME", str(Path.home()))
TAXAS_OPUS = {8000, 12000, 16000, 24000, 48000}

# --------------------------------------------------------------------------- texto
ABREV = re.compile(r"\b(Sr|Sra|Srta|Dr|Dra|Prof|Profa|Exmo|Exma|Av|Ltda|etc|p\.ex|n[ºo]|art|cap|pág|vol)\.$", re.I)


def _frases(paragrafo: str) -> list[str]:
    partes = re.split(r"(?<=[.!?…])\s+", paragrafo.strip())
    frases: list[str] = []
    for p in partes:
        if frases and ABREV.search(frases[-1]):
            frases[-1] += " " + p  # "Dr. Silva" não é fim de frase
        else:
            frases.append(p)
    return [f for f in frases if f.strip()]


def _quebra_longa(frase: str, maximo: int) -> list[str]:
    if len(frase) <= maximo:
        return [frase]
    pedacos, atual = [], ""
    for parte in re.split(r"(?<=[,;:])\s+|\s+", frase):
        if atual and len(atual) + 1 + len(parte) > maximo:
            pedacos.append(atual)
            atual = parte
        else:
            atual = f"{atual} {parte}".strip()
    if atual:
        pedacos.append(atual)
    return pedacos


BLOCO_GRAVACAO = 1 << 15


def _grava_em_blocos(saida, dados) -> None:
    """Grava o áudio em pedaços de 32 k quadros.

    O libvorbis (via libsndfile) faz alloca do tamanho do primeiro bloco: 90 s de música a 48 kHz
    num write só estouram a pilha de 8 MB e derrubam o gateway com SIGSEGV (visto em 03/11).
    """
    for i in range(0, len(dados), BLOCO_GRAVACAO):
        saida.write(dados[i:i + BLOCO_GRAVACAO])


def dividir(texto: str, maximo: int = 400) -> list[dict]:
    """[{"texto": trecho, "pausa": segundos de silêncio depois}] — parágrafo vira pausa maior."""
    trechos: list[dict] = []
    paragrafos = [p for p in re.split(r"\n\s*\n", texto.replace("\r", "")) if p.strip()]
    for pi, par in enumerate(paragrafos):
        par = re.sub(r"\s*\n\s*", " ", par)
        atual = ""
        for frase in _frases(par):
            for pedaco in _quebra_longa(frase, maximo):
                if atual and len(atual) + 1 + len(pedaco) > maximo:
                    trechos.append({"texto": atual, "pausa": 0.25})
                    atual = pedaco
                else:
                    atual = f"{atual} {pedaco}".strip()
        if atual:
            trechos.append({"texto": atual, "pausa": 0.7 if pi < len(paragrafos) - 1 else 0.0})
    return trechos


# --------------------------------------------------------------------------- motores
def _porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pasta_do_motor(nome: str, m: dict) -> Path:
    p = Path(m.get("pasta") or AUDIO / "motores" / nome)
    return p if p.is_absolute() else RAIZ / p


def _python_do_motor(nome: str, m: dict) -> Path:
    if m.get("python"):
        p = Path(m["python"])
        return p if p.is_absolute() else RAIZ / p
    return _pasta_do_motor(nome, m) / ".venv/bin/python"


def carrega_catalogo() -> dict[str, dict]:
    if not CATALOGO.exists():
        return {}
    return (yaml.safe_load(CATALOGO.read_text(encoding="utf-8")) or {}).get("motores") or {}


class GerenteAudio:
    def __init__(self) -> None:
        self.estado: dict[str, dict[str, Any]] = {}
        self.procs: dict[str, subprocess.Popen] = {}
        self.tarefas: dict[str, dict[str, Any]] = {}
        self.fila: asyncio.Queue | None = None
        self._lock = asyncio.Lock()
        self._iniciado = False
        SAIDAS.mkdir(parents=True, exist_ok=True)
        self._carrega_historico()

    # ---- histórico
    def _carrega_historico(self) -> None:
        arq = SAIDAS / "historico.jsonl"
        if not arq.exists():
            return
        for ln in arq.read_text(encoding="utf-8").splitlines():
            try:
                t = json.loads(ln)
            except ValueError:
                continue
            if (SAIDAS / f"{t['id']}.ogg").exists():
                self.tarefas[t["id"]] = t

    def _grava_historico(self, t: dict) -> None:
        with (SAIDAS / "historico.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({k: v for k, v in t.items() if not k.startswith("_")}, ensure_ascii=False) + "\n")

    def _inicia(self) -> None:
        if self._iniciado:
            return
        self._iniciado = True
        self.fila = asyncio.Queue()
        loop = asyncio.get_running_loop()
        loop.create_task(self._trabalhador())
        loop.create_task(self._vigia_ocioso())

    # ---- estado
    def status(self) -> dict:
        cat = carrega_catalogo()
        motores = {}
        for nome, m in cat.items():
            e = self.estado.get(nome) or {"status": "parado"}
            p = self.procs.get(nome)
            if p and p.poll() is not None and e.get("status") in ("ligando", "no_ar"):
                e = {"status": "erro", "erro": f"o motor saiu (código {p.returncode}); veja logs/audio-{nome}.log"}
                self.estado[nome] = e
                self.procs.pop(nome, None)
            instalado = _python_do_motor(nome, m).exists()
            motores[nome] = {**{k: v for k, v in m.items() if k not in ("comando",)}, **e, "instalado": instalado}
        tarefas = sorted(self.tarefas.values(), key=lambda t: -t["criado"])[:60]
        return {"motores": motores, "tarefas": [{k: v for k, v in t.items() if not k.startswith("_")} for t in tarefas],
                "ocioso_s": OCIOSO_S}

    async def ligar(self, nome: str) -> dict:
        self._inicia()
        cat = carrega_catalogo()
        if nome not in cat:
            raise ValueError("motor desconhecido")
        async with self._lock:
            for outro in list(self.procs):
                if outro != nome:
                    self.desligar(outro)
            p = self.procs.get(nome)
            if p and p.poll() is None and self.estado.get(nome, {}).get("status") in ("ligando", "no_ar"):
                return self.status()
            pasta = _pasta_do_motor(nome, cat[nome])
            py = _python_do_motor(nome, cat[nome])
            if not py.exists():
                raise RuntimeError(f"motor {nome} não instalado (rode audio/instalar.sh {nome})")
            porta = _porta_livre()
            env = {
                "PATH": f"{py.parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": HOME, "LANG": os.environ.get("LANG", "pt_BR.UTF-8"), "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
                # Na hora de gerar, nada vem da internet: nem peso, nem código remoto.
                "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                "HF_HOME": str(AUDIO / "hf-cache"), "PYTORCH_ENABLE_MPS_FALLBACK": "1",
                "GW_MODELOS": str(AUDIO / "modelos"),
            }
            for k, v in (cat[nome].get("env") or {}).items():  # ajustes por motor (memória, precisão)
                env[str(k)] = str(v).replace("{RAIZ}", str(RAIZ))
            LOGS.mkdir(parents=True, exist_ok=True)
            saida = (LOGS / f"audio-{nome}.log").open("ab")
            proc = subprocess.Popen([str(py), "worker.py", "--porta", str(porta)], cwd=str(pasta), env=env,
                                    stdout=saida, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            (LOGS / f"audio-{nome}.pid").write_text(str(proc.pid))
            self.procs[nome] = proc
            self.estado[nome] = {"status": "ligando", "porta": porta, "pid": proc.pid, "desde": time.time(), "ultimo_uso": time.time()}
            asyncio.get_running_loop().create_task(self._espera_subir(nome, porta, proc))
        return self.status()

    def desligar(self, nome: str) -> None:
        p = self.procs.pop(nome, None)
        arq = LOGS / f"audio-{nome}.pid"
        pid = p.pid if p else (int(arq.read_text()) if arq.exists() and arq.read_text().strip().isdigit() else None)
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
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

    def ocupado(self) -> bool:
        return any(t["status"] in ("na_fila", "gerando") for t in self.tarefas.values())

    def descarregar(self) -> dict:
        """Botão do painel: tira o modelo da memória. Recusa no meio de uma geração (mataria a tarefa)."""
        if self.ocupado():
            raise RuntimeError("tem geração em andamento: cancele ou espere terminar")
        self.desligar_todos()
        return self.status()

    def desligar_todos(self) -> None:
        for nome in list(set(self.procs) | set(self.estado)):
            self.desligar(nome)

    async def _espera_subir(self, nome: str, porta: int, p: subprocess.Popen) -> None:
        limite = time.time() + SUBIDA_MAX_S
        async with httpx.AsyncClient(timeout=3) as c:
            while time.time() < limite:
                if p.poll() is not None:
                    self.estado[nome] = {"status": "erro", "erro": f"o motor saiu (código {p.returncode}); veja logs/audio-{nome}.log"}
                    return
                try:
                    r = await c.get(f"http://127.0.0.1:{porta}/health")
                    if r.status_code == 200:
                        self.estado[nome].update(status="no_ar", info=r.json(), ultimo_uso=time.time())
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
        self.estado[nome] = {"status": "erro", "erro": f"não respondeu em {SUBIDA_MAX_S} s"}

    async def _vigia_ocioso(self) -> None:
        while True:
            await asyncio.sleep(15)
            ocupado = self.ocupado()
            for nome, e in list(self.estado.items()):
                if e.get("status") == "no_ar" and not ocupado and time.time() - e.get("ultimo_uso", 0) > OCIOSO_S:
                    self.desligar(nome)  # IA local: usou, encerra (Mac esquenta)

    # ---- tarefas
    async def criar(self, pedido: dict) -> dict:
        self._inicia()
        cat = carrega_catalogo()
        motor = str(pedido.get("motor") or "")
        if motor not in cat:
            raise ValueError("motor desconhecido")
        tipo = cat[motor].get("tipo", "voz")
        texto = str(pedido.get("texto") or pedido.get("prompt") or "").strip()
        if not texto:
            raise ValueError("texto vazio")
        t = {
            "id": uuid.uuid4().hex[:12], "tipo": tipo, "motor": motor, "criado": time.time(), "status": "na_fila",
            "titulo": re.sub(r"\s+", " ", texto)[:80], "voz": pedido.get("voz"), "velocidade": pedido.get("velocidade", 1.0),
            "feito": 0, "total": 0, "segundos": 0.0, "erro": None,
            "_pedido": {"texto": texto, "voz": pedido.get("voz"), "velocidade": float(pedido.get("velocidade") or 1.0),
                        "letra": pedido.get("letra") or "", "duracao": float(pedido.get("duracao") or 30),
                        "semente": pedido.get("semente"), "idioma": pedido.get("idioma"),
                        "instrumental": bool(pedido.get("instrumental"))},
        }
        self.tarefas[t["id"]] = t
        await self.fila.put(t["id"])
        return {k: v for k, v in t.items() if not k.startswith("_")}

    def cancelar(self, tid: str) -> None:
        t = self.tarefas.get(tid)
        if t and t["status"] in ("na_fila", "gerando"):
            t["_cancelar"] = True
            if t["status"] == "na_fila":
                t["status"] = "cancelado"

    def apagar(self, tid: str) -> None:
        t = self.tarefas.pop(tid, None)
        if t:
            (SAIDAS / f"{tid}.ogg").unlink(missing_ok=True)
            arq = SAIDAS / "historico.jsonl"
            if arq.exists():
                linhas = [ln for ln in arq.read_text(encoding="utf-8").splitlines() if f'"id": "{tid}"' not in ln]
                arq.write_text("\n".join(linhas) + ("\n" if linhas else ""), encoding="utf-8")

    async def _trabalhador(self) -> None:
        while True:
            tid = await self.fila.get()
            t = self.tarefas.get(tid)
            if not t or t.get("_cancelar"):
                continue
            try:
                await self._executa(t)
            except Exception as e:  # noqa: BLE001 - erro vira estado da tarefa
                t.update(status="erro", erro=f"{type(e).__name__}: {e}"[:400])
            finally:
                if t["status"] in ("pronto", "cancelado") and t.get("segundos"):
                    self._grava_historico(t)

    async def _garante_motor(self, nome: str) -> int:
        e = self.estado.get(nome) or {}
        if e.get("status") != "no_ar":
            await self.ligar(nome)
            while (self.estado.get(nome) or {}).get("status") == "ligando":
                await asyncio.sleep(1)
        e = self.estado.get(nome) or {}
        if e.get("status") != "no_ar":
            raise RuntimeError(e.get("erro") or f"motor {nome} não subiu")
        return e["porta"]

    async def _executa(self, t: dict) -> None:
        t["status"] = "gerando"
        t["_inicio"] = time.time()
        porta = await self._garante_motor(t["motor"])
        info = self.estado[t["motor"]].get("info") or {}
        taxa = int(info.get("taxa") or 24000)
        pedido = t["_pedido"]
        import soundfile as sf

        destino = SAIDAS / f"{t['id']}.ogg"
        parcial = SAIDAS / f"{t['id']}.parcial.ogg"
        subtipo = "OPUS" if t["tipo"] == "voz" and taxa in TAXAS_OPUS else "VORBIS"
        async with httpx.AsyncClient(timeout=httpx.Timeout(1800, connect=10)) as c:  # música de 4 min leva uns 7 min
            if t["tipo"] == "musica":
                t["total"] = 1
                r = await c.post(f"http://127.0.0.1:{porta}/musica", json={
                    "prompt": pedido["texto"], "letra": pedido["letra"], "duracao": pedido["duracao"],
                    "semente": pedido["semente"], "idioma": pedido.get("idioma"), "instrumental": pedido.get("instrumental")})
                if r.status_code != 200:
                    raise RuntimeError(f"motor respondeu HTTP {r.status_code}: {r.text[:200]}")
                dados, sr = sf.read(io.BytesIO(r.content), dtype="float32", always_2d=True)
                canais = min(2, dados.shape[1])  # música fica em estéreo
                with sf.SoundFile(parcial, "w", samplerate=sr, channels=canais, format="OGG", subtype="VORBIS") as saida:
                    _grava_em_blocos(saida, dados[:, :canais])
                t.update(feito=1, segundos=round(len(dados) / sr, 1))
            else:
                with sf.SoundFile(parcial, "w", samplerate=taxa, channels=1, format="OGG", subtype=subtipo) as saida:
                    trechos = dividir(pedido["texto"], int(info.get("max_chars") or 400))
                    t["total"] = len(trechos)
                    import numpy as np

                    for tr in trechos:
                        if t.get("_cancelar"):
                            t["status"] = "cancelado"
                            break
                        r = await c.post(f"http://127.0.0.1:{porta}/voz", json={
                            "texto": tr["texto"], "voz": pedido["voz"], "velocidade": pedido["velocidade"],
                            "sessao": t["id"]})  # motor que clona a voz do 1º trecho usa isto
                        if r.status_code != 200:
                            raise RuntimeError(f"motor respondeu HTTP {r.status_code}: {r.text[:200]}")
                        dados, sr = sf.read(io.BytesIO(r.content), dtype="float32", always_2d=False)
                        if dados.ndim > 1:
                            dados = dados.mean(axis=1)
                        _grava_em_blocos(saida, dados)
                        if tr["pausa"]:
                            saida.write(np.zeros(int(sr * tr["pausa"]), dtype="float32"))
                        t["feito"] += 1
                        t["segundos"] = round(t["segundos"] + len(dados) / sr + tr["pausa"], 1)
                        t.setdefault("trechos", []).append([len(tr["texto"]), round(len(dados) / sr, 2)])
                        self.estado[t["motor"]]["ultimo_uso"] = time.time()
        self.estado.get(t["motor"], {})["ultimo_uso"] = time.time()
        if t["status"] == "cancelado" and not t["segundos"]:
            parcial.unlink(missing_ok=True)
            return
        parcial.replace(destino)
        if t["status"] != "cancelado":
            t["status"] = "pronto"
        t["formato"] = f"ogg/{subtipo.lower()}"
        t["gerado_em_s"] = round(time.time() - t["_inicio"], 1)
        t["bytes"] = destino.stat().st_size


gerente_audio = GerenteAudio()
