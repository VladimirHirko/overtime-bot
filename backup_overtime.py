#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OvertimeBot — автоматический бэкап PostgreSQL
Запускать через launchd на macOS
Сохраняет .dump в backups/
При желании можно расширить JSON-экспортом
"""

import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path


# ====== НАСТРОЙКИ ======
DATABASE_URL = "postgresql://postgres:mAJBwkPtmmoRerjbbDogpYlstgtOxUMX@gondola.proxy.rlwy.net:12376/railway"
PG_DUMP_PATH = "/usr/local/opt/postgresql@17/bin/pg_dump"

BASE_DIR = Path("/Users/vladimir_hirko/Documents/OvertimeBot")
BACKUP_DIR = BASE_DIR / "backups"
LOG_FILE = BACKUP_DIR / "backup.log"

BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def cleanup_old_backups(days: int = 30) -> None:
    cutoff = datetime.now() - timedelta(days=days)
    removed = 0

    for file in BACKUP_DIR.glob("overtime_*.dump"):
        file_time = datetime.fromtimestamp(file.stat().st_mtime)
        if file_time < cutoff:
            file.unlink(missing_ok=True)
            removed += 1

    log(f"🗑️ Удалено старых бэкапов: {removed}")


def create_pg_dump() -> None:
    if not os.path.isfile(PG_DUMP_PATH):
        raise FileNotFoundError(f"pg_dump не найден: {PG_DUMP_PATH}")

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_file = BACKUP_DIR / f"overtime_{ts}.dump"

    cmd = [
        PG_DUMP_PATH,
        "-Fc",
        DATABASE_URL,
        "-f",
        str(out_file),
    ]

    log("🔄 Начинаем создание бэкапа PostgreSQL...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        log("❌ Ошибка pg_dump:")
        log(result.stderr.strip())
        raise RuntimeError("pg_dump завершился с ошибкой")

    if not out_file.exists():
        raise RuntimeError("Файл бэкапа не был создан")

    size_kb = round(out_file.stat().st_size / 1024, 2)
    log(f"✅ Бэкап создан: {out_file.name} ({size_kb} KB)")


def main() -> None:
    try:
        log("===== START BACKUP =====")
        create_pg_dump()
        cleanup_old_backups(days=30)
        log("===== END BACKUP =====")
    except Exception as e:
        log(f"❌ Ошибка бэкапа: {e}")


if __name__ == "__main__":
    main()
