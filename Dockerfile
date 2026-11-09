FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt /app/
RUN pip install -r requirements.txt

# Copy project files (db.sqlite3 included; see .dockerignore)
COPY . /app/

# The committed db.sqlite3 becomes the seed. On first start the entrypoint copies it
# into /app/data (a persistent volume) so data survives redeploys.
RUN mkdir -p /app/seed /app/data /app/media \
    && mv /app/db.sqlite3 /app/seed/db.sqlite3 \
    && chmod +x /app/docker-entrypoint.sh \
    && SECRET_KEY=build-only DEBUG=False python manage.py collectstatic --noinput

ENV SQLITE_PATH=/app/data/db.sqlite3 \
    MEDIA_ROOT=/app/media

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "60", "--access-logfile", "-"]
