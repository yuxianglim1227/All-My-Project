# NOTE: written but not built in the session that created it - build and run `socialagent selfcheck` first.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fontconfig fonts-noto-cjk fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY config ./config

RUN useradd --create-home agent && mkdir -p /app/data && chown -R agent /app
USER agent

ENV DATA_DIR=/app/data BRAND_FILE=/app/config/brand.yaml HTTP_HOST=0.0.0.0 HTTP_PORT=8080
VOLUME ["/app/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('HTTP_PORT','8080')+'/healthz',timeout=4)"
CMD ["socialagent", "run"]
