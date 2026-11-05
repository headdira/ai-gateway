"""Motor de áudio FALSO para testes: responde o protocolo do gateway com um tom.
Duração proporcional ao tamanho do texto (0,05 s por caractere). Só biblioteca padrão."""
import argparse, io, json, math, struct, wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TAXA = 24000


def tom(segundos: float, freq: float = 440.0) -> bytes:
    n = int(TAXA * segundos)
    quadros = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / TAXA))) for i in range(n))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(TAXA); w.writeframes(quadros)
    return buf.getvalue()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            self._json({"ok": True, "tipo": "voz", "taxa": TAXA, "max_chars": 60, "vozes": ["teste_a", "teste_b"]})
        else:
            self._json({"erro": "rota"}, 404)

    def do_POST(self):
        corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        if self.path == "/voz":
            dados = tom(0.05 * len(corpo.get("texto", "")))
        elif self.path == "/musica":
            dados = tom(float(corpo.get("duracao", 2)), 220)
        else:
            return self._json({"erro": "rota"}, 404)
        self.send_response(200); self.send_header("Content-Type", "audio/wav"); self.send_header("Content-Length", str(len(dados))); self.end_headers(); self.wfile.write(dados)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--porta", type=int, required=True); a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.porta), H).serve_forever()
