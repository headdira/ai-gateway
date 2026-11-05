"""Motor ACE-Step 1.5 turbo (música CANTADA em pt/en ou instrumental), ajustado para M4 16 GB.

Do jeito padrão não cabe (medido: 21 GB em fp32; 15-18 GB em bf16). O que fez caber (03/11):
- PyTorch em bfloat16 no MPS (o ACE-Step usa float32 no Mac);
- DiT convertido para MLX TENSOR A TENSOR em bf16, soltando cada tensor do PyTorch na hora
  (a conversão original monta tudo em float32 de uma vez), e quantizado em 8 bits;
- cache do MLX limitado a 1 GB e teto do alocador MPS (PYTORCH_MPS_*_WATERMARK_RATIO no catálogo);
- VAE do MLX em float16 (ACESTEP_MLX_VAE_FP16=1).
Medido pelo painel: carrega em ~1 min; 30 s em ~55 s, 90 s em ~75 s, 240 s em ~3 min; pico ~13 GB.
Código do ACE-Step: clone do GitHub travado em ca1e85f (GW_ACESTEP_ROOT); nada do HF é executado.
"""
import gc
import glob
import os
import sys
import tempfile
from pathlib import Path

import soundfile as sf

ROOT = os.environ["GW_ACESTEP_ROOT"]
sys.path.insert(0, ROOT)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mlx.core as mx
import mlx.nn as mnn
import torch
from servidor_motor import servir

mx.set_cache_limit(1 << 30)
mx.set_memory_limit(10 << 30)

import acestep.models.mlx.dit_convert as dc
from acestep.handler import AceStepHandler
from acestep.inference import GenerationConfig, GenerationParams, generate_music
from acestep.llm_inference import LLMHandler

MAX_S = 240.0
IDIOMAS = {"pt": "Português", "en": "Inglês"}

_orig_q = AceStepHandler._validate_quantization_setup


def _bf16(self, *a, **k):
    self.dtype = torch.bfloat16
    return _orig_q(self, *a, **k)


AceStepHandler._validate_quantization_setup = _bf16


def _chave(key):
    if key.startswith("proj_in.1."):
        return key.replace("proj_in.1.", "proj_in."), "in"
    if key.startswith("proj_out.1."):
        return key.replace("proj_out.1.", "proj_out."), "out"
    return key, None


def _converte_8bit(pytorch_model, mlx_decoder):
    dec = pytorch_model.decoder
    pesos = []
    for key, t in list(dec.named_parameters()) + list(dec.named_buffers()):
        if "rotary_emb" in key:
            continue
        novo, tipo = _chave(key)
        a = mx.array(t.detach().to("cpu", torch.float32).numpy())
        if tipo == "in" and novo.endswith(".weight"):
            a = a.swapaxes(1, 2)
        if tipo == "out" and novo.endswith(".weight"):
            a = a.transpose(1, 2, 0)
        a = a.astype(mx.bfloat16)
        mx.eval(a)
        pesos.append((novo, a))
        t.data = torch.empty(0, dtype=t.dtype, device=t.device)
    gc.collect()
    torch.mps.empty_cache()
    mlx_decoder.load_weights(pesos)
    del pesos
    gc.collect()
    mnn.quantize(mlx_decoder, group_size=64, bits=8,
                 class_predicate=lambda p, m: isinstance(m, mnn.Linear) and m.weight.shape[-1] % 64 == 0)
    mx.eval(mlx_decoder.parameters())


dc.convert_and_load = _converte_8bit


class AceStep:
    def __init__(self):
        self.dit, self.lm = AceStepHandler(), LLMHandler()
        self.dit.initialize_service(project_root=ROOT, config_path="acestep-v15-turbo", device="mps",
                                    prefer_source="huggingface", use_mlx_dit=True)
        if not (self.dit.use_mlx_dit and self.dit.mlx_decoder is not None):
            raise RuntimeError("DiT em MLX não subiu: sem ele não cabe na memória")
        self.lm.initialize(checkpoint_dir=f"{ROOT}/checkpoints", lm_model_path="acestep-5Hz-lm-0.6B",
                           backend="mlx", device="mps")

    def info(self):
        return {"ok": True, "tipo": "musica", "taxa": 48000, "max_s": MAX_S, "letra": True,
                "idiomas": [{"id": k, "nome": v} for k, v in IDIOMAS.items()]}

    def musica(self, p):
        letra = (p.get("letra") or "").strip()
        instrumental = bool(p.get("instrumental")) or not letra
        idioma = p.get("idioma") if p.get("idioma") in IDIOMAS else "en"
        semente = p.get("semente")
        params = GenerationParams(
            caption=(p.get("prompt") or "").strip()[:500] or "pop song",
            lyrics="[Instrumental]" if instrumental else letra[:4000],
            instrumental=instrumental, vocal_language=idioma,
            duration=float(min(MAX_S, max(10.0, float(p.get("duracao") or 30)))),
            seed=int(semente) if semente is not None else -1,
        )
        cfg = GenerationConfig(batch_size=1, audio_format="wav", use_random_seed=semente is None)
        with tempfile.TemporaryDirectory(prefix="acestep-") as pasta:
            generate_music(self.dit, self.lm, params, cfg, save_dir=pasta)
            arqs = sorted(glob.glob(f"{pasta}/*.wav"))
            if not arqs:
                raise RuntimeError("o ACE-Step não devolveu áudio")
            audio, taxa = sf.read(arqs[-1], dtype="float32", always_2d=True)
        gc.collect()
        mx.clear_cache()
        torch.mps.empty_cache()
        return audio, taxa


if __name__ == "__main__":
    servir(AceStep())
