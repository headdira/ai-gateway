"""Catálogo do gateway (modelos.yaml): provedores, pool de modelos e rotas.

Usado pelo gerar_config.py (monta a config do LiteLLM) e pelo roteador (escolhe
o modelo a cada pedido), para os dois enxergarem exatamente o mesmo pool.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

RAIZ = Path(__file__).resolve().parent.parent


def caminho_catalogo() -> Path:
    return Path(os.environ.get("GW_CATALOGO", RAIZ / "modelos.yaml"))


def carrega(caminho: Path | None = None) -> dict:
    return yaml.safe_load((caminho or caminho_catalogo()).read_text(encoding="utf-8"))


def provedor_ativo(p: dict) -> bool:
    """Ativo quando tem o que precisa: URL por env (local, Cloudflare) e/ou chave."""
    if p.get("api_base_env") and not os.environ.get(p["api_base_env"]):
        return False
    if p.get("anonimo"):  # aceita chamada sem conta; a chave, se houver, só aumenta a cota
        return True
    if p.get("chave"):
        return bool(os.environ.get(p["chave"]))
    return bool(p.get("api_base_env"))


def nome_deployment(item: dict) -> str:
    return f"m:{item['provedor']}/{item['id']}"


def itens_ativos(cat: dict) -> list[dict]:
    """Itens do pool cujo provedor tem chave/URL, já com os dados do provedor mesclados."""
    provs = cat["provedores"]
    saida = []
    for item in cat.get("pool") or []:
        p = provs.get(item["provedor"])
        if p is None or not provedor_ativo(p):
            continue
        modelo_id = item.get("id") or os.environ.get(item.get("id_env", ""), "")
        if not modelo_id:
            continue
        saida.append({
            **item,
            "id": modelo_id,
            "nome": f"m:{item['provedor']}/{modelo_id}",
            "_provedor": p,
            "velocidade": item.get("velocidade", p.get("velocidade")),
            "rpd": item.get("rpd"),
            "rpd_provedor": p.get("rpd"),
        })
    return saida


def atende_rota(item: dict, regra: dict) -> bool:
    if regra.get("ref_contem") and regra["ref_contem"] not in (item.get("ref") or item["id"]):
        return False
    if regra.get("provedor"):
        return item["provedor"] == regra["provedor"]
    if item["provedor"] == "local":
        return False  # modelo local só pela rota local
    if regra.get("visao") and not item.get("visao"):
        return False
    return not (regra.get("contexto_min") and (item.get("contexto") or 0) < regra["contexto_min"])


def itens_da_rota(cat: dict, rota: str, ativos: list[dict] | None = None) -> list[dict]:
    regra = (cat.get("rotas") or {}).get(rota)
    if regra is None:
        return []
    return [i for i in (ativos if ativos is not None else itens_ativos(cat)) if atende_rota(i, regra)]


def contexto_anunciado(cat: dict, rota: str, ativos: list[dict] | None = None) -> int | None:
    """Janela que o /v1/models mostra ao agente para a rota.

    O agente (Hermes) lê esse número uma vez, no começo da sessão, e resume a conversa
    quando ela chega perto dele. Anunciar o MAIOR modelo do pool (1M) fazia o Hermes
    nunca resumir e a conversa só caber nos modelos de 1M. Por isso cada rota pode
    fixar `contexto_anunciado` no modelos.yaml; sem isso, vale o maior da rota.
    """
    regra = (cat.get("rotas") or {}).get(rota)
    if regra is None:
        return None
    if regra.get("contexto_anunciado"):
        return int(regra["contexto_anunciado"])
    tamanhos = [int(i["contexto"]) for i in itens_da_rota(cat, rota, ativos) if i.get("contexto")]
    return max(tamanhos) if tamanhos else None


def params_litellm(item: dict) -> dict[str, Any]:
    p = item["_provedor"]
    params: dict[str, Any] = {"model": f"{p['prefixo']}/{item['id']}"}
    params["api_base"] = os.environ[p["api_base_env"]] if p.get("api_base_env") else p["api_base"]
    if p.get("chave") and os.environ.get(p["chave"]):
        params["api_key"] = f"os.environ/{p['chave']}"
    elif p.get("anonimo"):
        # O Kilo valida qualquer Bearer quando o pedido vem de SDK (User-Agent OpenAI/x-stainless)
        # e devolve 401; sem Authorization ele trata como anônimo. O SDK exige api_key, então o
        # cabeçalho sai vazio por cima (testado 05/11).
        params["api_key"] = "anonimo"
        params["extra_headers"] = {"Authorization": ""}
    else:
        params["api_key"] = "local"
    if item.get("rpm") or p.get("rpm"):
        params["rpm"] = item.get("rpm") or p.get("rpm")
    # Tier grátis às vezes aceita a conexão e não manda token (fila): timeout curto.
    # NVIDIA: o LiteLLM repete 3x por dentro (medido), então lá o tempo real é ~3x.
    params["timeout"] = item.get("timeout", p.get("timeout", 45))
    params["stream_timeout"] = item.get("stream_timeout", p.get("stream_timeout", 20))
    params["max_retries"] = 0
    return params
