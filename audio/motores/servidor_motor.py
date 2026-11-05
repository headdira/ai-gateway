"""Servidor HTTP mínimo dos motores de áudio (protocolo do gw/audio.py).

Um pedido por vez (HTTPServer sem threads): modelo MLX não é thread-safe e o Mac
agradece. Cada motor implementa info() e falar()/musica() e chama servir().
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import soundfile as sf


def wav(audio: np.ndarray, taxa: int) -> bytes:
    buf = io.BytesIO()
    sf.write(buf, np.asarray(audio, dtype="float32"), taxa, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def servir(motor) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, required=True)
    porta = ap.parse_args().porta

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _resp(self, code: int, corpo: bytes, tipo: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def do_GET(self):
            if self.path == "/health":
                self._resp(200, json.dumps(motor.info(), ensure_ascii=False).encode(), "application/json")
            else:
                self._resp(404, b"{}", "application/json")

        def do_POST(self):
            try:
                pedido = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/voz" and hasattr(motor, "falar"):
                    audio, taxa = motor.falar(pedido)
                elif self.path == "/musica" and hasattr(motor, "musica"):
                    audio, taxa = motor.musica(pedido)
                else:
                    return self._resp(404, b'{"erro": "rota"}', "application/json")
                self._resp(200, wav(audio, taxa), "audio/wav")
            except Exception as e:  # noqa: BLE001 - erro volta para o gateway como texto
                traceback.print_exc(file=sys.stderr)
                self._resp(500, json.dumps({"erro": f"{type(e).__name__}: {e}"[:400]}).encode(), "application/json")

    print(f"motor no ar na porta {porta}", flush=True)
    HTTPServer(("127.0.0.1", porta), H).serve_forever()
