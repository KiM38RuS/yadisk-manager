"""
ui_shared — константы и вспомогательные функции для GUI YaDisk Manager.
Используется всеми ui-модулями.
"""

import hashlib
import logging
import os
from pathlib import Path

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QIcon, QPixmap, QPainter
from PySide6.QtSvg import QSvgRenderer

import db

logger = logging.getLogger(__name__)

# ── константы ────────────────────────────────────────────

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "Assets")

POLL_INTERVAL_MS = 60000

ICONS = {
    "cloud": "☁️", "local": "💾", "syncing": "🔄",
    "folder": "📁", "file": "📄", "image": "🖼️",
    "video": "🎬", "audio": "🎵", "archive": "📦",
    "pdf": "📕", "code": "📝",
}
STATUS_CHAR = {}
STATUS_LABELS = {
    "cloud_only": "в облаке", "downloaded": "на компьютере",
    "syncing": "синхронизация",
    "unknown": "неизвестно",
    "partial": "частично на компьютере",
    "deleting": "удаление",
}
STATUS_ICON = {
    "cloud_only": "cloud.svg",
    "downloaded": "loaded.svg",
    "syncing": "synchronize.svg",
    "unknown": "synchronize-light-update.svg",
    "partial": "loaded-partically.svg",
    "deleting": "trash.svg",
}
STATUS_COLOR = {
    "cloud_only": QColor("#999"),
    "downloaded": QColor("#2a2"),
    "syncing": QColor("#08f"),
    "unknown": QColor("#888"),
    "deleting": QColor("#c44"),
}
STATUS_COLOR_LIGHT = {
    "cloud_only": QColor("#999"),
    "downloaded": QColor("#1B7A1B"),
    "syncing": QColor("#0078D4"),
    "unknown": QColor("#666"),
    "deleting": QColor("#C00"),
}

