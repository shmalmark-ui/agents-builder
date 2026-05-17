#!/bin/bash
# Деплой Telegram-бота для agents-builder.ru на Coolify.
# Запускать из /bot/ — секреты подгружаются из ../.deploy.env (см. .deploy.env.example).
set -e

cd "$(dirname "$0")"

if [ ! -f ../.deploy.env ]; then
  echo "✗ Нет ../.deploy.env. Скопируйте ../.deploy.env.example и заполните."
  exit 1
fi
set -a
. ../.deploy.env
set +a

: "${COOLIFY_SERVER:?нужно задать COOLIFY_SERVER в .deploy.env}"
: "${COOLIFY_API_TOKEN:?нужно задать COOLIFY_API_TOKEN в .deploy.env}"
: "${BOT_APP_UUID:?нужно задать BOT_APP_UUID в .deploy.env}"

echo "→ Синхронизирую файлы бота на сервер…"
rsync -avz --delete --exclude='.git' --exclude='deploy.sh' --exclude='.env' --exclude='__pycache__' ./ "${COOLIFY_SERVER}":/opt/agents-builder/bot/

echo "→ Собираю образ и пушу в локальный registry…"
ssh "${COOLIFY_SERVER}" 'cd /opt/agents-builder/bot && docker build -t agents-builder-bot:latest . && docker tag agents-builder-bot:latest localhost:5000/agents-builder-bot:latest && docker push localhost:5000/agents-builder-bot:latest' | tail -3

echo "→ Триггер деплоя через Coolify…"
ssh "${COOLIFY_SERVER}" "curl -s -X POST 'http://localhost:8000/api/v1/deploy?uuid=${BOT_APP_UUID}&force=true' -H 'Authorization: Bearer ${COOLIFY_API_TOKEN}'"
echo ""

echo "→ Проверяю /api/health…"
sleep 15
curl -sm 10 --resolve agents-builder.ru:443:5.42.117.7 -o /dev/null -w "Готово: HTTP %{http_code}\n" https://agents-builder.ru/api/health
