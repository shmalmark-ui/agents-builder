#!/bin/bash
# Деплой статического сайта agents-builder.ru на Coolify.
# Запускать из /site/ — секреты подгружаются из ../.deploy.env (см. .deploy.env.example).
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
: "${SITE_APP_UUID:?нужно задать SITE_APP_UUID в .deploy.env}"

echo "→ Синхронизирую файлы сайта на сервер…"
rsync -avz --delete --exclude='.git' --exclude='deploy.sh' ./ "${COOLIFY_SERVER}":/opt/agents-builder/site/

echo "→ Собираю образ и пушу в локальный registry…"
ssh "${COOLIFY_SERVER}" 'cd /opt/agents-builder/site && docker build -t agents-builder:latest . && docker tag agents-builder:latest localhost:5000/agents-builder:latest && docker push localhost:5000/agents-builder:latest' | tail -3

echo "→ Триггер деплоя через Coolify…"
ssh "${COOLIFY_SERVER}" "curl -s -X POST 'http://localhost:8000/api/v1/deploy?uuid=${SITE_APP_UUID}&force=true' -H 'Authorization: Bearer ${COOLIFY_API_TOKEN}'"
echo ""

echo "→ Проверяю сайт…"
sleep 25
curl -sm 10 --resolve agents-builder.ru:443:5.42.117.7 -o /dev/null -w "Готово: HTTP %{http_code} за %{time_total}s\n" https://agents-builder.ru/
