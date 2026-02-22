#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
from pathlib import Path

def main():
    # Подстрой под свой проект:
    project_root = Path(__file__).resolve().parents[2]  # .../src/scripts -> .../
    db_path = project_root / "data" / "overtime.db"

    # Безопасность: не даём снести что-то не то
    if not str(db_path).endswith("data/overtime.db"):
        raise RuntimeError(f"Refusing to delete suspicious path: {db_path}")

    if db_path.exists():
        db_path.unlink()
        print(f"✅ Deleted DB file: {db_path}")
    else:
        print(f"ℹ️ DB file not found: {db_path}")

    # Создадим пустую папку data если её нет
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Дальше важно: заново создать таблицы.
    # Тут два пути:
    # A) если у тебя есть метод db.init_schema() — просто вызови его
    # B) либо импортируй Database и создай объект (он обычно сам делает миграции/схему)

    from src.db import Database  # подстрой импорт под твоё имя файла/класса

    db = Database(db_file=str(db_path))
    # Если у Database есть явный метод:
    if hasattr(db, "init_schema"):
        db.init_schema()

    print("✅ Fresh DB schema created.")

if __name__ == "__main__":
    main()