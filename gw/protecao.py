"""Callback do LiteLLM: proteção de dados + observabilidade.

Registrado como callback comum (``litellm_settings.callbacks``), e não como
guardrail, de propósito: guardrail do LiteLLM pode ser desligado pela própria
requisição (``disable_global_guardrail`` / opt-out no metadata). Callback não
tem desligamento por requisição.

- ``async_pre_call_hook``: mascara a requisição ANTES do roteamento, então
  vale para qualquer provedor, retry e fallback.
- ``async_post_call_success_hook`` / ``async_post_call_streaming_iterator_hook``:
  devolve os valores reais na resposta (texto e argumentos de tool call).
- ``async_log_success_event`` / ``async_log_failure_event``: uma linha JSONL por
  chamada em ``logs/requisicoes.jsonl``, sem o conteúdo das mensagens.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time

import dados_sensiveis as ds
import roteador as rt
from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("gw.protecao")

AVISO_MARCADORES = (
    "Aviso do gateway: trechos no formato [TIPO_xxxxxxxx] (por exemplo [CPF_1a2b3c4d] ou "
    "[PESSOA_9f8e7d6c]) são dados reais protegidos. Trate cada um como o valor verdadeiro e, "
    "quando precisar usá-lo numa resposta ou num argumento de ferramenta, copie o marcador "
    "exatamente como está, com os colchetes. Não tente adivinhar o valor."
)
CAMPOS_REQUISICAO = ("messages", "input", "prompt", "system", "instructions", "contents")
CHAMADAS_CHAT = {"completion", "acompletion"}
RAIZ = Path(__file__).resolve().parent.parent


TIPOS_ANEXO = {
    "image_url": "imagem", "input_image": "imagem", "image": "imagem",
    "file": "arquivo", "input_file": "arquivo", "document": "arquivo",
    "input_audio": "áudio", "audio": "áudio",
}


def _anexos(obj: Any, profundidade: int = 0) -> set[str]:
    """Tipos de anexo (imagem, arquivo, áudio) presentes no conteúdo da requisição."""
    if profundidade > 8 or obj is None:
        return set()
    achados: set[str] = set()
    if isinstance(obj, list):
        for x in obj:
            achados |= _anexos(x, profundidade + 1)
    elif isinstance(obj, dict):
        tipo = obj.get("type")
        if isinstance(tipo, str) and tipo in TIPOS_ANEXO:
            achados.add(TIPOS_ANEXO[tipo])
        for k, v in obj.items():
            if k in ("content", "input", "messages", "parts"):
                achados |= _anexos(v, profundidade + 1)
    return achados


class Protecao(CustomLogger):
    # Faz o proxy rodar este hook mesmo quando executa "só guardrails".
    enforces_request_content = True

    def __init__(self) -> None:
        super().__init__()
        chave = os.environ.get("GW_CHAVE_MASCARA", "")
        if len(chave) < 32:
            # Falha fechada: sem chave não sobe, em vez de subir sem proteção.
            raise RuntimeError("GW_CHAVE_MASCARA ausente ou curta (mín. 32 caracteres); rode ./criar_env.sh")
        self.cofre = ds.Cofre(chave.encode())
        self.bloquear = {t.strip().upper() for t in os.environ.get("GW_BLOQUEAR", "").split(",") if t.strip()}
        # Imagem/arquivo não dá para inspecionar aqui (sem OCR/visão local): por
        # padrão é barrado. GW_IMAGENS=permitir libera, por sua conta.
        self.imagens_permitidas = os.environ.get("GW_IMAGENS", "bloquear").strip().lower() == "permitir"
        pasta = Path(os.environ.get("GW_LOG_DIR", RAIZ / "logs"))
        pasta.mkdir(parents=True, exist_ok=True)
        self.arq_log = pasta / "requisicoes.jsonl"
        self.arq_rot = pasta / "roteamento.jsonl"
        self._lock_log = threading.Lock()
        try:
            self.roteador: rt.Roteador | None = rt.Roteador.do_disco(RAIZ)
        except Exception:
            log.exception("roteador desligado: catálogo/qualidade não carregou")
            self.roteador = None
        # Carrega NER e secret-gate já na subida: a 1ª requisição não paga o custo.
        ds._carrega_scan()
        ds._carrega_nlp()

    # ------------------------------------------------------------------ ida
    async def async_pre_call_hook(self, user_api_key_dict, cache, data: dict, call_type, **_: Any):
        if not self.imagens_permitidas:
            tipos = sorted({t for c in CAMPOS_REQUISICAO for t in _anexos(data.get(c))})
            if tipos:
                self._grava({"status": "bloqueado", "tipos": tipos, "rota": data.get("model")})
                from fastapi import HTTPException

                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Requisição barrada pelo gateway: tem {', '.join(tipos)}, que não dá para "
                        "inspecionar em busca de dado sensível. Para liberar: GW_IMAGENS=permitir no .env."
                    ),
                )
        contagem: Counter = Counter()
        for campo in CAMPOS_REQUISICAO:
            if data.get(campo) is not None:
                data[campo] = ds.mascarar_estrutura(data[campo], self.cofre, contagem)

        bloqueados = sorted(t for t in contagem if t in self.bloquear)
        if bloqueados:
            self._grava({"status": "bloqueado", "tipos": bloqueados, "rota": data.get("model")})
            from fastapi import HTTPException

            raise HTTPException(
                status_code=400,
                detail=f"Requisição barrada pelo gateway: contém {', '.join(bloqueados)} (política GW_BLOQUEAR).",
            )

        if contagem and call_type in CHAMADAS_CHAT and isinstance(data.get("messages"), list):
            self._injeta_aviso(data["messages"])
        rt.tira_raciocinio_do_historico(data.get("messages"))

        meta_chave = "litellm_metadata" if isinstance(data.get("litellm_metadata"), dict) else "metadata"
        meta = data.get(meta_chave)
        if not isinstance(meta, dict):
            meta = {}
            data[meta_chave] = meta
        meta["gw_mascarados"] = dict(contagem)
        meta["gw_plano"] = self._roteia(data)
        # data foi alterado no lugar

    def _roteia(self, data: dict) -> dict | None:
        """Rota pedida → ordem de modelos deste pedido (data['model'] + data['fallbacks'])."""
        pedido = data.get("model")
        if self.roteador is None or not isinstance(pedido, str):
            return None
        if pedido in self.roteador.itens:  # modelo explícito (ex.: sondagem): sem escolha
            return {"rota": pedido, "ordem": [pedido], "t0": time.time(), "ranking": [], "tools": bool(data.get("tools"))}
        if pedido not in self.roteador.rotas():
            return None
        try:
            ordem, avaliacao = self.roteador.planejar(pedido, data)
        except rt.ContextoEstourado as e:
            # Texto e código no padrão OpenAI: o Hermes reconhece "context length exceeded",
            # resume a conversa e manda de novo, em vez de tratar como erro de provedor.
            from fastapi import HTTPException

            self._grava({"status": "bloqueado", "tipos": ["contexto_estourado"], "rota": pedido,
                         "tokens_estimados": e.tokens, "maior_contexto": e.maior})
            raise HTTPException(
                status_code=400,
                detail={"message": (
                    f"context length exceeded: o pedido tem ~{e.tokens} tokens e o maior contexto disponível "
                    f"agora na rota '{pedido}' é {e.maior} tokens. Reduce the length of the messages."
                ), "code": "context_length_exceeded", "type": "invalid_request_error"},
            ) from e
        if not ordem:
            if rt.necessidades(data)["tools"]:
                # Sem plano, o LiteLLM escolheria qualquer modelo do grupo da rota,
                # inclusive quem não chama ferramenta. Melhor recusar com o motivo.
                from fastapi import HTTPException

                self._grava({"status": "bloqueado", "tipos": ["sem_modelo_com_tool"], "rota": pedido})
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"Gateway: nenhum modelo da rota '{pedido}' com tool call provada está disponível agora. "
                        "Rode `testar_provedores.py --sondar` para provar modelos ou espere sair da espera (painel)."
                    ),
                )
            return None
        data["model"] = ordem[0]
        data["fallbacks"] = ordem[1:]
        resumo = [
            {k: a[k] for k in ("nome", "nota", "velocidade", "confiabilidade", "pontuacao", "fora")}
            for a in avaliacao[:8]
        ]
        return {"rota": pedido, "ordem": ordem, "t0": time.time(), "ranking": resumo, "tools": bool(data.get("tools"))}

    @staticmethod
    def _plano(data: dict | None) -> dict | None:
        for chave in ("metadata", "litellm_metadata"):
            meta = (data or {}).get(chave)
            if isinstance(meta, dict) and isinstance(meta.get("gw_plano"), dict):
                return meta["gw_plano"]
        return None

    def _registra_resultado(self, data: dict | None, atendeu: str | None, lat_ms: float | None,
                            usou_tools: bool, excecao: BaseException | None = None,
                            uso: dict | None = None, ttft_ms: float | None = None,
                            texto: str | None = None) -> None:
        """Atualiza a saúde: quem veio antes de quem atendeu falhou; quem atendeu, sucesso."""
        plano = self._plano(data)
        if plano is None or self.roteador is None:
            return
        r = self.roteador
        ordem = plano["ordem"]
        if lat_ms is None:
            lat_ms = (time.time() - plano["t0"]) * 1000
        tool_falsa = bool(plano.get("tools")) and not usou_tools and rt.tool_call_em_texto(texto)
        if excecao is None and atendeu:
            for nome in ordem:
                if nome == atendeu:
                    break
                r.saude.falha(nome, r.provedor_de(nome), "caiu")
            if tool_falsa:
                # Respondeu "certo" para o HTTP, mas a chamada de ferramenta veio como texto:
                # para o agente é falha. Sai da fila de pedidos com tools até provar de novo.
                r.saude.falha(atendeu, r.provedor_de(atendeu), "erro", tools_falhou=True)
                status = "tool_em_texto"
            else:
                r.saude.sucesso(atendeu, r.provedor_de(atendeu), lat_ms, tools_ok=True if usou_tools else None)
                status = "ok"
        else:
            motivo = rt.motivo_da_excecao(excecao)
            sem_tools = plano.get("tools") and rt.erro_de_modelo_sem_tool(excecao)
            for i, nome in enumerate(ordem):
                r.saude.falha(nome, r.provedor_de(nome), motivo if i == len(ordem) - 1 else "caiu",
                              tools_falhou=bool(sem_tools))
            status = "erro"
        r.saude.salvar(forcar=True)
        meta = (data or {}).get("metadata") or {}
        registro = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "status": status, "rota": plano["rota"], "ordem": ordem,
            "escolhido": ordem[0], "atendeu": atendeu, "latencia_ms": round(lat_ms),
            "tentativas": (ordem.index(atendeu) + 1) if atendeu in ordem else len(ordem),
            "mascarados": meta.get("gw_mascarados") or {}, "ranking": plano.get("ranking"),
            "erro": (f"{type(excecao).__name__}: {excecao}"[:300] if excecao else None),
            "tokens_entrada": (uso or {}).get("prompt_tokens"),
            "tokens_saida": (uso or {}).get("completion_tokens"),
            "ttft_ms": round(ttft_ms) if ttft_ms else None,
            "agente": (data or {}).get("user") or self._agente_do_header(data),
            "stream": bool((data or {}).get("stream")),
            "tools": bool(plano.get("tools")),
        }
        linha = json.dumps(registro, ensure_ascii=False, default=str)
        with self._lock_log, self.arq_rot.open("a", encoding="utf-8") as f:
            f.write(linha + "\n")

    @staticmethod
    def _agente_do_header(data: dict | None) -> str | None:
        """Harness que não manda "user" (Hermes) se identifica pelo header X-GW-Agente."""
        psr = (data or {}).get("proxy_server_request") or {}
        cab = psr.get("headers") or {}
        for k, v in cab.items():
            if str(k).lower() == "x-gw-agente":
                return str(v)[:40]
        return None

    async def async_post_call_failure_hook(self, request_data: dict, original_exception: Exception,
                                           user_api_key_dict, traceback_str: str | None = None):
        try:
            self._registra_resultado(request_data, None, None, False, excecao=original_exception)
        except Exception:
            log.exception("falha ao registrar erro no roteador")

    @staticmethod
    def _injeta_aviso(mensagens: list) -> None:
        primeira = mensagens[0] if mensagens else None
        if isinstance(primeira, dict) and primeira.get("role") in ("system", "developer"):
            conteudo = primeira.get("content")
            if isinstance(conteudo, str):
                if AVISO_MARCADORES not in conteudo:
                    primeira["content"] = f"{conteudo}\n\n{AVISO_MARCADORES}"
                return
            if isinstance(conteudo, list):
                if not any(isinstance(p, dict) and p.get("text") == AVISO_MARCADORES for p in conteudo):
                    conteudo.append({"type": "text", "text": AVISO_MARCADORES})
                return
        mensagens.insert(0, {"role": "system", "content": AVISO_MARCADORES})

    # ---------------------------------------------------------------- volta
    async def async_post_call_success_hook(self, data: dict, user_api_key_dict, response: Any):
        self._desmascara_objeto(response)
        try:
            hp = getattr(response, "_hidden_params", None) or {}
            usou_tools = any(
                getattr(getattr(c, "message", None), "tool_calls", None)
                for c in (getattr(response, "choices", None) or [])
            )
            u = getattr(response, "usage", None)
            uso = {"prompt_tokens": getattr(u, "prompt_tokens", None), "completion_tokens": getattr(u, "completion_tokens", None)} if u else None
            texto = "".join(
                c for c in (getattr(getattr(ch, "message", None), "content", None)
                            for ch in (getattr(response, "choices", None) or []))
                if isinstance(c, str)
            )
            self._registra_resultado(data, hp.get("model_id"), hp.get("_response_ms"), bool(usou_tools), uso=uso,
                                     texto=texto)
        except Exception:
            log.exception("falha ao registrar sucesso no roteador")
        return response

    def _desmascara_objeto(self, obj: Any, profundidade: int = 0, json_str: bool = False) -> Any:
        """Troca marcadores por valores reais em qualquer resposta (ModelResponse, Responses, dict…).
        Em `arguments` (JSON em string) o valor real entra escapado para o JSON continuar válido."""
        if profundidade > 14 or obj is None:
            return obj
        if isinstance(obj, str):
            return ds.desmascarar_texto(obj, self.cofre, json_escape=json_str)
        if isinstance(obj, list):
            for i, v in enumerate(obj):
                obj[i] = self._desmascara_objeto(v, profundidade + 1)
            return obj
        if isinstance(obj, dict):
            for k, v in list(obj.items()):
                obj[k] = self._desmascara_objeto(v, profundidade + 1, json_str=(k == "arguments"))
            return obj
        # Objetos do LiteLLM/pydantic: alguns (ex.: ChatCompletionMessageToolCall)
        # guardam os campos fora do __dict__, então junta as três fontes de nome.
        nomes: set[str] = set(getattr(obj, "__dict__", None) or {})
        nomes.update(getattr(type(obj), "model_fields", None) or {})
        nomes.update(getattr(obj, "__pydantic_extra__", None) or {})
        for k in nomes:
            if k.startswith("_"):
                continue
            try:
                v = getattr(obj, k)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(v, (str, list, dict)) and not hasattr(v, "__dict__"):
                continue
            novo = self._desmascara_objeto(v, profundidade + 1, json_str=(k == "arguments"))
            if isinstance(v, str) and novo != v:
                try:
                    setattr(obj, k, novo)
                except Exception:  # noqa: BLE001 - objeto imutável: segue
                    pass
        return obj

    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict, response, request_data: dict):
        desmascaradores: dict[tuple, ds.Desmascarador] = {}
        ultimo_alvo: dict[tuple, tuple[Any, str]] = {}
        segurado = None
        atendeu: str | None = None
        usou_tools = False
        uso: dict | None = None
        t0 = time.time()
        ttft: float | None = None
        texto: list[str] = []
        tam_texto = 0
        try:
            async for pedaco in response:
                if atendeu is None:
                    atendeu = (getattr(pedaco, "_hidden_params", None) or {}).get("model_id")
                u = getattr(pedaco, "usage", None)
                if u is not None and getattr(u, "prompt_tokens", None):
                    uso = {"prompt_tokens": u.prompt_tokens, "completion_tokens": getattr(u, "completion_tokens", None)}
                for c in (getattr(pedaco, "choices", None) or []):
                    conteudo = getattr(getattr(c, "delta", None), "content", None)
                    if isinstance(conteudo, str) and conteudo:
                        if ttft is None:
                            ttft = (time.time() - t0) * 1000
                        if tam_texto < 200_000:  # só para detectar tool call em texto
                            texto.append(conteudo)
                            tam_texto += len(conteudo)
                vistos = self._desmascara_pedaco(pedaco, desmascaradores, ultimo_alvo)
                usou_tools = usou_tools or any(k[1] == "tool" for k in vistos)
                # Campo que parou de vir: o que ficou pendente vai para o pedaço segurado.
                for chave in [k for k in desmascaradores if k not in vistos]:
                    self._descarrega(chave, desmascaradores, ultimo_alvo)
                if segurado is not None:
                    yield segurado
                segurado = pedaco
        except Exception as e:
            self._registra_resultado(request_data, None, None, False, excecao=e)
            raise
        for chave in list(desmascaradores):
            self._descarrega(chave, desmascaradores, ultimo_alvo)
        if segurado is not None:
            yield segurado
        try:
            self._registra_resultado(request_data, atendeu, None, usou_tools, uso=uso, ttft_ms=ttft,
                                     texto="".join(texto))
        except Exception:
            log.exception("falha ao registrar stream no roteador")

    def _desmascara_pedaco(self, pedaco: Any, desm: dict, alvo: dict) -> set:
        vistos: set = set()
        for escolha in getattr(pedaco, "choices", None) or []:
            delta = getattr(escolha, "delta", None)
            if delta is None:
                continue
            idx = getattr(escolha, "index", 0)
            for campo in ("content", "reasoning_content"):
                valor = getattr(delta, campo, None)
                if isinstance(valor, str) and valor:
                    chave = (idx, campo)
                    d = desm.setdefault(chave, ds.Desmascarador(self.cofre))
                    setattr(delta, campo, d.alimentar(valor))
                    alvo[chave] = (delta, campo)
                    vistos.add(chave)
            for tc in getattr(delta, "tool_calls", None) or []:
                fn = getattr(tc, "function", None)
                args = getattr(fn, "arguments", None) if fn is not None else None
                if isinstance(args, str) and args:
                    chave = (idx, "tool", getattr(tc, "index", 0))
                    d = desm.setdefault(chave, ds.Desmascarador(self.cofre, json_escape=True))
                    fn.arguments = d.alimentar(args)
                    alvo[chave] = (fn, "arguments")
                    vistos.add(chave)
        return vistos

    @staticmethod
    def _descarrega(chave: tuple, desm: dict, alvo: dict) -> None:
        d = desm.get(chave)
        if d is None or not d.pendente or chave not in alvo:
            return
        obj, campo = alvo[chave]
        setattr(obj, campo, (getattr(obj, campo, None) or "") + d.finalizar())

    # -------------------------------------------------------- observabilidade
    def _grava(self, registro: dict) -> None:
        registro.setdefault("ts", datetime.now(UTC).isoformat(timespec="milliseconds"))
        linha = json.dumps(registro, ensure_ascii=False, default=str)
        with self._lock_log, self.arq_log.open("a", encoding="utf-8") as f:
            f.write(linha + "\n")

    @staticmethod
    def _segundos(v: Any) -> float | None:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return v.timestamp()
        except Exception:  # noqa: BLE001
            return None

    def _registro_base(self, kwargs: dict, inicio: Any, fim: Any) -> dict:
        slo = kwargs.get("standard_logging_object") or {}
        lp = kwargs.get("litellm_params") or {}
        meta = lp.get("metadata") or lp.get("litellm_metadata") or {}
        meta_slo = slo.get("metadata") or {}
        t0, t1 = self._segundos(inicio), self._segundos(fim)
        primeiro_token = self._segundos(slo.get("completion_start_time"))
        return {
            "rota": slo.get("model_group") or meta.get("model_group") or kwargs.get("model"),
            "modelo": slo.get("model") or kwargs.get("model"),
            "provedor": slo.get("custom_llm_provider") or lp.get("custom_llm_provider"),
            "api_base": slo.get("api_base") or lp.get("api_base"),
            "stream": bool(slo.get("stream") if slo else kwargs.get("stream")),
            "latencia_ms": round((t1 - t0) * 1000) if t0 and t1 else None,
            "ttft_ms": round((primeiro_token - t0) * 1000) if t0 and primeiro_token and primeiro_token > t0 else None,
            "tokens_entrada": slo.get("prompt_tokens"),
            "tokens_saida": slo.get("completion_tokens"),
            "mascarados": meta.get("gw_mascarados") or {},
            "chave": meta_slo.get("user_api_key_alias") or meta.get("user_api_key_alias"),
            "agente": kwargs.get("user") or (kwargs.get("optional_params") or {}).get("user"),
        }

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            self._grava({"status": "ok", **self._registro_base(kwargs, start_time, end_time)})
        except Exception:
            log.exception("falha ao gravar log de sucesso")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        try:
            excecao = kwargs.get("exception")
            slo = kwargs.get("standard_logging_object") or {}
            erro = slo.get("error_str") or (f"{type(excecao).__name__}: {excecao}" if excecao else None)
            self._grava({
                "status": "erro",
                "http": getattr(excecao, "status_code", None),
                "erro": (erro or "")[:400],
                **self._registro_base(kwargs, start_time, end_time),
            })
        except Exception:
            log.exception("falha ao gravar log de erro")


instancia = Protecao()
