"""
Фоновые потоки (QThread) для YaDisk Manager:
- _WorkerThread, _SyncThread, _ApiListThread, _AllFilesThread
- _FolderDownloadThread, _RecentFilesThread, _SearchThread
- _MetaFetchThread, _StartupScanThread, _AutoDownloadThread, _DropUploadThread
"""

import hashlib
import logging
import os
import shutil
from datetime import datetime, timezone

from PySide6.QtCore import QThread, Signal

import db
import disk_api
import sync
from ui_shared import _local_path, _md5_file, _is_windows_reserved

logger = logging.getLogger("ui.threads")


class WorkerThread(QThread):
    """Поток для воркера (скачивание/загрузка с прогрессом).

    Воркер перемещается в этот поток (для корректной работы сигналов),
    но run() воркера вызывается напрямую из QThread.run().
    """

    def __init__(self, worker):
        super().__init__()
        self._worker = worker
        worker.moveToThread(self)

    def run(self):
        self._worker.run()

    def cancel(self):
        self._worker.cancel()
        self.quit()
        self.wait(3000)


class SyncThread(QThread):
    """Фоновый поток для полной синхронизации."""
    finished = Signal(dict)
    auth_error = Signal(str)

    def __init__(self, api, database, cache_dir, parent=None):
        super().__init__(parent)
        self._api = api
        self._database = database
        self._cache_dir = cache_dir

    def cancel(self):
        self.quit()
        self.wait(3000)

    def run(self):
        logger.info("SyncThread: starting full sync...")
        try:
            result = sync.full_sync(self._api, self._database,
                                    self._cache_dir)
            logger.info("SyncThread: done -> %s", result)
            self.finished.emit(result)
        except disk_api.AuthError as e:
            logger.error("SyncThread auth error: %s", e)
            self.auth_error.emit(str(e))
        except Exception as e:
            logger.error("SyncThread error: %s", e)
            self.finished.emit({"matched": 0, "uploaded": 0,
                                "downloaded": 0, "moved": 0,
                                "deleted": 0})


class ApiListThread(QThread):
    """Поток для фонового запроса списка файлов/папок с API."""
    finished = Signal(list, str)  # items, error_msg

    def __init__(self, api, path: str, parent=None):
        super().__init__(parent)
        self._api = api
        self._path = path

    def run(self):
        logger.info("ApiListThread: fetching %s...", self._path)
        try:
            items = self._api.list_folder(self._path)
            logger.info("ApiListThread: %s -> %d items", self._path, len(items))
            self.finished.emit(items, "")
        except Exception as e:
            logger.error("ApiListThread: %s FAILED: %s", self._path, e)
            self.finished.emit([], str(e))


class DbChildrenThread(QThread):
    """Фоновое чтение подпапок из БД: уровень 1 + наличие подпапок (стрелки).

    Мгновенная альтернатива ApiListThread для раскрытия дерева: локальный
    SQLite-запрос вместо сетевого. Объекты возвращаются как есть:
    finished(list[dict], set[str], str)
    """
    finished = Signal(list, object, str)  # folders, has_subdirs, error_msg

    def __init__(self, database, parent_path: str, parent=None):
        super().__init__(parent)
        self._database = database
        self._parent_path = parent_path

    def run(self):
        try:
            folders, has_subdirs = self._database.get_dir_tree_levels(self._parent_path)
            self.finished.emit(folders, has_subdirs, "")
        except Exception as e:
            logger.error("DbChildrenThread: %s FAILED: %s", self._parent_path, e)
            self.finished.emit([], set(), str(e))


class DirPathsThread(QThread):
    """Фоновое чтение всех облачных путей папок (автодополнение адресной строки)."""
    finished = Signal(list, str)  # paths, error_msg

    def __init__(self, database, parent=None):
        super().__init__(parent)
        self._database = database

    def run(self):
        try:
            self.finished.emit(self._database.get_all_dir_paths(), "")
        except Exception as e:
            logger.error("DirPathsThread FAILED: %s", e)
            self.finished.emit([], str(e))


