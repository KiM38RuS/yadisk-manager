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


# ── Theme ───────────────────────────────────────────────


def get_theme() -> str:
    """Вернуть тему: 'system', 'light' или 'dark'. По умолчанию 'system'."""
    return load_config().get("theme", "system")


def set_theme(theme: str) -> None:
    cfg = load_config()
    cfg["theme"] = theme
    save_config(cfg)


# ── Window geometry ────────────────────────────────────────


def get_window_geometry() -> str:
    return load_config().get("window_geometry", "")


def set_window_geometry(data: str) -> None:
    cfg = load_config()
    if data:
        cfg["window_geometry"] = data
    else:
        cfg.pop("window_geometry", None)
    save_config(cfg)


def get_save_window_geometry() -> bool:
    return load_config().get("save_window_geometry", False)


def set_save_window_geometry(v: bool) -> None:
    cfg = load_config()
    cfg["save_window_geometry"] = v
    save_config(cfg)


def get_zip_download_enabled() -> bool:
    """Включена ли опция скачивания папок как ZIP."""
    return load_config().get("zip_download_enabled", False)


def set_zip_download_enabled(v: bool) -> None:
    cfg = load_config()
    cfg["zip_download_enabled"] = v
    save_config(cfg)


# ── All-files offset (докачка при обрыве) ────────────────


def get_all_files_offset() -> int:
    """Текущий offset для докачки полного списка файлов.
    0 = нет сохранённого прогресса (начать с начала).
    """
    return load_config().get("all_files_offset", 0)


def set_all_files_offset(offset: int) -> None:
    """Сохранить/сбросить offset докачки файлов."""
    cfg = load_config()
    if offset > 0:
        cfg["all_files_offset"] = offset
    else:
        cfg.pop("all_files_offset", None)
    save_config(cfg)


