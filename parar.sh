#!/usr/bin/env bash
# Para SÓ o que o subir.sh e o chat iniciaram (pelos PIDs gravados), nunca por nome.
set -euo pipefail
cd "$(dirname "$0")"
# Harness do chat (Hermes) sobe em sessão própria: derruba o grupo de cada um.
for arq in logs/harness-*.pid logs/audio-*.pid; do
  [ -f "$arq" ] || continue
  hp="$(cat "$arq")"
  if kill -0 "$hp" 2>/dev/null; then kill -TERM -- "-$hp" 2>/dev/null || kill "$hp"; echo "Parado: $(basename "$arq" .pid) (PID $hp)."; fi
  rm -f "$arq"
done
[ -f logs/proxy.pid ] || { echo "Gateway não está no ar (sem logs/proxy.pid)."; exit 0; }
pid="$(cat logs/proxy.pid)"
if kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
  echo "Gateway parado (PID $pid)."
else
  echo "PID $pid já não existia."
fi
rm -f logs/proxy.pid