WINDOWS_RESERVED = frozenset(
    n.lower() for n in (
        "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
        "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    )
)

AUTORUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTORUN_NAME = "YaDiskManager"


# ── helpers ──────────────────────────────────────────────


def _svg_icon(name: str, size: int = 36) -> QIcon:
    """Загрузить SVG-иконку из Assets, с учётом темы (светлая/тёмная).

    Для иконок с чёрным цветом есть -light варианты для тёмной темы.
    """
    try:
        theme = db.get_theme()
    except Exception:
        theme = "system"
    is_dark = theme == "dark"
    if theme == "system":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize")
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            is_dark = (val == 0)
        except Exception:
            pass

    # Проверяем, есть ли -light вариант
    base, ext = os.path.splitext(name)
    light_name = f"{base}-light{ext}"
    light_path = os.path.join(ASSETS_DIR, light_name)
    normal_path = os.path.join(ASSETS_DIR, name)

    if is_dark and os.path.isfile(light_path):
        path = light_path
    else:
        path = normal_path

    if os.path.isfile(path):
        icon = QIcon(path)
        if not icon.isNull():
            # Создаём disabled-версию с пониженной непрозрачностью,
            # чтобы Qt не накладывал свой серый фильтр, который даёт
            # неестественный цвет (тёмно-серый на светлой теме,
            # светло-серый на тёмной).
            # Для светлой темы (чёрные иконки) прозрачность сильнее (35%),
            # для тёмной (белые иконки) — 60%, иначе почти неотличимы от активных.
            opacity = 0.35 if not is_dark else 0.6
            pixmap = icon.pixmap(size, size)
            if not pixmap.isNull():
                disabled = QPixmap(pixmap.size())
                disabled.fill(Qt.transparent)
                p = QPainter(disabled)
                p.setOpacity(opacity)
                p.drawPixmap(0, 0, pixmap)
                p.end()
                icon.addPixmap(disabled, QIcon.Disabled)
            return icon
    return QIcon()


# ── вращение иконок ─────────────────────────────────────

_rotation_angle = 0  # градусы, модуль 360
_ANIMATED_STATUSES = frozenset({"syncing", "unknown", "deleting"})
# 60 кадров × 6° = полный оборот за секунду при 60fps
_NUM_FRAMES = 60
_FRAME_STEP = 360 // _NUM_FRAMES


def is_animated_status(status: str) -> bool:
    return status in _ANIMATED_STATUSES


def advance_rotation(step: int = -6) -> None:
    """Повернуть глобальный угол иконок (отрицательный = против часовой)."""
    global _rotation_angle
    _rotation_angle = (_rotation_angle + step) % 360
    global _current_frame
    _current_frame = int(_rotation_angle // _FRAME_STEP) % _NUM_FRAMES


_current_frame = 0


def get_rotation_angle() -> int:
    return _rotation_angle


def get_current_frame() -> int:
    return _current_frame


def _get_svg_path(name: str) -> str:
    """Найти путь к SVG с учётом темы (светлая/тёмная).

    Повторяет логику _svg_icon() для определения темы.
    """
    try:
        theme = db.get_theme()
    except Exception:
        theme = "system"
    is_dark = theme == "dark"
    if theme == "system":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            is_dark = (val == 0)
        except Exception:
            pass
    base, ext = os.path.splitext(name)
    light_name = f"{base}-light{ext}"
    light_path = os.path.join(ASSETS_DIR, light_name)
    normal_path = os.path.join(ASSETS_DIR, name)
    if is_dark and os.path.isfile(light_path):
        return light_path
    return normal_path


def _rotated_svg_icon(name: str, size: int = 36, angle: float = 0) -> QIcon:
    """Загрузить SVG-иконку с поворотом на angle градусов (по часовой).

    Результаты кешируются по (имя_файла, размер, угол_квантованный).
    """
    # Квантование угла до 12 положений — минимизируем кеш
    step = _FRAME_STEP
    qangle = (int(round(angle / step)) * step) % 360
    cache_key = (name, size, qangle)
    cached = _rotated_cache.get(cache_key)
    if cached is not None:
        return cached
    path = _get_svg_path(name)
    if not os.path.isfile(path):
        return QIcon()
    renderer = QSvgRenderer(path)
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    p.translate(size / 2, size / 2)
    p.rotate(qangle)
    p.translate(-size / 2, -size / 2)
    renderer.render(p, QRectF(0, 0, size, size))
    p.end()
    icon = QIcon(pixmap)
    # Кеш: 2 имени × 60 углов × 2 размера = 240 записей
    if len(_rotated_cache) < 256:
        _rotated_cache[cache_key] = icon
    return icon


_rotated_cache: dict[tuple[str, int, int], QIcon] = {}


def _cache_dir() -> str:
    return db.get_cache_dir() or os.path.join(os.path.expanduser("~"), ".yadisk-cache")


def _local_path(cloud_path: str) -> str:
    """Преобразовать cloud_path (из API) в локальный путь к файлу.

    API Яндекс.Диска может вернуть path как "disk:/foo/bar.xlsx" —
    убираем префикс "disk:", иначе на Windows получится
    неверный путь с двоеточием в середине (disk:\foo\bar.xlsx).
    """
    clean = cloud_path
    if clean.startswith("disk:"):
        clean = clean[5:]  # убираем "disk:"
    # Убираем ведущий слеш и формируем полный путь
    return os.path.join(_cache_dir(), clean.lstrip("/"))


def _icon_for(name: str, is_dir: bool = False) -> str:
    if is_dir:
        return ICONS["folder"]
    ext = Path(name).suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"):
        return ICONS["image"]
    if ext in (".mp4", ".avi", ".mkv", ".mov", ".wmv", ".webm"):
        return ICONS["video"]
    if ext in (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".wma"):
        return ICONS["audio"]
    if ext in (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"):
        return ICONS["archive"]
    if ext == ".pdf":
        return ICONS["pdf"]
    if ext in (".py", ".js", ".ts", ".html", ".css", ".cpp", ".c", ".h",
               ".java", ".rs", ".go", ".rb", ".php", ".sh", ".bat", ".json",
               ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".md", ".txt"):
        return ICONS["code"]
    return ICONS["file"]


def _is_windows_reserved(name: str) -> bool:
    """Проверить, является ли имя файла Windows-резервированным (nul, con, etc.)."""
    base = name.rsplit(".", 1)[0].lower() if "." in name else name.lower()
    return base in WINDOWS_RESERVED


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} PB"


def _md5_file(path: str) -> str:
    """MD5 хеш файла. Возвращает пустую строку при ошибке доступа (Excel lock и т.п.)."""
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except PermissionError:
        logger.warning("Permission denied reading (maybe locked by another app): %s", path)
        return ""
    except FileNotFoundError:
        return ""


# ── Автозапуск (Windows) ─────────────────────────────────


def _autorun_is_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTORUN_KEY, 0,
                            winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, AUTORUN_NAME)
            return True
    except (FileNotFoundError, OSError, ImportError):
        pass
    return False


def _autorun_set(enabled: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTORUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        if enabled:
            exe = __import__("sys").executable
            script = os.path.abspath(__import__("sys").argv[0])
            winreg.SetValueEx(k, AUTORUN_NAME, 0, winreg.REG_SZ,
                              f'"{exe}" "{script}"')
        else:
            try:
                winreg.DeleteValue(k, AUTORUN_NAME)
            except FileNotFoundError:
                pass