class FolderLoadThread(QThread):
    """Фоновый поток: список папки (API) + синхронизация БД + сбор данных таблицы.

    Объединяет все SQLite-операции, которые раньше выполнялись в главном
    потоке (sync_children_from_api, get_children, nav-fix с md5, lazy reverse
    проверку) — на БД с 100К+ файлами это вызывало FREEZE UI 0.5–1.5с
    при старте и навигации.

    use_api=True  — запросить список с API, синхронизировать БД, собрать items
    use_api=False — только БД + файловая система (навигация по локальному кешу)

    finished(path, items, fixed_paths, reverse_checks, error_msg)
      path           — облачный путь
      items          — записи, готовые к отображению (list[dict])
      fixed_paths    — cloud_path файлов, найденных локально (статус исправлен → upload)
      reverse_checks — list[(cloud_path, local_path, last_sync_md5)] для reverse sync
      error_msg      — "" при успехе
    """
    finished = Signal(str, list, list, list, str)

    def __init__(self, api, database, path: str, use_api: bool = True,
                 recently_deleted=None, syncing=None, parent=None):
        super().__init__(parent)
        self._api = api
        self._database = database
        self._path = path
        self._use_api = use_api
        self._recently_deleted = set(recently_deleted or ())
        self._syncing = set(syncing or ())

    def run(self):
        try:
            if self._use_api:
                items = self._api.list_folder(self._path)
                # Фильтруем пути, которые пользователь только что удалил
                if self._recently_deleted:
                    items = [it for it in items
                             if it.get("path", "").rstrip("/") not in self._recently_deleted
                             and not any(d in it.get("path", "")
                                         for d in self._recently_deleted)]
                try:
                    self._database.sync_children_from_api(self._path, items)
                except Exception as e:
                    logger.error("sync_children_from_api failed: %s — fallback to upsert only", e)
                    self._database.upsert_files_batch(items)

            records = self._database.get_children(self._path)

            # Исправление статуса: файлы, числящиеся cloud_only, но присутствующие
            # локально (созданы программами, восстановлены вручную и т.п.).
            fixed_paths: list[str] = []
            for r in records:
                if r["type"] != "file":
                    continue
                status = r["status"] or ""
                if status != "cloud_only":
                    continue
                lp = r.get("local_path") or ""
                if not lp or not os.path.exists(lp):
                    # Пробуем вычислить локальный путь из cloud_path
                    lp = _local_path(r["cloud_path"])
                    if not lp or not os.path.exists(lp):
                        continue
                # Файл есть на диске, но в БД как cloud_only — исправляем
                try:
                    local_md5 = _md5_file(lp)
                except OSError:
                    continue
                self._database.set_downloaded(r["cloud_path"], lp, local_md5)
                fixed_paths.append(r["cloud_path"])
                logger.info("Nav fix: %s → downloaded (found locally)", r["cloud_path"])

            # Повторно читаем после возможных исправлений
            if fixed_paths:
                records = self._database.get_children(self._path)

            items = [{
                "path": r["cloud_path"],
                "name": r["name"],
                "type": r["type"],
                "size": r["size"],
                "modified": r["modified"],
                "md5": r["md5"] or "",
                "mime_type": r["mime_type"] or "",
                # stale syncing — статус неизвестен (файл может быть в облаке
                # или локально, просто не успели определить)
                "status": "unknown" if (r["status"] == "syncing"
                                        and r["cloud_path"] not in self._syncing)
                          else (r["status"] or ""),
                "local_path": r["local_path"] or "",
            } for r in records if not _is_windows_reserved(r["name"])]

            # Для папок сбрасываем статус — он будет вычислен агрегатно по детям
            # в set_path(). Сырой статус записи папки в БД не отражает детей.
            for item in items:
                if item["type"] == "dir":
                    item["status"] = ""

            # Lazy reverse sync: проверяем downloaded файлы в этой папке
            reverse_checks: list[tuple] = []
            for item in items:
                if item["type"] != "file" or item["status"] != "downloaded":
                    continue
                rec = self._database.get_file(item["path"])
                if not rec:
                    continue
                cloud_md5 = rec.get("md5") or ""
                last_sync = rec.get("last_sync_md5") or ""
                if cloud_md5 and last_sync and cloud_md5 != last_sync:
                    lp = item.get("local_path") or _local_path(item["path"])
                    if os.path.exists(lp):
                        reverse_checks.append((item["path"], lp, last_sync))

            self.finished.emit(self._path, items, fixed_paths, reverse_checks, "")
        except Exception as e:
            logger.error("FolderLoadThread: %s FAILED: %s", self._path, e)
            self.finished.emit(self._path, [], [], [], str(e))


