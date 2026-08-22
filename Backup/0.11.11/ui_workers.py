"""
Фоновые воркеры с прогрессом — скачивание/загрузка/ZIP.
"""

import hashlib
import logging
import os
from datetime import datetime

from PySide6.QtCore import QObject, Signal

import disk_api

logger = logging.getLogger("ui.workers")


class DownloadWorker(QObject):
    """Скачивает файл из облака в фоновом потоке с отчётом прогресса."""
    progress = Signal(int, int)    # downloaded, total
    finished = Signal(str)         # local_path
    error = Signal(str)
    conflict = Signal(str, str)    # cloud_path, local_path

    def __init__(self, api: disk_api.YaDiskAPI,
                 cloud_path: str, local_path: str,
                 cloud_modified: str = ""):
        super().__init__()
        self.api = api
        self.cloud_path = cloud_path
        self.local_path = local_path
        self.cloud_modified = cloud_modified
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            logger.info("DLW: get_download_url %s", self.cloud_path)
            href = self.api.get_download_url(self.cloud_path)
            logger.info("DLW: href OK -> %s...", href[:80] if len(href) > 80 else href)
            resp = self.api._session.get(href, stream=True, timeout=120)
            logger.info("DLW: GET status=%d", resp.status_code)
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0))
            logger.info("DLW: total=%d bytes, local_path=%s", total, self.local_path)
            os.makedirs(os.path.dirname(self.local_path), exist_ok=True)
            logger.info("DLW: dirs created")
            downloaded = 0
            with open(self.local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if self._cancelled:
                        logger.info("DLW: cancelled")
                        return
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        self.progress.emit(downloaded, total)
            # Сохраняем дату изменения из облака
            if self.cloud_modified:
                try:
                    dt = datetime.fromisoformat(
                        self.cloud_modified.replace("Z", "+00:00"))
                    ts = dt.timestamp()
                    os.utime(self.local_path, (ts, ts))
                except Exception:
                    pass  # не критично
            logger.info("DLW: finished, wrote %d bytes to %s", downloaded, self.local_path)
            self.finished.emit(self.local_path)
        except Exception as e:
            logger.error("DLW: EXCEPTION: %s", e, exc_info=True)
            self.error.emit(str(e))


class UploadWorker(QObject):
    """Загружает файл в облако в фоновом потоке с отчётом прогресса."""
    progress = Signal(int, int)    # uploaded, total
    finished = Signal(str)         # cloud_path
    error = Signal(str)
    conflict = Signal(str, str, str)  # cloud_path, local_md5, cloud_md5

    def __init__(self, api: disk_api.YaDiskAPI,
                 local_path: str, cloud_path: str):
        super().__init__()
        self.api = api
        self.local_path = local_path
        self.cloud_path = cloud_path
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            href = self.api.get_upload_url(self.cloud_path)
            self._do_put(href)
        except Exception as e:
            err_str = str(e)
            # 409 = parent folder doesn't exist → create folders and retry once
            if "409" in err_str and not self._cancelled:
                try:
                    self.api.ensure_folder_path(self.cloud_path)
                except Exception:
                    pass
                # повторная попытка
                try:
                    href = self.api.get_upload_url(self.cloud_path)
                    self._do_put(href)
                    return
                except Exception as e2:
                    self.error.emit(str(e2))
                    return
            self.error.emit(err_str)

    def _do_put(self, href: str):
        """Выполнить PUT-запрос с данными файла по href."""
        total = os.path.getsize(self.local_path)
        uploaded = 0

        def gen_chunks():
            nonlocal uploaded
            with open(self.local_path, "rb") as f:
                while True:
                    if self._cancelled:
                        break
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    uploaded += len(chunk)
                    self.progress.emit(uploaded, total)
                    yield chunk

        resp = self.api._session.put(href, data=gen_chunks(),
                                     timeout=300)
        resp.raise_for_status()
        if not self._cancelled:
            self.finished.emit(self.cloud_path)


class ZipDownloadWorker(QObject):
    """Скачивает папку с Диска как ZIP-архив (фоновый поток, прогресс)."""

    progress = Signal(int, int)    # downloaded, total
    finished = Signal(str)         # local_path
    error = Signal(str)

    def __init__(self, api: disk_api.YaDiskAPI,
                 cloud_path: str, local_path: str):
        super().__init__()
        self.api = api
        self.cloud_path = cloud_path
        self.local_path = local_path
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            href = self.api.get_download_url(self.cloud_path)
            resp = self.api._session.get(href, stream=True, timeout=300)
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            os.makedirs(os.path.dirname(self.local_path), exist_ok=True)
            with open(self.local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if self._cancelled:
                        return
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        self.progress.emit(downloaded, total)
            self.finished.emit(self.local_path)
        except Exception as e:
            logger.error("ZipDownloadWorker error: %s", e, exc_info=True)
            self.error.emit(str(e))
