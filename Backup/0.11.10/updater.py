"""
Система обновлений YaDiskManager.

Проверяет GitHub Releases (API), ненавязчиво уведомляет о новой версии,
скачивает exe с полосой прогресса и перезапускает обновлённую программу.

Режимы:
- frozen (exe): скачивание в "<каталог exe>/YaDiskManager.new.exe",
  применение через apply_update.bat (ждать, переименовать, запустить новый).
- dev (из исходников): только скачивание во временную папку — применять
  нельзя, т.к. pythonw.exe — это интерпретатор, а не приложение.

Перед релизом: указать UPDATE_REPO (username/repo). Пока поле пустое —
проверка обновлений отключена (check_update возвращает None без сети).
"""

import logging
import os
import re
import subprocess
import sys
import tempfile

import requests
from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)

# ── Настройки ─────────────────────────────────────────────

# GitHub-репозиторий: "username/repo". ЗАПОЛНИТЬ перед релизом!
UPDATE_REPO = "KiM38RuS/yadisk-manager"

# Интервал авто-проверки во время работы (мс): 4 часа
UPDATE_CHECK_INTERVAL_MS = 4 * 3600 * 1000
# Задержка первой авто-проверки после старта (мс): 20 секунд —
# чтобы не конкурировать со стартовой загрузкой списка файлов
UPDATE_CHECK_DELAY_MS = 20_000
# Таймаут сетевых запросов (сек)
UPDATE_TIMEOUT_S = 8

RELEASE_URL = "https://api.github.com/repos/{repo}/releases/latest"
# Список последних релизов (нужен для pre-release: /releases/latest не
# отдаёт их — только обычные релизы)
RELEASES_URL = "https://api.github.com/repos/{repo}/releases?per_page=10"
UA = "YaDiskManager-Updater/0.1 (+https://github.com/{repo})"


# ── Версии ────────────────────────────────────────────────

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(text: str) -> tuple | None:
    """'v0.11.6' / '0.11.6' → (0, 11, 6). None — не распознано."""
    m = _VERSION_RE.search(text or "")
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def is_newer(latest: str, current: str) -> bool:
    """True, если latest новее current (сравнение по (major, minor, patch))."""
    lv = parse_version(latest)
    cv = parse_version(current)
    if lv is None or cv is None:
        return False
    return lv > cv


# ── Данные о релизе ───────────────────────────────────────

class UpdateInfo:
    """Найденное обновление: версия, ссылка на exe, описание."""

    __slots__ = ("version", "download_url", "size", "notes", "repo")

    def __init__(self, version: str, download_url: str, size: int,
                 notes: str, repo: str):
        self.version = version
        self.download_url = download_url
        self.size = size
        self.notes = notes
        self.repo = repo

    def __repr__(self):
        return f"<UpdateInfo {self.version} {self.size}b>"


def fetch_latest_release(repo: str, timeout: float = UPDATE_TIMEOUT_S,
                         include_prerelease: bool = False):
    """GET /releases/latest (или /releases, если include_prerelease).

    Возвращает dict JSON или None (нет релизов/ошибка). При
    include_prerelease=True выбирает самую свежую НЕ draft запись —
    в т.ч. pre-release (обычный /releases/latest их не отдаёт).
    """
    if include_prerelease:
        url = RELEASES_URL.format(repo=repo)
    else:
        url = RELEASE_URL.format(repo=repo)
    try:
        r = requests.get(url, headers={"User-Agent": UA.format(repo=repo)},
                         timeout=timeout)
    except requests.RequestException as e:
        logger.info("Update check failed (network): %s", e)
        return None
    if r.status_code == 404:
        logger.info("Update check: no releases yet for %s", repo)
        return None
    if r.status_code != 200:
        logger.info("Update check: HTTP %s from GitHub", r.status_code)
        return None
    try:
        data = r.json()
    except ValueError:
        logger.warning("Update check: bad JSON from GitHub")
        return None
    if include_prerelease:
        # data — список; первая НЕ draft запись (GitHub сортирует по дате)
        for rel in data:
            if rel.get("draft"):
                continue
            return rel
        logger.info("Update check: no releases for %s", repo)
        return None
    return data


