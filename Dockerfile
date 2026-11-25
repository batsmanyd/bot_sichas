FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --uid 10001 app \
    && install -d -o app -g app /data
COPY --chmod=0644 main.py index.html admin.html legal.html manifest.webmanifest *.svg *.png alembic.ini ./
COPY --chown=app:app migrations/ migrations/
USER app
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--worker-class", "gthread", "--workers", "1", "--threads", "16", "--timeout", "0", "main:app"]