class AllFilesThread(QThread):
    """Фоновый поток: загрузка ВСЕХ файлов с Диска постранично с сохранением прогресса.

    Фичи:
    - Постраничная загрузка (по 200 файлов)
    - Сохранение offset в config.json после каждой страницы
    - Авто-ретрай при обрыве (до 5 попыток, 3 сек пауза)
    - Докачка после перезапуска программы
    - Прогресс в статус-бар (через signal)
    """
    finished = Signal(int)        # всего загружено файлов (счётчик), 0 = неудача
    progress = Signal(int, int)   # offset, текущее количество

    PAGE_LIMIT = 200
    MAX_RETRIES = 5
    RETRY_DELAY_MS = 3000

    def __init__(self, api, database, parent=None):
        super().__init__(parent)
        self._api = api
        self._database = database
        self._stop_requested = False

    def request_stop(self):
        """Попросить поток остановиться (вызывать из главного потока)."""
        self._stop_requested = True

    def run(self):
        offset = db.get_all_files_offset()
        loaded = 0
        retries = 0

        logger.info("AllFilesThread: loading all files (resume offset=%d)...", offset)

        while True:
            if self._stop_requested:
                logger.info("AllFilesThread: stop requested")
                db.set_all_files_offset(offset)
                self.finished.emit(loaded)
                return

            try:
                page = self._api.get_all_files_page(self.PAGE_LIMIT, offset)
            except Exception as e:
                retries += 1
                logger.error(
                    "AllFilesThread: error at offset=%d (retry %d/%d): %s",
                    offset, retries, self.MAX_RETRIES, e,
                )
                # Сохраняем прогресс
                db.set_all_files_offset(offset)

                if retries >= self.MAX_RETRIES:
                    logger.error("AllFilesThread: max retries exceeded")
                    self.finished.emit(loaded)
                    return

                # Ждём перед повтором (в фоновом потоке — не блокирует GUI)
                self.msleep(self.RETRY_DELAY_MS)
                continue  # повтор с тем же offset

            # Пустая страница = конец списка
            if not page:
                break

            # Сохраняем в БД
            try:
                self._database.upsert_files_batch(page)
            except Exception as e:
                logger.error("AllFilesThread: DB insert failed: %s", e)
                self.finished.emit(loaded)
                return

            loaded += len(page)
            offset += self.PAGE_LIMIT
            retries = 0  # удачный запрос сбрасывает счётчик ретраев

            # Сохраняем прогресс
            db.set_all_files_offset(offset)
            self.progress.emit(offset, loaded)

        # Успех — все файлы загружены, сбрасываем offset
        db.set_all_files_offset(0)
        logger.info("AllFilesThread: %d files loaded", loaded)
        self.finished.emit(loaded)


