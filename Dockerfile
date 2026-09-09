FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Phụ thuộc hệ thống. build-essential chỉ cần lúc biên dịch wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        postgresql-client \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Cài phụ thuộc Python ở lớp riêng để tận dụng cache khi chỉ đổi mã nguồn.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chạy dưới user không phải root.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/media /app/logs /app/backups /app/staticfiles /app/exports \
    && chown -R appuser:appuser /app

COPY --chown=appuser:appuser . .

RUN chmod +x /app/entrypoint.sh

# Gom static file vào image ngay lúc build.
# PHẢI chạy ở chế độ production (DEBUG=0) để sinh staticfiles.json — runtime
# dùng ManifestStaticFilesStorage và sẽ lỗi nếu thiếu manifest này.
RUN DEBUG=0 \
    SECRET_KEY=build-time-key-not-used-at-runtime-0123456789abcdefghijklmnop \
    ALLOWED_HOSTS=localhost \
    python manage.py collectstatic --noinput --clear \
 && test -f /app/staticfiles/staticfiles.json \
 || (echo 'LỖI: collectstatic không sinh được manifest' && exit 1)

USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]

CMD ["gunicorn", "annotahub.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--worker-class", "gevent", \
     "--workers", "3", \
     "--worker-connections", "200", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
