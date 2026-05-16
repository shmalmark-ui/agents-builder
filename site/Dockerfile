FROM nginx:1.27-alpine

RUN apk add --no-cache curl

COPY index.html /usr/share/nginx/html/index.html
COPY assets/ /usr/share/nginx/html/assets/
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS --max-time 3 http://127.0.0.1:80/ > /dev/null || exit 1