class FolderDownloadThread(QThread):
    """Фоновый поток: BFS-обход папки, сбор файлов в БД, классификация для toggle.

    download_mode=True: собирает только файлы для скачивания (cloud_only, нет локально)
    download_mode=False: собирает только файлы для удаления (downloaded)
    """

    finished = Signal(list, list)  # to_download: list[str], to_remove: list[str]

    def __init__(self, api, database, folder_paths: list[str],
                 download_mode: bool = True, parent=None):
        super().__init__(parent)
        self._api = api
        self._database = database
        self._folder_paths = folder_paths
        self._download_mode = download_mode

    def run(self):
        logger.info("FolderDownloadThread: BFS %d folder(s)...", len(self._folder_paths))
        all_items: list[dict] = []
        try:
            from collections import deque
            for root in self._folder_paths:
                queue = deque([root])
                while queue:
                    path = queue.popleft()
                    try:
                        items = self._api.list_folder(path)
                    except Exception as e:
                        logger.error("FolderDownloadThread: failed to list %s: %s",
                                     path, e)
                        continue
                    all_items.extend(items)
                    for item in items:
                        if item["type"] == "dir":
                            queue.append(item["path"])
            if all_items:
                # Upsert в БД (обновляем метаданные + регистрируем новые файлы)
                self._database.upsert_files_batch(all_items)
                # Кешируем исходные статусы ДО установки syncing
                pre_status: dict[str, str] = {}
                for item in all_items:
                    if item["type"] != "file":
                        continue
                    try:
                        db_info = self._database.get_file(item["path"])
                        pre_status[item["path"]] = (
                            db_info["status"] if db_info else "cloud_only"
                        )
                    except Exception:
                        pre_status[item["path"]] = "cloud_only"
                logger.info("FolderDownloadThread: %d items collected", len(all_items))
                # Классифицируем из кешированных исходных статусов,
                # а не из БД (там может быть syncing, если _toggle_local_copy
                # уже тронул статус папки до запуска этого потока).
                to_download: list[str] = []
                to_remove: list[str] = []
                for item in all_items:
                    if item["type"] != "file":
                        continue
                    status = pre_status.get(item["path"], "cloud_only")
                    if self._download_mode and status == "cloud_only":
                        to_download.append(item["path"])
                    elif not self._download_mode and status == "downloaded":
                        to_remove.append(item["path"])
                # Ставим syncing ТОЛЬКО для файлов, которые реально будут
                # обработаны. Остальные оставляем с их исходным статусом —
                # иначе скачанные файлы зависнут в syncing после BFS.
                for cp in to_download:
                    self._database.set_status(cp, "syncing")
                for cp in to_remove:
                    self._database.set_status(cp, "syncing")
                self.finished.emit(to_download, to_remove)
            else:
                self.finished.emit([], [])
        except Exception as e:
            logger.error("FolderDownloadThread failed: %s", e)
            self.finished.emit([], [])


class RecentFilesThread(QThread):
    """Фоновый поток для загрузки недавно изменённых файлов (быстрый polling)."""
    finished = Signal(list)

    def __init__(self, api, db, parent=None):
        super().__init__(parent)
        self._api = api
        self._db = db

    def run(self):
        try:
            items = self._api.get_recent_uploaded(limit=50)
            self.finished.emit(items)
        except Exception:
            self.finished.emit([])


class SearchThread(QThread):
    """Фоновый поток для глобального поиска по БД (по имени)."""
    finished = Signal(str, list)  # (query, results)

    def __init__(self, database, query, parent=None):
        super().__init__(parent)
        self._db = database
        self._query = query

    def run(self):
        try:
            results = self._db.search_by_name(self._query)
            self.finished.emit(self._query, results)
        except Exception:
            self.finished.emit(self._query, [])


class MetaFetchThread(QThread):
    """Фоновый поток для запроса метаданных файла в облаке + local MD5."""
    finished = Signal(dict)

    def __init__(self, api, cloud_path, local_path="", parent=None):
        super().__init__(parent)
        self._api = api
        self._cloud_path = cloud_path
        self._local_path = local_path

    def run(self):
        local_md5 = ""
        if self._local_path:
            try:
                local_md5 = self._md5_file(self._local_path)
            except Exception:
                pass
        try:
            meta = self._api.get_meta(self._cloud_path)
            self.finished.emit({
                "cloud_md5": meta.get("md5", ""),
                "cloud_modified": meta.get("modified", ""),
                "cloud_size": meta.get("size", 0),
                "cloud_path": self._cloud_path,
                "local_md5": local_md5,
            })
        except Exception as e:
            err_str = str(e)
            if "404" in err_str:
                logger.debug("MetaFetchThread (404, expected): %s — %s", self._cloud_path, e)
            else:
                logger.warning("MetaFetchThread: %s — %s", self._cloud_path, e)
            self.finished.emit({
                "cloud_md5": "",
                "cloud_modified": "",
                "cloud_size": 0,
                "cloud_path": self._cloud_path,
                "local_md5": local_md5,
            })

    @staticmethod
    def _md5_file(filepath: str, chunk_size: int = 64 * 1024) -> str:
        import hashlib
        h = hashlib.md5()
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()


