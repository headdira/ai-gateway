"""Gera qualidade.json: a nota (0-100) de cada modelo do pool, a partir dos benchmarks
públicos do catálogo Routeway (GET https://api.routeway.ai/v1/models, sem chave).

Método:
- Benchmarks usados: os informativos e com boa cobertura no pool. Ficam de fora
  terminalbench_v4_0 (quase tudo 0/0,01) e hallucination_rate (inconsistente).
- Cada benchmark é normalizado entre o pior e o melhor modelo DO POOL (0 a 1).
- Nota = média dos benchmarks que o modelo tem × 100 (mínimo de 3 benchmarks).
- Modelo sem ref ou com menos de 3 benchmarks usa a "nota" manual do modelos.yaml.

Uso: .venv/bin/python atualizar_qualidade.py
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "gw"))
import catalogo  # noqa: E402

RAIZ = Path(__file__).resolve().parent
BENCHMARKS = {
    "hle": "Humanity's Last Exam (raciocínio difícil)",
    "scicode": "SciCode (código científico)",
    "lcr": "LCR (contexto longo)",
    "gpqa": "GPQA Diamond (ciência, nível doutorado)",
    "tau2": "τ²-bench (uso de ferramenta / agente)",
    "ifbench": "IFBench (seguir instrução)",
    "terminalbench_v2_1": "Terminal-Bench 2.1 (agente no terminal)",
}
MIN_BENCHMARKS = 3


def main() -> int:
    cat = catalogo.carrega()
    refs = sorted({i["ref"] for i in cat["pool"] if i.get("ref")})
    r = httpx.get("https://api.routeway.ai/v1/models", timeout=30)
    r.raise_for_status()
    por_id = {m["id"]: m for m in r.json()["data"]}

    brutos: dict[str, dict[str, float]] = {}
    for ref in refs:
        b = (por_id.get(ref) or {}).get("benchmarks") or {}
        brutos[ref] = {k: float(b[k]) for k in BENCHMARKS if b.get(k) is not None}

    faixa = {}
    for k in BENCHMARKS:
        vals = [v[k] for v in brutos.values() if k in v]
        if len(vals) >= 2:
            faixa[k] = (min(vals), max(vals))

    modelos = {}
    for ref, vals in brutos.items():
        comp = {}
        for k, v in vals.items():
            if k not in faixa:
                continue
            lo, hi = faixa[k]
            comp[k] = {"valor": round(v, 3), "norm": round((v - lo) / (hi - lo), 3) if hi > lo else 0.5}
        nota = round(100 * sum(c["norm"] for c in comp.values()) / len(comp), 1) if len(comp) >= MIN_BENCHMARKS else None
        modelos[ref] = {"nota": nota, "componentes": comp, "no_catalogo": ref in por_id}

    saida = {
        "fonte": "benchmarks do catálogo Routeway (api.routeway.ai/v1/models)",
        "gerado_em": datetime.now(UTC).isoformat(timespec="seconds"),
        "metodo": "cada benchmark normalizado entre o pior e o melhor do pool; nota = média × 100",
        "benchmarks": BENCHMARKS,
        "modelos": modelos,
    }
    (RAIZ / "qualidade.json").write_text(json.dumps(saida, ensure_ascii=False, indent=1), encoding="utf-8")
    for ref, m in sorted(modelos.items(), key=lambda kv: -(kv[1]["nota"] or -1)):
        print(f"{m['nota'] if m['nota'] is not None else 'manual':>7}  {ref:30} ({len(m['componentes'])} benchmarks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
