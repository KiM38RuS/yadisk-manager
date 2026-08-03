"""
Yandex Disk REST API client.
Документация: https://yandex.ru/dev/disk/rest/
"""

import os
import time
import logging
from typing import Optional
from collections import deque
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://cloud-api.yandex.net/v1/disk"
MAX_RETRIES = 3
RETRY_DELAY = 1.0


class _RateLimiter:
    """Скользящее окно — не более max_per_second запросов в секунду."""

    def __init__(self, max_per_second: int = 40):
        self.max_per_second = max_per_second
        self._timestamps = deque()

    def wait(self) -> None:
        """При необходимости усыпить поток, чтобы не превысить лимит."""
        now = time.monotonic()
        # Удаляем метки старше 1 секунды
        while self._timestamps and self._timestamps[0] <= now - 1.0:
            self._timestamps.popleft()

        if len(self._timestamps) >= self.max_per_second:
            # Окно заполнено — спим до освобождения места
            sleep_for = 1.0 - (now - self._timestamps[0])
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            # После сна чистим окно заново
            while self._timestamps and self._timestamps[0] <= now - 1.0:
                self._timestamps.popleft()

        self._timestamps.append(now)


class YaDiskError(Exception):
    """Ошибка API Яндекс.Диска."""


class AuthError(YaDiskError):
    """Ошибка авторизации — токен недействителен."""


