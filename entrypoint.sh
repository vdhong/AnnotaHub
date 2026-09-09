#!/bin/sh
#
# Entrypoint cho mọi service (web, worker, beat).
#
# Nguyên tắc:
# - KHÔNG cài package lúc runtime (đã cài đầy đủ trong image lúc build).
# - KHÔNG tự động restore backup (restore là thao tác thủ công, có chủ đích).
# - KHÔNG nuốt lỗi migration: schema sai thì phải dừng ngay, không chạy tiếp.
# - Chỉ service nào đặt RUN_MIGRATIONS=1 mới chạy migrate (tránh đua giữa 3 container).

set -e

DB_HOST="${DB_HOST:-db}"
DB_USER="${POSTGRES_USER:-annotahub_user}"
DB_NAME="${POSTGRES_DB:-annotahub}"

echo "[entrypoint] Chờ PostgreSQL tại ${DB_HOST}..."
timeout=60
while ! pg_isready -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; do
    timeout=$((timeout - 1))
    if [ "$timeout" -le 0 ]; then
        echo "[entrypoint] LỖI: PostgreSQL không phản hồi sau 60 giây." >&2
        exit 1
    fi
    sleep 1
done
echo "[entrypoint] PostgreSQL đã sẵn sàng."

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
    echo "[entrypoint] Chạy migration..."
    python manage.py migrate --noinput
    echo "[entrypoint] Migration hoàn tất."
else
    echo "[entrypoint] Bỏ qua migration (RUN_MIGRATIONS != 1)."
fi

exec "$@"
