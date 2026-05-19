FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY knowledge.py .
COPY main.py .

VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import os, json, pathlib; \
    p = pathlib.Path(os.environ.get('DATA_DIR', '/data')) / 'state.json'; \
    print('ok' if not p.exists() or json.loads(p.read_text()) is not None else 'bad')" \
    || exit 1

CMD ["python", "main.py"]
