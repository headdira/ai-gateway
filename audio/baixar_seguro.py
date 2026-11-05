"""Baixa pesos do Hugging Face com checagem de segurança ANTES de baixar.

Regras (falha fechada):
- só formatos sem execução de código: .safetensors .onnx .gguf .json .txt .model
  .tiktoken .yaml .wav .flac .npz/.npy (lidos sem pickle) — qualquer outro é recusado;
- nunca baixa código (.py) nem pickle (.bin .pt .pth .ckpt .pkl .pickle .joblib), a menos
  que o arquivo esteja numa exceção explícita (--permitir) E o scanner do HF diga "safe";
- recusa arquivo que o scanner do HF marcou como unsafe/suspicious (ClamAV ou pickle);
- trava o commit (revision = sha) e grava manifesto com sha256 de cada arquivo.

Uso:
  baixar_seguro.py <repo> <padrão-glob> [<padrão> ...] [--destino DIR] [--permitir ARQ ...] [--só-checar]
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
import time
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parent
SEGUROS = {".safetensors", ".onnx", ".gguf", ".json", ".txt", ".model", ".tiktoken", ".yaml", ".yml",
           ".wav", ".flac", ".npz", ".npy", ".md", ".data", ".jinja"}
PICKLE = {".bin", ".pt", ".pth", ".ckpt", ".pkl", ".pickle", ".joblib"}
CODIGO = {".py", ".sh", ".so", ".dylib", ".exe", ".js", ".ipynb"}


def ext(nome: str) -> str:
    return Path(nome).suffix.lower()


def status_scan(item: dict) -> tuple[str, list[str]]:
    """Resume o securityFileStatus do HF: ('safe'|'unsafe'|'suspicious'|'unscanned', detalhes).

    Vale o "status" geral do arquivo; qualquer sub-scan (ProtectAI, antivírus, pickle, JFrog,
    VirusTotal) que acuse algo rebaixa para unsafe/suspicious. Sub-scan "unscanned" é neutro.
    """
    s = item.get("securityFileStatus") or {}
    det, ruim = [], None
    for k, v in s.items():
        if not isinstance(v, dict):
            continue
        st = str(v.get("status") or "")
        if st and st not in ("unscanned", "safe"):
            det.append(f"{k}={st}")
        if st in ("unsafe", "infected", "malicious"):
            ruim = "unsafe"
        elif st == "suspicious" and ruim is None:
            ruim = "suspicious"
        perigosos = [i for i in v.get("pickleImports") or [] if i.get("safety") not in ("innocuous", None)]
        if perigosos:
            ruim = "unsafe" if any(i.get("safety") == "dangerous" for i in perigosos) else (ruim or "suspicious")
            det.append("imports: " + ", ".join(f"{i.get('module')}.{i.get('name')}({i.get('safety')})" for i in perigosos[:5]))
    if ruim:
        return ruim, det
    geral = str(s.get("status") or "unscanned")
    return ("safe" if geral == "safe" else "unscanned"), det


def listar(repo: str) -> tuple[str, list[dict], dict]:
    info = httpx.get(f"https://huggingface.co/api/models/{repo}", params={"securityStatus": "true"}, timeout=30).json()
    sha = info.get("sha")
    arquivos: list[dict] = []
    for tentativa in range(3):  # a API às vezes devolve a árvore sem o resultado do scan
        arquivos = []
        url: str | None = f"https://huggingface.co/api/models/{repo}/tree/{sha}?recursive=true&expand=true"
        while url:  # a árvore vem paginada: segue o Link rel="next" até o fim
            r = httpx.get(url, timeout=60)
            r.raise_for_status()
            arquivos += [x for x in r.json() if x.get("type") == "file"]
            url = r.links.get("next", {}).get("url")
        if any("securityFileStatus" in x for x in arquivos):
            break
        time.sleep(2 * (tentativa + 1))
    return sha, arquivos, info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("padroes", nargs="+")
    ap.add_argument("--destino", default=None)
    ap.add_argument("--permitir", nargs="*", default=[], help="pickle explicitamente aceito (exige scan safe)")
    ap.add_argument("--so-checar", "--só-checar", dest="so_checar", action="store_true")
    a = ap.parse_args()

    sha, arquivos, info = listar(a.repo)
    print(f"{a.repo} @ {sha[:12]}  (autor: {info.get('author')}, licença: {(info.get('cardData') or {}).get('license')}, "
          f"código remoto: {'SIM' if any(ext(f['path']) == '.py' for f in arquivos) else 'não'})")
    escolhidos, recusas = [], []
    for f in arquivos:
        p = f["path"]
        if not any(fnmatch.fnmatch(p, pad) for pad in a.padroes):
            continue
        e = ext(p)
        st, det = status_scan(f)
        motivo = None
        if st in ("unsafe", "suspicious"):
            motivo = f"scanner do HF: {st} ({'; '.join(det)})"
        elif e in CODIGO:
            motivo = "código executável: nunca baixado"
        elif e in PICKLE:
            if p not in a.permitir:
                motivo = "formato pickle (executa código ao carregar)"
            elif st != "safe":
                motivo = f"pickle permitido só com scan safe; scan = {st}"
        elif e not in SEGUROS:
            motivo = f"extensão {e or '(nenhuma)'} fora da lista segura"
        tam = (f.get("lfs") or {}).get("size") or f.get("size") or 0
        linha = f"  {'✓' if not motivo else '✗'} {p:60} {tam / 1e6:9.1f} MB  scan={st}"
        print(linha + (f"  → {motivo}" if motivo else ""))
        (recusas if motivo else escolhidos).append((f, motivo))
    if recusas:
        print(f"\n{len(recusas)} arquivo(s) recusado(s); nada foi baixado.")
        return 2
    if not escolhidos:
        print("Nenhum arquivo casou com os padrões.")
        return 1
    total = sum(((f.get("lfs") or {}).get("size") or f.get("size") or 0) for f, _ in escolhidos)
    print(f"\n{len(escolhidos)} arquivo(s), {total / 1e9:.2f} GB")
    if a.so_checar:
        return 0

    from huggingface_hub import hf_hub_download

    destino = Path(a.destino or RAIZ / "modelos" / a.repo.replace("/", "__"))
    destino.mkdir(parents=True, exist_ok=True)
    manifesto = {"repo": a.repo, "commit": sha, "baixado_em": time.strftime("%Y-%m-%dT%H:%M:%S"), "arquivos": {}}
    for f, _ in escolhidos:
        caminho = hf_hub_download(a.repo, f["path"], revision=sha, local_dir=str(destino))
        h = hashlib.sha256()
        with open(caminho, "rb") as fh:
            for bloco in iter(lambda: fh.read(1 << 20), b""):
                h.update(bloco)
        esperado = (f.get("lfs") or {}).get("oid")
        if esperado and esperado != h.hexdigest():
            print(f"✗ sha256 não bate em {f['path']}: apagando")
            Path(caminho).unlink(missing_ok=True)
            return 3
        manifesto["arquivos"][f["path"]] = {"sha256": h.hexdigest(), "bytes": Path(caminho).stat().st_size,
                                            "scan": status_scan(f)[0]}
        print(f"  baixado {f['path']}")
    (destino / "MANIFESTO.json").write_text(json.dumps(manifesto, indent=1, ensure_ascii=False))
    print(f"ok → {destino}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
