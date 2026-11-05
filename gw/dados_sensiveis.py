"""Detecta e mascara dado sensível ANTES de qualquer coisa sair para um provedor.

A regra do gateway: provedor nenhum vê dado sensível, seja qual for o termo de uso dele.

Como funciona
-------------
1. Detectores acham trechos sensíveis no texto (ver ``DETECTORES`` e ``detectar``):
   - documentos BR com dígito verificador (CPF, CNPJ numérico e alfanumérico,
     RENAVAM) ou com contexto (CNH, RG);
   - cartão (Luhn), placa, chassi, telefone, e-mail, CEP;
   - segredos: as mesmas regras do secret-gate (um ``scan.py`` externo apontado por ``GW_SECRET_GATE`` (opcional)),
     mais bloco PEM e ``Bearer``;
   - pessoa e endereço de rua por NER em português (spaCy ``pt_core_news_md``),
     sem LLM.
2. Cada valor vira um marcador determinístico, ``[CPF_1a2b3c4d]``. O sufixo é um
   HMAC do valor com a chave local ``GW_CHAVE_MASCARA``, então o mesmo CPF dá o
   mesmo marcador em todo turno. Isso mantém a conversa coerente e o cache de
   prompt do provedor funcionando.
3. O ``Cofre`` guarda marcador → valor original só em memória, nunca em disco.
   Na volta, ``desmascarar`` troca os marcadores pelos valores reais. O agente
   recebe o dado de verdade (inclusive em argumentos de tool call) e o provedor
   nunca viu.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import logging
import os
import re
import sys
import threading
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("gw.dados_sensiveis")

# ---------------------------------------------------------------------------
# Marcador
# ---------------------------------------------------------------------------
TIPOS = (
    "SEGREDO", "CARTAO", "CNPJ", "CPF", "RENAVAM", "CNH", "RG", "CHASSI", "PLACA",
    "EMAIL", "TELEFONE", "CEP", "ENDERECO", "PESSOA",
)
# Prioridade quando dois achados se sobrepõem: o primeiro da lista vence.
PRIORIDADE = {t: i for i, t in enumerate(TIPOS)}

_TIPOS_RX = "|".join(TIPOS)
# Tolerante: o modelo às vezes devolve o marcador sem os colchetes.
MARCADOR_RX = re.compile(rf"\[?\b({_TIPOS_RX})_([0-9a-f]{{8}})\b\]?")


@dataclass(frozen=True)
class Achado:
    inicio: int
    fim: int
    tipo: str
    valor: str


# ---------------------------------------------------------------------------
# Validadores de documento
# ---------------------------------------------------------------------------
def _so_digitos(s: str) -> str:
    return re.sub(r"\D", "", s)


def cpf_valido(s: str) -> bool:
    d = _so_digitos(s)
    if len(d) != 11 or d == d[0] * 11:
        return False
    for n in (9, 10):
        soma = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        if (soma * 10) % 11 % 10 != int(d[n]):
            return False
    return True


def cnpj_valido(s: str) -> bool:
    """CNPJ numérico e alfanumérico (IN RFB 2.229/2024): valor do caractere = ord - 48."""
    c = re.sub(r"[.\-/\s]", "", s).upper()
    if len(c) != 14 or not re.fullmatch(r"[A-Z0-9]{12}\d{2}", c) or c == c[0] * 14:
        return False
    vals = [ord(ch) - 48 for ch in c]
    for n in (12, 13):
        pesos = list(range(n - 7, 1, -1)) + list(range(9, 1, -1))
        soma = sum(v * p for v, p in zip(vals[:n], pesos))
        dv = 11 - soma % 11
        if (0 if dv >= 10 else dv) != vals[n]:
            return False
    return True


def renavam_valido(s: str) -> bool:
    d = _so_digitos(s).zfill(11)
    if len(d) != 11:
        return False
    pesos = [3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    dv = (sum(int(a) * b for a, b in zip(d[:10], pesos)) * 10) % 11
    return (0 if dv == 10 else dv) == int(d[10])


def luhn_valido(s: str) -> bool:
    d = _so_digitos(s)
    if not 13 <= len(d) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(d)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


# ---------------------------------------------------------------------------
# Detectores por regex
# ---------------------------------------------------------------------------
def _contexto_antes(texto: str, inicio: int, rx: re.Pattern[str], janela: int = 40) -> bool:
    return bool(rx.search(texto[max(0, inicio - janela):inicio]))


CTX_CNH = re.compile(r"\b(cnh|habilita[cç][aã]o|registro\s+nacional)\b", re.IGNORECASE)
CTX_RG = re.compile(r"\b(rg|r\.g\.|identidade|registro\s+geral)\b", re.IGNORECASE)
CTX_RENAVAM = re.compile(r"\brenavam\b", re.IGNORECASE)

RX_CPF = re.compile(r"(?<![\w.\-/])\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?![\w\-/]|\.\d)")
RX_CNPJ_NUM = re.compile(r"(?<![\w.\-/])\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?![\w\-/]|\.\d)")
RX_CNPJ_ALFA = re.compile(r"(?<![\w.\-/])[A-Z0-9]{2}\.[A-Z0-9]{3}\.[A-Z0-9]{3}/[A-Z0-9]{4}-\d{2}(?![\w\-/])")
RX_11_DIGITOS = re.compile(r"(?<![\w.\-/])\d{11}(?![\w\-/]|\.\d)")
RX_RG = re.compile(r"(?<![\w.\-/])\d{1,2}\.?\d{3}\.?\d{3}-?[\dXx](?![\w\-/])")
RX_CARTAO = re.compile(r"(?<![\w\-])[3-6]\d{3}(?:[ \-]?\d{3,4}){2,4}(?![\w\-])")
RX_PLACA = re.compile(r"(?<![\w\-])[A-Z]{3}-?\d[A-Z0-9]\d{2}(?![\w\-])")
RX_CHASSI = re.compile(r"(?<![\w\-])[A-HJ-NPR-Z0-9]{17}(?![\w\-])")
RX_EMAIL = re.compile(r"(?<![\w.+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?![\w])")
RX_TELEFONE = re.compile(
    r"(?<![\w+])(?:\+55[\s\-]?)?(?:\(\d{2}\)\s?|\d{2}[\s\-])(?:9\s?\d{4}|\d{4})[\s\-]\d{4}(?![\w])"
    r"|(?<![\w+])\+55\d{10,11}(?![\w])"
)
RX_CEP = re.compile(r"(?<![\w\-])\d{5}-\d{3}(?![\w\-])")
RX_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL)
RX_BEARER = re.compile(r"(?i)\b(?:bearer|token)\s+([A-Za-z0-9\-._~+/]{20,}=*)")
RX_ENDERECO = re.compile(
    r"\b(?:Rua|R\.|Avenida|Av\.?|Travessa|Tv\.|Alameda|Al\.|Rodovia|Rod\.|Estrada|Pra[cç]a|Largo|Quadra|Q\.)"
    r"\s+(?:(?:d[aeo]s?|de)\s+)?[A-ZÀ-Ý0-9][\wÀ-ÿ.'\- ]{1,60}?(?:,?\s*(?:n[ºo°.]?\s*)?\d{1,6}[A-Za-z]?)\b",
)
DDD_VALIDOS = {int(x) for x in ["11", "12", "13", "14", "15", "16", "17", "18", "19", "21", "22", "24", "27", "28", "31", "32", "33", "34", "35", "37", "38", "41", "42", "43", "44", "45", "46", "47", "48", "49", "51", "53", "54", "55", "61", "62", "63", "64", "65", "66", "67", "68", "69", "71", "73", "74", "75", "77", "79", "81", "82", "83", "84", "85", "86", "87", "88", "89", "91", "92", "93", "94", "95", "96", "97", "98", "99"]}


def _detecta_documentos(texto: str) -> Iterable[Achado]:
    for m in RX_CNPJ_NUM.finditer(texto):
        if cnpj_valido(m.group()):
            yield Achado(m.start(), m.end(), "CNPJ", m.group())
    for m in RX_CNPJ_ALFA.finditer(texto):
        if not m.group().replace(".", "").replace("/", "").replace("-", "").isdigit() and cnpj_valido(m.group()):
            yield Achado(m.start(), m.end(), "CNPJ", m.group())
    for m in RX_CPF.finditer(texto):
        if cpf_valido(m.group()):
            yield Achado(m.start(), m.end(), "CPF", m.group())
    # 11 dígitos colados que não são CPF: RENAVAM/CNH pelo contexto, celular pelo DDD.
    for m in RX_11_DIGITOS.finditer(texto):
        v = m.group()
        if cpf_valido(v):
            continue
        if _contexto_antes(texto, m.start(), CTX_RENAVAM) and renavam_valido(v):
            yield Achado(m.start(), m.end(), "RENAVAM", v)
        elif _contexto_antes(texto, m.start(), CTX_CNH):
            yield Achado(m.start(), m.end(), "CNH", v)
        elif int(v[:2]) in DDD_VALIDOS and v[2] == "9":
            yield Achado(m.start(), m.end(), "TELEFONE", v)
    for m in re.finditer(r"(?<![\w.\-/])\d{9,10}(?![\w\-/]|\.\d)", texto):
        if _contexto_antes(texto, m.start(), CTX_RENAVAM) and renavam_valido(m.group()):
            yield Achado(m.start(), m.end(), "RENAVAM", m.group())
    for m in RX_RG.finditer(texto):
        if _contexto_antes(texto, m.start(), CTX_RG, janela=25):
            yield Achado(m.start(), m.end(), "RG", m.group())


def _detecta_contato_e_veiculo(texto: str) -> Iterable[Achado]:
    for m in RX_CARTAO.finditer(texto):
        if luhn_valido(m.group()):
            yield Achado(m.start(), m.end(), "CARTAO", m.group())
    for m in RX_PLACA.finditer(texto):
        yield Achado(m.start(), m.end(), "PLACA", m.group())
    for m in RX_CHASSI.finditer(texto):
        v = m.group()
        if re.search(r"[A-Z]", v) and re.search(r"\d", v) and sum(ch.isdigit() for ch in v) >= 4:
            yield Achado(m.start(), m.end(), "CHASSI", v)
    for m in RX_EMAIL.finditer(texto):
        yield Achado(m.start(), m.end(), "EMAIL", m.group())
    for m in RX_TELEFONE.finditer(texto):
        ddd = re.search(r"\d{2}", m.group().replace("+55", "", 1))
        if ddd and int(ddd.group()) in DDD_VALIDOS:
            yield Achado(m.start(), m.end(), "TELEFONE", m.group())
    for m in RX_CEP.finditer(texto):
        yield Achado(m.start(), m.end(), "CEP", m.group())
    for m in RX_ENDERECO.finditer(texto):
        yield Achado(m.start(), m.end(), "ENDERECO", m.group())


# ---------------------------------------------------------------------------
# Segredos: regras do secret-gate, carregadas do próprio scan.py (fonte única)
# ---------------------------------------------------------------------------
_SCAN = None
_SCAN_TENTADO = False


def _carrega_scan():
    global _SCAN, _SCAN_TENTADO
    if _SCAN_TENTADO:
        return _SCAN
    _SCAN_TENTADO = True
    if not os.environ.get("GW_SECRET_GATE"):
        return None  # sem scanner externo: só as regras embutidas
    caminho = Path(os.environ["GW_SECRET_GATE"]).expanduser()
    if not caminho.exists():
        log.warning("secret-gate não encontrado em %s; usando só as regras embutidas", caminho)
        return None
    try:
        spec = importlib.util.spec_from_file_location("gw_secret_gate_scan", caminho)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        # @dataclass do scan.py procura o próprio módulo em sys.modules durante a execução.
        sys.modules["gw_secret_gate_scan"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        _SCAN = mod
    except Exception as e:  # noqa: BLE001 - gateway não pode cair por causa do gate
        sys.modules.pop("gw_secret_gate_scan", None)
        log.warning("falha ao carregar o secret-gate (%s); usando só as regras embutidas", e)
    return _SCAN


# Usadas quando o scan.py não está disponível.
_REGRAS_EMBUTIDAS = [
    re.compile(r"\b(A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b"),
    re.compile(r"\bhv[sb]\.[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b(gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-(proj-)?[A-Za-z0-9_-]{32,}"),
    re.compile(r"\bsk-or-v1-[A-Za-z0-9]{32,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
]
RX_DSN_SENHA = re.compile(r"\b[a-z][a-z0-9+.\-]{2,}://[^\s:/@]{1,64}:([^\s:/@]{6,})@[^\s/]{3,}", re.IGNORECASE)
RX_SENHA_SIMPLES = re.compile(r"(?i)\b(senha|password|passwd|pwd|secret|api[_-]?key|token)\b\s*[:=]\s*[\"']?([^\s\"']{6,})")


def _detecta_segredos(texto: str) -> Iterable[Achado]:
    for m in RX_PEM.finditer(texto):
        yield Achado(m.start(), m.end(), "SEGREDO", m.group())
    for m in RX_BEARER.finditer(texto):
        yield Achado(m.start(1), m.end(1), "SEGREDO", m.group(1))
    scan = _carrega_scan()
    if scan is None:
        for rx in _REGRAS_EMBUTIDAS:
            for m in rx.finditer(texto):
                yield Achado(m.start(), m.end(), "SEGREDO", m.group())
        for m in RX_DSN_SENHA.finditer(texto):
            yield Achado(m.start(1), m.end(1), "SEGREDO", m.group(1))
        for m in RX_SENHA_SIMPLES.finditer(texto):
            yield Achado(m.start(2), m.end(2), "SEGREDO", m.group(2))
        return
    for rid, rx, _motivo in scan.REGRAS_CONTEUDO:
        if rid in ("provider/chave-privada", "provider/ssh-privada"):
            continue  # já coberto pelo bloco PEM inteiro acima
        for m in rx.finditer(texto):
            if rid == "provider/dsn-com-senha":
                if scan.dsn_inofensivo(m.group(0)):
                    continue
                # mascara só a senha do DSN, o resto (host, banco) segue legível
                if m.lastindex:
                    yield Achado(m.start(1), m.end(1), "SEGREDO", m.group(1))
                    continue
            yield Achado(m.start(), m.end(), "SEGREDO", m.group())
    for m in scan.ATRIBUICAO.finditer(texto):
        nome, valor = m.group("nome"), m.group("v")
        if not scan.NOME_SEGREDO.search(nome) or scan.NOME_FALSO_POSITIVO.search(nome):
            continue
        dec = scan.decodifica_base64_curto(valor)
        if scan.parece_credencial(valor) or (dec and scan.parece_credencial(dec)):
            yield Achado(m.start("v"), m.end("v"), "SEGREDO", valor)
    # `senha: abc123` curto e sem entropia não passa no parece_credencial do gate
    # (que foi feito para código). Num prompt, senha é senha.
    for m in RX_SENHA_SIMPLES.finditer(texto):
        if m.group(1).lower() in ("senha", "password", "passwd", "pwd"):
            yield Achado(m.start(2), m.end(2), "SEGREDO", m.group(2))


# ---------------------------------------------------------------------------
# NER em português (pessoa e endereço) — spaCy, sem LLM
# ---------------------------------------------------------------------------
_NLP = None
_NLP_TENTADO = False
_NLP_LOCK = threading.Lock()
NER_MAX_CHARS = int(os.environ.get("GW_NER_MAX_CHARS", "100000"))
NER_LIGADO = os.environ.get("GW_NER", "1") not in ("0", "false", "off", "")
RX_PALAVRA_NOME = re.compile(r"^[A-ZÀ-Ý][a-zà-ÿ]+(?:['\-][A-ZÀ-Ý]?[a-zà-ÿ]+)?$")
RX_CONECTOR_NOME = re.compile(r"^(?:d[aeo]s?|e|de)$")
TITULOS = frozenset({
    "cliente", "segurado", "segurada", "sr", "sra", "srta", "dr", "dra", "senhor", "senhora", "corretor",
    "corretora", "motorista", "terceiro", "terceira", "condutor", "condutora", "titular", "o", "a", "os", "as",
})


def _trechos_de_nome(texto: str) -> Iterable[tuple[int, int]]:
    """Dentro de uma entidade PER, os trechos com forma de nome próprio.

    O NER às vezes cola palavra vizinha na entidade ("Carlos Mendes CPF"): aqui só
    sobra a sequência de palavras capitalizadas, com conectores (da, de, dos, e) no
    meio. Pontuação depois de uma palavra ("Mendes,") encerra o nome.
    """
    pontuacao = ",.;:!?()\"'"
    run: list[tuple[int, int, bool]] = []  # (início, fim, é palavra de nome)

    def fecha():
        while run and not run[-1][2]:
            run.pop()  # conector no fim não faz parte do nome
        if run and run[-1][1] - run[0][0] >= 3:
            yield run[0][0], run[-1][1]
        run.clear()

    for m in re.finditer(r"\S+", texto):
        bruto = m.group()
        palavra = bruto.strip(pontuacao)
        if not palavra:
            yield from fecha()
            continue
        ini = m.start() + (len(bruto) - len(bruto.lstrip(pontuacao)))
        fim = ini + len(palavra)
        eh_nome = bool(RX_PALAVRA_NOME.match(palavra)) and palavra.lower() not in TITULOS
        if eh_nome or (run and RX_CONECTOR_NOME.match(palavra)):
            run.append((ini, fim, eh_nome))
            if eh_nome and bruto.rstrip(pontuacao) != bruto:
                yield from fecha()
        else:
            yield from fecha()
    yield from fecha()


def _carrega_nlp():
    global _NLP, _NLP_TENTADO
    with _NLP_LOCK:
        if _NLP_TENTADO:
            return _NLP
        _NLP_TENTADO = True
        try:
            import spacy

            _NLP = spacy.load(
                os.environ.get("GW_NER_MODELO", "pt_core_news_md"),
                disable=["parser", "lemmatizer", "morphologizer", "attribute_ruler", "tagger"],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("NER indisponível (%s): nomes de pessoa não serão mascarados", e)
        return _NLP


def _detecta_entidades(texto: str) -> Iterable[Achado]:
    if not NER_LIGADO or not texto.strip():
        return
    nlp = _carrega_nlp()
    if nlp is None:
        return
    pedaco = 5000
    limite = min(len(texto), NER_MAX_CHARS)
    pos = 0
    while pos < limite:
        fim = min(limite, pos + pedaco)
        if fim < limite:  # corta em quebra de linha/espaço para não partir um nome
            corte = max(texto.rfind("\n", pos, fim), texto.rfind(" ", pos, fim))
            if corte > pos + pedaco // 2:
                fim = corte
        doc = nlp(texto[pos:fim])
        for ent in doc.ents:
            if ent.label_ != "PER":
                continue
            for a, b in _trechos_de_nome(ent.text):
                ini = pos + ent.start_char + a
                yield Achado(ini, pos + ent.start_char + b, "PESSOA", ent.text[a:b])
        pos = fim


# ---------------------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------------------
DETECTORES: list[Callable[[str], Iterable[Achado]]] = [
    _detecta_segredos,
    _detecta_documentos,
    _detecta_contato_e_veiculo,
    _detecta_entidades,
]


def _carrega_permitidos() -> set[str]:
    arq = Path(os.environ.get("GW_PERMITIDOS", Path(__file__).resolve().parent.parent / "permitidos.txt"))
    termos: set[str] = set()
    for a in (arq, arq.with_name(arq.stem + ".local" + arq.suffix)):  # .local: termos seus, fora do git
        if a.exists():
            termos |= {
                ln.strip().lower()
                for ln in a.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")
            }
    return termos


PERMITIDOS = _carrega_permitidos()


def detectar(texto: str) -> list[Achado]:
    """Todos os achados do texto, sem sobreposição (o tipo mais prioritário vence)."""
    if not texto:
        return []
    candidatos: list[Achado] = []
    for det in DETECTORES:
        try:
            candidatos.extend(det(texto))
        except Exception as e:
            log.exception("detector %s falhou: %s", det.__name__, e)
    candidatos = [
        a for a in candidatos
        if a.valor.strip().lower() not in PERMITIDOS and not MARCADOR_RX.fullmatch(a.valor)
    ]
    candidatos.sort(key=lambda a: (PRIORIDADE[a.tipo], -(a.fim - a.inicio), a.inicio))
    escolhidos: list[Achado] = []
    for a in candidatos:
        if all(a.fim <= b.inicio or a.inicio >= b.fim for b in escolhidos):
            escolhidos.append(a)
    escolhidos.sort(key=lambda a: a.inicio)
    return escolhidos


class Cofre:
    """Marcador → valor real, só em memória, com teto (LRU)."""

    def __init__(self, chave: bytes, teto: int = 200_000):
        self._chave = chave
        self._teto = teto
        self._mapa: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def marcador(self, tipo: str, valor: str) -> str:
        normal = _so_digitos(valor) if tipo in ("CPF", "RENAVAM", "CNH", "TELEFONE", "CARTAO", "CEP") else valor.strip()
        if tipo in ("CNPJ", "PLACA", "CHASSI"):
            normal = re.sub(r"[.\-/\s]", "", valor).upper()
        if tipo == "EMAIL":
            normal = valor.strip().lower()
        sufixo = hmac.new(self._chave, f"{tipo}:{normal}".encode(), hashlib.sha256).hexdigest()[:8]
        m = f"{tipo}_{sufixo}"
        with self._lock:
            if m not in self._mapa:
                self._mapa[m] = valor
                if len(self._mapa) > self._teto:
                    self._mapa.popitem(last=False)
            else:
                self._mapa.move_to_end(m)
        return f"[{m}]"

    def valor(self, chave_marcador: str) -> str | None:
        with self._lock:
            return self._mapa.get(chave_marcador)


def mascarar_texto(texto: str, cofre: Cofre, contagem: Counter | None = None) -> str:
    achados = detectar(texto)
    if not achados:
        return texto
    partes: list[str] = []
    pos = 0
    for a in achados:
        partes.append(texto[pos:a.inicio])
        partes.append(cofre.marcador(a.tipo, a.valor))
        if contagem is not None:
            contagem[a.tipo] += 1
        pos = a.fim
    partes.append(texto[pos:])
    return "".join(partes)


def desmascarar_texto(texto: str, cofre: Cofre, json_escape: bool = False) -> str:
    """json_escape=True: o texto é JSON (ex.: `arguments` de tool call) e o valor real
    entra DENTRO de uma string JSON, então sai escapado (aspas, barra, quebra de linha)."""
    if not texto or "_" not in texto:
        return texto

    def troca(m: re.Match[str]) -> str:
        v = cofre.valor(f"{m.group(1)}_{m.group(2)}")
        if v is None:
            return m.group(0)
        return json.dumps(v, ensure_ascii=False)[1:-1] if json_escape else v

    return MARCADOR_RX.sub(troca, texto)


# Chaves cujo valor nunca é texto do usuário (ids, papéis, binário de imagem).
CHAVES_IGNORADAS = frozenset({
    "role", "type", "id", "tool_call_id", "call_id", "name", "image_url", "url", "file_data",
    "file_id", "input_audio", "data", "media_type", "cache_control", "signature", "format",
    "detail", "mime_type", "status", "model", "index",
})


def _mascarar_valores(obj: Any, cofre: Cofre, contagem: Counter) -> Any:
    """Dentro de um JSON decodificado: mascara TODO valor string (nenhuma chave é ignorada,
    `name` aqui pode ser o nome de uma pessoa)."""
    if isinstance(obj, str):
        return mascarar_texto(obj, cofre, contagem)
    if isinstance(obj, list):
        return [_mascarar_valores(x, cofre, contagem) for x in obj]
    if isinstance(obj, dict):
        return {k: _mascarar_valores(v, cofre, contagem) for k, v in obj.items()}
    return obj


def mascarar_texto_ou_json(texto: str, cofre: Cofre, contagem: Counter) -> str:
    """String que é JSON (arguments de tool call, resultado de tool): decodifica, mascara os
    valores e codifica de novo. Mascarar o texto cru quebrava escape: `anota\\u00ed@pix.mock`
    virava `anota\\[EMAIL_…]`, JSON inválido que a NVIDIA recusa (04/11, Hermes)."""
    if texto[:1] in "{[" or texto.lstrip()[:1] in "{[":
        try:
            dado = json.loads(texto)
        except ValueError:
            dado = None
        if isinstance(dado, (dict, list)):
            antes = sum(contagem.values())
            mascarado = _mascarar_valores(dado, cofre, contagem)
            if sum(contagem.values()) == antes:
                return texto  # nada sensível: devolve o original byte a byte
            return json.dumps(mascarado, ensure_ascii=False)
    return mascarar_texto(texto, cofre, contagem)


def mascarar_estrutura(obj: Any, cofre: Cofre, contagem: Counter) -> Any:
    """Percorre dict/list e mascara toda string que é conteúdo (texto, resultado de tool, argumentos)."""
    if isinstance(obj, str):
        return mascarar_texto_ou_json(obj, cofre, contagem)
    if isinstance(obj, list):
        return [mascarar_estrutura(x, cofre, contagem) for x in obj]
    if isinstance(obj, dict):
        return {
            k: (v if k in CHAVES_IGNORADAS else mascarar_estrutura(v, cofre, contagem))
            for k, v in obj.items()
        }
    return obj


def desmascarar_estrutura(obj: Any, cofre: Cofre) -> Any:
    if isinstance(obj, str):
        return desmascarar_texto(obj, cofre)
    if isinstance(obj, list):
        return [desmascarar_estrutura(x, cofre) for x in obj]
    if isinstance(obj, dict):
        return {k: desmascarar_estrutura(v, cofre) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# Streaming: o marcador pode vir partido entre dois pedaços
# ---------------------------------------------------------------------------
RX_CAUDA_MARCADOR = re.compile(rf"(?:\[(?:[A-Z]{{0,10}}(?:_[0-9a-f]{{0,8}})?)?|\b(?:{_TIPOS_RX})(?:_[0-9a-f]{{0,8}})?|\b[A-Z]{{1,9}})$")


class Desmascarador:
    """Desmascara texto que chega em pedaços, segurando só a cauda que pode ser um marcador incompleto."""

    def __init__(self, cofre: Cofre, json_escape: bool = False):
        self.cofre = cofre
        self.pendente = ""
        self.json_escape = json_escape

    def alimentar(self, pedaco: str) -> str:
        buf = self.pendente + (pedaco or "")
        m = RX_CAUDA_MARCADOR.search(buf)
        if m and len(buf) - m.start() <= 24:
            corte = m.start()
        else:
            corte = len(buf)
        pronto, self.pendente = buf[:corte], buf[corte:]
        return desmascarar_texto(pronto, self.cofre, self.json_escape)

    def finalizar(self) -> str:
        resto, self.pendente = self.pendente, ""
        return desmascarar_texto(resto, self.cofre, self.json_escape)
