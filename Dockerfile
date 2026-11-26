FROM python:3.12-slim AS application

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --uid 10001 app \
    && install -d -o app -g app /data
COPY --chmod=0644 main.py index.html admin.html legal.html about.html about-en.html offline.html service-worker.js manifest.webmanifest *.svg *.png alembic.ini ./
COPY --chown=app:app migrations/ migrations/
COPY --chmod=0644 scripts/bootstrap_db.py ./
USER app
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--worker-class", "gthread", "--workers", "1", "--threads", "16", "--timeout", "0", "main:app"]

FROM application AS demo
USER root
RUN install -d -o app -g app /demo-data
COPY --chmod=0644 scripts/local_demo.py ./
USER app
CMD ["python", "/app/local_demo.py"]

FROM application AS release
