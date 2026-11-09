FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8080 \
    TZ=Europe/Stockholm \
    PUID=99 \
    PGID=100

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app/ /app/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'), timeout=4)" || exit 1

LABEL org.opencontainers.image.title="BraXhn Verkstad" \
      org.opencontainers.image.description="Arbetsordrar, bokningar och lager för cykel- och elektronikverkstad"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
