# Бот в контейнере: docker compose up -d --build  (настройки — в файле .env)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY run.py .
COPY finbot ./finbot
RUN python -m compileall -q finbot run.py \
    && useradd --system --uid 10001 --home-dir /data finbot \
    && mkdir -p /data && chown finbot:finbot /data

USER finbot
VOLUME ["/data"]
CMD ["python", "run.py"]
