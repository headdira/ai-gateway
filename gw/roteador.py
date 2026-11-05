"""Roteamento inteligente: escolhe, a cada pedido, a melhor ordem de modelos da rota.

pontuação = (peso_q × qualidade + peso_v × velocidade) × confiabilidade

- qualidade: nota 0-100 do qualidade.json (benchmarks) ou nota manual do modelos.yaml.
- velocidade: da latência medida (média móvel); sem medida, palpite do catálogo ou 0,5.
- confiabilidade: (sucesso+2)/(sucesso+falha+2,5) com esquecimento: começa em 0,8
  para quem ainda não foi testado (otimista) e o passado pesa menos.
- pedido com ferramenta (tools) só vai para modelo que PROVOU tool call (saúde
  tools=True: sondagem `testar_provedores.py --sondar` ou tool call real atendida).
  Quem nunca provou ou já falhou fica fora, inclusive do último recurso "em espera".
- fora da disputa: modelo em espera depois de erro (429: 10 min; timeout: 5 min;
  chave/permissão: 1 h; outro: 2 min; dobra a cada erro seguido, teto 1 h), cota do
  dia estourada, sem visão para pedido com imagem, ou contexto menor que o prompt.

O cooldown é nosso, não do LiteLLM: o dele não disparou nos testes de 03/11.
A saúde fica em logs/saude.json (o painel lê de lá) e sobrevive a reinício.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PERFIS = {
    "qualidade": (0.85, 0.15),
    "equilibrado": (0.60, 0.40),
    "velocidade": (0.25, 0.75),
}
ESPERA_S = {"429": 600, "timeout": 300, "auth": 3600, "caiu": 300, "erro": 120}
ESPERA_MAX_S = 3600
ESQUECIMENTO = 0.9  # cada evento novo multiplica os contadores antigos por isto
# Palpite otimista para modelo sem histórico (0,8): o roteador experimenta os de nota
# alta em vez de ficar preso no primeiro que funcionou. Uma falha já derruba para
# ~0,57 e coloca em espera; sucessos levam para perto de 1.
PRIOR_OK, PRIOR_TOTAL = 2.0, 2.5
CHARS_POR_TOKEN = 3.5
PREFIXO_SEM_ESPACO = "prompt"  # início do motivo "fora" quando só o tamanho barrou


class ContextoEstourado(Exception):
    """Nenhum modelo da rota (nem da reserva) cabe o pedido; os que só não cabem estavam livres."""

    def __init__(self, tokens: int, maior: int):
        super().__init__(f"~{tokens} tokens; maior contexto disponível {maior}")
        self.tokens = tokens
        self.maior = maior


def hoje() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class Saude:
    def __init__(self, arquivo: Path, somente_leitura: bool = False):
        self.arquivo = arquivo
        self.somente_leitura = somente_leitura
        self._lock = threading.Lock()
        self._ultimo_salvo = 0.0
        self.dados: dict[str, Any] = {"modelos": {}, "uso": {}}
        if arquivo.exists():
            try:
                self.dados = json.loads(arquivo.read_text(encoding="utf-8"))
                self.dados.setdefault("modelos", {})
                self.dados.setdefault("uso", {})
            except (ValueError, OSError):
                pass

    def modelo(self, nome: str) -> dict:
        return self.dados["modelos"].setdefault(nome, {
            "ok": 0.0, "falhas": 0.0, "seguidas": 0, "lat_ms": None, "tools": None,
            "ate": 0, "motivo": None, "ultima_ok": None, "ultima_falha": None,
        })

    def uso_hoje(self, chave: str) -> int:
        return int(self.dados["uso"].get(hoje(), {}).get(chave, 0))

    def _conta_uso(self, nome: str, provedor: str) -> None:
        dia = self.dados["uso"].setdefault(hoje(), {})
        for k in (nome, f"provedor:{provedor}"):
            dia[k] = dia.get(k, 0) + 1
        for velho in sorted(self.dados["uso"])[:-7]:  # guarda 7 dias
            del self.dados["uso"][velho]

    def sucesso(self, nome: str, provedor: str, lat_ms: float | None, tools_ok: bool | None = None) -> None:
        with self._lock:
            m = self.modelo(nome)
            m["ok"] = m["ok"] * ESQUECIMENTO + 1
            m["falhas"] *= ESQUECIMENTO
            m["seguidas"] = 0
            m["ate"] = 0
            m["motivo"] = None
            m["ultima_ok"] = time.time()
            if lat_ms:
                m["lat_ms"] = lat_ms if m["lat_ms"] is None else round(0.7 * m["lat_ms"] + 0.3 * lat_ms)
            if tools_ok is not None:
                m["tools"] = tools_ok
            self._conta_uso(nome, provedor)
        self.salvar()

    def falha(self, nome: str, provedor: str, motivo: str, tools_falhou: bool = False) -> None:
        with self._lock:
            m = self.modelo(nome)
            m["falhas"] = m["falhas"] * ESQUECIMENTO + 1
            m["ok"] *= ESQUECIMENTO
            m["seguidas"] += 1
            espera = min(ESPERA_MAX_S, ESPERA_S.get(motivo, 120) * 2 ** (m["seguidas"] - 1))
            m["ate"] = time.time() + espera
            m["motivo"] = motivo
            m["ultima_falha"] = time.time()
            if tools_falhou:
                m["tools"] = False
            self._conta_uso(nome, provedor)
        self.salvar()

    def salvar(self, forcar: bool = False) -> None:
        if self.somente_leitura:
            return
        agora = time.time()
        if not forcar and agora - self._ultimo_salvo < 0.5:
            return
        self._ultimo_salvo = agora
        with self._lock:
            texto = json.dumps(self.dados, ensure_ascii=False, indent=1)
        tmp = self.arquivo.with_suffix(".tmp")
        tmp.write_text(texto, encoding="utf-8")
        tmp.replace(self.arquivo)


def motivo_da_excecao(exc: BaseException | None) -> str:
    if exc is None:
        return "erro"
    status = getattr(exc, "status_code", None)
    nome = type(exc).__name__.lower()
    if status == 429 or "ratelimit" in nome:
        return "429"
    if "timeout" in nome or status == 408:
        return "timeout"
    if status in (401, 403, 404) or "authentication" in nome or "notfound" in nome or "permission" in nome:
        return "auth"
    return "erro"


# Tool call escrita como TEXTO em vez de `tool_calls`: XML no estilo Claude (com ou sem
# prefixo, ex.: <atem:invoke>), <tool_call> do Qwen/Hermes, <function=…> do Llama,
# [TOOL_CALLS] do Mistral, <|tool_call|>. Visto em 04/11 com o muse-glimmer.
_RX_TOOL_EM_TEXTO = re.compile(
    r"</?(?:[\w-]+:)?(?:invoke|function_calls|tool_calls?|antml)\b"
    r"|<(?:[\w-]+:)?parameter\s+name="
    r"|<function=[\w.-]+"
    r"|\[TOOL_CALLS\]"
    r"|<\|tool_call",
    re.IGNORECASE,
)


# Erro que diz que o MODELO não suporta ferramenta. Antes bastava ter "tool" na mensagem:
# um 400 de validação do histórico ("messages[228].tool_calls[0].function.arguments must
# be a valid JSON…") marcou como sem-tool todos os modelos bons do plano (04/11).
_RX_SEM_SUPORTE_TOOL = re.compile(
    r"(?:does not|doesn't|do not|not) support(?:s|ed)?\s+(?:the\s+)?(?:tools?|tool[ _]calls?|tool[ _]use|function[ _]call(?:ing|s)?|tool[ _]choice)"
    r"|(?:tools?|tool[ _]calls?|tool[ _]use|function[ _]call(?:ing|s)?|tool[ _]choice)\s+(?:is|are)\s+not\s+(?:supported|available|enabled)"
    r"|no endpoints found that support tool",
    re.IGNORECASE,
)


def erro_de_modelo_sem_tool(exc: BaseException | None) -> bool:
    return exc is not None and _RX_SEM_SUPORTE_TOOL.search(str(exc)) is not None


def tool_call_em_texto(texto: str | None) -> bool:
    """O modelo escreveu a chamada de ferramenta no texto da resposta?"""
    return bool(texto) and _RX_TOOL_EM_TEXTO.search(texto) is not None


def necessidades(data: dict) -> dict:
    """O que o pedido exige, só por fatos do próprio pedido."""
    mensagens = data.get("messages") or []
    imagem = False
    for m in mensagens:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") in ("image_url", "input_image", "image") for p in c):
            imagem = True
            break
    try:
        tamanho = len(json.dumps(mensagens, ensure_ascii=False)) + len(json.dumps(data.get("tools") or []))
    except (TypeError, ValueError):
        tamanho = 0
    return {
        "tools": bool(data.get("tools") or data.get("functions")),
        "imagem": imagem,
        "tokens": int(tamanho / CHARS_POR_TOKEN) + int(data.get("max_tokens") or 0),
    }


class Roteador:
    def __init__(self, cat: dict, itens: list[dict], qualidade: dict, saude: Saude):
        self.cat = cat
        self.itens = {i["nome"]: i for i in itens}
        self.qualidade = (qualidade or {}).get("modelos", {})
        self.saude = saude
        self.n_fallbacks = int(cat.get("fallbacks_por_pedido", 3))

    @classmethod
    def do_disco(cls, raiz: Path, somente_leitura: bool = False) -> Roteador:
        import catalogo

        cat = catalogo.carrega()
        arq_q = Path(os.environ.get("GW_QUALIDADE", raiz / "qualidade.json"))
        qualidade = json.loads(arq_q.read_text(encoding="utf-8")) if arq_q.exists() else {}
        pasta = Path(os.environ.get("GW_LOG_DIR", raiz / "logs"))
        pasta.mkdir(parents=True, exist_ok=True)
        return cls(cat, catalogo.itens_ativos(cat), qualidade, Saude(pasta / "saude.json", somente_leitura))

    def rotas(self) -> dict:
        return self.cat.get("rotas") or {}

    def nota(self, item: dict) -> tuple[float, str]:
        q = self.qualidade.get(item.get("ref") or "", {})
        if q.get("nota") is not None:
            return float(q["nota"]), "benchmarks"
        if item.get("nota") is not None:
            return float(item["nota"]), "manual"
        return 30.0, "sem dado"

    def velocidade(self, item: dict) -> tuple[float, bool]:
        lat = self.saude.modelo(item["nome"]).get("lat_ms")
        if lat:
            return 1 / (1 + (lat / 1000) / 3), True
        return float(item.get("velocidade") or 0.5), False

    def avaliar(self, rota: str, nec: dict | None = None, agora: float | None = None) -> list[dict]:
        import catalogo

        regra = self.rotas().get(rota)
        if regra is None:
            return []
        nec = nec or {"tools": False, "imagem": False, "tokens": 0}
        agora = agora or time.time()
        wq, wv = PERFIS.get(regra.get("perfil", "equilibrado"), PERFIS["equilibrado"])
        saida = []
        for item in self.itens.values():
            if not catalogo.atende_rota(item, regra):
                continue
            s = self.saude.modelo(item["nome"])
            nota, fonte = self.nota(item)
            vel, medida = self.velocidade(item)
            conf = (s["ok"] + PRIOR_OK) / (s["ok"] + s["falhas"] + PRIOR_TOTAL)
            fora = None
            cabe = not (item.get("contexto") and nec["tokens"] > 0.85 * item["contexto"])
            # Ferramenta vem antes da espera: modelo sem tool call provada não pode
            # voltar pela porta do "todos em espera, tenta assim mesmo".
            if nec["tools"] and s.get("tools") is False:
                fora = "ferramenta falhou antes"
            elif nec["tools"] and s.get("tools") is not True:
                fora = "tool call não provada"
            elif s["ate"] > agora:
                fora = f"em espera ({s['motivo']}) até {datetime.fromtimestamp(s['ate']).strftime('%H:%M')}"
            elif item.get("rpd") and self.saude.uso_hoje(item["nome"]) >= item["rpd"]:
                fora = f"cota do dia ({item['rpd']}) esgotada"
            elif item.get("rpd_provedor") and self.saude.uso_hoje(f"provedor:{item['provedor']}") >= item["rpd_provedor"]:
                fora = f"cota do dia do provedor ({item['rpd_provedor']}) esgotada"
            elif nec["imagem"] and not item.get("visao"):
                fora = "sem visão"
            elif not cabe:
                fora = PREFIXO_SEM_ESPACO + f" (~{nec['tokens']} tokens) maior que o contexto"
            pont = (wq * nota / 100 + wv * vel) * conf
            saida.append({
                "nome": item["nome"], "provedor": item["provedor"], "modelo": item["id"],
                "nota": round(nota, 1), "fonte_nota": fonte, "velocidade": round(vel, 2), "vel_medida": medida,
                "lat_ms": s.get("lat_ms"), "confiabilidade": round(conf, 2), "tools": s.get("tools"),
                "uso_hoje": self.saude.uso_hoje(item["nome"]), "pontuacao": round(pont, 3), "fora": fora,
                "espera_ate": s["ate"] if s["ate"] > agora else None, "motivo": s.get("motivo"),
                "peso_q": wq, "peso_v": wv, "contexto": item.get("contexto"), "visao": bool(item.get("visao")),
                "cabe": cabe,
            })
        saida.sort(key=lambda a: (a["fora"] is not None, -a["pontuacao"]))
        return saida

    def planejar(self, rota: str, data: dict) -> tuple[list[str], list[dict]]:
        """Ordem de tentativa para este pedido: [escolhido, fallback1, ...]."""
        nec = necessidades(data)
        av = self.avaliar(rota, nec)
        aptos = [a["nome"] for a in av if a["fora"] is None]
        reserva = (self.rotas().get(rota) or {}).get("reserva")
        av_reserva: list[dict] = []
        if reserva and reserva != rota and len(aptos) < 1 + self.n_fallbacks:
            # A rota não completou a fila (modelos em espera/fora): entram os melhores da reserva.
            av_reserva = self.avaliar(reserva, nec)
            extra = [a["nome"] for a in av_reserva if a["fora"] is None and a["nome"] not in aptos]
            aptos += extra[: 1 + self.n_fallbacks - len(aptos)]
        if not aptos:
            # Todos em espera/cota: tenta mesmo assim os que só estão em espera,
            # do que volta mais cedo para o que volta mais tarde. Quem não cabe o
            # pedido não entra: só devolveria erro de contexto do provedor.
            em_espera = [a for a in av if a["fora"] and a["fora"].startswith("em espera") and a["cabe"]]
            em_espera.sort(key=lambda a: self.saude.modelo(a["nome"])["ate"])
            aptos = [a["nome"] for a in em_espera]
        if not aptos:
            # Só o tamanho barrou (o modelo estava livre para o resto): avisa o agente
            # com o erro padrão de contexto estourado, para ele resumir e tentar de novo.
            sem_espaco = [a for a in av + av_reserva if (a["fora"] or "").startswith(PREFIXO_SEM_ESPACO)]
            if sem_espaco:
                raise ContextoEstourado(nec["tokens"], max(a["contexto"] or 0 for a in sem_espaco))
        return aptos[: 1 + self.n_fallbacks], av

    def provedor_de(self, nome: str) -> str:
        item = self.itens.get(nome)
        return item["provedor"] if item else nome.split("/")[0].removeprefix("m:")
