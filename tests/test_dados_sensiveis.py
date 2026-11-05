"""Comportamento da máscara: o que tem de sumir, o que tem de ficar e a volta exata ao original."""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gw"))
os.environ.setdefault("GW_PERMITIDOS", str(Path(__file__).resolve().parent.parent / "permitidos.txt"))

import dados_sensiveis as ds

# Tokens falsos montados em runtime: o arquivo não carrega a assinatura literal.
TOKEN_GITHUB = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
CHAVE_AWS = "AK" + "IA" + "Z7Q4W9X2C5V8B1N3"
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow" + "A" * 60 + "\n-----END " + "RSA PRIVATE KEY-----"
TRECHO_FALSO = "Xy9" + "#kLm2pQ"
DSN = "postgres://app" + ":" + TRECHO_FALSO + "@" + "db.interno:5432/app"


@pytest.fixture()
def cofre() -> ds.Cofre:
    return ds.Cofre(b"k" * 32)


def ida_e_volta(texto: str, cofre: ds.Cofre) -> tuple[str, Counter]:
    c: Counter = Counter()
    mascarado = ds.mascarar_texto(texto, cofre, c)
    assert ds.desmascarar_texto(mascarado, cofre) == texto, "a volta tem de reproduzir o original exato"
    return mascarado, c


# --------------------------------------------------------------- validadores
@pytest.mark.parametrize("v,ok", [
    ("529.982.247-25", True), ("52998224725", True), ("529.982.247-24", False), ("111.111.111-11", False),
])
def test_cpf(v, ok):
    assert ds.cpf_valido(v) is ok


@pytest.mark.parametrize("v,ok", [
    ("11.222.333/0001-81", True), ("11222333000181", True), ("11.222.333/0001-80", False),
    ("12.ABC.345/01DE-35", True),  # CNPJ alfanumérico (exemplo oficial da Receita)
])
def test_cnpj(v, ok):
    assert ds.cnpj_valido(v) is ok


def test_luhn():
    assert ds.luhn_valido("4111 1111 1111 1111")
    assert not ds.luhn_valido("4111 1111 1111 1112")


# --------------------------------------------------------- o que tem de sumir
@pytest.mark.parametrize("texto,tipo,valor", [
    ("CPF do segurado: 529.982.247-25.", "CPF", "529.982.247-25"),
    ("cpf 52998224725 no cadastro", "CPF", "52998224725"),
    ("Empresa 11.222.333/0001-81 contratante", "CNPJ", "11.222.333/0001-81"),
    ("Cartão 4111 1111 1111 1111 recusado", "CARTAO", "4111 1111 1111 1111"),
    ("Veículo placa BRA2E19 batido", "PLACA", "BRA2E19"),
    ("placa antiga ABC-1234", "PLACA", "ABC-1234"),
    ("chassi 9BWZZZ377VT004251", "CHASSI", "9BWZZZ377VT004251"),
    ("Liga no (31) 98765-4321 amanhã", "TELEFONE", "(31) 98765-4321"),
    ("celular 31987654321", "TELEFONE", "31987654321"),
    ("manda pra maria.silva@exemplo.com.br", "EMAIL", "maria.silva@exemplo.com.br"),
    ("CEP 30140-071, BH", "CEP", "30140-071"),
    ("CNH 02650306461 vencida", "CNH", "02650306461"),
    ("RG: 12.345.678-9", "RG", "12.345.678-9"),
    ("mora na Rua das Flores, 123 ap 4", "ENDERECO", "Rua das Flores, 123"),
])
def test_detecta(cofre, texto, tipo, valor):
    mascarado, c = ida_e_volta(texto, cofre)
    assert valor not in mascarado
    assert c[tipo] == 1, (tipo, c, mascarado)


