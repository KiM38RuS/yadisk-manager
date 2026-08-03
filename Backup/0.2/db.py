"""
SQLite-база для отслеживания файлов: какие скачаны, какие изменены.
"""

import os
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Путь к БД — рядом с программой или в %APPDATA%
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                       "yadisk-client")
os.makedirs(APP_DIR, exist_ok=True)
DB_PATH = os.path.join(APP_DIR, "yadisk.db")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Config (JSON) ────────────────────────────────────────

def load_config() -> dict:
    """Загрузить конфиг (токен, настройки)."""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    return {}


def save_config(cfg: dict) -> None:
    """Сохранить конфиг."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def get_token() -> Optional[str]:
    return load_config().get("token")


def set_token(token: str) -> None:
    cfg = load_config()
    cfg["token"] = token
    save_config(cfg)


def get_cache_dir() -> str:
    return load_config().get("cache_dir", "")


# ── Database ─────────────────────────────────────────────

def set_cache_dir(path: str) -> None:
    cfg = load_config()
    cfg["cache_dir"] = path
    save_config(cfg)


class Database:
    """Отслеживает статус каждого файла (скачан, изменён, только в облаке)."""

    def __init__(self, db_path: str = DB_PATH):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                cloud_path    TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                type          TEXT NOT NULL DEFAULT 'file',
                mime_type     TEXT,
                size          INTEGER DEFAULT 0,
                modified      TEXT,
                md5           TEXT,
                last_sync_md5 TEXT,
                status        TEXT NOT NULL DEFAULT 'cloud_only'
                              CHECK(status IN ('cloud_only','downloaded','modified')),
                local_path    TEXT,
                local_mtime   TEXT,
                created_at    TEXT DEFAULT (datetime('now')),
                updated_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        # обратная совместимость — добавляем колонку если её нет
        try:
            self._conn.execute("ALTER TABLE files ADD COLUMN last_sync_md5 TEXT")
        except sqlite3.OperationalError:
            pass  # колонка уже есть
        self._conn.commit()

    # ── CRUD ──────────────────────────────────────────────

    def upsert_files_batch(self, file_list: list[dict]) -> None:
        """
        Массовое обновление/вставка файлов в одной транзакции.
        Значительно быстрее, чем поочерёдный вызов upsert_file() с коммитом после каждого.
        """
        if not file_list:
            return
        now = _now()
        self._conn.execute("BEGIN TRANSACTION")
        for item in file_list:
            self._conn.execute("""
                INSERT INTO files (cloud_path, name, type, mime_type, size,
                                   modified, md5, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cloud_path) DO UPDATE SET
                    name=excluded.name,
                    type=excluded.type,
                    mime_type=excluded.mime_type,
                    size=excluded.size,
                    modified=excluded.modified,
                    md5=excluded.md5,
                    updated_at=excluded.updated_at
            """, (
                item["path"],
                item["name"],
                item["type"],
                item.get("mime_type", ""),
                item.get("size", 0),
                item.get("modified", ""),
                item.get("md5", ""),
                now,
            ))
        self._conn.commit()

    def upsert_file(self, cloud_path: str, name: str, type_: str,
                    size: int = 0, modified: str = "", md5: str = "",
                    mime_type: str = "") -> None:
        """Добавить или обновить запись о файле из данных облака."""
        now = _now()
        self._conn.execute("""
            INSERT INTO files (cloud_path, name, type, mime_type, size,
                               modified, md5, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cloud_path) DO UPDATE SET
                name=excluded.name,
                type=excluded.type,
                mime_type=excluded.mime_type,
                size=excluded.size,
                modified=excluded.modified,
                md5=excluded.md5,
                updated_at=excluded.updated_at
        """, (cloud_path, name, type_, mime_type, size, modified, md5, now))
        self._conn.commit()

    def set_downloaded(self, cloud_path: str, local_path: str,
                       last_sync_md5: str = "") -> None:
        """Отметить файл как скачанный."""
        now = _now()
        self._conn.execute("""
            UPDATE files SET status='downloaded', local_path=?,
                             local_mtime=?, last_sync_md5=?,
                             updated_at=?
            WHERE cloud_path=?
        """, (local_path, now, last_sync_md5, now, cloud_path))
        self._conn.commit()

    def set_modified(self, cloud_path: str) -> None:
        """Отметить файл как изменённый локально."""
        now = _now()
        self._conn.execute("""
            UPDATE files SET status='modified', local_mtime=?,
                             updated_at=?
            WHERE cloud_path=?
        """, (now, now, cloud_path))
        self._conn.commit()

    def set_cloud_only(self, cloud_path: str) -> None:
        """Сбросить статус — файл только в облаке."""
        now = _now()
        self._conn.execute("""
            UPDATE files SET status='cloud_only', local_path=NULL,
                             local_mtime=NULL, updated_at=?
            WHERE cloud_path=?
        """, (now, cloud_path))
        self._conn.commit()

    def get_file(self, cloud_path: str) -> Optional[dict]:
        """Получить информацию о файле."""
        row = self._conn.execute(
            "SELECT * FROM files WHERE cloud_path=?", (cloud_path,)
        ).fetchone()
        if row:
            return dict(row)
        return None

    def get_all_files(self) -> list[dict]:
        """Все отслеживаемые файлы."""
        rows = self._conn.execute("SELECT * FROM files ORDER BY cloud_path")
        return [dict(r) for r in rows.fetchall()]

    def get_by_status(self, status: str) -> list[dict]:
        """Файлы по статусу."""
        rows = self._conn.execute(
            "SELECT * FROM files WHERE status=?", (status,)
        )
        return [dict(r) for r in rows.fetchall()]

    def get_children(self, folder_path: str) -> list[dict]:
        """Файлы внутри папки (для отображения в GUI)."""
        prefix = folder_path.rstrip("/")
        if prefix:
            pattern = prefix + "/%"
        else:
            pattern = "/%"
        rows = self._conn.execute("""
            SELECT * FROM files
            WHERE cloud_path LIKE ?
              AND cloud_path != ?
            ORDER BY type DESC, name ASC
        """, (pattern, folder_path))
        return [dict(r) for r in rows.fetchall()]

    def remove_file(self, cloud_path: str) -> None:
        """Удалить запись о файле."""
        self._conn.execute("DELETE FROM files WHERE cloud_path=?", (cloud_path,))
        self._conn.commit()

    def file_exists(self, cloud_path: str) -> bool:
        """Проверить, есть ли запись."""
        row = self._conn.execute(
            "SELECT 1 FROM files WHERE cloud_path=?", (cloud_path,)
        ).fetchone()
        return row is not None

    def get_by_local_path(self, local_path: str) -> Optional[dict]:
        """Найти запись по локальному пути."""
        row = self._conn.execute(
            "SELECT * FROM files WHERE local_path=?", (local_path,)
        ).fetchone()
        return dict(row) if row else None

    def close(self):
        self._conn.close()
