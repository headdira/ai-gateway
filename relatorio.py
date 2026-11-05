"""Resumo do logs/requisicoes.jsonl: uso, erros, latência e o que foi mascarado.

Uso: .venv/bin/python relatorio.py [--horas 24]
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parent


def pct(valores: list[int], p: float) -> int | None:
    if not valores:
        return None
    valores = sorted(valores)
    return valores[min(len(valores) - 1, int(round(p * (len(valores) - 1))))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horas", type=float, default=24)
    ap.add_argument("--arquivo", default=str(RAIZ / "logs/requisicoes.jsonl"))
    a = ap.parse_args()
    arq = Path(a.arquivo)
    if not arq.exists():
        print("Sem log ainda:", arq)
        return
    desde = datetime.now(UTC) - timedelta(hours=a.horas)
    regs = []
    for ln in arq.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(ln)
            if datetime.fromisoformat(r["ts"]) >= desde:
                regs.append(r)
        except (ValueError, KeyError):
            continue
    if not regs:
        print(f"Nenhuma chamada nas últimas {a.horas:g} h.")
        return

    por_alvo: dict[tuple, list[dict]] = defaultdict(list)
    for r in regs:
        por_alvo[(r.get("rota") or "?", r.get("provedor") or "-", r.get("modelo") or "-")].append(r)

    print(f"Últimas {a.horas:g} h: {len(regs)} chamadas\n")
    print(f"{'rota':16} {'provedor':11} {'modelo':44} {'n':>4} {'erro':>4} {'429':>4} {'p50ms':>6} {'p95ms':>6} {'tok in':>8} {'tok out':>8}")
    for (rota, prov, modelo), rs in sorted(por_alvo.items(), key=lambda kv: -len(kv[1])):
        lat = [r["latencia_ms"] for r in rs if r.get("latencia_ms") is not None]
        erros = [r for r in rs if r.get("status") != "ok"]
        n429 = sum(1 for r in erros if r.get("http") == 429 or "429" in (r.get("erro") or "") or "rate" in (r.get("erro") or "").lower())
        tin = sum(r.get("tokens_entrada") or 0 for r in rs)
        tout = sum(r.get("tokens_saida") or 0 for r in rs)
        print(f"{rota[:16]:16} {prov[:11]:11} {modelo[-44:]:44} {len(rs):>4} {len(erros):>4} {n429:>4} "
              f"{pct(lat, .5) or '-':>6} {pct(lat, .95) or '-':>6} {tin:>8} {tout:>8}")

    mascarados: Counter = Counter()
    for r in regs:
        mascarados.update(r.get("mascarados") or {})
    bloqueadas = sum(1 for r in regs if r.get("status") == "bloqueado")
    print("\nDado sensível que NÃO saiu da máquina (ocorrências mascaradas):")
    print("  " + (", ".join(f"{t}: {n}" for t, n in mascarados.most_common()) or "nenhum"))
    if bloqueadas:
        print(f"  requisições barradas pela política GW_BLOQUEAR: {bloqueadas}")

    erros = [r for r in regs if r.get("status") == "erro"]
    if erros:
        print("\nÚltimos erros:")
        for r in erros[-5:]:
            print(f"  {r['ts'][11:19]} {r.get('rota')} {r.get('provedor')}: {(r.get('erro') or '')[:140]}")
    ttft = [r["ttft_ms"] for r in regs if r.get("ttft_ms")]
    if ttft:
        print(f"\nPrimeiro token (stream): p50 {statistics.median(ttft):.0f} ms")


if __name__ == "__main__":
    main()
