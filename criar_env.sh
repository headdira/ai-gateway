#!/usr/bin/env bash
# Cria o .env a partir do .env.example, gerando GW_MASTER_KEY e GW_CHAVE_MASCARA.
# Não sobrescreve um .env existente.
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then echo ".env já existe; nada feito."; exit 0; fi
umask 077
master="sk-gw-$(openssl rand -hex 24)"
mascara="$(openssl rand -hex 32)"
sed -e "s|^GW_MASTER_KEY=.*|GW_MASTER_KEY=${master}|" \
    -e "s|^GW_CHAVE_MASCARA=.*|GW_CHAVE_MASCARA=${mascara}|" .env.example > .env
chmod 600 .env
echo ".env criado. Cole as chaves dos provedores nele e rode ./subir.sh"
