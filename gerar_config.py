"""Gera config.gerado.yaml (LiteLLM) a partir de modelos.yaml + chaves presentes no ambiente.

- Cada modelo do pool vira um deployment próprio, "m:<provedor>/<id>": é o que o
  roteador (gw/roteador.py) escolhe a cada pedido.
- Cada rota também vira um grupo com os mesmos modelos, como rede de segurança e
  para aparecer em /v1/models. Na prática quem decide a ordem é o roteador.

Uso: .venv/bin/python gerar_config.py [--saida config.gerado.yaml]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / "gw"))
import catalogo  # noqa: E402


def gerar() -> tuple[dict, list[str], set[str]]:
    cat = catalogo.carrega()
    ativos = catalogo.itens_ativos(cat)
    provs_ativos = {i["provedor"] for i in ativos}
    avisos = [f"sem chave/URL, fora: {n}" for n, p in cat["provedores"].items() if not catalogo.provedor_ativo(p)]

    lista: list[dict] = []
    for item in ativos:
        info = {"id": item["nome"]}
        if item.get("contexto"):
            info["max_input_tokens"] = item["contexto"]
        lista.append({"model_name": item["nome"], "litellm_params": catalogo.params_litellm(item), "model_info": info})

    for rota in cat.get("rotas") or {}:
        for item in catalogo.itens_da_rota(cat, rota, ativos):
            info = {"id": f"{rota}::{item['nome']}"}
            if item.get("contexto"):
                info["max_input_tokens"] = item["contexto"]
            lista.append({"model_name": rota, "litellm_params": catalogo.params_litellm(item), "model_info": info})

    config = {
        "model_list": lista,
        "router_settings": {
            "routing_strategy": "simple-shuffle",
            "enable_pre_call_checks": True,
            # Quem tenta de novo é o roteador, pelo "fallbacks" de cada pedido.
            "num_retries": 0,
            "timeout": 180,
        },
        "litellm_settings": {
            "callbacks": ["gw.protecao.instancia"],
            "drop_params": True,
            # O LiteLLM não guarda conteúdo de mensagem nos logs dele.
            "turn_off_message_logging": True,
        },
        "general_settings": {"master_key": "os.environ/GW_MASTER_KEY"},
    }
    if not ativos:
        avisos.append("NENHUM provedor ativo: preencha as chaves GW_*_KEY no .env")
    return config, avisos, provs_ativos


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", default=str(RAIZ / "config.gerado.yaml"))
    args = ap.parse_args()
    config, avisos, provs = gerar()
    Path(args.saida).write_text(
        "# GERADO por gerar_config.py a partir de modelos.yaml. Não editar à mão.\n"
        + yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    for a in avisos:
        print(f"aviso: {a}", file=sys.stderr)
    n_pool = sum(1 for d in config["model_list"] if d["model_name"].startswith("m:"))
    rotas = sorted({d["model_name"] for d in config["model_list"] if not d["model_name"].startswith("m:")})
    print(f"pool: {n_pool} modelos ({', '.join(sorted(provs))}) · rotas: {', '.join(rotas) or 'nenhuma'}")
    return 0 if config["model_list"] else 2


if __name__ == "__main__":
    sys.exit(main())
