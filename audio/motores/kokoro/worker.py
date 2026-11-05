"""Motor Kokoro-82M (MLX, safetensors) — vozes pt-BR. Rápido: 100 s de áudio em ~12 s no M4 (medido 03/11)."""
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from servidor_motor import servir  # noqa: E402

MODELO = Path(os.environ.get("GW_MODELOS", "../../modelos")) / "kokoro-mlx"
VOZES = [
    {"id": "pf_dora", "nome": "Dora (feminina)"},
    {"id": "pm_alex", "nome": "Alex (masculina)"},
    {"id": "pm_santa", "nome": "Santa (masculina, grave)"},
]


class Kokoro:
    def __init__(self):
        from mlx_audio.tts.utils import load_model

        self.m = load_model(str(MODELO))
        self.taxa = int(getattr(self.m, "sample_rate", 24000))

    def info(self):
        return {"ok": True, "tipo": "voz", "taxa": self.taxa, "max_chars": 300, "vozes": VOZES}

    def falar(self, p):
        voz = p.get("voz") if p.get("voz") in {v["id"] for v in VOZES} else "pf_dora"
        # Voz sempre pelo arquivo local: o padrão do mlx-audio buscaria outro repo.
        partes = [np.array(r.audio) for r in self.m.generate(
            text=p["texto"], voice=str(MODELO / "voices" / f"{voz}.safetensors"), lang_code="p",
            speed=float(p.get("velocidade") or 1.0))]
        return np.concatenate(partes).astype("float32"), self.taxa


if __name__ == "__main__":
    servir(Kokoro())
