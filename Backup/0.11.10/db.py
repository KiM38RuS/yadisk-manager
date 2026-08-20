"""
SQLite-база для отслеживания файлов: какие скачаны, какие изменены.
"""

import os
import json
import logging
import sqlite3
import threading
import time as _time
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

_config_lock = threading.Lock()
# Страховка от зависшего держателя lock (фоновый поток, например
# AllFilesThread в save_config): acquire с таймаутом, чтобы главный
# поток при отрисовке иконок не блокировался навсегда.
_CONFIG_LOCK_TIMEOUT_S = 3.0


def load_config() -> dict:
    """Загрузить конфиг (токен, настройки)."""
    if not _config_lock.acquire(timeout=_CONFIG_LOCK_TIMEOUT_S):
        logger.warning(
            "config.json: lock timeout in load_config (%ss) — using empty config",
            _CONFIG_LOCK_TIMEOUT_S)
        return {}
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        return {}
    finally:
        _config_lock.release()


def save_config(cfg: dict) -> None:
    """Сохранить конфиг в атомарной записи."""
    if not _config_lock.acquire(timeout=_CONFIG_LOCK_TIMEOUT_S):
        logger.warning(
            "config.json: lock timeout in save_config (%ss) — write skipped",
            _CONFIG_LOCK_TIMEOUT_S)
        return
    try:
        # Атомарная запись: сначала во временный файл, потом переименовать
        tmp_path = CONFIG_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        # Windows: os.replace требует право удаления файла-назначения. Если
        # config.json в этот момент открыт другим процессом (второй экземпляр
        # приложения), возникает PermissionError (WinError 32). Повторяем с
        # короткими паузами; при неудаче — не роняем closeEvent, а логируем.
        for attempt in range(3):
            try:
                os.replace(tmp_path, CONFIG_PATH)
                return
            except PermissionError:
                if attempt < 2:
                    _time.sleep(0.05)
        logger.warning(
            "config.json: replace failed (file busy by another process) — "
            "config NOT saved; tmp left at %s", tmp_path)
    finally:
        _config_lock.release()


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

# get_theme() вызывается из _svg_icon при КАЖДОЙ отрисовке иконки
# (десятки-сотни раз за repaint). Кеш на 1с убирает чтение config.json
# и _config_lock из горячего пути — это устраняло фризы UI (дедлок с
# фоновым AllFilesThread, который пишет offset в тот же конфиг).
_theme_cache: dict = {"ts": 0.0, "val": None}
_THEME_CACHE_TTL_S = 1.0


def get_theme() -> str:
    """Вернуть тему: 'system', 'light' или 'dark'. По умолчанию 'system'."""
    now = _time.monotonic()
    cached = _theme_cache["val"]
    if cached is not None and now - _theme_cache["ts"] < _THEME_CACHE_TTL_S:
        return cached
    theme = load_config().get("theme", "system")
    _theme_cache["ts"] = now
    _theme_cache["val"] = theme
    return theme


def set_theme(theme: str) -> None:
    _theme_cache["val"] = None  # инвалидация кеша
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


def get_show_log() -> bool:
    """Показывать ли окно лога при старте."""
    return load_config().get("show_log", False)


def set_show_log(v: bool) -> None:
    cfg = load_config()
    cfg["show_log"] = v
    save_config(cfg)


def get_show_log_on_startup() -> bool:
    """Показывать окно лога при запуске."""
    return load_config().get("show_log_on_startup", False)


def set_show_log_on_startup(v: bool) -> None:
    cfg = load_config()
    cfg["show_log_on_startup"] = v
    save_config(cfg)


def get_check_updates_enabled() -> bool:
    """Авто-проверка обновлений (GitHub Releases). По умолчанию вкл."""
    return load_config().get("check_updates_enabled", True)


def set_check_updates_enabled(v: bool) -> None:
    cfg = load_config()
    cfg["check_updates_enabled"] = v
    save_config(cfg)


def get_update_beta_enabled() -> bool:
    """Обновляться до бета-версий (pre-release с GitHub). По умолчанию выкл."""
    return load_config().get("update_beta_enabled", False)


def set_update_beta_enabled(v: bool) -> None:
    cfg = load_config()
    cfg["update_beta_enabled"] = v
    save_config(cfg)


def get_auto_update_enabled() -> bool:
    """Бесшумные обновления (как Яндекс Диск 3.0). По умолчанию выкл."""
    return load_config().get("auto_update_enabled", False)