class StartupScanThread(QThread):
    """Фоновый поток для стартового сканирования: stale, changed, new files + полная обработка."""
    # Сигналы для разных этапов обработки
    progress = Signal(str, int)  # message, count
    finished = Signal()  # завершение всей обработки
    auth_error = Signal(str)  # ошибка авторизации
    network_error = Signal()  # нет интернета

    def __init__(self, api, database, cache_dir, parent=None):
        super().__init__(parent)
        self._api = api
        self._database = database
        self._cache_dir = cache_dir
        self._stop_requested = False

    def request_stop(self):
        self._stop_requested = True

    def run(self):
        logger.info("StartupScanThread: scanning local cache...")
        stale: set[str] = set()
        changed: set[str] = set()
        new_files: set[str] = set()

        try:
            # 1. All downloaded files from DB — check existence + md5
            for rec in self._database.get_by_status("downloaded"):
                if self._stop_requested:
                    return
                try:
                    local_path = rec.get("local_path") or self._local_path(rec["cloud_path"])
                    if not os.path.exists(local_path):
                        stale.add(rec["cloud_path"])
                        continue
                    local_md5 = self._md5_file(local_path)
                    last_sync_md5 = rec.get("last_sync_md5") or rec.get("md5") or ""
                    if last_sync_md5 and local_md5 != last_sync_md5:
                        changed.add(rec["cloud_path"])
                except Exception:
                    continue

            # 2. New files in cache not tracked in DB
            cache_dir_norm = self._cache_dir.replace("\\", "/")
            known_local = set()
            for rec in self._database.get_by_status("downloaded"):
                lp = rec.get("local_path") or self._local_path(rec["cloud_path"])
                if lp:
                    known_local.add(lp.replace("\\", "/").lower())

            for root, _dirs, files in os.walk(self._cache_dir):
                if self._stop_requested:
                    return
                for fname in files:
                    if fname.startswith(".") or fname.startswith("~") or fname.endswith("~") or fname.endswith(".tmp"):
                        continue
                    fpath = os.path.join(root, fname).replace("\\", "/")
                    if fpath.lower() in known_local:
                        continue
                    new_files.add(fpath)

        except Exception as e:
            logger.error("StartupScanThread error: %s", e)

        # 3. Directories on disk not tracked in DB
        new_dirs: set[str] = set()
        cache_dir_norm = self._cache_dir.replace("\\", "/")
        for root, dirs, _files in os.walk(self._cache_dir):
            if self._stop_requested:
                return
            for d in dirs:
                if d.startswith(".") or d == "__pycache__":
                    continue
                dpath = os.path.join(root, d).replace("\\", "/")
                rel = dpath[len(cache_dir_norm):].lstrip("/")
                cloud_path = "/" + rel
                existing = self._database.get_file(cloud_path)
                if not existing:
                    new_dirs.add(dpath)

        logger.info("StartupScanThread: stale=%d, changed=%d, new=%d, new_dirs=%d",
                     len(stale), len(changed), len(new_files), len(new_dirs))
        
        # Обрабатываем результаты в этом же потоке (без блокировки UI)
        self._process_results(stale, changed, new_files, new_dirs)
        
        self.finished.emit()

    def _process_results(self, stale: set, changed: set, new_files: set, new_dirs: set):
        """Обработка результатов сканирования в фоне."""
        # Проверяем, был ли кеш восстановлен (флаг устанавливается в MainWindow._check_cache_dir)
        # Флаг хранится в экземпляре БД как атрибут
        cache_was_restored = getattr(self._database, '_cache_was_restored', False)
        
        if cache_was_restored and stale:
            # Кеш был удалён, пользователь выбрал "Восстановить" —
            # ставим все файлы на перекачку
            logger.info("Restore: queuing %d files for re-download", len(stale))
            for cp in stale:
                if self._stop_requested:
                    return
                self._database.set_status(cp, "syncing")
        else:
            # Stale — обновляем статус
            for cp in stale:
                if self._stop_requested:
                    return
                self._database.set_cloud_only(cp)
                logger.info("Startup: local file missing, set cloud_only: %s", cp)

        # Changed — ставим на загрузку
        if changed:
            logger.info("Startup: %d local files changed, will upload", len(changed))
        for cp in changed:
            if self._stop_requested:
                return
            info = self._database.get_file(cp)
            if info and info.get("local_path") and os.path.exists(info["local_path"]):
                # Вычисляем MD5 и обновляем БД
                local_md5 = self._md5_file(info["local_path"])
                self._database.set_downloaded(cp, info["local_path"], local_md5)
            # Если файл не найден локально — пропускаем (будет обработан при старте воркеров)

        # Новые папки на диске — создаём записи в БД
        if new_dirs:
            cache_dir_norm = self._cache_dir.replace("\\", "/")
            dir_count = 0
            for dpath in new_dirs:
                if self._stop_requested:
                    return
                rel = dpath[len(cache_dir_norm):].lstrip("/")
                cloud_path = "/" + rel
                name = cloud_path.rstrip("/").split("/")[-1]
                # Создаём все родительские папки
                parts = cloud_path.strip("/").split("/")
                for i in range(1, len(parts) + 1):
                    parent = "/" + "/".join(parts[:i])
                    if not self._database.get_file(parent):
                        pname = parent.rstrip("/").split("/")[-1]
                        self._database.upsert_file(parent, pname, "dir")
                        self._database.set_status(parent, "downloaded")
                # Отмечаем как downloaded
                self._database.upsert_file(cloud_path, name, "dir")
                self._database.set_downloaded(cloud_path, dpath)
                dir_count += 1
            logger.info("Startup: registered %d local directories", dir_count)

        # New files — вычисляем cloud_path и регистрируем в БД
        cache_dir_norm = self._cache_dir.replace("\\", "/")
        new_count = len(new_files)
        if new_count:
            logger.info("Startup: %d new local files detected", new_count)
        for fpath in new_files:
            if self._stop_requested:
                return
            rel = fpath[len(cache_dir_norm):].lstrip("/")
            cloud_path = "/" + rel
            existing = self._database.get_file(cloud_path)
            if existing:
                # Файл уже есть в облаке и в БД (cloud_only после
                # «Оставить только в облаке»). Пользователь вручную
                # скопировал файл обратно — просто отмечаем как downloaded.
                local_md5 = self._md5_file(fpath)
                self._database.set_downloaded(cloud_path, fpath, local_md5)
                logger.info("Startup: restored local copy (was %s): %s",
                            existing["status"], cloud_path)
            else:
                # Нет записи в БД — файл был удалён из облака через Delete,
                # потом вручную восстановлен локально. Регистрируем как
                # downloaded (с local_path), чтобы он отображался в интерфейсе.
                try:
                    st = os.stat(fpath)
                    size = st.st_size
                    modified = datetime.fromtimestamp(
                        st.st_mtime, tz=timezone.utc).isoformat()
                    local_md5 = self._md5_file(fpath)
                except OSError:
                    size = 0
                    modified = ""
                    local_md5 = ""
                name = cloud_path.rstrip("/").split("/")[-1]
                self._database.upsert_file(cloud_path, name, 'file',
                                           size=size, modified=modified, md5=local_md5)
                self._database.set_downloaded(cloud_path, fpath, last_sync_md5=local_md5)
                logger.info("Startup: registered local file as downloaded (no cloud copy): %s",
                            cloud_path)

    def _local_path(self, cloud_path: str) -> str:
        """Compute local path from cloud path, using configured cache dir."""
        rel = cloud_path.lstrip("/").replace("/", "\\")
        return os.path.join(self._cache_dir, rel)

    @staticmethod
    def _md5_file(filepath: str, chunk_size: int = 64 * 1024) -> str:
        h = hashlib.md5()
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()


