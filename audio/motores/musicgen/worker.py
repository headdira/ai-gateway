"""Motor MusicGen small (Meta, PyTorch/MPS, só model.safetensors) — música instrumental.

Gera em blocos de ~20 s: cada bloco continua os últimos 8 s do anterior (o modelo foi
treinado em trechos de 30 s). A saída do modelo traz a referência recodificada + o pedaço
novo; cortamos a referência e emendamos com crossfade curto para não estalar.
Medido no M4: 10 s de música em ~17 s.
"""
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from servidor_motor import servir  # noqa: E402

MODELO = Path(os.environ.get("GW_MODELOS", "../../modelos")) / "musicgen-small"
BLOCO_S, CAUDA_S, CROSS_S, MAX_S = 20.0, 8.0, 0.2, 240.0


class MusicGen:
    def __init__(self):
        import torch
        from transformers import AutoProcessor, MusicgenForConditionalGeneration

        self.torch = torch
        self.dev = "mps" if torch.backends.mps.is_available() else "cpu"
        self.proc = AutoProcessor.from_pretrained(str(MODELO))
        self.m = MusicgenForConditionalGeneration.from_pretrained(str(MODELO), use_safetensors=True).to(self.dev)
        self.taxa = int(self.m.config.audio_encoder.sampling_rate)
        self.fps = int(self.m.config.audio_encoder.frame_rate)

    def info(self):
        return {"ok": True, "tipo": "musica", "taxa": self.taxa, "max_s": MAX_S, "letra": False}

    def _gera(self, prompt, segundos, cauda=None):
        kw = {"text": [prompt], "padding": True, "return_tensors": "pt"}
        if cauda is not None:
            kw.update(audio=[cauda], sampling_rate=self.taxa)
        ins = self.proc(**kw).to(self.dev)
        with self.torch.no_grad():
            out = self.m.generate(**ins, do_sample=True, guidance_scale=3.0, max_new_tokens=int(segundos * self.fps))
        return out[0, 0].float().cpu().numpy()

    def musica(self, p):
        prompt = (p.get("prompt") or "").strip() or "instrumental music"
        total = min(MAX_S, max(5.0, float(p.get("duracao") or 30)))
        if p.get("semente") is not None:
            self.torch.manual_seed(int(p["semente"]))
        audio = self._gera(prompt, min(total, BLOCO_S + CAUDA_S))
        cross = int(CROSS_S * self.taxa)
        while len(audio) / self.taxa < total - 0.5:
            falta = total - len(audio) / self.taxa
            cauda = audio[-int(CAUDA_S * self.taxa):]
            saida = self._gera(prompt, min(BLOCO_S, falta) + CAUDA_S, cauda)
            n = len(cauda)
            novo = saida[n:]
            if len(novo) == 0:
                break
            rampa = np.linspace(0, 1, cross, dtype="float32")
            junta = audio[-cross:] * (1 - rampa) + saida[n - cross:n] * rampa
            audio = np.concatenate([audio[:-cross], junta, novo])
        audio = audio[: int(total * self.taxa)]
        pico = float(np.max(np.abs(audio))) or 1.0
        return (audio / max(pico, 1.0)).astype("float32"), self.taxa


if __name__ == "__main__":
    servir(MusicGen())
