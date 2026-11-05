"""Dados do painel (GET /painel/dados?periodo=1h|24h|7d).

Tudo sai de logs/roteamento.jsonl (uma linha por requisição) + saúde do roteador.
Nunca inclui conteúdo de mensagem nem chave.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import catalogo
import roteador as rt

RAIZ = Path(__file__).resolve().parent.parent
PERIODOS = {"1h": (timedelta(hours=1), timedelta(minutes=5)),
            "24h": (timedelta(hours=24), timedelta(hours=1)),
            "7d": (timedelta(days=7), timedelta(hours=6))}


def _linhas(arq: Path, max_bytes: int = 8_000_000) -> list[dict]:
    if not arq.exists():
        return []
    with arq.open("rb") as f:
        f.seek(0, 2)
        tam = f.tell()
        f.seek(max(0, tam - max_bytes))
        brutas = f.read().decode("utf-8", "ignore").splitlines()
    saida = []
    for ln in brutas:
        try:
            saida.append(json.loads(ln))
        except ValueError:
            continue
    return saida


def _p(valores: list[float], q: float) -> float | None:
    if not valores:
        return None
    v = sorted(valores)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def _q_meta() -> dict:
    arq = Path(os.environ.get("GW_QUALIDADE", RAIZ / "qualidade.json"))
    try:
        j = json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: j.get(k) for k in ("fonte", "gerado_em", "metodo", "benchmarks")}


def montar(periodo: str = "24h") -> dict:
    periodo = periodo if periodo in PERIODOS else "24h"
    janela, passo = PERIODOS[periodo]
    r = rt.Roteador.do_disco(RAIZ, somente_leitura=True)
    pasta = Path(os.environ.get("GW_LOG_DIR", RAIZ / "logs"))
    agora = datetime.now().astimezone()
    inicio = agora - janela

    todas = _linhas(pasta / "roteamento.jsonl")
    for d in todas:
        try:
            d["_t"] = datetime.fromisoformat(d["ts"]).astimezone()
        except (KeyError, ValueError):
            d["_t"] = None
    reqs = [d for d in todas if d["_t"] and d["_t"] >= inicio]

    # --- série temporal
    n = int(janela / passo)
    base = inicio
    baldes = [{"ini": (base + i * passo).isoformat(timespec="minutes"), "ok": 0, "fallback": 0, "erro": 0,
               "tok_in": 0, "tok_out": 0, "_lat": []} for i in range(n)]
    for d in reqs:
        i = min(n - 1, int((d["_t"] - base) / passo))
        b = baldes[i]
        if d.get("status") != "ok":
            b["erro"] += 1
        elif (d.get("tentativas") or 1) > 1:
            b["fallback"] += 1
        else:
            b["ok"] += 1
        b["tok_in"] += d.get("tokens_entrada") or 0
        b["tok_out"] += d.get("tokens_saida") or 0
        if d.get("status") == "ok" and d.get("latencia_ms"):
            b["_lat"].append(d["latencia_ms"])
    for b in baldes:
        b["lat_p50"] = _p(b.pop("_lat"), 0.5)

    # --- totais
    ok = [d for d in reqs if d.get("status") == "ok"]
    lat = [d["latencia_ms"] for d in ok if d.get("latencia_ms")]
    mascarados: dict[str, int] = {}
    for d in reqs:
        for t, q in (d.get("mascarados") or {}).items():
            mascarados[t] = mascarados.get(t, 0) + q
    por_modelo: dict[str, dict] = {}
    for d in reqs:
        nome = d.get("atendeu") or d.get("escolhido")
        if not nome:
            continue
        m = por_modelo.setdefault(nome, {"nome": nome, "req": 0, "erro": 0, "tok": 0, "_lat": []})
        m["req"] += 1
        m["erro"] += d.get("status") != "ok"
        m["tok"] += (d.get("tokens_entrada") or 0) + (d.get("tokens_saida") or 0)
        if d.get("latencia_ms") and d.get("status") == "ok":
            m["_lat"].append(d["latencia_ms"])
    for m in por_modelo.values():
        m["lat_p50"] = _p(m.pop("_lat"), 0.5)

    # --- modelos (pool) com saúde
    geral = {a["nome"]: a for a in r.avaliar("geral")}
    modelos = []
    for item in r.itens.values():
        s = r.saude.modelo(item["nome"])
        a = geral.get(item["nome"]) or {}
        nota, fonte = r.nota(item)
        uso = por_modelo.get(item["nome"], {})
        modelos.append({
            "nome": item["nome"], "modelo": item["id"], "provedor": item["provedor"],
            "contexto": item.get("contexto"), "visao": bool(item.get("visao")), "nota": round(nota, 1),
            "fonte_nota": fonte, "lat_ms": s.get("lat_ms"), "confiabilidade": a.get("confiabilidade"),
            "tools": s.get("tools"), "uso_hoje": r.saude.uso_hoje(item["nome"]),
            "req_periodo": uso.get("req", 0), "tok_periodo": uso.get("tok", 0),
            "espera_ate": s["ate"] if s["ate"] > agora.timestamp() else None, "motivo": s.get("motivo"),
        })
    modelos.sort(key=lambda m: -m["nota"])

    # --- provedores
    provedores = []
    for nome, p in (r.cat.get("provedores") or {}).items():
        itens = [i for i in r.itens.values() if i["provedor"] == nome]
        erros = sum(1 for d in reqs if d.get("status") != "ok" and (d.get("escolhido") or "").startswith(f"m:{nome}/"))
        provedores.append({
            "nome": nome, "ativo": catalogo.provedor_ativo(p), "modelos": len(itens),
            "uso_hoje": r.saude.uso_hoje(f"provedor:{nome}"), "rpd": p.get("rpd"), "rpm": p.get("rpm"),
            "api_base": p.get("api_base") or ("(local)" if p.get("api_base_env") else None),
            "req_periodo": sum(1 for d in reqs if (d.get("atendeu") or "").startswith(f"m:{nome}/")),
            "erros_periodo": erros, "cadastro": p.get("cadastro"),
            "em_espera": sum(1 for i in itens if r.saude.modelo(i["nome"])["ate"] > agora.timestamp()),
        })

    # --- hoje (compatível com o e2e e com o cabeçalho)
    dia = agora.strftime("%Y-%m-%d")
    de_hoje = [d for d in todas if d["_t"] and d["_t"].strftime("%Y-%m-%d") == dia]
    lat_h = sorted(d["latencia_ms"] for d in de_hoje if d.get("status") == "ok" and d.get("latencia_ms"))
    masc_h: dict[str, int] = {}
    for d in de_hoje:
        for t, q in (d.get("mascarados") or {}).items():
            masc_h[t] = masc_h.get(t, 0) + q

    campos = ("ts", "status", "rota", "escolhido", "atendeu", "ordem", "tentativas", "latencia_ms", "ttft_ms",
              "tokens_entrada", "tokens_saida", "mascarados", "erro", "agente", "stream", "tools")
    return {
        "gerado_em": agora.isoformat(timespec="seconds"),
        "periodo": periodo,
        "resumo": {
            "requisicoes": len(reqs), "ok": len(ok),
            "fallback": sum(1 for d in ok if (d.get("tentativas") or 1) > 1),
            "erro": len(reqs) - len(ok),
            "tokens_entrada": sum(d.get("tokens_entrada") or 0 for d in reqs),
            "tokens_saida": sum(d.get("tokens_saida") or 0 for d in reqs),
            "lat_p50": _p(lat, 0.5), "lat_p95": _p(lat, 0.95),
            "protegidos": sum(mascarados.values()), "mascarados": mascarados,
            "custo_usd": 0.0,
        },
        "serie": baldes,
        "top_modelos": sorted(por_modelo.values(), key=lambda m: -m["req"])[:8],
        "atividade": [{k: d.get(k) for k in campos} for d in reversed(reqs[-300:])],
        "modelos": modelos,
        "provedores": provedores,
        "rotas": {rota: {"perfil": regra.get("perfil"), "reserva": regra.get("reserva"), "ranking": r.avaliar(rota)}
                  for rota, regra in r.rotas().items()},
        "hoje": {
            "chamadas": len(de_hoje), "ok": sum(1 for d in de_hoje if d.get("status") == "ok"),
            "caiu_para_fallback": sum(1 for d in de_hoje if d.get("status") == "ok" and (d.get("tentativas") or 1) > 1),
            "lat_p50_ms": lat_h[len(lat_h) // 2] if lat_h else None, "mascarados": masc_h,
        },
        "politica": {
            "imagens": os.environ.get("GW_IMAGENS", "bloquear"),
            "bloquear": [t for t in os.environ.get("GW_BLOQUEAR", "").split(",") if t.strip()],
            "ner": os.environ.get("GW_NER", "1") not in ("0", "false", "off", ""),
        },
        "qualidade": {"fonte": _q_meta(), "componentes": {ref: m.get("componentes") for ref, m in r.qualidade.items()}},
    }
