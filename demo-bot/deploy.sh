#!/bin/bash
# Деплой demo-бота на сервер (webhook через Traefik path `/demo-bot`).
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
WEBHOOK_BASE_URL="${WEBHOOK_BASE_URL:-https://agents-builder.ru/demo-bot}"

# Persistent webhook secret across redeploys (avoid resetting it each time).
# If not in env, generate once and stash to .deploy.env.
if [ -z "${WEBHOOK_SECRET:-}" ]; then
  WEBHOOK_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
  echo "WEBHOOK_SECRET=\"${WEBHOOK_SECRET}\"" >> .deploy.env
  echo "→ Сгенерирован новый WEBHOOK_SECRET, дописан в .deploy.env"
fi

CONTAINER_NAME="agents-builder-demobot"
IMAGE_TAG="agents-builder-demobot:latest"
VOLUME_DIR="/opt/agents-builder/demo-bot-data"

# Traefik network — same one Coolify-proxy uses
TRAEFIK_NETWORK="coolify"
# Internal port the FastAPI listens on
INTERNAL_PORT="8000"
# Unique slug for Traefik router names
SLUG="demobot"

echo "→ Синхронизирую файлы на сервер…"
rsync -avz -e "ssh -o ServerAliveInterval=15 -o ServerAliveCountMax=20" --delete \
  --exclude='.git' --exclude='deploy.sh' --exclude='__pycache__' --exclude='.deploy.env' \
  ./ "${COOLIFY_SERVER}":/opt/agents-builder/demo-bot/

echo "→ Собираю docker-образ на сервере…"
ssh -o ServerAliveInterval=15 -o ServerAliveCountMax=20 "${COOLIFY_SERVER}" \
  "cd /opt/agents-builder/demo-bot && docker build -t ${IMAGE_TAG} . 2>&1 | tail -3"

echo "→ Останавливаю старый контейнер и запускаю новый (с Traefik labels)…"
ssh -o ServerAliveInterval=15 -o ServerAliveCountMax=20 "${COOLIFY_SERVER}" "\
  mkdir -p ${VOLUME_DIR} && \
  docker rm -f ${CONTAINER_NAME} 2>/dev/null || true && \
  docker run -d \
    --name ${CONTAINER_NAME} \
    --restart unless-stopped \
    --network ${TRAEFIK_NETWORK} \
    -v ${VOLUME_DIR}:/data \
    -e TELEGRAM_TOKEN='${TELEGRAM_TOKEN}' \
    -e LLM_API_KEY='${LLM_API_KEY}' \
    -e LLM_BASE_URL='${LLM_BASE_URL}' \
    -e LLM_MODEL='${LLM_MODEL}' \
    -e OWNER_USERNAME='${OWNER_USERNAME}' \
    -e WEBHOOK_BASE_URL='${WEBHOOK_BASE_URL}' \
    -e WEBHOOK_SECRET='${WEBHOOK_SECRET}' \
    --label 'traefik.enable=true' \
    --label 'traefik.docker.network=${TRAEFIK_NETWORK}' \
    --label 'traefik.http.routers.${SLUG}-https.rule=Host(\`agents-builder.ru\`) && PathPrefix(\`/demo-bot\`)' \
    --label 'traefik.http.routers.${SLUG}-https.entrypoints=https' \
    --label 'traefik.http.routers.${SLUG}-https.tls=true' \
    --label 'traefik.http.routers.${SLUG}-https.tls.certresolver=letsencrypt' \
    --label 'traefik.http.routers.${SLUG}-https.service=${SLUG}-svc' \
    --label 'traefik.http.routers.${SLUG}-https.middlewares=${SLUG}-strip' \
    --label 'traefik.http.routers.${SLUG}-http.rule=Host(\`agents-builder.ru\`) && PathPrefix(\`/demo-bot\`)' \
    --label 'traefik.http.routers.${SLUG}-http.entrypoints=http' \
    --label 'traefik.http.routers.${SLUG}-http.middlewares=redirect-to-https' \
    --label 'traefik.http.middlewares.${SLUG}-strip.stripprefix.prefixes=/demo-bot' \
    --label 'traefik.http.services.${SLUG}-svc.loadbalancer.server.port=${INTERNAL_PORT}' \
    ${IMAGE_TAG}"

echo "→ Проверяю что контейнер живой…"
sleep 8
ssh -o ServerAliveInterval=15 "${COOLIFY_SERVER}" \
  "docker ps --filter name=${CONTAINER_NAME} --format '{{.Status}}'"
echo ""
echo "→ Последние логи:"
ssh -o ServerAliveInterval=15 "${COOLIFY_SERVER}" \
  "docker logs --tail 20 ${CONTAINER_NAME}"

echo ""
echo "✓ Готово. Webhook: ${WEBHOOK_BASE_URL}/webhook"
echo "  Стрим логов: ssh ${COOLIFY_SERVER} 'docker logs -f ${CONTAINER_NAME}'"