def find_update(repo: str, current_version: str, timeout: float = UPDATE_TIMEOUT_S,
                include_prerelease: bool = False):
    """Проверить релизы. Вернуть UpdateInfo или None (нет обновления/ошибка)."""
    if not repo:
        return None
    data = fetch_latest_release(repo, timeout=timeout,
                                include_prerelease=include_prerelease)
    if not data:
        return None
    tag = data.get("tag_name", "")
    if not is_newer(tag, current_version):
        logger.info("Update check: latest %s — already current (%s)",
                    tag, current_version)
        return None
    # Ищем ассет с exe (не Setup/инсталлер)
    asset = None
    for a in data.get("assets", []):
        name = a.get("name", "")
        if name.lower().endswith(".exe") and "setup" not in name.lower():
            asset = a
            break
    if not asset:
        logger.info("Update check: %s has no exe asset", tag)
        return None
    return UpdateInfo(
        version=tag.lstrip("vV"),
        download_url=asset.get("browser_download_url", ""),
        size=asset.get("size", 0),
        notes=data.get("body", ""),
        repo=repo,
    )


# ── Скачивание ────────────────────────────────────────────

def _target_path() -> str:
    """Куда скачивать новый exe. Frozen → рядом с текущим exe, иначе temp."""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        return os.path.join(exe_dir, "YaDiskManager.new.exe")
    return os.path.join(tempfile.gettempdir(), "YaDiskManager.new.exe")


def download_update(info: UpdateInfo, dest: str | None = None,
                    progress_cb=None) -> str:
    """Скачать exe (stream, 64 КБ) с прогрессом. Возвращает путь к файлу."""
    dest = dest or _target_path()
    logger.info("Downloading %s → %s", info.download_url, dest)
    tmp = dest + ".part"
    with requests.get(info.download_url, stream=True, timeout=60,
                      headers={"User-Agent": UA.format(repo=info.repo)}) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0) or 0)
        done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb(done, total or done)
    os.replace(tmp, dest)
    logger.info("Downloaded %s (%d bytes)", dest, done)
    return dest


# ── Применение (frozen) ───────────────────────────────────

_APPLY_BAT = r"""@echo off
rem Автоматически сгенерировано YaDiskManager — применение обновления.
ping -n 4 127.0.0.1 >nul 2>&1
set "D=%~dp0"
del "%D%YaDiskManager.exe.old" 2>nul
rename "%D%YaDiskManager.exe" YaDiskManager.exe.old
rename "%D%YaDiskManager.new.exe" YaDiskManager.exe
start "" "%D%YaDiskManager.exe"
"""


def apply_update(new_exe: str) -> bool:
    """Заменить текущий exe новым и запустить его (после выхода приложения).

    Только для frozen-режима. Запускает скрытый apply_update.bat, который
    ждёт 3 сек (пока приложение закроется), переименовывает файлы и
    стартует новый exe. Возвращает True, если bat запущен.
    """
    if not getattr(sys, "frozen", False):
        logger.warning("Apply skipped: dev mode, downloaded to %s", new_exe)
        return False
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    expected = os.path.join(exe_dir, "YaDiskManager.new.exe")
    if os.path.normcase(new_exe) != os.path.normcase(expected):
        logger.error("Apply aborted: %s is not %s", new_exe, expected)
        return False
    bat = os.path.join(exe_dir, "apply_update.bat")
    try:
        with open(bat, "w", encoding="ascii", newline="\r\n") as f:
            f.write(_APPLY_BAT)
        creation = 0x08000000 | 0x00000004  # CREATE_NO_WINDOW | DETACHED_PROCESS
        subprocess.Popen(["cmd.exe", "/c", bat],
                         cwd=exe_dir, creationflags=creation,
                         close_fds=True)
    except OSError as e:
        logger.error("Apply failed: %s", e)
        return False
    logger.info("Apply: apply_update.bat launched, app will restart")
    return True


# ── Потоки (для Qt) ───────────────────────────────────────

class UpdateCheckThread(QThread):
    """Фоновая проверка обновлений. finished(info|None, error_msg)."""

    finished = Signal(object, str)

    def __init__(self, repo: str, current_version: str,
                 include_prerelease: bool = False, parent=None):
        super().__init__(parent)
        self._repo = repo
        self._current_version = current_version
        self._include_prerelease = include_prerelease

    def run(self):
        try:
            info = find_update(self._repo, self._current_version,
                               include_prerelease=self._include_prerelease)
            self.finished.emit(info, "")
        except Exception as e:
            logger.exception("Update check crashed")
            self.finished.emit(None, str(e))


class UpdateDownloadThread(QThread):
    """Скачивание нового exe. progress(done, total), finished(path|None, err)."""

    progress = Signal(int, int)
    finished = Signal(object, str)

    def __init__(self, info: UpdateInfo, parent=None):
        super().__init__(parent)
        self._info = info

    def run(self):
        try:
            path = download_update(self._info, progress_cb=self._on_progress)
            self.finished.emit(path, "")
        except Exception as e:
            logger.exception("Download crashed")
            self.finished.emit(None, str(e))

    def _on_progress(self, done: int, total: int):
        self.progress.emit(done, total)