@pytest.mark.parametrize("texto,valor", [
    (f"export GITHUB_TOKEN={TOKEN_GITHUB}", TOKEN_GITHUB),
    (f"aws_access_key_id = {CHAVE_AWS}", CHAVE_AWS),
    ("a senha: Acme@2026 do banco", "Acme@2026"),
    (DSN, TRECHO_FALSO),
    ("Authorization: Bearer abcDEF1234567890ghiJKL0987654321zz", "abcDEF1234567890ghiJKL0987654321zz"),
    (f"chave:\n{PEM}\nfim", PEM),
])
def test_segredos(cofre, texto, valor):
    mascarado, c = ida_e_volta(texto, cofre)
    assert valor not in mascarado
    assert c["SEGREDO"] >= 1


def test_secret_gate_carrega_quando_existe():
    caminho = Path(os.environ.get("GW_SECRET_GATE", "")).expanduser()
    if not os.environ.get("GW_SECRET_GATE") or not caminho.exists():
        pytest.skip("GW_SECRET_GATE não configurado nesta máquina")
    assert ds._carrega_scan() is not None, "scan.py existe mas não carregou: regras do gate fora do gateway"


def test_nome_de_pessoa_pelo_ner(cofre):
    texto = "O segurado Carlos Eduardo Mendes ligou e falou com a Maria Aparecida da Silva."
    mascarado, c = ida_e_volta(texto, cofre)
    if ds._carrega_nlp() is None:
        pytest.skip("modelo pt do spaCy não instalado")
    assert "Carlos Eduardo Mendes" not in mascarado
    assert "Maria Aparecida da Silva" not in mascarado
    assert c["PESSOA"] == 2


@pytest.mark.parametrize("texto,nome,fica", [
    ("Cliente Carlos Eduardo Mendes CPF 529.982.247-25", "Carlos Eduardo Mendes", "Cliente"),  # NER cola o "CPF"
    ("Segurado: José da Silva Xavier, placa ABC1234", "José da Silva Xavier", "Segurado"),
    ("falei com o Dr. João Pedro hoje", "João Pedro", "Dr."),
])
def test_nome_em_frase_curta(cofre, texto, nome, fica):
    if ds._carrega_nlp() is None:
        pytest.skip("modelo pt do spaCy não instalado")
    mascarado, c = ida_e_volta(texto, cofre)
    assert nome not in mascarado and fica in mascarado and c["PESSOA"] == 1, mascarado


def test_permitidos_nao_sao_mascarados(cofre):
    texto = "O Hermes roda no OpenRouter com DeepSeek."
    assert ds.mascarar_texto(texto, cofre) == texto


# ------------------------------------------------- o que tem de ficar intacto
@pytest.mark.parametrize("texto", [
    "def soma(a, b):\n    return a + b  # versão 1.106.6",
    "commit 3f9a1c2b7d4e5f60718293a4b5c6d7e8f9012345 em 2026-11-03T12:00:00Z",
    "uuid 123e4567-e89b-12d3-a456-426614174000 porta 4000 pid 81234",
    "total = 12345678900 + 42",
    "ISO 8601 e UTF-8 e HTTP 429",
])
def test_codigo_comum_passa_intacto(cofre, texto):
    assert ds.mascarar_texto(texto, cofre) == texto


# ---------------------------------------------------------------- marcadores
def test_mesmo_valor_mesmo_marcador_com_formatos_diferentes(cofre):
    a = ds.mascarar_texto("529.982.247-25", cofre)
    b = ds.mascarar_texto("52998224725", cofre)
    assert a == b and a.startswith("[CPF_")


def test_marcador_sem_colchete_tambem_volta(cofre):
    m = ds.mascarar_texto("529.982.247-25", cofre)
    sem = m.strip("[]")
    assert ds.desmascarar_texto(f"o cpf é {sem}.", cofre) == "o cpf é 529.982.247-25."


