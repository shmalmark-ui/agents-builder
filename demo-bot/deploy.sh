#!/bin/bash
# Деплой demo-бота на сервер.
# Запускать из /demo-bot/ — секреты в ./.deploy.env (см. .deploy.env.example).
set -e

cd "$(dirname "$0")"

if [ ! -f ./.deploy.env ]; then
  echo "✗ Нет ./.deploy.env. Скопируйте .deploy.env.example и заполните."
  exit 1
fi
set -a
. ./.deploy.env
set +a

: "${TELEGRAM_TOKEN:?нужно задать TELEGRAM_TOKEN в .deploy.env}"
: "${LLM_API_KEY:?нужно задать LLM_API_KEY в .deploy.env}"
: "${OWNER_USERNAME:?нужно задать OWNER_USERNAME в .deploy.env}"
: "${COOLIFY_SERVER:?нужно задать COOLIFY_SERVER в .deploy.env}"

LLM_BASE_URL="${LLM_BASE_URL:-https://api.vsegpt.ru/v1}"
LLM_MODEL="${LLM_MODEL:-anthropic/claude-sonnet-4.6}"

CONTAINER_NAME="agents-builder-demobot"
IMAGE_TAG="agents-builder-demobot:latest"
VOLUME_DIR="/opt/agents-builder/demo-bot-data"

echo "→ Синхронизирую файлы на сервер…"
rsync -avz -e "ssh -o ServerAliveInterval=15 -o ServerAliveCountMax=20" --delete \
  --exclude='.git' --exclude='deploy.sh' --exclude='__pycache__' --exclude='.deploy.env' \
  ./ "${COOLIFY_SERVER}":/opt/agents-builder/demo-bot/

echo "→ Собираю docker-образ на сервере…"
ssh -o ServerAliveInterval=15 -o ServerAliveCountMax=20 "${COOLIFY_SERVER}" \
  "cd /opt/agents-builder/demo-bot && docker build -t ${IMAGE_TAG} . 2>&1 | tail -3"

echo "→ Останавливаю старый контейнер и запускаю новый…"
ssh -o ServerAliveInterval=15 -o ServerAliveCountMax=20 "${COOLIFY_SERVER}" "\
  mkdir -p ${VOLUME_DIR} && \
  docker rm -f ${CONTAINER_NAME} 2>/dev/null || true && \
  docker run -d \
    --name ${CONTAINER_NAME} \
    --restart unless-stopped \
    -v ${VOLUME_DIR}:/data \
    -e TELEGRAM_TOKEN='${TELEGRAM_TOKEN}' \
    -e LLM_API_KEY='${LLM_API_KEY}' \
    -e LLM_BASE_URL='${LLM_BASE_URL}' \
    -e LLM_MODEL='${LLM_MODEL}' \
    -e OWNER_USERNAME='${OWNER_USERNAME}' \
    ${IMAGE_TAG}"

echo "→ Проверяю что контейнер живой…"
sleep 5
ssh -o ServerAliveInterval=15 "${COOLIFY_SERVER}" \
  "docker ps --filter name=${CONTAINER_NAME} --format '{{.Status}}'"
echo ""
echo "→ Последние логи:"
ssh -o ServerAliveInterval=15 "${COOLIFY_SERVER}" \
  "docker logs --tail 15 ${CONTAINER_NAME}"

echo ""
echo "✓ Готово. Бот должен начать отвечать в Telegram."
echo "  Стрим логов: ssh ${COOLIFY_SERVER} 'docker logs -f ${CONTAINER_NAME}'"
