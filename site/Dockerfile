FROM nginx:1.27-alpine

RUN apk add --no-cache curl

COPY index.html /usr/share/nginx/html/index.html
COPY favicon.svg /usr/share/nginx/html/favicon.svg
COPY robots.txt /usr/share/nginx/html/robots.txt
COPY sitemap.xml /usr/share/nginx/html/sitemap.xml
COPY og-image.png /usr/share/nginx/html/og-image.png
COPY apple-touch-icon.png /usr/share/nginx/html/apple-touch-icon.png
COPY 50d8465c49968e3da64c25401a204ea6.txt /usr/share/nginx/html/50d8465c49968e3da64c25401a204ea6.txt
COPY assets/ /usr/share/nginx/html/assets/
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS --max-time 3 http://127.0.0.1:80/ > /dev/null || exit 1
