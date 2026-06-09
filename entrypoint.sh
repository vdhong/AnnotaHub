#!/bin/sh

set -e

# Wait for database to be ready
echo "Waiting for database to be ready..."
while ! pg_isready -h db -U toxyspan_user -d toxyspan 2>/dev/null; do
  sleep 1
done
echo "Database is ready!"

# Run Django migrations (ignore errors if tables already exist)
echo "Running Django migrations..."
python manage.py migrate --noinput || true

# Restore from backup if backup file exists
BACKUP_FILE="/app/backups/annotahub_backup.json"
if [ -f "$BACKUP_FILE" ]; then
  echo "Found backup file: $BACKUP_FILE"
  echo "Restoring from backup..."
  python manage.py restore_annotahub "$BACKUP_FILE"
  echo "Backup restored successfully!"
else
  echo "No backup file found at $BACKUP_FILE, skipping restoration."
fi

# Execute the main command
exec "$@"