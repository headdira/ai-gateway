"""Motor VoxCPM2 (MLX 8-bit, safetensors) — voz descrita em texto, 48 kHz.

Texto longo sem trocar de voz: o 1º trecho de cada tarefa ("sessao") sai da descrição
(instruct) e vira a referência; os trechos seguintes CLONAM essa referência. Com só a
descrição, a voz mudava a cada trecho (medido: pitch 158 → 120 → 140 Hz).
Lento: ~2,5-3× o tempo real no M4, mais quando o Mac esquenta.
"""
import os
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from servidor_motor import servir  # noqa: E402

MODELO = Path(os.environ.get("GW_MODELOS", "../../modelos")) / "voxcpm2-mlx-8bit"
VOZES = [
    {"id": "feminina-calorosa", "nome": "Feminina, calorosa", "descricao": "Mulher brasileira de uns trinta anos, voz calorosa e clara, sotaque paulista, ritmo tranquilo"},
    {"id": "masculina-grave", "nome": "Masculina, grave", "descricao": "Homem brasileiro de uns quarenta anos, voz grave e firme, sotaque paulista, ritmo pausado de locutor"},
    {"id": "narrador", "nome": "Narrador de audiolivro", "descricao": "Narrador brasileiro de audiolivro, voz média e aveludada, dicção perfeita, ritmo calmo"},
    {"id": "feminina-jovem", "nome": "Feminina, jovem", "descricao": "Moça brasileira de uns vinte anos, voz leve e animada, sotaque carioca"},
]


class VoxCPM2:
    def __init__(self):
        from mlx_audio.tts.utils import load_model

        self.m = load_model(str(MODELO))
        self.taxa = int(self.m.sample_rate)
        self.pasta = Path(tempfile.mkdtemp(prefix="voxcpm2-ref-"))
        self.refs: OrderedDict[str, Path] = OrderedDict()

    def info(self):
        return {"ok": True, "tipo": "voz", "taxa": self.taxa, "max_chars": 220, "vozes": VOZES}

    def _gera(self, texto, **kw):
        return np.concatenate([np.array(r.audio) for r in self.m.generate(texto, **kw)]).astype("float32")

    def falar(self, p):
        voz = next((v for v in VOZES if v["id"] == p.get("voz")), VOZES[0])
        chave = f"{p.get('sessao') or 'avulso'}:{voz['id']}"
        ref = self.refs.get(chave)
        if ref is None:
            audio = self._gera(p["texto"], instruct=voz["descricao"])
            ref = self.pasta / f"{len(self.refs)}.wav"
            sf.write(ref, audio, self.taxa)
            self.refs[chave] = ref
            while len(self.refs) > 20:
                _, velho = self.refs.popitem(last=False)
                velho.unlink(missing_ok=True)
            return audio, self.taxa
        return self._gera(p["texto"], ref_audio=str(ref)), self.taxa


if __name__ == "__main__":
    servir(VoxCPM2())
