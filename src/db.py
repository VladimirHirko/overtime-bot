# src/db.py
from __future__ import annotations

import os
import json
import sqlite3
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row


Role = str  # "worker" | "foreman" | "director" | "admin"


class DB:
    """
    DB wrapper that supports:
      - PostgreSQL (via DATABASE_URL)  ✅ for Railway
      - SQLite fallback (local)        ✅ for quick local tests
    Query placeholder style:
      Use "{p}" in SQL, it will be replaced by "%s" (Postgres) or "?" (SQLite).
    """

    def delete_user_by_tg(self, tg_id: int) -> None:
        self.execute("DELETE FROM users WHERE tg_id={p}", (tg_id,))

    def __init__(self, database_url: Optional[str]) -> None:
        self.database_url = database_url
        self.driver = "postgres" if (database_url and database_url.startswith("postgres")) else "sqlite"

        if self.driver == "postgres":
            self.conn = psycopg.connect(database_url, autocommit=True, row_factory=dict_row)
            self.conn.autocommit = True
        else:
            os.makedirs("data", exist_ok=True)
            path = os.path.join("data", "overtime.db")
            self.conn = sqlite3.connect(path)
            self.conn.row_factory = sqlite3.Row

            # ✅ SQLite: включаем FK (иначе каскады/целостность не работают)
            try:
                self.conn.execute("PRAGMA foreign_keys = ON;")
            except Exception:
                pass

        self.ensure_schema()

    def mark_welcome_seen(self, tg_id: int) -> None:
        if self.driver == "postgres":
            self.execute("UPDATE users SET seen_welcome=TRUE WHERE tg_id={p}", (tg_id,))
        else:
            self.execute("UPDATE users SET seen_welcome=1 WHERE tg_id={p}", (tg_id,))

    def has_seen_welcome(self, tg_id: int) -> bool:
        row = self.execute("SELECT seen_welcome FROM users WHERE tg_id={p}", (tg_id,), fetch="one")
        if not row:
            return False
        v = row.get("seen_welcome")
        return bool(v)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def _ph(self) -> str:
        return "%s" if self.driver == "postgres" else "?"

    def _fmt(self, sql: str) -> str:
        # replace each "{p}" with proper placeholder
        ph = self._ph()
        return sql.replace("{p}", ph)

    def execute(self, sql: str, params: tuple[Any, ...] = (), *, fetch: str | None = None) -> Any:
        sql = self._fmt(sql)
        if self.driver == "postgres":
            with self.conn.cursor() as cur:
                cur.execute(sql, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return None
        else:
            cur = self.conn.cursor()
            cur.execute(sql, params)
            self.conn.commit()
            if fetch == "one":
                row = cur.fetchone()
                return dict(row) if row else None
            if fetch == "all":
                rows = cur.fetchall()
                return [dict(r) for r in rows]
            return None

    # ---------- Schema ----------

    def ensure_schema(self) -> None:
        if self.driver == "postgres":
            self._ensure_schema_pg()
        else:
            self._ensure_schema_sqlite()

    def _ensure_schema_pg(self) -> None:
        self.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            foreman_user_id INTEGER NULL
        );
        """)

        self.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            tg_id BIGINT NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL,
            view_mode TEXT NOT NULL DEFAULT 'worker',
            team_id INTEGER NULL REFERENCES teams(id) ON DELETE SET NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        self.execute("""
        CREATE TABLE IF NOT EXISTS operations (
            id SERIAL PRIMARY KEY,
            target_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_by_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            op_type TEXT NOT NULL,                 -- "credit" | "debit"
            op_date DATE NOT NULL,
            hours NUMERIC(10,2) NOT NULL,          -- positive number
            comment TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,                  -- "pending" | "approved" | "rejected" | "cancelled"
            decided_by_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
            decision_comment TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            decided_at TIMESTAMPTZ NULL
        );
        """)

        self.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id SERIAL PRIMARY KEY,
            actor_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
            event TEXT NOT NULL,
            entity TEXT NOT NULL,
            entity_id INTEGER NULL,
            meta_json TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        self.execute("CREATE INDEX IF NOT EXISTS idx_users_team ON users(team_id);")
        self.execute("CREATE INDEX IF NOT EXISTS idx_ops_target ON operations(target_user_id);")
        self.execute("CREATE INDEX IF NOT EXISTS idx_ops_status ON operations(status);")
        self.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);")
        # --- lightweight migrations ---
        self.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS seen_welcome BOOLEAN NOT NULL DEFAULT FALSE;")

        # --- view_mode (foreman/admin can switch to worker UI) ---
        self.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS view_mode TEXT;")
        self.execute("UPDATE users SET view_mode = role WHERE view_mode IS NULL;")
        self.execute("ALTER TABLE users ALTER COLUMN view_mode SET DEFAULT 'worker';")

    def _ensure_schema_sqlite(self) -> None:
        self.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            foreman_user_id INTEGER NULL
        );
        """)

        self.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL,
            view_mode TEXT NOT NULL DEFAULT 'worker',
            team_id INTEGER NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """)

        self.execute("""
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_user_id INTEGER NOT NULL,
            created_by_user_id INTEGER NOT NULL,
            op_type TEXT NOT NULL,
            op_date TEXT NOT NULL,
            hours REAL NOT NULL,
            comment TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            decided_by_user_id INTEGER NULL,
            decision_comment TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            decided_at TEXT NULL
        );
        """)

        self.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER NULL,
            event TEXT NOT NULL,
            entity TEXT NOT NULL,
            entity_id INTEGER NULL,
            meta_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """)

        self.execute("CREATE INDEX IF NOT EXISTS idx_users_team ON users(team_id);")
        self.execute("CREATE INDEX IF NOT EXISTS idx_ops_target ON operations(target_user_id);")
        self.execute("CREATE INDEX IF NOT EXISTS idx_ops_status ON operations(status);")
        self.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);")
        # --- lightweight migrations ---
        # seen_welcome
        try:
            self.execute("ALTER TABLE users ADD COLUMN seen_welcome INTEGER NOT NULL DEFAULT 0;")
        except Exception:
            pass

        # view_mode
        try:
            self.execute("ALTER TABLE users ADD COLUMN view_mode TEXT NOT NULL DEFAULT 'worker';")
        except Exception:
            pass

        # normalize old rows
        try:
            self.execute("UPDATE users SET view_mode = role WHERE view_mode IS NULL OR view_mode='';")
        except Exception:
            pass

    # ---------- RESET / WIPE ----------
    def reset_all_data(self) -> None:
        """
        Deletes ALL rows from all business tables, but keeps schema intact.
        Works for both Postgres (Railway) and SQLite (local).
        """
        if self.driver == "postgres":
            self._reset_all_data_pg()
        else:
            self._reset_all_data_sqlite()

    def _reset_all_data_pg(self) -> None:
        """
        Postgres: TRUNCATE + RESTART IDENTITY + CASCADE
        """
        # Важно: TRUNCATE не принимает плейсхолдеры, поэтому обычной строкой.
        # Порядок не критичен из-за CASCADE, но держим логичный.
        self.execute("""
            TRUNCATE TABLE
                audit_log,
                operations,
                users,
                teams
            RESTART IDENTITY CASCADE;
        """)

    def _reset_all_data_sqlite(self) -> None:
        """
        SQLite: DELETE FROM tables + reset autoincrement counters.
        """
        # В SQLite лучше отключить FK на время, иначе можно словить ограничения
        try:
            self.conn.execute("PRAGMA foreign_keys = OFF;")
        except Exception:
            pass

        cur = self.conn.cursor()

        # Порядок: сначала дочерние, потом родительские
        for t in ("audit_log", "operations", "users", "teams"):
            cur.execute(f"DELETE FROM {t};")

        # Сброс автоинкремента (если есть AUTOINCREMENT)
        try:
            cur.execute("DELETE FROM sqlite_sequence;")
        except Exception:
            # sqlite_sequence может отсутствовать, если AUTOINCREMENT не использовался
            pass

        self.conn.commit()

        try:
            self.conn.execute("PRAGMA foreign_keys = ON;")
        except Exception:
            pass

    # ---------- Helpers (Users / Teams) ----------

    def get_user_by_tg(self, tg_id: int) -> dict | None:
        return self.execute(
            "SELECT * FROM users WHERE tg_id={p} AND active=TRUE" if self.driver == "postgres"
            else "SELECT * FROM users WHERE tg_id={p} AND active=1",
            (tg_id,),
            fetch="one",
        )

    def create_user(self, tg_id: int, full_name: str, role: Role, team_id: int | None = None) -> dict:
        if self.driver == "postgres":
            row = self.execute(
                "INSERT INTO users (tg_id, full_name, role, view_mode, team_id) VALUES ({p},{p},{p},{p},{p}) RETURNING *",
                (tg_id, full_name, role, role, team_id),
                fetch="one",
            )
            return row
        else:
            self.execute(
                "INSERT INTO users (tg_id, full_name, role, view_mode, team_id) VALUES ({p},{p},{p},{p},{p})",
                (tg_id, full_name, role, role, team_id),
            )
            return self.get_user_by_tg(tg_id)  # type: ignore

    def upsert_user_minimal_admin(self, tg_id: int, full_name: str, role: Role = "admin") -> dict:
        # ensure admin exists
        user = self.get_user_by_tg(tg_id)
        if user:
            return user
        return self.create_user(tg_id, full_name, role, None)

    def set_user_role_team(self, tg_id: int, role: Role, team_id: int | None) -> None:
        # Берём текущий view_mode (чтобы не уничтожать переключение у foreman)
        cur = self.execute("SELECT view_mode FROM users WHERE tg_id={p}", (tg_id,), fetch="one")
        cur_view = (cur.get("view_mode") if cur else None) or "worker"

        if role == "foreman":
            new_view = cur_view if cur_view in ("worker", "foreman") else "foreman"
        else:
            new_view = role  # director/admin/worker — ок

        self.execute(
            "UPDATE users SET role={p}, team_id={p}, view_mode={p} WHERE tg_id={p}",
            (role, team_id, new_view, tg_id),
        )

        # авто-привязка/отвязка foreman — оставляем твою логику как есть
        if role == "foreman" and team_id is not None:
            u = self.execute("SELECT id FROM users WHERE tg_id={p}", (tg_id,), fetch="one")
            if u:
                self.set_team_foreman(int(team_id), int(u["id"]))

        if role != "foreman" or team_id is None:
            u = self.execute("SELECT id FROM users WHERE tg_id={p}", (tg_id,), fetch="one")
            if u:
                self.execute(
                    "UPDATE teams SET foreman_user_id=NULL WHERE foreman_user_id={p}",
                    (int(u["id"]),),
                )

    def list_users(self) -> list[dict]:
        return self.execute("SELECT id,tg_id,full_name,role,team_id FROM users ORDER BY role, full_name", fetch="all")

    def create_team(self, name: str) -> dict:
        if self.driver == "postgres":
            return self.execute(
                "INSERT INTO teams (name) VALUES ({p}) ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING *",
                (name,),
                fetch="one",
            )
        else:
            # sqlite: INSERT OR IGNORE then select
            self.execute("INSERT OR IGNORE INTO teams (name) VALUES ({p})", (name,))
            return self.execute("SELECT * FROM teams WHERE name={p}", (name,), fetch="one")

    def list_teams(self) -> list[dict]:
        return self.execute("SELECT * FROM teams ORDER BY name", fetch="all")

    def set_team_foreman(self, team_id: int, foreman_user_id: int) -> None:
        self.execute("UPDATE teams SET foreman_user_id={p} WHERE id={p}", (foreman_user_id, team_id))

    def get_team_foreman_tg(self, team_id: int) -> int | None:
        row = self.execute("""
            SELECT u.tg_id AS tg_id
            FROM teams t
            JOIN users u ON u.id = t.foreman_user_id
            WHERE t.id={p}
        """, (team_id,), fetch="one")
        return int(row["tg_id"]) if row else None

    def list_director_tg_ids(self) -> list[int]:
        rows = self.execute("SELECT tg_id FROM users WHERE role='director' AND active=TRUE" if self.driver == "postgres"
                            else "SELECT tg_id FROM users WHERE role='director' AND active=1", fetch="all")
        return [int(r["tg_id"]) for r in rows]

    def set_user_view_mode(self, tg_id: int, view_mode: str) -> None:
        self.execute("UPDATE users SET view_mode={p} WHERE tg_id={p}", (view_mode, tg_id))

    # ---------- Operations / Balance ----------

    def create_operation(
        self,
        target_user_id: int,
        created_by_user_id: int,
        op_type: str,  # credit/debit
        op_date: str,  # YYYY-MM-DD
        hours: float,
        comment: str,
        status: str = "pending",
    ) -> dict:
        if self.driver == "postgres":
            return self.execute(
                """INSERT INTO operations
                   (target_user_id, created_by_user_id, op_type, op_date, hours, comment, status)
                   VALUES ({p},{p},{p},{p},{p},{p},{p})
                   RETURNING *""",
                (target_user_id, created_by_user_id, op_type, op_date, hours, comment, status),
                fetch="one",
            )
        else:
            self.execute(
                """INSERT INTO operations
                   (target_user_id, created_by_user_id, op_type, op_date, hours, comment, status)
                   VALUES ({p},{p},{p},{p},{p},{p},{p})""",
                (target_user_id, created_by_user_id, op_type, op_date, hours, comment, status),
            )
            row = self.execute("SELECT * FROM operations ORDER BY id DESC LIMIT 1", fetch="one")
            return row

    def get_operation(self, op_id: int) -> dict | None:
        return self.execute("SELECT * FROM operations WHERE id={p}", (op_id,), fetch="one")

    def list_pending_for_foreman(self, foreman_user_id: int) -> list[dict]:
        # pending operations for users in foreman's team
        foreman = self.execute("SELECT team_id FROM users WHERE id={p}", (foreman_user_id,), fetch="one")
        if not foreman or not foreman.get("team_id"):
            return []
        team_id = foreman["team_id"]
        return self.execute("""
            SELECT o.*, u.full_name AS target_name
            FROM operations o
            JOIN users u ON u.id = o.target_user_id
            WHERE o.status='pending' AND u.team_id={p}
            ORDER BY o.created_at ASC
        """, (team_id,), fetch="all")

    def approve_operation(self, op_id: int, foreman_user_id: int) -> None:
        if self.driver == "postgres":
            self.execute("""
                UPDATE operations
                SET status='approved', decided_by_user_id={p}, decided_at=NOW(), decision_comment=''
                WHERE id={p} AND status='pending'
            """, (foreman_user_id, op_id))
        else:
            self.execute("""
                UPDATE operations
                SET status='approved', decided_by_user_id={p}, decided_at=datetime('now'), decision_comment=''
                WHERE id={p} AND status='pending'
            """, (foreman_user_id, op_id))

    def reject_operation(self, op_id: int, foreman_user_id: int, reason: str) -> None:
        if self.driver == "postgres":
            self.execute("""
                UPDATE operations
                SET status='rejected', decided_by_user_id={p}, decided_at=NOW(), decision_comment={p}
                WHERE id={p} AND status='pending'
            """, (foreman_user_id, reason, op_id))
        else:
            self.execute("""
                UPDATE operations
                SET status='rejected', decided_by_user_id={p}, decided_at=datetime('now'), decision_comment={p}
                WHERE id={p} AND status='pending'
            """, (foreman_user_id, reason, op_id))

    def calc_balance_hours(self, user_id: int) -> float:
        row = self.execute("""
            SELECT
              COALESCE(SUM(CASE WHEN status='approved' AND op_type='credit' THEN hours ELSE 0 END),0) AS plus_h,
              COALESCE(SUM(CASE WHEN status='approved' AND op_type='debit'  THEN hours ELSE 0 END),0) AS minus_h
            FROM operations
            WHERE target_user_id={p}
        """, (user_id,), fetch="one")
        plus_h = float(row["plus_h"]) if row else 0.0
        minus_h = float(row["minus_h"]) if row else 0.0
        return round(plus_h - minus_h, 2)

    def list_statement(self, user_id: int, limit: int = 20) -> list[dict]:
        if self.driver == "postgres":
            return self.execute("""
                SELECT o.*, u.full_name AS decided_by_name
                FROM operations o
                LEFT JOIN users u ON u.id = o.decided_by_user_id
                WHERE o.target_user_id={p}
                ORDER BY o.created_at DESC
                LIMIT {limit}
            """.replace("{limit}", str(limit)), (user_id,), fetch="all")
        else:
            return self.execute("""
                SELECT o.*, u.full_name AS decided_by_name
                FROM operations o
                LEFT JOIN users u ON u.id = o.decided_by_user_id
                WHERE o.target_user_id={p}
                ORDER BY o.created_at DESC
                LIMIT {limit}
            """.replace("{limit}", str(limit)), (user_id,), fetch="all")

    def log_event(self, actor_user_id: int | None, event: str, entity: str, entity_id: int | None, meta: dict) -> None:
        meta_json = json.dumps(meta, ensure_ascii=False)
        self.execute(
            "INSERT INTO audit_log (actor_user_id, event, entity, entity_id, meta_json) VALUES ({p},{p},{p},{p},{p})",
            (actor_user_id, event, entity, entity_id, meta_json),
        )

    def list_audit(self, limit: int = 30) -> list[dict]:
        if self.driver == "postgres":
            return self.execute(f"""
                SELECT a.*, u.full_name AS actor_name
                FROM audit_log a
                LEFT JOIN users u ON u.id=a.actor_user_id
                ORDER BY a.created_at DESC
                LIMIT {limit}
            """, fetch="all")
        else:
            return self.execute(f"""
                SELECT a.*, u.full_name AS actor_name
                FROM audit_log a
                LEFT JOIN users u ON u.id=a.actor_user_id
                ORDER BY a.created_at DESC
                LIMIT {limit}
            """, fetch="all")

    def list_workers_for_foreman(self, foreman_user_id: int) -> list[dict]:
        foreman = self.execute("SELECT team_id FROM users WHERE id={p}", (foreman_user_id,), fetch="one")
        if not foreman or not foreman.get("team_id"):
            return []
        team_id = foreman["team_id"]

        if self.driver == "postgres":
            return self.execute(
                "SELECT id, tg_id, full_name FROM users "
                "WHERE role IN ('worker','foreman') AND team_id={p} AND active=TRUE "
                "ORDER BY full_name",
                (team_id,),
                fetch="all",
            )
        return self.execute(
            "SELECT id, tg_id, full_name FROM users "
            "WHERE role IN ('worker','foreman') AND team_id={p} AND active=1 "
            "ORDER BY full_name",
            (team_id,),
            fetch="all",
        )

    def create_adjustment_operation(
        self,
        target_user_id: int,
        created_by_user_id: int,
        op_type: str,   # "credit" | "debit"
        op_date: str,   # YYYY-MM-DD
        hours: float,
        comment: str,
    ) -> dict:
        """
        Foreman/admin manual adjustment: creates operation immediately approved,
        and sets decided_by_user_id + decided_at.
        """
        if self.driver == "postgres":
            return self.execute(
                """
                INSERT INTO operations
                  (target_user_id, created_by_user_id, op_type, op_date, hours, comment, status,
                   decided_by_user_id, decided_at, decision_comment)
                VALUES
                  ({p},{p},{p},{p},{p},{p},'approved',
                   {p}, NOW(), '')
                RETURNING *
                """,
                (target_user_id, created_by_user_id, op_type, op_date, hours, comment, created_by_user_id),
                fetch="one",
            )

        self.execute(
            """
            INSERT INTO operations
              (target_user_id, created_by_user_id, op_type, op_date, hours, comment, status,
               decided_by_user_id, decided_at, decision_comment)
            VALUES
              ({p},{p},{p},{p},{p},{p},'approved',
               {p}, datetime('now'), '')
            """,
            (target_user_id, created_by_user_id, op_type, op_date, hours, comment, created_by_user_id),
        )
        row = self.execute("SELECT * FROM operations ORDER BY id DESC LIMIT 1", fetch="one")
        return row

