# Studio Monitor Hub — FastAPI + SQLAlchemy (PostgreSQL via docker-compose)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# only what the hub needs: the shared contract/telegram modules of the agent package and the hub package
COPY pyproject.toml README.md ./
COPY src/studio_monitor ./src/studio_monitor
COPY src/hub ./src/hub

RUN pip install --upgrade pip \
 && pip install "fastapi>=0.115" "uvicorn[standard]>=0.30" "sqlalchemy>=2.0" "psycopg[binary]>=3.2" "jinja2>=3.1" \
                "itsdangerous>=2.2" "python-multipart>=0.0.9" "httpx>=0.27" "pillow>=10"

ENV PYTHONPATH=/app/src
RUN useradd -r -u 10001 hub && mkdir -p /data/evidence && chown -R hub /data
USER hub
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health', timeout=4).status==200 else 1)"
CMD ["python", "-m", "hub", "serve", "--host", "0.0.0.0", "--port", "8080"]