class AutoDownloadThread(QThread):
    """Фоновый поток для поиска cloud_only-файлов в полностью скачанных папках.

    Определяет, какие файлы нужно автоматически скачать, и возвращает
    список cloud_path-ов на главный поток (который запускает скачивание).
    """
    finished = Signal(list)  # list[str] — cloud_paths для скачивания

    def __init__(self, database, parent=None):
        super().__init__(parent)
        self._database = database

    def run(self):
        logger.info("AutoDownloadThread: analyzing folders...")
        try:
            # Быстрый выход: нет скачанных файлов → нет synced-папок
            if self._database.count_by_status("downloaded") == 0:
                self.finished.emit([])
                return

            cloud_only = self._database.get_by_status("cloud_only")
            parents: dict[str, list[str]] = {}
            for rec in cloud_only:
                if rec.get("type") != "file":
                    continue
                cp = rec["cloud_path"]
                parts = cp.rstrip("/").split("/")
                parent = "/".join(parts[:-1]) if len(parts) > 2 else "/"
                parents.setdefault(parent, []).append(cp)

            if not parents:
                self.finished.emit([])
                return

            to_download: list[str] = []
            for parent, orphans in parents.items():
                siblings = self._database.get_children(parent)
                other_files = [s for s in siblings
                               if s["type"] == "file" and s["cloud_path"] not in orphans]
                if not other_files:
                    continue  # папка состоит только из orphans
                all_synced = all(s["status"] == "downloaded" for s in other_files)
                if all_synced:
                    to_download.extend(orphans)

            logger.info("AutoDownloadThread: %d file(s) to auto-download",
                        len(to_download))
            self.finished.emit(to_download)
        except Exception as e:
            logger.warning("AutoDownloadThread failed: %s", e)
            self.finished.emit([])


