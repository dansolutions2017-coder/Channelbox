FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY authserver ./authserver
COPY manage.py .

RUN useradd --system --uid 10001 auth && mkdir -p /data /config && chown auth /data /config
USER auth

ENV AUTH_DB=/data/auth.db \
    SERVERS_FILE=/config/servers.json \
    APP_CONFIG_FILE=/config/app_config.json \
    PORT=8000

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4)"
CMD ["python", "-m", "authserver"]
