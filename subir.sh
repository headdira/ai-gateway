#!/usr/bin/env bash
# Sobe o gateway em 127.0.0.1 (só a sua máquina acessa). Não sobe modelo local.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "Falta o .env: rode ./criar_env.sh"; exit 1; }
set -a; . ./.env; set +a
PORTA="${GW_PORTA:-4000}"
mkdir -p logs
if [ -f logs/proxy.pid ] && kill -0 "$(cat logs/proxy.pid)" 2>/dev/null; then
  echo "Já está no ar (PID $(cat logs/proxy.pid)) em http://127.0.0.1:${PORTA}/v1"; exit 0
fi
# Gateway fora do ar mas com motor/harness vivo = sobra de um crash (atexit não roda em SIGSEGV).
# Derruba pelo PID gravado, senão o próximo pedido sobe um 2º modelo pesado ao lado do órfão.
for arq in logs/harness-*.pid logs/audio-*.pid; do
  [ -f "$arq" ] || continue
  hp="$(cat "$arq")"
  if kill -0 "$hp" 2>/dev/null; then kill -TERM -- "-$hp" 2>/dev/null || kill "$hp"; echo "Órfão parado: $(basename "$arq" .pid) (PID $hp)."; fi
  rm -f "$arq"
done
if lsof -nP -iTCP:"$PORTA" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Porta $PORTA ocupada por outro processo. Defina GW_PORTA no .env."; exit 1
fi
.venv/bin/python gerar_config.py
# Ambiente LIMPO (env -i): só PATH/HOME/LANG + o .env. Chave de provedor que
# esteja exportada no seu shell (OPENAI_API_KEY, OPENROUTER_API_KEY…) não entra
# no processo. As chaves vão por arquivo, nunca pela linha de comando (ps).
nohup env -i PATH="/usr/bin:/bin:/usr/sbin:/sbin" HOME="$HOME" LANG="${LANG:-pt_BR.UTF-8}" \
  TMPDIR="${TMPDIR:-/tmp}" GW_PORTA_EFETIVA="$PORTA" \
  bash -c 'set -a; . ./.env; set +a; exec .venv/bin/python gw/servidor.py --config config.gerado.yaml --host 127.0.0.1 --port "$GW_PORTA_EFETIVA"' \
  >> logs/proxy.log 2>&1 &
echo $! > logs/proxy.pid
for _ in $(seq 1 90); do
  if curl -sf "http://127.0.0.1:${PORTA}/health/liveliness" >/dev/null 2>&1; then
    echo "No ar: http://127.0.0.1:${PORTA}/v1  (PID $(cat logs/proxy.pid))"; exit 0
  fi
  kill -0 "$(cat logs/proxy.pid)" 2>/dev/null || { echo "Caiu na subida; veja logs/proxy.log"; exit 1; }
  sleep 1
done
echo "Não respondeu em 90 s; veja logs/proxy.log"; exit 1