def test_estrutura_de_chat_com_tool_call(cofre):
    msgs = [
        {"role": "system", "content": "Você é um agente."},
        {"role": "user", "content": [{"type": "text", "text": "Consulta o CPF 529.982.247-25"}]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "consulta_cliente", "arguments": '{"cpf": "529.982.247-25"}'},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"nome": "Carlos Eduardo Mendes", "placa": "BRA2E19"}'},
    ]
    c: Counter = Counter()
    saida = ds.mascarar_estrutura(msgs, cofre, c)
    texto = str(saida)
    for v in ("529.982.247-25", "BRA2E19"):
        assert v not in texto
    assert saida[2]["tool_calls"][0]["id"] == "call_1"
    assert saida[2]["tool_calls"][0]["function"]["name"] == "consulta_cliente"
    assert saida[3]["tool_call_id"] == "call_1"
    assert ds.desmascarar_estrutura(saida, cofre) == msgs


def test_streaming_com_marcador_partido(cofre):
    original = "Cliente 529.982.247-25 com placa BRA2E19 ok."
    mascarado = ds.mascarar_texto(original, cofre)
    for tamanho in (1, 2, 3, 5, 7, 11):
        d = ds.Desmascarador(cofre)
        pedacos = [mascarado[i:i + tamanho] for i in range(0, len(mascarado), tamanho)]
        saida = "".join(d.alimentar(p) for p in pedacos) + d.finalizar()
        assert saida == original, (tamanho, saida)


# --- arguments de tool call são JSON em string: máscara não pode quebrar o JSON (04/11, Hermes)

def test_argumentos_json_com_escape_unicode_continuam_validos(cofre):
    import json
    # caso real: "anotaí@pix.mock" serializado com \u00ed; a máscara crua gerava anota\[EMAIL_…]
    conteudo = 'value="anotaí@pix.mock"\n<p>responsável: maria@empresa.com.br</p>'
    args = json.dumps({"path": "app/page.tsx", "content": conteudo})  # ensure_ascii: vira \u00ed
    assert "\\u00ed" in args
    c = Counter()
    saida = ds.mascarar_estrutura({"role": "assistant", "tool_calls": [{"function": {"arguments": args}}]}, cofre, c)
    novo = saida["tool_calls"][0]["function"]["arguments"]
    assert json.loads(novo)["path"] == "app/page.tsx"  # JSON válido
    assert "maria@empresa.com.br" not in novo and c["EMAIL"] == 1
    assert "anotaí" in json.loads(novo)["content"]  # o texto em volta fica intacto


def test_mascara_crua_quebrava_o_json_do_hermes(cofre):
    import json
    args = json.dumps({"content": 'value="anotaí@pix.mock"'})
    with pytest.raises(ValueError):
        json.loads(ds.mascarar_texto(args, cofre, Counter()))  # o bug: máscara no texto cru
    json.loads(ds.mascarar_texto_ou_json(args, cofre, Counter()))  # a correção


def test_json_sem_dado_sensivel_passa_byte_a_byte(cofre):
    args = '{"cmd":  "ls -la",\n "cwd": "/tmp"}'
    assert ds.mascarar_estrutura({"arguments": args}, cofre, Counter())["arguments"] == args


def test_dentro_do_json_nenhuma_chave_e_ignorada(cofre):
    import json
    args = json.dumps({"name": "cliente 529.982.247-25"})
    novo = ds.mascarar_texto_ou_json(args, cofre, Counter())
    assert "529.982.247-25" not in novo and json.loads(novo)


def test_volta_com_aspas_e_barra_mantem_json_valido(cofre):
    import json
    m = cofre.marcador("PESSOA", 'Ana "Nina" C:\\dados')
    args = json.dumps({"nome": m})
    volta = ds.desmascarar_texto(args, cofre, json_escape=True)
    assert json.loads(volta)["nome"] == 'Ana "Nina" C:\\dados'
    d = ds.Desmascarador(cofre, json_escape=True)
    partes = [args[i:i + 4] for i in range(0, len(args), 4)]
    stream = "".join(d.alimentar(p) for p in partes) + d.finalizar()
    assert json.loads(stream)["nome"] == 'Ana "Nina" C:\\dados'
