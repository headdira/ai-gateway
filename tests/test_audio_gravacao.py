"""Gravação de OGG longo: um write único de 90 s a 48 kHz derrubava o gateway (SIGSEGV no libvorbis)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Roda em subprocesso: se a gravação estourar a pilha, o teste falha em vez de matar o pytest.
SCRIPT = """
import sys
import numpy as np
import soundfile as sf
sys.path.insert(0, sys.argv[1])
from audio import _grava_em_blocos
d = (np.random.default_rng(0).standard_normal((90 * 48000, 2)) * 0.1).astype("float32")
with sf.SoundFile(sys.argv[2], "w", samplerate=48000, channels=2, format="OGG", subtype="VORBIS") as s:
    _grava_em_blocos(s, d)
i = sf.info(sys.argv[2])
print(i.format, i.subtype, i.channels, round(i.duration, 1))
"""


def test_musica_de_90s_grava_em_blocos_sem_derrubar_o_processo(tmp_path):
    alvo = tmp_path / "longa.ogg"
    r = subprocess.run([sys.executable, "-c", SCRIPT, str(RAIZ / "gw"), str(alvo)],
                       capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stderr[-500:]
    assert r.stdout.split() == ["OGG", "VORBIS", "2", "90.0"]
