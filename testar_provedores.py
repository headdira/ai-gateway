"""Confere os provedores e alimenta a saúde do roteador.

Sem opção: para cada provedor com chave, confere a chave (GET /models) e se cada
modelo do pool existe no catálogo dele. Não gasta geração.

--chat    manda "responda só: ok" por cada ROTA, passando pelo gateway (precisa estar no ar).
--sondar  chama CADA modelo do pool uma vez, pelo gateway, pedindo uma tool call:
          mede a latência real e prova (ou não) a ferramenta. O resultado vai para
          logs/saude.json e o roteador passa a usar. Gasta 1 chamada por modelo.

Uso: .venv/bin/python testar_provedores.py [--chat] [--sondar]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / "gw"))
import catalogo  # noqa: E402

FERRAMENTA = [{"type": "function", "function": {
    "name": "hora_atual", "description": "Devolve a hora atual do servidor.",
    "parameters": {"type": "object", "properties": {"fuso": {"type": "string"}}, "required": ["fuso"]},
}}]


def carrega_env() -> None:
    arq = RAIZ / ".env"
    if not arq.exists():
        return
    for ln in arq.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def confere_catalogos(cat: dict) -> int:
    problemas = 0
    for nome, p in cat["provedores"].items():
        if p.get("api_base_env"):
            continue
        chave = os.environ.get(p.get("chave") or "", "")
        if not chave and not p.get("anonimo"):
            print(f"-  {nome:11} sem chave ({p['chave']}) → cadastro: {p.get('cadastro')}")
            continue
        cab = {"Authorization": f"Bearer {chave}"} if chave else {}
        try:
            r = httpx.get(f"{p['api_base'].rstrip('/')}/models", headers=cab, timeout=30)
        except httpx.HTTPError as e:
            print(f"✗  {nome:11} sem conexão: {e}")
            problemas += 1
            continue
        if r.status_code in (401, 403):
            print(f"✗  {nome:11} chave recusada (HTTP {r.status_code})")
            problemas += 1
            continue
        ids = {m.get("id") for m in (r.json().get("data") or [])} if r.status_code == 200 else set()
        print(f"✓  {nome:11} {'chave ok' if chave else 'sem conta (anônimo)'} ({len(ids)} modelos no catálogo)")
        for item in cat["pool"]:
            if item["provedor"] != nome or not item.get("id"):
                continue
            ok = item["id"] in ids
            problemas += 0 if ok else 1
            print(f"     {'✓' if ok else '✗ NÃO ACHEI'}  {item['id']}")
    return problemas


def base_e_cabecalho() -> tuple[str, dict]:
    porta = os.environ.get("GW_PORTA", "4000")
    return f"http://127.0.0.1:{porta}", {"Authorization": f"Bearer {os.environ.get('GW_MASTER_KEY', '')}"}


def chat_por_rota(cat: dict) -> int:
    base, cab = base_e_cabecalho()
    print(f"\nChat por rota via gateway ({base}):")
    problemas = 0
    for rota in cat.get("rotas") or {}:
        t = time.time()
        try:
            r = httpx.post(f"{base}/v1/chat/completions", headers=cab, timeout=150, json={
                "model": rota, "user": "testar_provedores",
                "messages": [{"role": "user", "content": "Responda só: ok"}], "max_tokens": 200})
        except httpx.HTTPError as e:
            problemas += 1
            print(f"  ✗ {rota:15} {type(e).__name__}")
            continue
        if r.status_code == 200:
            print(f"  ✓ {rota:15} {time.time() - t:5.1f} s  → {(r.json()['choices'][0]['message'].get('content') or '').strip()[:40]!r}")
        elif r.status_code == 400 and "nenhum" in r.text.lower():
            print(f"  - {rota:15} sem modelo ativo")
        else:
            problemas += 1
            print(f"  ✗ {rota:15} HTTP {r.status_code}: {r.text[:120]}")
    return problemas


def sonda(item: dict, base: str, cab: dict) -> str:
    t = time.time()
    try:
        r = httpx.post(f"{base}/v1/chat/completions", headers=cab, timeout=120, json={
            "model": item["nome"], "user": "sondagem", "tools": FERRAMENTA, "tool_choice": "auto",
            "messages": [{"role": "user", "content": "Que horas são em America/Sao_Paulo? Use a ferramenta hora_atual."}],
            "max_tokens": 400})
    except httpx.HTTPError as e:
        return f"  ✗ {item['nome']:50} {type(e).__name__} em {time.time() - t:.0f} s"
    dt = time.time() - t
    if r.status_code != 200:
        return f"  ✗ {item['nome']:50} HTTP {r.status_code} em {dt:.0f} s: {r.text[:90]}"
    msg = r.json()["choices"][0]["message"]
    tools = "ferramenta ✓" if msg.get("tool_calls") else "respondeu sem chamar a ferramenta"
    return f"  ✓ {item['nome']:50} {dt:5.1f} s  {tools}"


def sondar(cat: dict, provedor: str | None = None) -> int:
    base, cab = base_e_cabecalho()
    itens = [i for i in catalogo.itens_ativos(cat) if i["provedor"] != "local" and provedor in (None, i["provedor"])]
    print(f"\nSondagem: 1 chamada por modelo ({len(itens)} modelos, 4 por vez) via {base}")
    with ThreadPoolExecutor(max_workers=4) as ex:
        for linha in ex.map(lambda i: sonda(i, base, cab), itens):
            print(linha)
    print("Resultado gravado na saúde do roteador (logs/saude.json); veja em /painel.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", action="store_true")
    ap.add_argument("--sondar", action="store_true")
    ap.add_argument("--provedor", help="sonda só este provedor (ex.: kilo)")
    a = ap.parse_args()
    carrega_env()
    cat = catalogo.carrega()
    problemas = confere_catalogos(cat)
    if a.sondar:
        problemas += sondar(cat, a.provedor)
    if a.chat:
        problemas += chat_por_rota(cat)
    return 1 if problemas else 0


if __name__ == "__main__":
    sys.exit(main())