class YaDiskAPI:
    """Тонкая обёртка над REST API Яндекс.Диска."""

    def __init__(self, token: str):
        self.token = token
        self._session = requests.Session()
        self._rate_limiter = _RateLimiter()
        self._session.headers.update({
            "Authorization": f"OAuth {token}",
            "Content-Type": "application/json",
        })

    # ── helpers ──────────────────────────────────────────

    @staticmethod
    def _normalize_item_paths(items: list[dict]) -> list[dict]:
        """Нормализовать пути: убрать 'disk:' + гарантировать ведущий '/'.

        API Яндекс.Диска может вернуть path как:
          'disk:/foo/bar'  → '/foo/bar'
          '/foo/bar'       → '/foo/bar'
          'foo/bar'        → '/foo/bar'
        В БД и локальной навигации используем '/foo/bar'.
        """
        for item in items:
            p = item.get("path", "")
            # Убираем префикс disk:
            if p.startswith("disk:"):
                p = p[5:]
            # Гарантируем ведущий /
            if not p.startswith("/"):
                p = "/" + p
            item["path"] = p
        return items

    @staticmethod
    def _normalize_item_path(item: dict) -> dict:
        """Нормализовать путь одного элемента."""
        p = item.get("path", "")
        if p.startswith("disk:"):
            p = p[5:]
        if not p.startswith("/"):
            p = "/" + p
        item["path"] = p
        return item

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Выполнить запрос к API с повторными попытками."""
        self._rate_limiter.wait()
        url = f"{BASE_URL}{path}"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self._session.request(method, url, timeout=30, **kwargs)
            except requests.RequestException as e:
                if attempt == MAX_RETRIES:
                    raise YaDiskError(f"Network error: {e}") from e
                time.sleep(RETRY_DELAY * attempt)
                continue

            if resp.status_code in (429, 503):
                time.sleep(RETRY_DELAY * attempt)
                continue

            if resp.status_code == 401:
                msg = resp.json().get("message", resp.text) if resp.content else ""
                raise AuthError(f"HTTP {resp.status_code}: {msg}")

            if resp.status_code >= 400:
                msg = resp.json().get("message", resp.text) if resp.content else ""
                raise YaDiskError(f"HTTP {resp.status_code}: {msg}")

            # 204 No Content — нет тела
            if resp.status_code == 204:
                return {}

            return resp.json()

        raise YaDiskError("Max retries exceeded")

    def _get_download_href(self, path: str) -> str:
        """Получить временную ссылку на скачивание."""
        data = self._request(
            "GET",
            f"/resources/download?path={quote(path, safe='')}",
        )
        href = data.get("href")
        if not href:
            raise YaDiskError(f"No download href for {path}")
        return href

    def _get_upload_href(self, path: str) -> str:
        """Получить временную ссылку на загрузку."""
        data = self._request(
            "GET",
            f"/resources/upload?path={quote(path, safe='')}&overwrite=true",
        )
        href = data.get("href")
        if not href:
            raise YaDiskError(f"No upload href for {path}")
        return href

    # ── информация ───────────────────────────────────────

    def get_disk_info(self) -> dict:
        """Общая информация о диске (размер, место, etc.)."""
        return self._request("GET", "/")

    # ── навигация ────────────────────────────────────────

    def list_folder(self, path: str = "/", limit: int = 200,
                    offset: int = 0) -> list[dict]:
        """Список файлов и папок в указанной директории."""
        data = self._request(
            "GET",
            f"/resources?path={quote(path, safe='')}"
            f"&limit={limit}&offset={offset}",
        )
        embedded = data.get("_embedded") or {}
        items = embedded.get("items", [])
        # докачка, если превышен лимит
        total = embedded.get("total", 0)
        while len(items) < total:
            offset += limit
            data = self._request(
                "GET",
                f"/resources?path={quote(path, safe='')}"
                f"&limit={limit}&offset={offset}",
            )
            items.extend(data.get("_embedded", {}).get("items", []))
        return self._normalize_item_paths(items)

    def get_meta(self, path: str) -> dict:
        """Мета-информация о конкретном файле/папке."""
        result = self._request(
            "GET",
            f"/resources?path={quote(path, safe='')}",
        )
        return self._normalize_item_path(result)

    def get_all_files(self, limit: int = 200, offset: int = 0) -> list[dict]:
        """Плоский список всех файлов на диске с пагинацией по страницам.

        API /resources/files НЕ возвращает поле total, в отличие от /resources.
        Пагинация: пока страница содержит ровно limit элементов — запрашиваем следующую.
        """
        items: list[dict] = []
        while True:
            data = self._request(
                "GET",
                f"/resources/files?limit={limit}&offset={offset}",
            )
            page = data.get("items", [])
            items.extend(page)
            # Последняя страница: вернулось меньше элементов, чем запрашивали
            if len(page) < limit:
                break
            offset += limit
        return self._normalize_item_paths(items)

    # ── загрузка / скачивание ────────────────────────────

    def get_download_url(self, path: str) -> str:
        """Временная ссылка на скачивание файла."""
        return self._get_download_href(path)

    def download_file(self, path: str, local_path: str) -> None:
        """Скачать файл с Диска в локальный файл."""
        href = self.get_download_url(path)
        resp = self._session.get(href, stream=True, timeout=60)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

    def get_upload_url(self, path: str) -> str:
        """Временная ссылка для загрузки файла."""
        return self._get_upload_href(path)

    def upload_file(self, local_path: str, cloud_path: str) -> int:
        """Загрузить локальный файл на Диск. Возвращает HTTP-статус."""
        href = self.get_upload_url(cloud_path)
        total = os.path.getsize(local_path)
        with open(local_path, "rb") as f:
            resp = self._session.put(href, data=f, timeout=120)
        resp.raise_for_status()
        return resp.status_code

    # ── операции с файлами ───────────────────────────────

    def delete(self, path: str, permanently: bool = False) -> None:
        """Удалить файл или папку."""
        self._request(
            "DELETE",
            f"/resources?path={quote(path, safe='')}"
            f"&permanently={'true' if permanently else 'false'}",
        )

    def create_folder(self, path: str) -> None:
        """Создать папку."""
        self._request("PUT", f"/resources?path={quote(path, safe='')}")

    def move(self, src: str, dst: str, overwrite: bool = False) -> None:
        """Переместить/переименовать."""
        self._request(
            "POST",
            f"/resources/move?from={quote(src, safe='')}"
            f"&path={quote(dst, safe='')}"
            f"&overwrite={'true' if overwrite else 'false'}",
        )

    def copy(self, src: str, dst: str, overwrite: bool = False) -> None:
        """Скопировать."""
        self._request(
            "POST",
            f"/resources/copy?from={quote(src, safe='')}"
            f"&path={quote(dst, safe='')}"
            f"&overwrite={'true' if overwrite else 'false'}",
        )

    # ── публичная ссылка ─────────────────────────────────

    def publish(self, path: str) -> str:
        """Сделать файл публичным и вернуть ссылку."""
        data = self._request(
            "PUT",
            f"/resources/publish?path={quote(path, safe='')}",
        )
        return data.get("public_url", "")

    def unpublish(self, path: str) -> None:
        """Убрать публичный доступ."""
        self._request(
            "PUT",
            f"/resources/unpublish?path={quote(path, safe='')}",
        )

    # ── recent ───────────────────────────────────────────

    def get_recent_uploaded(self, limit: int = 50) -> list[dict]:
        """Недавно загруженные файлы."""
        data = self._request("GET", f"/resources/last-uploaded?limit={limit}")
        items = data.get("items", [])
        return self._normalize_item_paths(items)
