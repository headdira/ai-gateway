"""Regras do roteador: quem sobe, quem cai e por quê."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gw"))

import roteador as rt


def item(nome, nota, provedor="p", **kw):
    return {"nome": f"m:{provedor}/{nome}", "id": nome, "provedor": provedor, "nota": nota, **kw}


@pytest.fixture()
def montar(tmp_path):
    def _montar(itens, rotas=None):
        cat = {"rotas": rotas or {"geral": {"perfil": "equilibrado"}, "q": {"perfil": "qualidade"},
                                  "v": {"perfil": "velocidade"}, "visao": {"perfil": "qualidade", "visao": True}},
               "fallbacks_por_pedido": 2}
        return rt.Roteador(cat, itens, {}, rt.Saude(tmp_path / "saude.json"))
    return _montar


def nomes(av):
    return [a["nome"].split("/")[1] for a in av if a["fora"] is None]


def test_sem_historico_ganha_a_maior_nota(montar):
    r = montar([item("fraco", 30), item("forte", 90), item("medio", 60)])
    assert nomes(r.avaliar("q")) == ["forte", "medio", "fraco"]


def test_perfil_velocidade_prefere_o_rapido_medido(montar):
    r = montar([item("lento-bom", 90), item("rapido-ok", 60)])
    for _ in range(3):
        r.saude.sucesso("m:p/lento-bom", "p", 20_000)
        r.saude.sucesso("m:p/rapido-ok", "p", 300)
    assert nomes(r.avaliar("v"))[0] == "rapido-ok"
    assert nomes(r.avaliar("q"))[0] == "lento-bom"


def test_falha_coloca_em_espera_e_tira_da_disputa(montar):
    r = montar([item("a", 90), item("b", 50)])
    r.saude.falha("m:p/a", "p", "timeout")
    av = r.avaliar("geral")
    assert nomes(av) == ["b"]
    fora = next(a for a in av if a["nome"] == "m:p/a")["fora"]
    assert "em espera (timeout)" in fora


def test_espera_dobra_com_erros_seguidos(montar):
    r = montar([item("a", 90)])
    r.saude.falha("m:p/a", "p", "erro")
    primeira = r.saude.modelo("m:p/a")["ate"] - time.time()
    r.saude.falha("m:p/a", "p", "erro")
    segunda = r.saude.modelo("m:p/a")["ate"] - time.time()
    assert segunda > primeira * 1.8


def test_sucesso_tira_da_espera(montar):
    r = montar([item("a", 90)])
    r.saude.falha("m:p/a", "p", "429")
    r.saude.sucesso("m:p/a", "p", 500)
    assert nomes(r.avaliar("geral")) == ["a"]


def test_cota_do_dia_do_modelo_e_do_provedor(montar):
    r = montar([item("a", 90, rpd=2), item("b", 80, provedor="x", rpd_provedor=1), item("c", 10)])
    for _ in range(2):
        r.saude.sucesso("m:p/a", "p", 100)
    r.saude.sucesso("m:x/b", "x", 100)
    assert nomes(r.avaliar("geral")) == ["c"]


def test_imagem_so_vai_para_visao(montar):
    r = montar([item("texto", 95), item("olho", 40, visao=True)])
    nec = {"tools": False, "imagem": True, "tokens": 10}
    assert nomes(r.avaliar("geral", nec)) == ["olho"]
    assert nomes(r.avaliar("visao")) == ["olho"]


def test_pedido_com_ferramenta_so_vai_para_tool_call_provada(montar):
    r = montar([item("quebrou", 95), item("nunca-testou", 90), item("provada", 70)])
    r.saude.modelo("m:p/quebrou")["tools"] = False
    r.saude.modelo("m:p/provada")["tools"] = True
    nec = {"tools": True, "imagem": False, "tokens": 10}
    av = {a["nome"].split("/")[1]: a["fora"] for a in r.avaliar("q", nec)}
    assert av == {"provada": None, "nunca-testou": "tool call não provada", "quebrou": "ferramenta falhou antes"}
    assert nomes(r.avaliar("q"))[0] == "quebrou"  # sem ferramenta no pedido, volta a valer


def test_ferramenta_no_pedido_nao_volta_pelo_ultimo_recurso_em_espera(montar):
    r = montar([item("sem-tool", 95), item("com-tool", 70)])
    r.saude.modelo("m:p/com-tool")["tools"] = True
    r.saude.falha("m:p/sem-tool", "p", "timeout")
    r.saude.falha("m:p/com-tool", "p", "auth")
    pedido = {"messages": [{"role": "user", "content": "oi"}], "tools": [{"type": "function"}]}
    ordem, _ = r.planejar("geral", pedido)
    assert ordem == ["m:p/com-tool"]  # só quem provou tool, mesmo em espera
    r.saude.modelo("m:p/com-tool")["tools"] = None
    ordem, _ = r.planejar("geral", pedido)
    assert ordem == []  # ninguém com tool provada: plano vazio (o gateway recusa)


def test_reserva_tambem_exige_tool_call_provada(montar):
    r = montar([item("rota-tool", 90, provedor="a"), item("reserva-sem", 95, provedor="b"),
                item("reserva-tool", 60, provedor="b")],
               rotas={"r": {"perfil": "qualidade", "provedor": "a", "reserva": "geral"},
                      "geral": {"perfil": "equilibrado"}})
    for n in ("m:a/rota-tool", "m:b/reserva-tool"):
        r.saude.modelo(n)["tools"] = True
    ordem, _ = r.planejar("r", {"messages": [], "tools": [{"type": "function"}]})
    assert ordem == ["m:a/rota-tool", "m:b/reserva-tool"]


def test_prompt_maior_que_o_contexto_fica_de_fora(montar):
    r = montar([item("curto", 95, contexto=8000), item("longo", 50, contexto=200_000)])
    nec = {"tools": False, "imagem": False, "tokens": 50_000}
    assert nomes(r.avaliar("geral", nec)) == ["longo"]


def test_plano_respeita_numero_de_fallbacks(montar):
    r = montar([item(n, 90 - i) for i, n in enumerate("abcde")])
    ordem, _ = r.planejar("q", {"messages": [{"role": "user", "content": "oi"}]})
    assert [o.split("/")[1] for o in ordem] == ["a", "b", "c"]


def test_todos_em_espera_tenta_quem_volta_primeiro(montar):
    r = montar([item("a", 90), item("b", 80)])
    r.saude.falha("m:p/a", "p", "auth")     # 1 h
    r.saude.falha("m:p/b", "p", "timeout")  # 5 min
    ordem, _ = r.planejar("geral", {"messages": []})
    assert ordem == ["m:p/b", "m:p/a"]


def test_necessidades_le_so_fatos_do_pedido():
    nec = rt.necessidades({
        "messages": [{"role": "user", "content": [{"type": "text", "text": "x" * 350},
                                                  {"type": "image_url", "image_url": {"url": "data:"}}]}],
        "tools": [{"type": "function"}], "max_tokens": 100,
    })
    assert nec["tools"] and nec["imagem"] and nec["tokens"] > 200


@pytest.mark.parametrize("exc,motivo", [
    (type("RateLimitError", (Exception,), {"status_code": 429})(), "429"),
    (type("Timeout", (Exception,), {})(), "timeout"),
    (type("AuthenticationError", (Exception,), {"status_code": 401})(), "auth"),
    (type("InternalServerError", (Exception,), {"status_code": 500})(), "erro"),
])
def test_motivo_da_excecao(exc, motivo):
    assert rt.motivo_da_excecao(exc) == motivo


def test_saude_persiste_entre_reinicios(tmp_path):
    s = rt.Saude(tmp_path / "saude.json")
    s.sucesso("m:p/a", "p", 1234, tools_ok=True)
    s.salvar(forcar=True)
    s2 = rt.Saude(tmp_path / "saude.json")
    assert s2.modelo("m:p/a")["lat_ms"] == 1234 and s2.modelo("m:p/a")["tools"] is True


def test_reserva_completa_a_fila_quando_a_rota_esta_fora(montar):
    r = montar([item("deepseek-a", 90), item("outro", 60)],
               rotas={"geral": {"perfil": "equilibrado"},
                      "deepseek": {"perfil": "qualidade", "ref_contem": "deepseek", "reserva": "geral"}})
    r.saude.falha("m:p/deepseek-a", "p", "timeout")
    ordem, _ = r.planejar("deepseek", {"messages": []})
    assert ordem == ["m:p/outro"]


@pytest.mark.parametrize("texto", [
    '">\n<atem:parameter name="padrao">*</atem:parameter>\n</atem:invoke>\n</atem:function_calls>',
    '<function_calls><invoke name="ler"><parameter name="p">a</parameter></invoke></function_calls>',
    'vou buscar <tool_call>{"name": "buscar", "arguments": {}}</tool_call>',
    '<function=buscar>{"q": "x"}</function>',
    '[TOOL_CALLS] [{"name": "x"}]',
])
def test_tool_call_em_texto_pega_os_formatos_conhecidos(texto):
    assert rt.tool_call_em_texto(texto)


@pytest.mark.parametrize("texto", [
    None, "", "Opa, Ana. No que posso ajudar?",
    "Use a função `invoke()` do SDK e o parâmetro name.",
    "<div class=\"tool\">ok</div>",
])
def test_tool_call_em_texto_nao_confunde_texto_normal(texto):
    assert not rt.tool_call_em_texto(texto)


@pytest.mark.parametrize("msg", [
    "OpenrouterException - No endpoints found that support tool use",
    "This model does not support tools",
    "GroqException - model `x` does not support function calling",
    "Function calling is not supported by this model",
    "tool_choice is not supported",
])
def test_erro_de_modelo_sem_tool_reconhece_falta_de_suporte(msg):
    assert rt.erro_de_modelo_sem_tool(Exception(msg))


@pytest.mark.parametrize("msg", [
    # o 400 de 04/11: problema no HISTÓRICO, não no modelo
    "Nvidia_nimException - Validation: `messages[228].tool_calls[0].function.arguments` must be a valid JSON object string",
    "GroqException - 'messages.3' : for 'role:tool' the following must be satisfied",
    "Request timed out",
    "Rate limit exceeded",
])
def test_erro_de_modelo_sem_tool_nao_confunde_outros_erros(msg):
    assert not rt.erro_de_modelo_sem_tool(Exception(msg))


# --- janela de contexto (05/11): o Hermes lê o tamanho da rota e resume a conversa antes de estourar

def test_prompt_que_nao_cabe_em_nenhum_modelo_vira_estouro_de_contexto(montar):
    r = montar([item("a", 90, contexto=8000), item("b", 50, contexto=16_000)])
    with pytest.raises(rt.ContextoEstourado) as e:
        r.planejar("geral", {"messages": [{"role": "user", "content": "x" * 200_000}]})
    assert e.value.maior == 16_000 and e.value.tokens > 16_000


def test_estouro_nao_dispara_se_algum_modelo_cabe(montar):
    r = montar([item("curto", 95, contexto=8000), item("longo", 40, contexto=1_000_000)])
    ordem, _ = r.planejar("geral", {"messages": [{"role": "user", "content": "x" * 200_000}]})
    assert ordem == ["m:p/longo"]


def test_em_espera_que_nao_cabe_nao_entra_no_ultimo_recurso(montar):
    r = montar([item("curto", 95, contexto=8000)])
    r.saude.falha("m:p/curto", "p", "timeout")
    ordem, _ = r.planejar("geral", {"messages": [{"role": "user", "content": "x" * 200_000}]})
    assert ordem == []  # só em espera e sem caber: nada a tentar, e não é estouro (não é o contexto que barra)


def test_sem_estouro_quando_quem_nao_cabe_ja_estava_fora_por_outro_motivo(montar):
    r = montar([item("curto", 95, contexto=8000)])
    r.saude.modelo("m:p/curto")["tools"] = False
    ordem, _ = r.planejar("geral", {"messages": [{"role": "user", "content": "x" * 200_000}], "tools": [{"type": "function"}]})
    assert ordem == []


def test_contexto_anunciado_da_rota():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gw"))
    import catalogo

    cat = {"rotas": {"geral": {"contexto_anunciado": 131072}, "longo": {"contexto_min": 200_000}, "vazia": {"provedor": "x"}}}
    ativos = [{"nome": "m:p/a", "id": "a", "provedor": "p", "contexto": 42_000},
              {"nome": "m:p/b", "id": "b", "provedor": "p", "contexto": 1_000_000}]
    assert catalogo.contexto_anunciado(cat, "geral", ativos) == 131072
    assert catalogo.contexto_anunciado(cat, "longo", ativos) == 1_000_000  # sem config: o maior da rota
    assert catalogo.contexto_anunciado(cat, "vazia", ativos) is None
    assert catalogo.contexto_anunciado(cat, "nao-existe", ativos) is None


# --- raciocínio no histórico (05/11): a Groq recusa `reasoning_content` em mensagem de assistente
def test_tira_raciocinio_das_mensagens_do_assistente():
    msgs = [
        {"role": "user", "content": "oi"},
        {"role": "assistant", "content": "olá", "reasoning_content": "pensei", "reasoning": "x",
         "thinking_blocks": [{"t": 1}], "reasoning_details": [{"t": 2}]},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}], "reasoning_content": "y"},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    assert rt.tira_raciocinio_do_historico(msgs) == 2
    assert msgs[1] == {"role": "assistant", "content": "olá"}
    assert msgs[2] == {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}
    assert msgs[0] == {"role": "user", "content": "oi"} and msgs[3]["content"] == "ok"


def test_tira_raciocinio_aguenta_lixo():
    assert rt.tira_raciocinio_do_historico(None) == 0
    assert rt.tira_raciocinio_do_historico(["texto solto", {"role": "assistant"}]) == 0


# --- provedor com chave E url por env (Cloudflare: token + id da conta na URL)
def test_provedor_com_chave_e_url_por_env(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gw"))
    import catalogo

    p = {"prefixo": "openai", "chave": "GW_CF_KEY", "api_base_env": "GW_CF_URL"}
    monkeypatch.delenv("GW_CF_KEY", raising=False)
    monkeypatch.setenv("GW_CF_URL", "https://cf.exemplo/v1")
    assert not catalogo.provedor_ativo(p)            # sem chave: fora
    monkeypatch.setenv("GW_CF_KEY", "k")
    monkeypatch.delenv("GW_CF_URL")
    assert not catalogo.provedor_ativo(p)            # sem url: fora
    monkeypatch.setenv("GW_CF_URL", "https://cf.exemplo/v1")
    assert catalogo.provedor_ativo(p)
    params = catalogo.params_litellm({"id": "m", "_provedor": p})
    assert params["api_base"] == "https://cf.exemplo/v1" and params["api_key"] == "os.environ/GW_CF_KEY"
    local = {"prefixo": "openai", "chave": None, "api_base_env": "GW_LOCAL_URL"}
    monkeypatch.setenv("GW_LOCAL_URL", "http://127.0.0.1:8080/v1")
    assert catalogo.provedor_ativo(local) and catalogo.params_litellm({"id": "m", "_provedor": local})["api_key"] == "local"


# --- provedor que aceita chamada sem conta (Kilo Gateway: modelos :free sem chave, 05/11)
def test_provedor_anonimo_usa_chave_se_tiver(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gw"))
    import catalogo

    p = {"prefixo": "openai", "chave": "GW_KILO_KEY", "anonimo": True, "api_base": "https://kilo.exemplo/v1"}
    monkeypatch.delenv("GW_KILO_KEY", raising=False)
    assert catalogo.provedor_ativo(p)
    anon = catalogo.params_litellm({"id": "m", "_provedor": p})
    assert anon["api_key"] == "anonimo" and anon["extra_headers"] == {"Authorization": ""}
    monkeypatch.setenv("GW_KILO_KEY", "k")
    com_chave = catalogo.params_litellm({"id": "m", "_provedor": p})
    assert com_chave["api_key"] == "os.environ/GW_KILO_KEY" and "extra_headers" not in com_chave