class DeleteFilesThread(QThread):
    """Фоновый поток для удаления файлов/папок из облака.

    Удаляет каждый cloud_path через api.delete(), удаляет локальные файлы,
    чистит БД. Эмитит finished с результатами.
    """
    finished = Signal(dict)  # dict(success: int, errors: list[str])

    def __init__(self, api, db, items: list[dict], parent=None):
        super().__init__(parent)
        self._api = api
        self._db = db
        self._items = items  # list of dicts with cloud_path, name, is_dir, local_path

    def run(self):
        logger.info("🗑️ DeleteFilesThread: removing %d items from DB + cloud", len(self._items))
        # Шаг 1: Удаляем записи из БД немедленно (чтобы UI обновился)
        for item in self._items:
            cp = item["cloud_path"]
            is_dir = item.get("is_dir", False)
            local_path = item.get("local_path", "")
            if is_dir:
                self._db.remove_children(cp)
            self._db.remove_file(cp)
            # Удаляем локальную копию если есть
            if local_path and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except OSError:
                    pass

        # Шаг 2: Удаляем из облака (может занять время — облако не сразу удаляет)
        success = 0
        errors: list[str] = []

        for item in self._items:
            cp = item["cloud_path"]
            name = item.get("name", os.path.basename(cp.rstrip("/")))

            try:
                self._api.delete(cp)
                success += 1
            except Exception as e:
                errors.append(f"{name}: {e}")

        self.finished.emit({"success": success, "errors": errors})


class InternalDropThread(QThread):
    """Фоновый поток для внутреннего DnD: копирование/перемещение облачных путей."""
    finished = Signal(dict)  # dict(success, errors, processed, is_move)

    def __init__(self, api, db, items: list[tuple[str, str]], is_move: bool, parent=None):
        super().__init__(parent)
        self._api = api
        self._db = db
        self._items = items  # list of (src_cloud_path, dest_cloud_path)
        self._is_move = is_move

    def run(self):
        success = 0
        errors: list[str] = []
        processed: list[tuple[str, str]] = []
        failed: list[tuple[str, str]] = []

        for src, new_path in self._items:
            name = os.path.basename(src.rstrip("/"))
            try:
                if self._is_move:
                    self._api.move(src, new_path, overwrite=False)
                else:
                    self._api.copy(src, new_path, overwrite=False)
                processed.append((src, new_path))
                success += 1
            except Exception as e:
                errors.append(f"{name}: {e}")
                failed.append((src, new_path))

        self.finished.emit({
            "success": success, "errors": errors,
            "processed": processed, "failed": failed,
            "is_move": self._is_move,
        })