# ── Database (SQLite) ────────────────────────────────────


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
                status        TEXT NOT NULL DEFAULT 'cloud_only',
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
        # Миграция: убрать префикс 'disk:' из старых записей
        self._migrate_strip_disk_prefix()
        # Миграция: удалить устаревший CHECK constraint, если БД создана до 0.7
        self._migrate_drop_check_constraint()
        self._conn.commit()

    def _migrate_strip_disk_prefix(self):
        """Одноразовая миграция: убрать 'disk:' из cloud_path если есть.

        Старые версии хранили пути с префиксом 'disk:' из API Яндекс.Диска.
        Нормализуем их до '/path/to/file' для консистентности с локальными путями.
        """
        self._conn.execute("BEGIN TRANSACTION")
        try:
            rows = self._conn.execute(
                "SELECT * FROM files WHERE cloud_path LIKE 'disk:%'",
            ).fetchall()
            if not rows:
                self._conn.commit()
                return
            logger.info("DB migration: stripping 'disk:' prefix from %d row(s)", len(rows))
            for row in rows:
                d = dict(row)
                old_cp = d["cloud_path"]
                new_cp = old_cp[5:]  # убираем "disk:"
                # Проверяем, есть ли уже запись без 'disk:' префикса
                existing = self._conn.execute(
                    "SELECT status FROM files WHERE cloud_path=?", (new_cp,)
                ).fetchone()
                if existing:
                    # Запись без префикса существует и, возможно, актуальнее (скачана sync-ом)
                    # Удаляем дубликат с префиксом
                    self._conn.execute("DELETE FROM files WHERE cloud_path=?", (old_cp,))
                else:
                    # Запись есть только с префиксом — обновляем путь
                    d["cloud_path"] = new_cp
                    self._conn.execute("DELETE FROM files WHERE cloud_path=?", (old_cp,))
                    self._conn.execute(
                        """INSERT INTO files
                           (cloud_path, name, type, mime_type, size,
                            modified, md5, last_sync_md5, status,
                            local_path, local_mtime, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,
                                  COALESCE((SELECT created_at FROM files WHERE cloud_path=?),
                                           datetime('now')),
                                  ?)""",
                        (new_cp, d["name"], d["type"], d.get("mime_type", ""),
                         d["size"], d.get("modified", ""), d.get("md5", ""),
                         d.get("last_sync_md5", ""), d["status"],
                         d.get("local_path", ""), d.get("local_mtime", ""),
                         old_cp, d.get("updated_at", "")),
                    )
            self._conn.commit()
        except Exception as e:
            logger.error("DB migration failed: %s", e)
            self._conn.rollback()

    def _migrate_drop_check_constraint(self):
        """Удалить CHECK constraint из таблицы files для БД, созданных до 0.7.

        Новые БД уже создаются без CHECK, а старые нужно мигрировать.
        Проверяем: пробуем вставить тестовую запись с 'syncing',
        при ошибке пересоздаём таблицу без CHECK.
        """
        try:
            self._conn.execute(
                "INSERT INTO files (cloud_path, name, type, status) "
                "VALUES (?, ?, 'file', 'syncing')",
                ("__db_migrate_check__", "__db_migrate_check__"),
            )
            # Успех — CHECK уже позволяет syncing (или его нет)
            self._conn.execute(
                "DELETE FROM files WHERE cloud_path = '__db_migrate_check__'"
            )
            self._conn.commit()
            return
        except sqlite3.IntegrityError:
            self._conn.rollback()
            # CHECK блокирует syncing — пересоздаём таблицу без CHECK
            logger.info("DB migration: dropping CHECK constraint from 'files' table")
            self._conn.executescript("""
                CREATE TABLE files_new (
                    cloud_path    TEXT PRIMARY KEY,
                    name          TEXT NOT NULL,
                    type          TEXT NOT NULL DEFAULT 'file',
                    mime_type     TEXT,
                    size          INTEGER DEFAULT 0,
                    modified      TEXT,
                    md5           TEXT,
                    last_sync_md5 TEXT,
                    status        TEXT NOT NULL DEFAULT 'cloud_only',
                    local_path    TEXT,
                    local_mtime   TEXT,
                    created_at    TEXT DEFAULT (datetime('now')),
                    updated_at    TEXT DEFAULT (datetime('now'))
                );
                INSERT INTO files_new SELECT * FROM files;
                DROP TABLE files;
                ALTER TABLE files_new RENAME TO files;
            """)
            self._conn.commit()
            logger.info("DB migration: CHECK constraint dropped successfully")

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

    _VALID_STATUSES = frozenset({"cloud_only", "downloaded", "modified", "syncing"})

    def set_status(self, cloud_path: str, status: str) -> None:
        """Установить произвольный статус файла/папки."""
        if status not in self._VALID_STATUSES:
            raise ValueError(f"Invalid status: {status!r}")
        now = _now()
        self._conn.execute("""
            UPDATE files SET status=?, updated_at=?
            WHERE cloud_path=?
        """, (status, now, cloud_path))
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

    def count_by_status(self, status: str) -> int:
        """Количество файлов с указанным статусом (легковесный COUNT)."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM files WHERE status=?", (status,)
        ).fetchone()
        return row["cnt"] if row else 0

    def get_children(self, folder_path: str) -> list[dict]:
        """Файлы и подпапки ПРЯМО внутри папки (один уровень глубины).

        Для корня "/" возвращает только элементы вида /name,
        для "/ЗАПРАВКА" — только /ЗАПРАВКА/name (без /ЗАПРАВКА/sub/sub).
        Рекурсивный обход делается внешним кодом при необходимости.
        """
        prefix = folder_path.rstrip("/")
        if prefix:
            pattern_dir = prefix + "/%"
            pattern_deep = prefix + "/%/%"
        else:
            pattern_dir = "/%"
            pattern_deep = "/%/%"
        rows = self._conn.execute("""
            SELECT * FROM files
            WHERE cloud_path LIKE ?
              AND cloud_path != ?
              AND cloud_path NOT LIKE ?
            ORDER BY type DESC, name ASC
        """, (pattern_dir, folder_path, pattern_deep))
        return [dict(r) for r in rows.fetchall()]

    # ── Folder status ──────────────────────────────────────

    def is_folder_fully_synced(self, folder_path: str) -> bool:
        """Проверить, все ли файлы внутри папки имеют статус 'downloaded'.

        Возвращает True только если в папке есть файлы И все они скачаны.
        Пустая папка → False (нечего синхронизировать).
        Подпапки не учитываются — только файлы.
        """
        prefix = folder_path.rstrip("/")
        pattern = prefix + "/%" if prefix else "/%"
        row = self._conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='downloaded' THEN 1 ELSE 0 END) as synced
            FROM files
            WHERE cloud_path LIKE ?
              AND cloud_path != ?
              AND type = 'file'
        """, (pattern, folder_path)).fetchone()
        if not row or row["total"] == 0:
            return False
        return row["total"] == row["synced"]

    def get_folder_aggregate_status(self, folder_path: str) -> str:
        """Агрегированный статус папки по её дочерним файлам.

        Возвращает один из:
          'cloud_only' — ни один файл не скачан
          'partial'    — часть скачана, часть нет
          'downloaded' — все скачаны
          'modified'   — хотя бы один изменён локально
          'syncing'    — хотя бы один синхронизируется
          'cloud_only' — если файлов нет
        """
        prefix = folder_path.rstrip("/")
        pattern = prefix + "/%" if prefix else "/%"
        rows = self._conn.execute("""
            SELECT status, COUNT(*) as cnt FROM files
            WHERE cloud_path LIKE ?
              AND cloud_path != ?
              AND type = 'file'
            GROUP BY status
        """, (pattern, folder_path)).fetchall()
        statuses = {r["status"]: r["cnt"] for r in rows}
        if not statuses:
            return "cloud_only"
        if "syncing" in statuses:
            return "syncing"
        if "modified" in statuses:
            return "modified"
        if "cloud_only" in statuses and "downloaded" not in statuses:
            return "cloud_only"
        if "cloud_only" in statuses and "downloaded" in statuses:
            return "partial"
        if "downloaded" in statuses:
            return "downloaded"
        return "cloud_only"

    def get_unsynced_children(self, folder_path: str) -> list[dict]:
        """Вернуть все файлы (не папки) внутри folder_path со статусом != downloaded.

        Используется для: авто-загрузки новых файлов в полностью скачанных папках.
        """
        prefix = folder_path.rstrip("/")
        pattern = prefix + "/%" if prefix else "/%"
        rows = self._conn.execute("""
            SELECT * FROM files
            WHERE cloud_path LIKE ?
              AND cloud_path != ?
              AND type = 'file'
              AND status != 'downloaded'
            ORDER BY cloud_path ASC
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