def set_auto_update_enabled(v: bool) -> None:
    cfg = load_config()
    cfg["auto_update_enabled"] = v
    save_config(cfg)


def get_log_window_geometry() -> str:
    """Сохранённая геометрия окна лога."""
    return load_config().get("log_window_geometry", "")


def set_log_window_geometry(data: str) -> None:
    cfg = load_config()
    if data:
        cfg["log_window_geometry"] = data
    else:
        cfg.pop("log_window_geometry", None)
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
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._folder_status_cache: dict[str, str] = {}
        self._folder_cache_lock = threading.Lock()
        # Unicode-aware lower() для регистронезависимого поиска по кириллице
        self._conn.create_function("lower_utf8", 1,
                                   lambda s: s.lower() if s else s)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._read_conn = sqlite3.connect(db_path, timeout=5.0, check_same_thread=False)
        self._read_conn.row_factory = sqlite3.Row
        self._read_conn.create_function("lower_utf8", 1,
                                        lambda s: s.lower() if s else s)
        self._read_conn.execute("PRAGMA journal_mode=WAL")
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
        # Миграция/очистка: сбросить зависшие syncing статусы при старте
        # (если приложение упало во время синхронизации)
        self._reset_stale_syncing()
        self._conn.commit()

        # ── производительность: индексы ──────────────────────
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_local_path ON files(local_path)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_type_status ON files(type, status)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_cloud_path ON files(cloud_path)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_cloud_type_status "
            "ON files(cloud_path, type, status)"
        )
        self._conn.commit()

        # ── поиск: история запросов ─────────────────────────
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS search_history (
                query TEXT PRIMARY KEY,
                last_used TEXT DEFAULT (datetime('now'))
            )
        """)
        self._conn.commit()

        # ── pending operations (персистентность между перезапусками) ──
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_ops (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                op_type     TEXT NOT NULL,  -- 'copy', 'move', 'delete', 'rename'
                src_path    TEXT NOT NULL,
                dst_path    TEXT,           -- NULL for delete
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            )
        """)
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
        """CHECK constraint блокировал установку 'syncing'/'deleting'/'unknown'.
        Новые БД уже создаются без CHECK, а старые нужно мигрировать.
        Проверяем все статусы из _VALID_STATUSES; при ошибке пересоздаём таблицу.
        """
        needs_migration = False
        for test_status in self._VALID_STATUSES:
            try:
                tag = f"__migrate_check_{test_status}__"
                self._conn.execute(
                    "INSERT INTO files (cloud_path, name, type, status) "
                    "VALUES (?, ?, 'file', ?)",
                    (tag, tag, test_status),
                )
                self._conn.execute("DELETE FROM files WHERE cloud_path = ?", (tag,))
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                needs_migration = True
                break

        if not needs_migration:
            return

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

    def _reset_stale_syncing(self):
        """Сбросить зависшие статусы 'syncing' при старте приложения.

        Если приложение упало/было убито во время синхронизации,
        файлы остаются в статусе 'syncing'. При следующем запуске
        синхронизация не продолжится автоматически, поэтому
        сбрасываем их в корректный статус:

        - Если local_path есть — файл был скачан до краша → downloaded
        - Если local_path нет — файл не был скачан → cloud_only
        """
        try:
            total = 0
            with self._lock:
                cur = self._conn.execute(
                    "UPDATE files SET status='downloaded', "
                    "updated_at=datetime('now') "
                    "WHERE status='syncing' AND local_path IS NOT NULL"
                )
                dl_count = cur.rowcount
                total += dl_count
                cur2 = self._conn.execute(
                    "UPDATE files SET status='cloud_only', "
                    "local_path=NULL, local_mtime=NULL, "
                    "updated_at=datetime('now') "
                    "WHERE status='syncing' AND local_path IS NULL"
                )
                co_count = cur2.rowcount
                total += co_count
                if total:
                    logger.info("DB init: reset %d stale syncing entries "
                                "(%d with local_path → downloaded, %d without → cloud_only)",
                                total, dl_count, co_count)
                    self._conn.commit()
        except Exception as e:
            logger.error("Failed to reset stale syncing statuses: %s", e)

    # ── CRUD ──────────────────────────────────────────────

    def upsert_files_batch(self, file_list: list[dict]) -> None:
        """
        Массовое обновление/вставка файлов в одной транзакции.
        Значительно быстрее, чем поочерёдный вызов upsert_file() с коммитом после каждого.
        """
        if not file_list:
            return
        now = _now()
        with self._lock:
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

    def sync_children_from_api(self, parent_path: str, api_items: list[dict]) -> None:
        """Синхронизировать БД с ответом API.

        Удаляет из БД файлы/папки, которые есть в parent_path,
        но отсутствуют в api_items (кроме syncing — они ещё загружаются).
        Затем upsert'ит все api_items.

        Предотвращает ситуацию, когда удалённые из облака файлы
        навсегда остаются в БД и показываются при _load_folder_local.
        """
        now = _now()
        api_paths = {item["path"] for item in api_items}
        prefix = parent_path.rstrip("/")
        pattern = prefix + "/%" if prefix else "/%"

        with self._lock:
            self._conn.execute("BEGIN TRANSACTION")
            # === Шаг 1: находим "защищённые" папки — те, у которых есть потомки
            # с local_path IS NOT NULL. Эти папки существуют локально,
            # даже если API их не вернул, и их удалять нельзя.
            protected_rows = self._conn.execute("""\
                SELECT DISTINCT sub_parent.parent_cloud
                FROM (
                    SELECT substr(f.cloud_path, 1,
                        CASE WHEN instr(substr(f.cloud_path, ?), '/') > 0
                             THEN ? + instr(substr(f.cloud_path, ?), '/') - 1
                             ELSE NULL
                        END
                    ) AS parent_cloud
                    FROM files f
                    WHERE f.cloud_path LIKE ?
                      AND f.cloud_path != ?
                      AND f.local_path IS NOT NULL
                      AND f.local_path != ''
                ) sub_parent
            """, (len(prefix) + 2, len(prefix) + 2, len(prefix) + 2,
                  pattern, parent_path)).fetchall()
            protected_dirs = {r[0] for r in protected_rows}

            # === Шаг 2: находим детей для удаления (нет в API, не syncing,
            # нет local_path, не защищённая папка)
            rows = self._conn.execute("""\
                SELECT cloud_path FROM files
                WHERE cloud_path LIKE ?
                  AND cloud_path != ?
                  AND cloud_path NOT LIKE ?
                  AND status != 'syncing'
                  AND (local_path IS NULL OR local_path = '')
            """, (pattern, parent_path, pattern + "/%")).fetchall()
            to_delete = [r["cloud_path"] for r in rows
                         if r["cloud_path"] not in api_paths
                         and r["cloud_path"] not in protected_dirs]
            if to_delete:
                placeholders = ",".join("?" for _ in to_delete)
                self._conn.execute(
                    f"DELETE FROM files WHERE cloud_path IN ({placeholders})",
                    to_delete,
                )
                logger.info("DB: pruned %d stale children from %s (%s)",
                            len(to_delete), parent_path,
                            ", ".join(p.rsplit("/", 1)[-1] for p in to_delete[:5]))
            # Upsert API items
            for item in api_items:
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

        # После prune/upsert: создаём недостающие записи папок,
        # которые существуют локально (есть файлы с local_path),
        # но API их не вернул и БД не содержит.
        # Без этого get_children() не покажет папки в UI.
        self._ensure_missing_dirs(parent_path)

        # Инвалидируем кеш для parent_path и его предков
        self.invalidate_folder_cache(parent_path)

    def _ensure_missing_dirs(self, parent_path: str):
        """Создать записи папок для файлов с local_path, у которых
        родительская папка отсутствует в БД.

        Проходит по всем файлам с local_path, вычисляет их родительские папки
        и для каждой отсутствующей создаёт запись в БД.
        """
        prefix = parent_path.rstrip("/")
        pattern = prefix + "/%" if prefix else "/%"
        with self._lock:
            # Все файлы с local_path под этой папкой
            rows = self._conn.execute("""\
                SELECT cloud_path FROM files
                WHERE cloud_path LIKE ?
                  AND cloud_path != ?
                  AND local_path IS NOT NULL
                  AND local_path != ''
                  AND type = 'file'
            """, (pattern, parent_path)).fetchall()
            needed_dirs: set[str] = set()
            for r in rows:
                parts = r["cloud_path"].rstrip("/").split("/")
                # Идём от parent_path вниз, собираем все поддиректории
                for i in range(len(prefix.split("/")), len(parts) - 1):
                    parent_dir = "/".join(parts[:i + 1])
                    needed_dirs.add(parent_dir)
            if not needed_dirs:
                return
            # Проверяем, какие уже есть в БД
            placeholders = ",".join("?" for _ in needed_dirs)
            existing_rows = self._conn.execute(
                f"SELECT cloud_path FROM files WHERE cloud_path IN ({placeholders})",
                list(needed_dirs)).fetchall()
            existing_set = {r["cloud_path"] for r in existing_rows}
            missing = [d for d in needed_dirs if d not in existing_set]
            if not missing:
                return
            now = _now()
            for d in missing:
                name = d.rsplit("/", 1)[-1]
                self._conn.execute("""\
                    INSERT INTO files (cloud_path, name, type, status, updated_at)
                    VALUES (?, ?, 'dir', 'downloaded', ?)
                    ON CONFLICT(cloud_path) DO NOTHING
                """, (d, name, now))
            self._conn.commit()
            logger.info("DB: created %d missing dir entries under %s", len(missing), parent_path)

    def upsert_file(self, cloud_path: str, name: str, type_: str,
                    size: int = 0, modified: str = "", md5: str = "",
                    mime_type: str = "") -> None:
        """Добавить или обновить запись о файле из данных облака."""
        now = _now()
        with self._lock:
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
        with self._lock:
            self._conn.execute("""
                UPDATE files SET status='downloaded', local_path=?,
                                 local_mtime=?, last_sync_md5=?,
                                 updated_at=?
                WHERE cloud_path=?
            """, (local_path, now, last_sync_md5, now, cloud_path))
            self._conn.commit()
        self.invalidate_folder_cache(cloud_path)

    def set_cloud_only(self, cloud_path: str) -> None:
        """Сбросить статус — файл только в облаке."""
        now = _now()
        with self._lock:
            self._conn.execute("""
                UPDATE files SET status='cloud_only', local_path=NULL,
                                 local_mtime=NULL, updated_at=?
                WHERE cloud_path=?
            """, (now, cloud_path))
            self._conn.commit()
        self.invalidate_folder_cache(cloud_path)

    def set_syncing(self, cloud_path: str, local_path: str,
                    size: int = 0, modified: str = "") -> None:
        """Отметить файл как синхронизирующийся (загружается в облако).

        Вызывается при обнаружении нового локального файла — ДО начала upload-а,
        чтобы он немедленно отобразился в интерфейсе со статусом 'syncing'.
        """
        now = _now()
        name = cloud_path.rstrip("/").split("/")[-1]
        local_mtime = modified or now
        with self._lock:
            self._conn.execute("""
                INSERT INTO files (cloud_path, name, type, size,
                                   modified, local_path, local_mtime,
                                   status, updated_at)
                VALUES (?, ?, 'file', ?, ?, ?, ?, 'syncing', ?)
                ON CONFLICT(cloud_path) DO UPDATE SET
                    status='syncing',
                    local_path=excluded.local_path,
                    local_mtime=excluded.local_mtime,
                    size=excluded.size,
                    modified=excluded.modified,
                    updated_at=excluded.updated_at
            """, (cloud_path, name, size, modified or now,
                  local_path, local_mtime, now))
            self._conn.commit()
        self.invalidate_folder_cache(cloud_path)

    def update_last_sync_md5(self, cloud_path: str, md5: str) -> None:
        """Обновить last_sync_md5 (без изменения статуса)."""
        now = _now()
        with self._lock:
            self._conn.execute("""
                UPDATE files SET last_sync_md5=?, updated_at=?
                WHERE cloud_path=?
            """, (md5, now, cloud_path))
            self._conn.commit()

    _VALID_STATUSES = frozenset({"cloud_only", "downloaded", "modified", "syncing", "unknown", "deleting"})

    def set_status(self, cloud_path: str, status: str) -> None:
        """Установить произвольный статус файла/папки."""
        if status not in self._VALID_STATUSES:
            raise ValueError(f"Invalid status: {status!r}")
        now = _now()
        with self._lock:
            self._conn.execute("""
                UPDATE files SET status=?, updated_at=?
                WHERE cloud_path=?
            """, (status, now, cloud_path))
            self._conn.commit()
        self.invalidate_folder_cache(cloud_path)

    def get_file(self, cloud_path: str) -> Optional[dict]:
        """Получить информацию о файле."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM files WHERE cloud_path=?", (cloud_path,)
            ).fetchone()
        if row:
            return dict(row)
        return None

    def get_files_batch(self, cloud_paths: list[str]) -> dict[str, dict]:
        """Получить записи для списка путей — один запрос (read-only). Возвращает {path: row}."""
        if not cloud_paths:
            return {}
        placeholders = ",".join("?" for _ in cloud_paths)
        rows = self._read_conn.execute(
            f"SELECT * FROM files WHERE cloud_path IN ({placeholders})",
            cloud_paths
        ).fetchall()
        return {r["cloud_path"]: dict(r) for r in rows}

    def get_all_files(self) -> list[dict]:
        """Все отслеживаемые файлы."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM files ORDER BY cloud_path")
        return [dict(r) for r in rows.fetchall()]

    def get_changed_downloaded_files(self) -> list[dict]:
        """Файлы со статусом downloaded, у которых cloud MD5 != last_sync_md5."""
        with self._lock:
            rows = self._conn.execute("""
                SELECT * FROM files
                WHERE status='downloaded'
                  AND md5 IS NOT NULL AND md5 != ''
                  AND last_sync_md5 IS NOT NULL AND last_sync_md5 != ''
                  AND md5 != last_sync_md5
            """)
        return [dict(r) for r in rows.fetchall()]

    def get_by_status(self, status: str) -> list[dict]:
        """Файлы по статусу."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM files WHERE status=?", (status,)
            )
        return [dict(r) for r in rows.fetchall()]

    def count_by_status(self, status: str) -> int:
        """Количество файлов с указанным статусом (легковесный COUNT)."""
        with self._lock:
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
        with self._lock:
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

        Возвращает True только если в папке есть файлы И все они скачаны,
        или если папка пустая и отмечена как downloaded в БД.
        Подпапки не учитываются — только файлы.
        """
        prefix = folder_path.rstrip("/")
        pattern = prefix + "/%" if prefix else "/%"
        row = self._read_conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='downloaded' THEN 1 ELSE 0 END) as synced
            FROM files
            WHERE cloud_path LIKE ?
              AND cloud_path != ?
              AND type = 'file'
        """, (pattern, folder_path)).fetchone()
        if not row or row["total"] == 0:
            # Пустая папка — проверяем её собственный статус
            with self._lock:
                folder = self.get_file(folder_path)
            return folder is not None and folder["status"] == "downloaded"
        return row["total"] == row["synced"]

    def get_folder_aggregate_status(self, folder_path: str) -> str:
        """Агрегированный статус папки по её дочерним файлам.

        Использует in-memory кеш — не ходит в БД для уже известных папок.
        Кеш очищается при изменении статуса файлов в папке.

        Возвращает один из:
          'cloud_only' — ни один файл не скачан
          'partial'    — часть скачана, часть нет
          'downloaded' — все скачаны
          'modified'   — хотя бы один изменён локально
          'syncing'    — хотя бы один синхронизируется
          'cloud_only' — если файлов нет
        """
        with self._folder_cache_lock:
            cached = self._folder_status_cache.get(folder_path)
            if cached is not None:
                return cached
        prefix = folder_path.rstrip("/")
        pattern = prefix + "/%" if prefix else "/%"
        rows = self._read_conn.execute("""
            SELECT status, COUNT(*) as cnt FROM files
            WHERE cloud_path LIKE ?
              AND cloud_path != ?
              AND type = 'file'
            GROUP BY status
        """, (pattern, folder_path)).fetchall()
        statuses = {r["status"]: r["cnt"] for r in rows}
        if not statuses:
            # Нет файлов — проверяем, есть ли запись самой папки (пустая папка)
            folder_row = self._read_conn.execute(
                "SELECT status FROM files WHERE cloud_path=? AND type='dir'",
                (folder_path,)
            ).fetchone()
            result = folder_row["status"] if folder_row else "cloud_only"
        elif "deleting" in statuses:
            result = "deleting"
        elif "syncing" in statuses:
            result = "syncing"
        elif ("cloud_only" in statuses and "downloaded" in statuses) \
             or ("modified" in statuses and "downloaded" in statuses):
            result = "partial"
        elif "modified" in statuses:
            result = "modified"
        elif "cloud_only" in statuses:
            result = "cloud_only"
        elif "downloaded" in statuses:
            result = "downloaded"
        elif "unknown" in statuses:
            result = "unknown"
        else:
            result = "cloud_only"
        with self._folder_cache_lock:
            self._folder_status_cache[folder_path] = result
        return result

    def invalidate_folder_cache(self, cloud_path: str):
        """Сбросить кеш статуса для папки и всех её предков.
        Вызывать после изменения статуса любого файла в папке.
        """
        with self._folder_cache_lock:
            parts = cloud_path.rstrip("/").split("/")
            # Добавляем саму папку и всех предков (кроме корня).
            # parts[0] — пустая строка (ведущий "/"), поэтому len(parts)-1
            # компонент пути; при i == 2 попадает ближайший родитель.
            for i in range(len(parts), 1, -1):
                parent = "/".join(parts[:i]) if i > 1 else "/"
                self._folder_status_cache.pop(parent, None)
            self._folder_status_cache.pop("/", None)

    def prewarm_folder_cache(self, parent_path: str,
                             folder_paths: list[str] | None = None) -> None:
        """Предзаполнить кеш статусов для всех IMMEDIATE подпапок parent_path.

        Выполняет один SQL-запрос вместо N последовательных.
        Если передан folder_paths — все некешированные папки получают 'cloud_only'.
        """
        prefix = parent_path.rstrip("/")
        slash_len = len(prefix) + 1 if prefix else 1
        pattern = prefix + "/%" if prefix else "/%"

        with self._lock:
            rows = self._conn.execute("""
                SELECT cloud_path, status FROM files
                WHERE cloud_path LIKE ?
                  AND cloud_path != ?
                  AND type = 'file'
            """, (pattern, parent_path if parent_path != "/" else "/")).fetchall()

        # Группируем по immediate subfolder
        acc: dict[str, dict[str, int]] = {}
        for r in rows:
            cp = r["cloud_path"]
            rest = cp[slash_len:]
            idx = rest.find("/")
            if idx <= 0:
                continue
            sub = (prefix + "/" + rest[:idx]) if prefix else ("/" + rest[:idx])
            if sub not in acc:
                acc[sub] = {}
            cnt = acc[sub]
            s = r["status"]
            cnt[s] = cnt.get(s, 0) + 1

        with self._folder_cache_lock:
            for folder, st in acc.items():
                if "syncing" in st:
                    self._folder_status_cache[folder] = "syncing"
                elif "cloud_only" in st and "downloaded" not in st:
                    self._folder_status_cache[folder] = "cloud_only"
                elif "cloud_only" in st:
                    self._folder_status_cache[folder] = "partial"
                else:
                    self._folder_status_cache[folder] = "downloaded"
            # Для папок, в которых нет файлов — кешируем cloud_only
            if folder_paths:
                for fp in folder_paths:
                    if fp not in self._folder_status_cache:
                        self._folder_status_cache[fp] = "cloud_only"

    def get_folder_batch_aggregate_status(self, parent_path: str,
                                          folder_paths: list[str]) -> dict[str, str]:
        """Агрегированный статус для нескольких папок — один SQL-запрос (быстрый).

        Возвращает {folder_path: status} для всех переданных folder_paths
        под parent_path. Заменяет 20+ вызовов get_folder_aggregate_status.
        Использует INDEXED BY + диапазон вместо LIKE для покрывающего индекса.
        """
        if not folder_paths:
            return {}

        # Строим UNION ALL с range-условиями вместо LIKE
        parts = []
        for fp in folder_paths:
            lo = fp.rstrip("/") + "/"
            hi = fp.rstrip("/") + "0"  # '/' < '0' в ASCII, покрывает все после слеша
            parts.append(
                "SELECT ? as f, status, COUNT(*) as cnt "
                "FROM files INDEXED BY idx_files_cloud_type_status "
                "WHERE cloud_path >= ? AND cloud_path < ? AND type = 'file' "
                "GROUP BY status"
            )
        sql = " UNION ALL ".join(parts)
        # Параметры: для каждой папки (fp, lo, hi)
        params = []
        for fp in folder_paths:
            lo = fp.rstrip("/") + "/"
            hi = fp.rstrip("/") + "0"
            params.extend([fp, lo, hi])

        rows = self._read_conn.execute(sql, params).fetchall()

        # Группируем результаты: {folder: {status: count}}
        acc: dict[str, dict[str, int]] = {}
        for r in rows:
            sub = r["f"]
            s = r["status"]
            c = r["cnt"]
            if sub not in acc:
                acc[sub] = {}
            acc[sub][s] = acc[sub].get(s, 0) + c

        result: dict[str, str] = {}
        for folder in folder_paths:
            st = acc.get(folder)
            if st is None:
                result[folder] = "cloud_only"
            elif "deleting" in st:
                result[folder] = "deleting"
            elif "syncing" in st:
                result[folder] = "syncing"
            elif ("cloud_only" in st and "downloaded" in st) \
                 or ("modified" in st and "downloaded" in st):
                result[folder] = "partial"
            elif "modified" in st:
                result[folder] = "modified"
            elif "cloud_only" in st:
                result[folder] = "cloud_only"
            elif "unknown" in st:
                result[folder] = "unknown"
            else:
                result[folder] = "downloaded"
        return result

    def get_unsynced_children(self, folder_path: str) -> list[dict]:
        """Вернуть все файлы (не папки) внутри folder_path со статусом != downloaded.

        Используется для: авто-загрузки новых файлов в полностью скачанных папках.
        """
        prefix = folder_path.rstrip("/")
        pattern = prefix + "/%" if prefix else "/%"
        with self._lock:
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
        with self._lock:
            self._conn.execute("DELETE FROM files WHERE cloud_path=?", (cloud_path,))
            self._conn.commit()
        self.invalidate_folder_cache(cloud_path)

    def remove_children(self, parent_path: str) -> list[str]:
        """Удалить все файлы/папки внутри parent_path (рекурсивно).
        Возвращает список удалённых cloud_path."""
        pattern = parent_path.rstrip("/") + "/%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT cloud_path FROM files WHERE cloud_path LIKE ?",
                (pattern,)).fetchall()
            paths = [r["cloud_path"] for r in rows]
            if paths:
                self._conn.execute(
                    "DELETE FROM files WHERE cloud_path LIKE ?", (pattern,))
                self._conn.commit()
        self.invalidate_folder_cache(parent_path)
        return paths

    def set_status_batch(self, cloud_paths: list[str], status: str) -> None:
        """Установить статус для списка файлов одним запросом."""
        if not cloud_paths:
            return
        if status not in self._VALID_STATUSES:
            raise ValueError(f"Invalid status: {status!r}")
        now = _now()
        with self._lock:
            # SQLite limitation: max 999 vars, chunk if needed
            for i in range(0, len(cloud_paths), 500):
                chunk = cloud_paths[i:i + 500]
                placeholders = ",".join("?" for _ in chunk)
                self._conn.execute(
                    f"UPDATE files SET status=?, updated_at=? WHERE cloud_path IN ({placeholders})",
                    [status, now] + chunk)
            self._conn.commit()

    def file_exists(self, cloud_path: str) -> bool:
        """Проверить, есть ли запись."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM files WHERE cloud_path=?", (cloud_path,)
            ).fetchone()
        return row is not None

    def file_exists_batch(self, cloud_paths: list[str]) -> set[str]:
        """Проверить наличие записей для списка путей — один запрос (read-only)."""
        if not cloud_paths:
            return set()
        placeholders = ",".join("?" for _ in cloud_paths)
        rows = self._read_conn.execute(
            f"SELECT cloud_path FROM files WHERE cloud_path IN ({placeholders})",
            cloud_paths
        ).fetchall()
        return {r["cloud_path"] for r in rows}

    def get_by_local_path(self, local_path: str) -> Optional[dict]:
        """Найти запись по локальному пути."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM files WHERE local_path=?", (local_path,)
            ).fetchone()
        return dict(row) if row else None

    def get_by_local_path_prefix(self, prefix: str) -> list[dict]:
        """Найти все файлы, чей локальный путь начинается с префикса."""
        prefix_norm = prefix.replace("\\", "/")
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM files WHERE local_path IS NOT NULL"
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            lp = (d.get("local_path") or "").replace("\\", "/")
            if lp.startswith(prefix_norm):
                result.append(d)
        return result

    def search_by_name(self, query: str, limit: int = 200) -> list[dict]:
        """Глобальный поиск файлов/папок по имени (LIKE %query%).

        Результаты ранжируются по релевантности: точное совпадение имени >
        имя начинается с запроса > имя содержит запрос; при равном типе
        совпадения короче имя — выше. Папки немного выше файлов.
        """
        pattern = f"%{query}%"
        q_lower = query.lower()
        with self._lock:
            rows = self._conn.execute(
                "SELECT cloud_path, name, type, size, modified, md5, mime_type, status, local_path "
                "FROM files WHERE lower_utf8(name) LIKE lower_utf8(?) "
                "ORDER BY name ASC LIMIT ?",
                (pattern, limit * 2),
            ).fetchall()
        results = [dict(r) for r in rows]

        def _rank(item: dict) -> float:
            name = item["name"].lower()
            if name == q_lower:
                score = 100.0
            elif name.startswith(q_lower):
                score = 60.0
            else:
                score = 30.0
            # Чем короче имя — тем точнее совпадение
            score -= min(len(item["name"]), 100) * 0.1
            # Папки чуть выше файлов (как раньше type DESC)
            if item["type"] == "dir":
                score += 5.0
            return score

        results.sort(key=_rank, reverse=True)
        return results[:limit]

    # ── Search history ──────────────────────────────────────

    SEARCH_HISTORY_LIMIT = 30

    def add_search_query(self, query: str) -> None:
        """Сохранить запрос в историю поиска. Дубликаты обновляют timestamp.
        Старые записи сверх лимита удаляются.

        last_used пишется с миллисекундами: SQLite datetime('now') имеет
        точность 1с — несколько запросов за одну секунду не сортировались
        бы по времени и в истории стояли бы в произвольном порядке.
        """
        q = query.strip()
        if not q:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with self._lock:
            self._conn.execute(
                "INSERT INTO search_history (query, last_used) VALUES (?, ?) "
                "ON CONFLICT(query) DO UPDATE SET last_used=excluded.last_used",
                (q, now),
            )
            # Оставить только последние SEARCH_HISTORY_LIMIT запросов
            self._conn.execute("""
                DELETE FROM search_history WHERE query NOT IN (
                    SELECT query FROM search_history
                    ORDER BY last_used DESC LIMIT ?
                )
            """, (self.SEARCH_HISTORY_LIMIT,))
            self._conn.commit()

    def get_search_history(self, limit: int = 5) -> list[str]:
        """Последние N запросов поиска."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT query FROM search_history ORDER BY last_used DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r["query"] for r in rows]

    def clear_search_history(self) -> None:
        """Очистить всю историю поиска."""
        with self._lock:
            self._conn.execute("DELETE FROM search_history")
            self._conn.commit()

    # ── миграция после move/copy ─────────────────────────

    def migrate_path(self, old_path: str, new_path: str) -> int:
        """Обновить cloud_path всех записей, перемещённых из old_path в new_path.

        Вызывается после успешного cloud-перемещения папки/файла, чтобы
        БД осталась консистентной (сохранить local_path и статусы).
        Возвращает количество обновлённых строк.
        """
        old = old_path.rstrip("/")
        new_p = new_path.rstrip("/")
        with self._lock:
            # Сама папка/файл
            self._conn.execute(
                "UPDATE files SET cloud_path=?, name=?, updated_at=datetime('now') "
                "WHERE cloud_path=?",
                (new_p, os.path.basename(new_p), old),
            )
            fold_count = self._conn.rowcount
            # Все дети (для папки)
            self._conn.execute(
                "UPDATE files SET cloud_path=? || substr(cloud_path, ?), "
                "updated_at=datetime('now') WHERE cloud_path LIKE ?",
                (new_p + "/", len(old) + 2, old + "/%"),
            )
            child_count = self._conn.rowcount
            self._conn.commit()
        self.invalidate_folder_cache(old)
        self.invalidate_folder_cache(new_p)
        return fold_count + child_count

    def close(self):
        self._conn.close()

    # ── Pending operations (персистентность) ─────────────────

    def save_pending_op(self, op_type: str, src_path: str, dst_path: str | None = None) -> int:
        """Сохранить операцию в pending_ops. Возвращает id записи."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO pending_ops (op_type, src_path, dst_path) VALUES (?, ?, ?)",
                (op_type, src_path, dst_path),
            )
            self._conn.commit()
            return cur.lastrowid

    def complete_pending_op(self, op_id: int, status: str = "done") -> None:
        """Пометить операцию как завершённую/ошибочную."""
        with self._lock:
            self._conn.execute(
                "UPDATE pending_ops SET status=?, updated_at=datetime('now') WHERE id=?",
                (status, op_id),
            )
            self._conn.commit()

    def get_pending_ops(self) -> list[dict]:
        """Вернуть все незавершённые операции."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pending_ops WHERE status='pending' ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def remove_pending_op(self, op_id: int) -> None:
        """Удалить запись об операции."""
        with self._lock:
            self._conn.execute("DELETE FROM pending_ops WHERE id=?", (op_id,))
            self._conn.commit()

