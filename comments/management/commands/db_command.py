from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Backup/restore PostgreSQL bằng file SQL"

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", required=True)

        backup_parser = subparsers.add_parser("backup")
        backup_parser.add_argument(
            "--output-dir",
            default="/app/backups",
            help="Thư mục chứa file backup SQL",
        )

        restore_parser = subparsers.add_parser("restore")
        restore_parser.add_argument(
            "backup_file",
            help="Đường dẫn file .sql",
        )

        restore_parser.add_argument(
            "--clean-db",
            action="store_true",
            help="Drop và tạo lại database trước khi restore",
        )
        
    def get_db_config(self):
        db = settings.DATABASES["default"]

        if "postgresql" not in db["ENGINE"]:
            raise CommandError("Chỉ hỗ trợ PostgreSQL.")

        return {
            "host": db.get("HOST") or "localhost",
            "port": str(db.get("PORT") or 5432),
            "name": db["NAME"],
            "user": db["USER"],
            "password": db.get("PASSWORD", ""),
        }

    def run(self, command, *, password, stdin=None, stdout=None):
        env = os.environ.copy()

        if password:
            env["PGPASSWORD"] = password

        try:
            subprocess.run(
                command,
                env=env,
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.PIPE,
                check=True,
            )
        except FileNotFoundError:
            raise CommandError(
                "Không tìm thấy PostgreSQL client. "
                "Cài package postgresql-client trong container web."
            )
        except subprocess.CalledProcessError as exc:
            error = exc.stderr.decode(errors="replace")
            raise CommandError(
                f"PostgreSQL command thất bại ({exc.returncode}):\n{error}"
            )

    def handle(self, *args, **options):
        db = self.get_db_config()

        if options["action"] == "backup":
            self.backup(db, options["output_dir"])
        else:
            self.restore(
                db,
                options["backup_file"],
                options["clean_db"],
            )

    def backup(self, db, output_dir):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_file = output_dir / f"{db['name']}_{timestamp}.sql"

        command = [
            "pg_dump",
            "-h", db["host"],
            "-p", db["port"],
            "-U", db["user"],
            "--format=plain",
            "--no-owner",
            "--no-privileges",
            db["name"],
        ]

        self.stdout.write(f"Đang backup vào: {backup_file}")

        with backup_file.open("wb") as file:
            self.run(
                command,
                password=db["password"],
                stdout=file,
            )

        self.stdout.write(
            self.style.SUCCESS(f"Backup thành công: {backup_file}")
        )

    def restore(self, db, backup_file, clean_db):
        backup_file = Path(backup_file)

        if not backup_file.exists():
            raise CommandError(f"Không tìm thấy file: {backup_file}")

        if backup_file.suffix.lower() != ".sql":
            raise CommandError("Chỉ hỗ trợ file .sql.")

        if clean_db:
            self.recreate_database(db)

        command = [
            "psql",
            "-h", db["host"],
            "-p", db["port"],
            "-U", db["user"],
            "-d", db["name"],
            "--set", "ON_ERROR_STOP=1",
        ]

        self.stdout.write(
            self.style.WARNING(
                f"Đang restore {backup_file} vào database {db['name']}..."
            )
        )

        with backup_file.open("rb") as file:
            self.run(
                command,
                password=db["password"],
                stdin=file,
            )

        self.stdout.write(self.style.SUCCESS("Restore thành công."))

    def recreate_database(self, db):
        """
        Kết nối vào postgres database để terminate connection,
        drop database hiện tại, rồi tạo lại.
        """
        admin_command = [
            "psql",
            "-h", db["host"],
            "-p", db["port"],
            "-U", db["user"],
            "-d", "postgres",
            "--set", "ON_ERROR_STOP=1",
            "-c",
            (
                "SELECT pg_terminate_backend(pid) "
                "FROM pg_stat_activity "
                f"WHERE datname = '{db['name']}' "
                "AND pid <> pg_backend_pid();"
            ),
        ]

        self.run(admin_command, password=db["password"])

        drop_command = [
            "dropdb",
            "-h", db["host"],
            "-p", db["port"],
            "-U", db["user"],
            db["name"],
        ]
        self.run(drop_command, password=db["password"])

        create_command = [
            "createdb",
            "-h", db["host"],
            "-p", db["port"],
            "-U", db["user"],
            db["name"],
        ]
        self.run(create_command, password=db["password"])