class DropUploadThread(QThread):
    """Фоновый поток для обработки drag-and-drop: копирование файлов в кеш + MD5.

    Принимает список локальных путей (urls), копирует файлы в папку кеша
    под правильными cloud-путями и вычисляет MD5 — всё в фоновом потоке.
    """
    finished = Signal(list)  # list[tuple(cloud_path, local_path, local_md5)]

    def __init__(self, urls: list[str], current_path: str, parent=None):
        super().__init__(parent)
        self._urls = urls
        self._current_path = current_path

    def run(self):
        results: list[tuple[str, str, str]] = []
        # Собираем все файлы рекурсивно
        all_files: list[str] = []
        for url in self._urls:
            if os.path.isfile(url):
                all_files.append(url)
            elif os.path.isdir(url):
                for root, _dirs, files in os.walk(url):
                    for f in files:
                        all_files.append(os.path.join(root, f))

        cache_dir = self._get_cache_dir()
        for src in all_files:
            try:
                name = os.path.basename(src)
                cloud_path = (self._current_path.rstrip("/") + "/" + name).replace("//", "/")
                local_copy = self._local_path(cloud_path, cache_dir)
                os.makedirs(os.path.dirname(local_copy), exist_ok=True)
                shutil.copy2(src, local_copy)
                md5 = self._md5_file(local_copy)
                results.append((cloud_path, local_copy, md5))
            except Exception as e:
                logger.warning("DropUploadThread: skip %s — %s", src, e)

        self.finished.emit(results)

    @staticmethod
    def _get_cache_dir() -> str:
        # Берём из конфига или дефолт
        import db as _db
        cfg = _db.get_config()
        return cfg.get("cache_dir", os.path.join(os.path.expanduser("~"), ".yadisk-cache"))

    @staticmethod
    def _local_path(cloud_path: str, cache_dir: str) -> str:
        rel = cloud_path.lstrip("/").replace("/", "\\")
        return os.path.join(cache_dir, rel)

    @staticmethod
    def _md5_file(filepath: str, chunk_size: int = 64 * 1024) -> str:
        h = hashlib.md5()
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()


class PasteFilesThread(QThread):
    """Фоновый поток для вставки (Copy/Paste) — API-вызовы copy/move без блокировки UI.

    finished сигнал с результатами: (success_count, processed_cut, errors, any_copy)
      processed_cut: list[(src, dest)] для cut-операций
    """
    finished = Signal(int, list, list, bool)

    def __init__(self, api, items: list[tuple[str, str, str, bool]], parent=None):
        """
        items: list of (src_cloud_path, dest_cloud_path, action, overwrite)
          action = 'copy' or 'cut'
        """
        super().__init__(parent)
        self._api = api
        self._items = items

    def run(self):
        success_count = 0
        processed_cut: list[tuple[str, str]] = []
        errors: list[str] = []
        any_copy = any(a == "copy" for _, _, a, _ in self._items)

        for src_path, new_path, action, overwrite in self._items:
            name = os.path.basename(src_path.rstrip("/"))
            try:
                if action == "copy":
                    logger.info("📋 PasteFilesThread: copy %s → %s (overwrite=%s)",
                                src_path, new_path, overwrite)
                    self._api.copy(src_path, new_path, overwrite=overwrite)
                elif action == "cut":
                    logger.info("📋 PasteFilesThread: move %s → %s (overwrite=%s)",
                                src_path, new_path, overwrite)
                    self._api.move(src_path, new_path, overwrite=overwrite)
                success_count += 1
                if action == "cut":
                    processed_cut.append((src_path, new_path))
            except Exception as e:
                errors.append(f"{name}: {e}")

        self.finished.emit(success_count, processed_cut, errors, any_copy)
