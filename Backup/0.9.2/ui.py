"""
PySide6 GUI — двухпанельный файловый менеджер для Яндекс.Диска.
Фичи: on-demand sync, трей, автозапуск, прогресс, разрешение конфликтов.
"""

import os
import hashlib
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTreeView, QTableView, QHeaderView, QStatusBar,
    QToolBar, QMenu, QMessageBox, QDialog, QLabel,
    QLineEdit, QPushButton, QTextBrowser, QInputDialog,
    QDialogButtonBox, QAbstractItemView, QProgressBar,
    QFileDialog,
    QCheckBox, QGroupBox, QFormLayout, QStyle,
    QStackedWidget, QSizePolicy,
    QSystemTrayIcon, QComboBox, QStyledItemDelegate,
)

from disk_api import AuthError, YaDiskError
from PySide6.QtCore import (
    Qt, QAbstractItemModel, QModelIndex, QAbstractTableModel,
    QThread, Signal, QObject, QTimer, QUrl, QSize, QPoint, QRectF,
    QSortFilterProxyModel, QEvent, QByteArray, QItemSelectionModel,
    QItemSelection,
)
from PySide6.QtGui import (
    QAction, QKeySequence, QIcon, QColor, QFont,
    QDesktopServices, QPixmap, QPalette, QActionGroup,
    QPainter, QGuiApplication,
)

import disk_api
import db
import watcher
import sync

logger = logging.getLogger(__name__)

# ── константы ────────────────────────────────────────────

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "Assets")

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
            return icon
    return QIcon()


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
}
STATUS_ICON = {
    "cloud_only": "cloud.svg",
    "downloaded": "loaded.svg",
    "syncing": "synchronize.svg",
    "partial": "loaded-partically.svg",
}
STATUS_COLOR = {
    "cloud_only": QColor("#999"),
    "downloaded": QColor("#2a2"),
    "syncing": QColor("#08f"),
}
STATUS_COLOR_LIGHT = {
    "cloud_only": QColor("#999"),
    "downloaded": QColor("#1B7A1B"),
    "syncing": QColor("#0078D4"),
}


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


WINDOWS_RESERVED = frozenset(
    n.lower() for n in (
        "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
        "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    )
)


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
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Автозапуск (Windows) ─────────────────────────────────

AUTORUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTORUN_NAME = "YaDiskManager"


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


# ── Фоновые воркеры (прогресс) ────────────────────────────

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
        except Exception as e:
            self.error.emit(str(e))


class _ZipDownloadWorker(QObject):
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


# ── Модель для дерева папок ──────────────────────────────

class FolderTreeItem:
    def __init__(self, name: str, cloud_path: str, parent=None, status: str = None):
        self.name = name
        self.cloud_path = cloud_path
        self.parent = parent
        self.children: list["FolderTreeItem"] = []
        self.loaded = False
        self._has_children = True  # assume has children until proven otherwise
        self.status = status  # cached folder aggregate status

    def child(self, row: int):
        return self.children[row] if 0 <= row < len(self.children) else None

    def row(self):
        if self.parent:
            return self.parent.children.index(self)
        return 0


class FolderTreeModel(QAbstractItemModel):
    """Модель дерева папок — НИКОГДА не делает API-вызовов синхронно.

    hasChildren() возвращает True для не загруженных узлов (показывает стрелку),
    и _has_children для загруженных — так стрелка скрыта у пустых папок.
    """

    def __init__(self, api: disk_api.YaDiskAPI, db: Optional[db.Database] = None, parent=None):
        super().__init__(parent)
        self._api = api
        self._db = db
        self._root = FolderTreeItem("root", "")
        self._root.loaded = False

    # ── async population ────────────────────────────────

    def populate_children(self, cloud_path: str, items: list[dict]) -> None:
        """Асинхронно добавить children узлу (из главного потока)."""
        parent_item = self._find_item(cloud_path)
        if not parent_item or parent_item.loaded:
            return
        parent_item.loaded = True
        folders = [it for it in items if it.get("type") == "dir"]
        parent_item._has_children = len(folders) > 0
        logger.info("Tree: %s → %d папок", cloud_path, len(folders))
        if not folders:
            return
        parent_index = self._index_of(parent_item)
        self.beginInsertRows(parent_index, 0, len(folders) - 1)
        for f in folders:
            child = FolderTreeItem(
                name=f["name"],
                cloud_path=f["path"],
                parent=parent_item,
            )
            parent_item.children.append(child)
        self.endInsertRows()

    def _find_item(self, cloud_path: str) -> Optional[FolderTreeItem]:
        """Поиск узла по cloud_path (рекурсивно)."""
        if cloud_path in ("", "/"):
            return self._root
        return self._search_item(self._root, cloud_path)

    def _search_item(self, item: FolderTreeItem, cloud_path: str) -> Optional[FolderTreeItem]:
        for child in item.children:
            if child.cloud_path == cloud_path:
                return child
            found = self._search_item(child, cloud_path)
            if found:
                return found
        return None

    def _index_of(self, item: FolderTreeItem) -> QModelIndex:
        if item is self._root or item.parent is None:
            return QModelIndex()
        return self.createIndex(item.row(), 0, item)

    # ── QAbstractItemModel interface (синхронные, без API) ─

    def index(self, row: int, column: int,
              parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        parent_item = parent.internalPointer() if parent.isValid() else self._root
        child = parent_item.child(row)
        return self.createIndex(row, column, child) if child else QModelIndex()

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        item: FolderTreeItem = index.internalPointer()
        if item.parent is None or item.parent is self._root:
            return QModelIndex()
        return self.createIndex(item.parent.row(), 0, item.parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            item: FolderTreeItem = parent.internalPointer()
            return len(item.children)
        return len(self._root.children)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 1

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        """True для не загруженных (стрелка есть), для загруженных — только если есть подпапки."""
        if not parent.isValid():
            return True
        item: FolderTreeItem = parent.internalPointer()
        if item.loaded:
            return item._has_children
        return True  # не загружен → показываем стрелку, чтобы можно было развернуть

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        item: FolderTreeItem = index.internalPointer()
        if role == Qt.DisplayRole:
            status = item.status
            if status is None and self._db is not None:
                try:
                    status = self._db.get_folder_aggregate_status(item.cloud_path)
                    item.status = status
                except Exception:
                    status = None
            return item.name
        if role == Qt.DecorationRole:
            status = item.status
            if status is None and self._db is not None:
                try:
                    status = self._db.get_folder_aggregate_status(item.cloud_path)
                    item.status = status
                except Exception:
                    status = None
            if status and status in STATUS_ICON:
                return _svg_icon(STATUS_ICON[status], 20)
            return None
        if role == Qt.UserRole:
            return item.cloud_path
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return "Папки"
        return None

    def refresh(self):
        """Сбросить модель (перезагрузка делается внешним кодом)."""
        self.beginResetModel()
        self._root.children.clear()
        self._root.loaded = False  # сброс — populate_children сможет заново наполнить
        self.endResetModel()

    def invalidate_status(self, cloud_path: str = None):
        """Сбросить кешированный статус на узле и его предках.
        При следующем data() статус будет перечитан из БД.

        Если cloud_path=None — сбросить статусы на всех узлах.
        """
        if cloud_path is None:
            self._invalidate_all(self._root)
        else:
            seen = set()
            self._invalidate_path(self._root, cloud_path, seen)

    def _invalidate_all(self, item: FolderTreeItem):
        item.status = None
        for child in item.children:
            self._invalidate_all(child)

    def _invalidate_path(self, item: FolderTreeItem, cloud_path: str, seen: set) -> bool:
        """Инвалидировать item если он сам или кто-то из его детей — cloud_path.
        Возвращает True, если нашли.
        """
        if item.cloud_path == cloud_path:
            item.status = None
            seen.add(id(item))
            return True
        for child in item.children:
            if self._invalidate_path(child, cloud_path, seen):
                item.status = None
                seen.add(id(item))
                return True
        return False

    def emit_path_changed(self, cloud_path: str):
        """Испустить dataChanged для узла (чтобы дерево перерисовало иконку)."""
        item = self._find_item(cloud_path)
        if item and item is not self._root:
            idx = self._index_of(item)
            self.dataChanged.emit(idx, idx, [Qt.DecorationRole])


# ── Модель для таблицы файлов ────────────────────────────

class RowHoverDelegate(QStyledItemDelegate):
    """Делегат для подсветки целой строки в таблице при наведении.
    Также рендерит колонку «Имя» с эмодзи-иконкой + посимвольной элизией.

    По умолчанию QTableView подсвечивает только ячейку под курсором.
    Этот делегат красит фон всей строки, в которой стоит курсор мыши.
    Цвет подсветки адаптируется к текущей палитре (светлая/тёмная тема).
    """

    hovered_row = -1

    def paint(self, painter, option, index):
        # Подсветка строки при наведении
        if index.row() == self.hovered_row and not (option.state & QStyle.State_Selected):
            hl = option.palette.color(QPalette.Highlight)
            hl.setAlpha(50)
            painter.fillRect(option.rect, hl)

        # Колонка 1 (Имя): ручной рендеринг эмодзи + элизия
        if index.column() == 1:
            self._paint_name_cell(painter, option, index)
            return

        # Остальные колонки: стандартный рендеринг
        super().paint(painter, option, index)

    def _paint_name_cell(self, painter, option, index):
        """Нарисовать ячейку с эмодзи-иконкой и именем, обрезанным с многоточием.

        Эмодзи рисуется как текст (не QPixmap), чтобы не было обрезки по краям.
        Имя обрезается посимвольно через fontMetrics.elidedText.
        """
        painter.save()

        # Фон выделения
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
            painter.setPen(option.palette.highlightedText().color())
        else:
            painter.setPen(option.palette.text().color())

        # Получаем данные элемента из source-модели.
        # ВАЖНО: index — это ПРОКСИ-индекс (QSortFilterProxyModel).
        # index.row() — позиция в ОТСОРТИРОВАННОМ списке, а не в source-модели.
        # Используем mapToSource() чтобы получить правильный source-ряд.
        model = index.model()
        src = model
        row = index.row()
        if isinstance(model, QSortFilterProxyModel):
            src_idx = model.mapToSource(index)
            if src_idx.isValid():
                src = src_idx.model()
                row = src_idx.row()

        name = ""
        is_dir = False
        if hasattr(src, '_items') and 0 <= row < len(src._items):
            item = src._items[row]
            name = item.get("name", "")
            is_dir = item.get("is_dir", False)
        else:
            name = index.data(Qt.DisplayRole) or ""

        icon_emoji = _icon_for(name, is_dir)
        display_text = f"{icon_emoji} {name}"

        # Обрезаем с многоточием посимвольно
        text_rect = option.rect.adjusted(4, 0, -4, 0)
        elided = option.fontMetrics.elidedText(display_text, Qt.ElideRight,
                                                text_rect.width())

        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)
        painter.restore()


class FileTableModel(QAbstractTableModel):
    COLUMNS = ["Статус", "Имя", "Размер", "Изменён"]

    def __init__(self, database: db.Database, parent=None):
        super().__init__(parent)
        self._db = database
        self._items: list[dict] = []
        self._current_path = "/"

    def set_path(self, cloud_path: str, api_items: list[dict]):
        self.beginResetModel()
        self._current_path = cloud_path
        self._items = []

        # Элемент ".." для перехода в предыдущую папку (скрыт для корня)
        if cloud_path != "/":
            parent_path = "/".join(cloud_path.rstrip("/").split("/")[:-1]) or "/"
            self._items.append({
                "cloud_path": parent_path,
                "name": "..",
                "type": "dir",
                "size": 0,
                "modified": "",
                "md5": "",
                "mime_type": "",
                "status": "",
                "local_path": "",
                "is_dir": True,
                "is_parent_nav": True,
            })

        for item in api_items:
            # Пропускаем Windows-резервированные имена (nul, con, etc.)
            if _is_windows_reserved(item.get("name", "")):
                continue
            is_dir = item["type"] == "dir"
            path = item["path"]
            # Если status уже указан (из _load_folder_local), используем как есть
            if "status" in item and item["status"]:
                status = item["status"]
                local = item.get("local_path", "")
            else:
                db_file = self._db.get_file(path)
                if is_dir:
                    if db_file:
                        status = "downloaded" if self._db.is_folder_fully_synced(path) else "cloud_only"
                    else:
                        status = "cloud_only"
                    local = ""
                else:
                    status = db_file["status"] if db_file else "cloud_only"
                    local = db_file["local_path"] if db_file else ""
            self._items.append({
                "cloud_path": path,
                "name": item["name"],
                "type": item["type"],
                "size": item.get("size", 0),
                "modified": item.get("modified", ""),
                "md5": item.get("md5", ""),
                "mime_type": item.get("mime_type", ""),
                "status": status,
                "local_path": local,
                "is_dir": is_dir,
            })
        self.endResetModel()

    def update_status(self, cloud_path: str, new_status: str):
        for i, item in enumerate(self._items):
            if item["cloud_path"] == cloud_path:
                self._items[i]["status"] = new_status
                idx = self.index(i, 0)
                self.dataChanged.emit(idx, idx, [Qt.DisplayRole])
                break

    def update_item_after_download(self, cloud_path: str, new_size: int,
                                   new_modified: str,
                                   new_status: str = "downloaded"):
        """Обновить все поля записи после успешного скачивания."""
        for i, item in enumerate(self._items):
            if item["cloud_path"] == cloud_path:
                self._items[i]["status"] = new_status
                self._items[i]["size"] = new_size
                self._items[i]["modified"] = new_modified
                # Оповестить все столбцы (0 = статус, 3 = изменён)
                self.dataChanged.emit(self.index(i, 0), self.index(i, 3), [Qt.DisplayRole])
                break

    def update_item_status(self, cloud_path: str, new_status: str):
        """Обновить статус файла в модели (после toggle local copy)."""
        for i, item in enumerate(self._items):
            if item["cloud_path"] == cloud_path:
                self._items[i]["status"] = new_status
                idx = self.index(i, 0)
                self.dataChanged.emit(idx, idx, [Qt.DisplayRole])
                break

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.COLUMNS)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self._items[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == 0:
                s = item["status"]
                return STATUS_LABELS.get(s, "")
            elif col == 1:
                return item["name"]
            elif col == 2:
                return _human_size(item["size"]) if not item["is_dir"] else ""
            elif col == 3:
                mod = item["modified"]
                if mod:
                    try:
                        dt = datetime.fromisoformat(mod.replace("Z", "+00:00"))
                        return dt.strftime("%d.%m.%Y %H:%M")
                    except Exception:
                        return mod[:19].replace("T", " ")
                return ""
        if role == Qt.DecorationRole and col == 0:
            s = item["status"]
            icon_name = STATUS_ICON.get(s, "")
            if icon_name:
                return _svg_icon(icon_name, 20)
            return None
        if role == Qt.ForegroundRole and col == 0:
            s = item["status"]
            if s == "cloud_only":
                # Для "в облаке" используем цвет из палитры — адаптируется к теме
                app = QApplication.instance()
                if app:
                    return app.palette().color(QPalette.Disabled, QPalette.Text)
                return QColor("#999")
            # Выбираем набор цветов в зависимости от темы
            theme = db.get_theme()
            if theme == "light":
                return STATUS_COLOR_LIGHT.get(s)
            return STATUS_COLOR.get(s)
        if role == Qt.UserRole:
            return item["cloud_path"]
        if role == Qt.ToolTipRole:
            s = item["status"]
            return (f"{item['cloud_path']}\n"
                    f"Статус: {STATUS_LABELS.get(s, s)}")
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            # Column 0 — статус, показываем пустой заголовок
            label = self.COLUMNS[section]
            if section == 0:
                label = ""
            return label
        return None

    def get_item(self, row: int) -> Optional[dict]:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None


class FileTableSortModel(QSortFilterProxyModel):
    """Прокси-модель для сортировки и поиска по таблице файлов."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDynamicSortFilter(True)
        self._search_text = ""

    def set_search_text(self, text: str):
        """Установить текст поиска и инвалидировать фильтр."""
        self._search_text = text
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        """Поиск — по имени файла (колонка 1), регистронезависимо."""
        if not self._search_text:
            return True
        src = self.sourceModel()
        if source_row >= len(src._items):
            return True
        item = src._items[source_row]
        name = item.get("name", "")
        return self._search_text.lower() in name.lower()

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        col = self.sortColumn()
        src = self.sourceModel()
        l_item = src._items[left.row()]
        r_item = src._items[right.row()]

        # ".." — всегда первая строка, независимо от колонки и направления сортировки.
        # Внимание: в Qt при Descending сортировке lessThan(A,B)=True означает что A ПОСЛЕ B.
        # Поэтому для ".." возвращаем True в Asc, False в Desc.
        if l_item.get("is_parent_nav"):
            return self.sortOrder() == Qt.AscendingOrder
        if r_item.get("is_parent_nav"):
            return self.sortOrder() != Qt.AscendingOrder

        # Папки всегда сверху, файлы снизу (независимо от направления сортировки)
        if l_item["is_dir"] != r_item["is_dir"]:
            if self.sortOrder() == Qt.AscendingOrder:
                return l_item["is_dir"]
            else:
                return not l_item["is_dir"]

        if col == 0:  # Статус
            order = {"downloaded": 0, "syncing": 1, "cloud_only": 2}
            return order.get(l_item["status"], 99) < order.get(r_item["status"], 99)
        elif col == 1:  # Имя
            l_name = l_item["name"].lower()
            r_name = r_item["name"].lower()
            return l_name < r_name
        elif col == 2:  # Размер
            return l_item["size"] < r_item["size"]
        elif col == 3:  # Изменён
            return (l_item["modified"] or "") < (r_item["modified"] or "")
        return super().lessThan(left, right)

    def sort(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder):
        """Default: колонка «Изменён» — сначала по убыванию (свежие сверху).

        При повторном клике Qt сама переключает Asc↔Desc — не мешаем.
        """
        if column == 3 and self.sortColumn() != 3 and order == Qt.AscendingOrder:
            order = Qt.DescendingOrder
        super().sort(column, order)


# ── Диалог авторизации ──────────────────────────────────

class AuthDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Авторизация Яндекс.Диска")
        self.setMinimumSize(520, 420)
        self.token = None

        layout = QVBoxLayout(self)

        instructions = QTextBrowser(self)
        instructions.setOpenExternalLinks(True)
        instructions.setHtml("""
        <h2>Первый запуск</h2>
        <p>Создайте OAuth-токен для доступа к вашему Яндекс.Диску:</p>
        <ol>
          <li>Откройте <a href='https://oauth.yandex.ru/'>oauth.yandex.ru</a></li>
          <li>Нажмите <b>«Создать»</b> → выберите <b>«Для доступа к API или отладки»</b></li>
          <li>Заполните название сервиса (любое, например "MyDisk")</li>
          <li>В поле <b>«Название доступа»</b> выберите права:<br>
              <code>cloud_api:disk.info</code><br>
              <code>cloud_api:disk.read</code><br>
              <code>cloud_api:disk.write</code></li>
          <li>Нажмите <b>«Создать приложение»</b></li>
          <li>Скопируйте <b>ClientID</b> из карточки приложения</li>
          <li>Откройте в браузере:<br>
              <code>https://oauth.yandex.ru/authorize?response_type=token&amp;client_id=ВАШ_CLIENT_ID</code></li>
          <li>Нажмите <b>«Войти как ...»</b> — произойдёт редирект</li>
          <li>На открывшейся странице скопируйте токен и вставьте ниже</li>
        </ol>
        """)
        layout.addWidget(instructions)

        self.token_input = QLineEdit(self)
        self.token_input.setPlaceholderText("Вставьте OAuth-токен сюда")
        layout.addWidget(QLabel("OAuth-токен:"))
        layout.addWidget(self.token_input)

        btn_layout = QHBoxLayout()

        check_btn = QPushButton("Проверить и сохранить", self)
        check_btn.clicked.connect(self._verify)
        btn_layout.addWidget(check_btn)

        btn_layout.addStretch()

        cancel_btn = QPushButton("Отмена", self)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

    def _verify(self):
        token = self.token_input.text().strip()
        if not token:
            QMessageBox.warning(self, "Ошибка", "Введите токен")
            return
        try:
            api = disk_api.YaDiskAPI(token)
            info = api.get_disk_info()
            total = info.get("total_space", 0) / (1024**3)
            used = info.get("used_space", 0) / (1024**3)
            QMessageBox.information(
                self, "Успех!",
                f"✅ Токен работает!\n\n"
                f"Всего: {total:.1f} GB\nЗанято: {used:.1f} GB\n"
                f"Свободно: {total - used:.1f} GB",
            )
            self.token = token
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка",
                                 f"Не удалось подключиться:\n{e}")


# ── Диалог настроек ──────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, parent=None, on_cache_changed=None):
        super().__init__(parent)
        self._on_cache_changed = on_cache_changed
        self._old_cache = _cache_dir()
        self.setWindowTitle("Настройки")
        self.setMinimumSize(400, 250)

        layout = QVBoxLayout(self)

        # Автозапуск
        grp = QGroupBox("Запуск", self)
        frm = QFormLayout(grp)
        self._chk_autorun = QCheckBox("Автозапуск при старте Windows")
        self._chk_autorun.setChecked(_autorun_is_enabled())
        frm.addRow(self._chk_autorun)
        layout.addWidget(grp)

        # Внешний вид
        grp3 = QGroupBox("Внешний вид", self)
        frm3 = QFormLayout(grp3)
        self._theme_combo = QComboBox()
        self._theme_combo.addItem("Системная", "system")
        self._theme_combo.addItem("Светлая", "light")
        self._theme_combo.addItem("Тёмная", "dark")
        current = db.get_theme()
        idx = self._theme_combo.findData(current)
        if idx >= 0:
            self._theme_combo.setCurrentIndex(idx)
        frm3.addRow('Тема оформления:', self._theme_combo)
        # Live-переключение темы при выборе в комбобоксе
        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        self._chk_save_geo = QCheckBox('Сохранять положение окна')
        self._chk_save_geo.setChecked(db.get_save_window_geometry())
        frm3.addRow(self._chk_save_geo)
        layout.addWidget(grp3)

        # Расположение файлов
        grp2 = QGroupBox("Расположение файлов", self)
        frm2 = QFormLayout(grp2)

        cache_layout = QHBoxLayout()
        self._cache_edit = QLineEdit(_cache_dir())
        browse_btn = QPushButton("Обзор…")
        cache_layout.addWidget(self._cache_edit, 1)
        cache_layout.addWidget(browse_btn)
        frm2.addRow("Папка кеша:", cache_layout)

        frm2.addRow("База данных:", QLabel(db.DB_PATH))
        frm2.addRow("Конфиг:", QLabel(db.CONFIG_PATH))
        layout.addWidget(grp2)

        # Загрузка
        grp4 = QGroupBox("Загрузка", self)
        frm4 = QFormLayout(grp4)
        self._chk_zip = QCheckBox(
            "Показывать «Скачать как ZIP» в контекстном меню папок")
        self._chk_zip.setChecked(db.get_zip_download_enabled())
        self._chk_zip.setToolTip(
            "Добавляет пункт «Скачать как ZIP» в контекстное меню папок.\n"
            "Архив скачивается в выбранную вами папку на ПК,\n"
            "не затрагивая кеш и базу данных.")
        frm4.addRow(self._chk_zip)
        layout.addWidget(grp4)

        layout.addStretch()

        btn_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        btn_box.accepted.connect(self._save)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        # Сигнал для кнопки обзора
        browse_btn.clicked.connect(self._browse_cache)

    def _browse_cache(self):
        chosen = QFileDialog.getExistingDirectory(
            self, 'Выберите папку для файлов',
            self._cache_edit.text(),
        )
        if chosen:
            self._cache_edit.setText(chosen)

    def _on_theme_changed(self):
        """Live-переключение темы — сразу при выборе в комбобоксе."""
        theme = self._theme_combo.currentData()
        db.set_theme(theme)
        if self.parent() and hasattr(self.parent(), '_apply_theme'):
            self.parent()._apply_theme()

    def _save(self):
        _autorun_set(self._chk_autorun.isChecked())
        new_cache = self._cache_edit.text().strip()
        if new_cache and new_cache != self._old_cache:
            os.makedirs(new_cache, exist_ok=True)
            db.set_cache_dir(new_cache)
            if self._on_cache_changed:
                self._on_cache_changed(self._old_cache, new_cache)
        # Сохранить тему
        db.set_theme(self._theme_combo.currentData())
        # Сохранить настройку геометрии
        db.set_save_window_geometry(self._chk_save_geo.isChecked())
        # Сохранить настройку ZIP-скачивания
        db.set_zip_download_enabled(self._chk_zip.isChecked())
        if self.parent() and hasattr(self.parent(), "_apply_theme"):
            self.parent()._apply_theme()
        self.accept()


# ── Рабочий поток для задачи ──────────────────────────────

class _WorkerThread(QThread):
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


class _SyncThread(QThread):
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
        except AuthError as e:
            logger.error("SyncThread auth error: %s", e)
            self.auth_error.emit(str(e))
        except Exception as e:
            logger.error("SyncThread error: %s", e)
            self.finished.emit({"matched": 0, "uploaded": 0,
                                "downloaded": 0, "moved": 0,
                                "deleted": 0})


class _ApiListThread(QThread):
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


class _AllFilesThread(QThread):
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


class _FolderDownloadThread(QThread):
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
                self._database.upsert_files_batch(all_items)
            logger.info("FolderDownloadThread: %d items collected", len(all_items))
        except Exception as e:
            logger.error("FolderDownloadThread failed: %s", e)
            self.finished.emit([], [])
            return

        # Классифицируем через БД (рекурсия безопасна — глубина мала)
        to_download: list[str] = []
        to_remove: list[str] = []
        try:
            for root in self._folder_paths:
                self._collect_via_db(root, to_download, to_remove,
                                     self._download_mode)
        except Exception as e:
            logger.error("FolderDownloadThread classify failed: %s", e)
        self.finished.emit(to_download, to_remove)

    def _collect_via_db(self, cloud_path: str, dl: list[str], rm: list[str],
                        download_mode: bool):
        """Рекурсивно собрать файлы под cloud_path через БД (без API).

        Если download_mode=True — собираем только файлы для скачивания.
        Если download_mode=False — собираем только файлы для удаления.
        """
        import os
        for f in self._database.get_children(cloud_path):
            if f["type"] == "dir":
                self._collect_via_db(f["cloud_path"], dl, rm, download_mode)
            elif download_mode and f["status"] in ("cloud_only", "syncing"):
                # Режим скачивания: только cloud_only, которых нет локально
                lp = _local_path(f["cloud_path"])
                if os.path.exists(lp):
                    # Уже есть локально — просто обновляем БД, не качаем
                    logger.info("FolderDownloadThread: file exists locally, "
                                "updating DB: %s", f["cloud_path"])
                    self._database.set_downloaded(f["cloud_path"], lp)
                    # НЕ добавляем в rm — не удаляем!
                else:
                    dl.append(f["cloud_path"])
            elif not download_mode and f["status"] == "downloaded":
                # Режим удаления: только downloaded/modified
                rm.append(f["cloud_path"])


class _RecentFilesThread(QThread):
    """Фоновый поток для загрузки недавно изменённых файлов (быстрый polling)."""
    finished = Signal(list)

    def __init__(self, api, parent=None):
        super().__init__(parent)
        self._api = api

    def run(self):
        try:
            items = self._api.get_recent_uploaded(limit=50)
            self.finished.emit(items)
        except Exception:
            self.finished.emit([])


class _MetaFetchThread(QThread):
    """Фоновый поток для запроса метаданных файла в облаке."""
    finished = Signal(dict)

    def __init__(self, api, cloud_path, parent=None):
        super().__init__(parent)
        self._api = api
        self._cloud_path = cloud_path

    def run(self):
        try:
            meta = self._api.get_meta(self._cloud_path)
            self.finished.emit({
                "cloud_md5": meta.get("md5", ""),
                "cloud_modified": meta.get("modified", ""),
                "cloud_path": self._cloud_path,
            })
        except Exception:
            self.finished.emit({
                "cloud_md5": "",
                "cloud_modified": "",
                "cloud_path": self._cloud_path,
            })


class _StartupScanThread(QThread):
    """Фоновый поток для стартового сканирования: stale, changed, new files."""
    finished = Signal(set, set, set)  # stale, changed, new_files

    def __init__(self, database, cache_dir, parent=None):
        super().__init__(parent)
        self._database = database
        self._cache_dir = cache_dir

    def run(self):
        logger.info("StartupScanThread: scanning local cache...")
        stale: set[str] = set()
        changed: set[str] = set()
        new_files: set[str] = set()

        try:
            # 1. All downloaded files from DB — check existence + md5
            for rec in self._database.get_by_status("downloaded"):
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
                for fname in files:
                    if fname.startswith(".") or fname.endswith("~") or fname.endswith(".tmp"):
                        continue
                    fpath = os.path.join(root, fname).replace("\\", "/")
                    if fpath.lower() in known_local:
                        continue
                    new_files.add(fpath)

        except Exception as e:
            logger.error("StartupScanThread error: %s", e)

        logger.info("StartupScanThread: stale=%d, changed=%d, new=%d",
                     len(stale), len(changed), len(new_files))
        self.finished.emit(stale, changed, new_files)

    @staticmethod
    def _local_path(cloud_path: str) -> str:
        """Compute local path from cloud path (same as ui._local_path)."""
        rel = cloud_path.lstrip("/").replace("/", "\\")
        return os.path.join(os.path.expanduser("~"), ".yadisk-cache", rel)

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


class _AutoDownloadThread(QThread):
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


# ── Главное окно ────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, api: disk_api.YaDiskAPI,
                 database: db.Database,
                 cache_dir: str = ""):
        super().__init__()
        self._api = api
        self._db = database
        self._cache_dir = cache_dir or _cache_dir()
        self._current_path = "/"
        # После загрузки всех файлов переключаемся на навигацию через API
        self._last_requested_path = "/"
        self._syncing: set[str] = set()
        self._pend_upload: set[str] = set()
        self._active_threads: list[_WorkerThread] = []
        self._open_after_download: set[str] = set()  # cloud_path файлов для авто-открытия после скачивания
        self._toggle_state = None  # legacy — kept for safety
        self._first_close_hint = True  # показать уведомление в трее при первом сворачивании
        self._force_close = False  # True = реальный выход, не сворачивание

        # История навигации
        self._nav_history: list[str] = []
        self._nav_index: int = -1
        self._nav_history_suppress: bool = False

        # Буфер для Copy/Paste/Cut — список (cloud_path, action)
        self._clipboard_buffer: list[tuple[str, str]] = []  # (cloud_path, 'copy'|'cut')

        # Лимит одновременных скачиваний — чтобы не захламлять пул соединений
        self._max_concurrent = 4
        self._download_queue: list[tuple[str, str]] = []  # (cloud_path, local_path)
        self._active_downloads = 0

        # Debounce обновления тулбара — _update_toolbar_buttons вызывается
        # до 100 раз за раз, группируем в один
        self._toolbar_timer = QTimer(self)
        self._toolbar_timer.setSingleShot(True)
        self._toolbar_timer.timeout.connect(self._update_toolbar_buttons)
        self._toolbar_debounce_ms = 50

        # Потокобезопасная очередь для результатов из рабочих потоков (вместо QueuedConnection)
        self._download_results: list[tuple[str, str, str, str, bool]] = []  # (cloud_path, local_path, md5, action, is_error)
        self._result_timer = QTimer(self)
        self._result_timer.timeout.connect(self._process_result_queue)
        self._result_timer.start(100)  # poll every 100ms

        self._watcher_queue: list[tuple[str, str, str | None, bool]] = []
        self._watcher_timer = QTimer(self)
        self._watcher_timer.timeout.connect(self._flush_watcher_queue)
        self._watcher_timer.start(200)  # poll watcher queue every 200ms

        # ── type-ahead для таблицы ────────────────────────
        self._typeahead_buf = ""
        self._typeahead_timer = QTimer(self)
        self._typeahead_timer.setSingleShot(True)
        self._typeahead_timer.timeout.connect(self._clear_typeahead)

        # Предотвращение рекурсии при перекрёстном снятии выделения
        self._selection_updating = False

        self._init_ui()
        self._init_tray()
        self._check_cache_dir()
        self._init_watcher()

        # Стартовое сканирование локального кеша + облака
        QTimer.singleShot(500, self._startup_scan)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_cloud)
        self._poll_timer.start(POLL_INTERVAL_MS)

        # Асинхронная загрузка данных — окно покажется сразу
        self.statusBar().showMessage("Загрузка...")
        QTimer.singleShot(0, self._initial_load)

    def _reauthorize(self) -> bool:
        """Показать диалог авторизации и обновить API-клиент.

        Возвращает True, если токен получен, False если пользователь отменил.
        """
        auth = AuthDialog()
        if auth.exec() != QDialog.Accepted:
            return False

        new_token = auth.token
        db.set_token(new_token)

        # Создаём новый API-клиент с новым токеном
        import disk_api
        self._api = disk_api.YaDiskAPI(new_token)
        logger.info("Token renewed")
        return True

    def _handle_auth_error(self):
        """Обработать ошибку авторизации: показать диалог и перезагрузить."""
        self._tray_show()  # показываем окно
        msg = QMessageBox(
            QMessageBox.Warning,
            "Требуется авторизация",
            "Токен доступа недействителен или истёк.\n"
            "Хотите ввести новый токен?",
            parent=self,
        )
        msg.addButton("Да", QMessageBox.YesRole)
        msg.addButton("Нет", QMessageBox.NoRole)
        reply = msg.exec()
        if reply != 0:  # 0 = "Да" (первая кнопка = YesRole)
            return False

        if self._reauthorize():
            self.statusBar().showMessage("Токен обновлён, перезагружаю...")
            self.refresh_current()
            return True
        return False

    def _initial_load(self):
        """Асинхронная загрузка: дерево → файлы → синхронизация."""
        logger.info("Initial load: starting async fetch of /")
        self.statusBar().showMessage("Загрузка папок...")
        self._show_tree_loading()
        self._show_table_loading()
        self._fetch_folder_list("/", self._on_tree_loaded)

    def _fetch_folder_list(self, path, on_done):
        """Запросить список папок/файлов в фоновом потоке."""
        logger.info("Fetching %s...", path)
        thread = _ApiListThread(self._api, path, self)
        thread.finished.connect(lambda items, err: on_done(items, err))
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _is_auth_error(self, error_msg: str) -> bool:
        """Проверить, является ли сообщение об ошибке ошибкой авторизации."""
        return error_msg and "HTTP 401" in error_msg

    def _on_tree_loaded(self, items, error):
        """Обновить дерево папок после фоновой загрузки."""
        if error:
            logger.error("Tree load failed: %s", error)
            if self._is_auth_error(error):
                self._handle_auth_error()
                return
            self.statusBar().showMessage(f"Ошибка загрузки папок: {error}")
        else:
            self.tree_model.populate_children("/", items)
            logger.info("Tree loaded: root has %d children",
                        len(self.tree_model._root.children))
        self._hide_tree_loading()
        # После дерева — загружаем файлы
        self.statusBar().showMessage("Загрузка файлов...")
        self._show_table_loading()
        self._fetch_folder_list("/", self._on_file_list_loaded)

    def _on_file_list_loaded(self, items, error):
        """Обновить таблицу файлов после фоновой загрузки."""
        if error:
            logger.error("File list load failed: %s", error)
            if self._is_auth_error(error):
                self._handle_auth_error()
                return
            self.statusBar().showMessage(f"Ошибка загрузки файлов: {error}")
        else:
            self._apply_file_list("/", items)
            logger.info("File list loaded: %d items", len(items))
        self._hide_table_loading()
        # Загружаем полный список всех файлов в фоне (для локальной навигации)
        thread = _AllFilesThread(self._api, self._db, self)
        thread.finished.connect(self._on_all_files_loaded)
        thread.progress.connect(self._on_all_files_progress)
        thread.finished.connect(thread.deleteLater)
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()

    def _on_all_files_loaded(self, count: int):
        """Полный список файлов загружен (или ошибка) — переключаемся в локальный режим."""
        if count:
            logger.info("Local cache ready: %d files", count)
            self.statusBar().showMessage(f"Загружено {count} файлов")
        else:
            self.statusBar().showMessage("Список файлов загружен не полностью")
        # Обновляем текущий вид из БД (мгновенно, без API) — даже частичный список полезен
        self._load_folder_local(self._current_path)

    def _on_all_files_progress(self, offset: int, count: int):
        """Обновление прогресса загрузки всех файлов в статус-баре."""
        self.statusBar().showMessage(f"Загрузка файлов: {count} ({offset} обработано)")

    # ── loading helpers ─────────────────────────────────

    def _make_loading_widget(self, text: str = "Загрузка...") -> QWidget:
        """Виджет с индикатором загрузки (спиннер + текст)."""
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignCenter)
        spinner = QProgressBar()
        spinner.setRange(0, 0)  # Indeterminate
        spinner.setMaximumWidth(250)
        spinner.setTextVisible(False)
        spinner.setFixedHeight(6)
        layout.addWidget(spinner)
        label = QLabel(f"{text}")
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("font-size: 14px; color: palette(Mid);")
        layout.addWidget(label)
        return w

    def _show_tree_loading(self):
        self._tree_stack.setCurrentIndex(0)

    def _hide_tree_loading(self):
        self._tree_stack.setCurrentIndex(1)

    def _show_table_loading(self):
        self._table_stack.setCurrentIndex(0)

    def _hide_table_loading(self):
        self._table_stack.setCurrentIndex(1)

    def _on_tree_item_expanded(self, index: QModelIndex):
        """При разворачивании папки — асинхронно грузим её подпапки."""
        item: FolderTreeItem = index.internalPointer() if index.isValid() else None
        if not item or item.loaded:
            return
        path = item.cloud_path
        self._fetch_folder_list(path, lambda items, err: self._on_tree_children_loaded(items, err, path))

    def _on_tree_children_loaded(self, items, error, cloud_path: str):
        """Подпапки загружены — добавляем в модель дерева."""
        if error:
            return
        self.tree_model.populate_children(cloud_path, items)
        # Если после загрузки нет подпапок → скрываем стрелку раскрытия
        item = self.tree_model._find_item(cloud_path)
        if item and item.loaded and not item._has_children:
            idx = self.tree_model._index_of(item)
            self.tree_view.collapse(idx)

    def _sort_table_sync(self, path: str, items: list):
        """Заменить данные таблицы и отсортировать синхронно, без фликера.

        QSortFilterProxyModel c dynamicSortFilter=True при endResetModel()
        ставит отложенную сортировку через zero-timer (_q_sort). Таблица
        отрисовывается ДО срабатывания таймера — визуальный фликер.

        Решение: dynamicSortFilter=False перед set_path() предотвращает
        создание таймера; sort() после set_path() работает синхронно
        (dynamicSortFilter=False → прямой вызов d->sort()); затем включаем
        auto-sort для будущих изменений данных.
        """
        col = self.table_sort_model.sortColumn()
        order = self.table_sort_model.sortOrder()
        if col < 0:
            col = 1
            order = Qt.AscendingOrder
        self.table_sort_model.setDynamicSortFilter(False)
        self.table_model.set_path(path, items)
        self.table_sort_model.sort(col, order)
        self.table_sort_model.setDynamicSortFilter(True)

    def _apply_file_list(self, path: str, items: list):
        """Применить список файлов к БД и таблице (главный поток)."""
        # Массовая вставка в одной транзакции — без тормозов
        self._db.upsert_files_batch(items)
        self._sort_table_sync(path, items)
        self._current_path = path
        self.statusBar().showMessage(f"{path} — {len(items)} эл.", 5000)
        self._schedule_toolbar_update()

    def _run_full_sync(self):
        """Запустить полную синхронизацию в фоновом потоке."""
        self._run_sync_threaded(
            self._cache_dir,
            lambda r: self._on_sync_done(r),
        )

    def _on_sync_done(self, result: dict):
        """По завершении фоновой синхронизации."""
        summary = (
            f"{result.get('matched', 0)} совпало, "
            f"{result.get('uploaded', 0)} загружено, "
            f"{result.get('downloaded', 0)} скачано, "
            f"{result.get('moved', 0)} перемещено, "
            f"{result.get('deleted', 0)} удалено")
        self.statusBar().showMessage(f"Синхронизация: {summary}", 8000)
        # Авто-загрузка в фоновом потоке — не блокирует UI
        self._start_auto_download()
        self._load_folder_local(self._current_path)

    # ── Auto-download helpers ──────────────────────────────

    def _start_auto_download(self):
        """Запустить анализ папок для авто-загрузки в фоновом потоке."""
        thread = _AutoDownloadThread(self._db, self)
        thread.finished.connect(self._on_auto_download_ready)
        thread.finished.connect(thread.deleteLater)
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()

    def _on_auto_download_ready(self, paths: list[str]):
        """Запустить скачивание для файлов, найденных AutoDownloadThread."""
        if not paths:
            return
        for cp in paths:
            self._start_download(cp, _local_path(cp))

    # ── UI setup ──────────────────────────────────────────

    def _init_ui(self):
        self.setWindowTitle("YaDisk Manager")
        self.setMinimumSize(900, 600)
        self.resize(1100, 700)
        self._update_app_icons()

        splitter = QSplitter(Qt.Horizontal, self)

        # ── Дерево папок (с loading overlay) ──────────────
        self.tree_model = FolderTreeModel(self._api, self._db, self)
        self.tree_view = QTreeView()
        self.tree_view.setModel(self.tree_model)
        self.tree_view.setHeaderHidden(True)
        self.tree_view.setAnimated(True)
        self.tree_view.setIndentation(16)
        # Стиль: явно задаём фон выделения — Qt отключает родную Windows-отрисовку
        # (зелёная полоска слева — артефакт Windows-стиля, убирается заданием background)
        self.tree_view.setStyleSheet("""
            QTreeView::item:selected {
                background: palette(Highlight);
                color: palette(HighlightedText);
            }
            QTreeView::item:selected:!active {
                background: palette(Midlight);
                color: palette(Text);
            }
        """)
        self.tree_view.clicked.connect(self._on_folder_clicked)
        self.tree_view.expanded.connect(self._on_tree_item_expanded)
        self.tree_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree_view.customContextMenuRequested.connect(
            self._on_tree_context_menu)
        self.tree_view.selectionModel().selectionChanged.connect(
            self._on_tree_selection_changed)
        # Клик по пустому месту — сброс выделения
        self.tree_view.viewport().installEventFilter(self)

        self._tree_stack = QStackedWidget()
        self._tree_stack.addWidget(self._make_loading_widget("Загрузка папок..."))  # 0
        self._tree_stack.addWidget(self.tree_view)                                    # 1
        self._tree_stack.setCurrentIndex(0)
        splitter.addWidget(self._tree_stack)

        # ── Таблица файлов (с loading overlay) ────────────
        self.table_model = FileTableModel(self._db, self)
        self.table_sort_model = FileTableSortModel(self)
        self.table_sort_model.setSourceModel(self.table_model)
        self.table_view = QTableView()
        self.table_view.setModel(self.table_sort_model)
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table_view.setAlternatingRowColors(True)
        self.table_view.verticalHeader().hide()
        self.table_view.setSortingEnabled(True)
        self.table_view.setWordWrap(False)
        self.table_view.horizontalHeader().setSortIndicatorShown(True)
        self.table_view.sortByColumn(1, Qt.AscendingOrder)
        self.table_view.selectionModel().selectionChanged.connect(
            self._on_table_selection_changed)
        # Resizable столбцы
        for c in range(self.table_model.columnCount()):
            self.table_view.horizontalHeader().setSectionResizeMode(
                c, QHeaderView.Interactive)
        # Ширина столбцов по умолчанию
        self.table_view.setColumnWidth(0, 28)  # Статус (иконка) — минимальная ширина
        self.table_view.setColumnWidth(1, 300)  # Имя
        self.table_view.setColumnWidth(3, 100)  # Изменён
        self.table_view.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Fixed)
        self.table_view.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents)
        self.table_view.doubleClicked.connect(self._on_file_double_clicked)
        self.table_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(
            self._on_context_menu)
        # Подсветка строки при наведении — через делегат QStyledItemDelegate
        # (обычный QTableView::item:hover подсвечивает только ячейку, не строку)
        self._hover_delegate = RowHoverDelegate(self.table_view)
        self.table_view.setItemDelegate(self._hover_delegate)
        self.table_view.setMouseTracking(True)
        self.table_view.entered.connect(self._on_table_hovered)
        self.table_view.setStyleSheet("""
            QTableView::item:selected {
                background: palette(Highlight);
                color: palette(HighlightedText);
            }
            QHeaderView::section {
                font-weight: normal;
                font-size: 12px;
                padding: 3px 6px;
                border: none;
                border-right: 1px solid palette(Midlight);
                border-bottom: 1px solid palette(Midlight);
            }
            QHeaderView::section:last {
                border-right: none;
            }
            QHeaderView::section:highlighted {
                font-weight: normal;
            }
        """)

        self.table_view.viewport().installEventFilter(self)
        self.table_view.installEventFilter(self)

        self._table_stack = QStackedWidget()
        self._table_stack.addWidget(self._make_loading_widget("Загрузка файлов..."))  # 0
        self._table_stack.addWidget(self.table_view)                                    # 1
        self._table_stack.setCurrentIndex(0)
        splitter.addWidget(self._table_stack)

        splitter.setSizes([220, 680])
        self.setCentralWidget(splitter)

        # Меню
        self._create_menu()
        self._create_toolbar()

        # Drag-and-drop из Проводника
        self.setAcceptDrops(True)

        # Статусбар
        self._status_label = QLabel("")
        self.statusBar().addWidget(self._status_label, 1)
        self._status_progress = QProgressBar()
        self._status_progress.setMaximumWidth(200)
        self._status_progress.setMinimumWidth(120)
        self._status_progress.setVisible(False)
        self.statusBar().addPermanentWidget(self._status_progress)

        # Применить сохранённую тему при запуске
        self._apply_theme()
        # Восстановить положение окна
        self._restore_window_geometry()

        # Клавиатурные сокращения для навигации
        self._shortcut_back = QAction("Назад", self)
        self._shortcut_back.setShortcut(QKeySequence("Alt+Left"))
        self._shortcut_back.triggered.connect(self._nav_back)
        self.addAction(self._shortcut_back)

        self._shortcut_forward = QAction("Вперёд", self)
        self._shortcut_forward.setShortcut(QKeySequence("Alt+Right"))
        self._shortcut_forward.triggered.connect(self._nav_forward)
        self.addAction(self._shortcut_forward)

        # Backspace для «на уровень вверх» уже есть в _handle_table_key
        # Добавляем Alt+Up как дополнительное сокращение
        self._shortcut_up = QAction("Вверх", self)
        self._shortcut_up.setShortcut(QKeySequence("Alt+Up"))
        self._shortcut_up.triggered.connect(self._nav_up)
        self.addAction(self._shortcut_up)

    # ── Theme ─────────────────────────────────────────────

    def _restore_window_geometry(self):
        """Восстановить положение и размер окна из конфига."""
        if not db.get_save_window_geometry():
            return
        data = db.get_window_geometry()
        if not data:
            return
        self.restoreGeometry(QByteArray.fromBase64(data.encode()))
        # Страховка: если окно оказалось вне видимых экранов — сброс на центр
        center = self.geometry().center()
        screen = QGuiApplication.screenAt(center)
        if not screen:
            self.move(
                QGuiApplication.primaryScreen().availableGeometry().center()
                - self.rect().center())

    @staticmethod
    def _dark_qss() -> str:
        return ""

    @staticmethod
    def _light_qss() -> str:
        return ""

    def _apply_theme(self):
        """Применить тему (system / light / dark) из конфига.

        Fusion style уже установлен в main.py (до создания виджетов).
        Здесь только меняем QPalette — мгновенно, без QSS.
        """
        theme = db.get_theme()
        app = QApplication.instance()

        if theme == "light":
            palette = self._make_palette_light()
            app.setPalette(palette)
        elif theme == "dark":
            palette = self._make_palette_dark()
            app.setPalette(palette)
        elif self._is_windows_dark_mode():
            self._apply_dark_system_palette(app)
        else:
            app.setPalette(app.style().standardPalette())

        # Сбросить и переприменить QSS на всех виджетах — иначе palette() в
        # per-widget stylesheets не обновляется после app.setPalette().
        # Qt кеширует palette() в QSS при первом применении setStyleSheet().
        # Без этого тулбар и другие виджеты с palette() ссылками остаются старыми.
        app.setStyleSheet("")
        for w in app.allWidgets():
            if w.style():
                w.style().unpolish(w)
                w.style().polish(w)

        # QToolTip QSS — после реполошинга, чтобы не быть сброшенным app.setStyleSheet("")
        if theme == "light":
            self._apply_tooltip_qss(bg="#ffffe5", text="#000000", border="#C0C0C0")
        elif theme == "dark":
            self._apply_tooltip_qss(bg="#383838", text="#CCCCCC", border="#555555")
        elif self._is_windows_dark_mode():
            self._apply_tooltip_qss(bg="#383838", text="#CCCCCC", border="#555555")
        else:
            self._apply_tooltip_qss(bg="#ffffe5", text="#000000", border="#C0C0C0")

        self._update_app_icons()
        self._refresh_toolbar_icons()

    @staticmethod
    def _make_palette_light() -> QPalette:
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(0xF0, 0xF0, 0xF0))
        palette.setColor(QPalette.WindowText, QColor(0x00, 0x00, 0x00))
        palette.setColor(QPalette.Base, QColor(0xFF, 0xFF, 0xFF))
        palette.setColor(QPalette.AlternateBase, QColor(0xF5, 0xF5, 0xF5))
        palette.setColor(QPalette.ToolTipBase, QColor(0xFF, 0xFF, 0xDC))
        palette.setColor(QPalette.ToolTipText, QColor(0x00, 0x00, 0x00))
        palette.setColor(QPalette.Text, QColor(0x00, 0x00, 0x00))
        palette.setColor(QPalette.Button, QColor(0xF0, 0xF0, 0xF0))
        palette.setColor(QPalette.ButtonText, QColor(0x00, 0x00, 0x00))
        palette.setColor(QPalette.BrightText, QColor(0xFF, 0x00, 0x00))
        palette.setColor(QPalette.Highlight, QColor(0x00, 0x66, 0xFF))
        palette.setColor(QPalette.HighlightedText, QColor(0xFF, 0xFF, 0xFF))
        palette.setColor(QPalette.Link, QColor(0x00, 0x66, 0xFF))
        palette.setColor(QPalette.LinkVisited, QColor(0x80, 0x00, 0x80))
        palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(0x80, 0x80, 0x80))
        palette.setColor(QPalette.Disabled, QPalette.Text, QColor(0x80, 0x80, 0x80))
        palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(0x80, 0x80, 0x80))
        return palette

    @staticmethod
    def _make_palette_dark() -> QPalette:
        # Тёмная палитра — VS Code / Fluent Design
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(0x1E, 0x1E, 0x1E))
        palette.setColor(QPalette.WindowText, QColor(0xCC, 0xCC, 0xCC))
        palette.setColor(QPalette.Base, QColor(0x25, 0x25, 0x26))
        palette.setColor(QPalette.AlternateBase, QColor(0x2A, 0x2A, 0x2E))
        palette.setColor(QPalette.ToolTipBase, QColor(0x38, 0x38, 0x38))
        palette.setColor(QPalette.ToolTipText, QColor(0xCC, 0xCC, 0xCC))
        palette.setColor(QPalette.Text, QColor(0xCC, 0xCC, 0xCC))
        palette.setColor(QPalette.Button, QColor(0x2D, 0x2D, 0x30))
        palette.setColor(QPalette.ButtonText, QColor(0xCC, 0xCC, 0xCC))
        palette.setColor(QPalette.BrightText, QColor(0xFF, 0x00, 0x00))
        palette.setColor(QPalette.Highlight, QColor(0x09, 0x47, 0x71))
        palette.setColor(QPalette.HighlightedText, QColor(0xFF, 0xFF, 0xFF))
        palette.setColor(QPalette.Link, QColor(0x00, 0xA0, 0xFF))
        palette.setColor(QPalette.LinkVisited, QColor(0x80, 0x60, 0xFF))
        palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(0x60, 0x60, 0x60))
        palette.setColor(QPalette.Disabled, QPalette.Text, QColor(0x60, 0x60, 0x60))
        palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(0x60, 0x60, 0x60))
        palette.setColor(QPalette.Disabled, QPalette.Highlight, QColor(0x50, 0x50, 0x50))
        palette.setColor(QPalette.Disabled, QPalette.HighlightedText, QColor(0x80, 0x80, 0x80))
        return palette

    @staticmethod
    def _apply_tooltip_qss(bg: str, text: str, border: str):
        """Применить QSS для QToolTip, так как на Windows vs. Fusion
        QPalette.ToolTipBase может игнорироваться системным стилем."""
        app = QApplication.instance()
        if app is None:
            return
        qss = (
            f"QToolTip {{"
            f"  background-color: {bg};"
            f"  color: {text};"
            f"  border: 1px solid {border};"
            f"  padding: 2px 4px;"
            f"}}"
        )
        app.setStyleSheet(qss)

    @staticmethod
    def _apply_dark_system_palette(app):
        """Установить тёмную палитру для системной темы на Windows в dark mode."""
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(0x1E, 0x1E, 0x1E))
        palette.setColor(QPalette.WindowText, QColor(0xCC, 0xCC, 0xCC))
        palette.setColor(QPalette.Base, QColor(0x25, 0x25, 0x26))
        palette.setColor(QPalette.AlternateBase, QColor(0x2A, 0x2A, 0x2E))
        palette.setColor(QPalette.ToolTipBase, QColor(0x38, 0x38, 0x38))
        palette.setColor(QPalette.ToolTipText, QColor(0xCC, 0xCC, 0xCC))
        palette.setColor(QPalette.Text, QColor(0xCC, 0xCC, 0xCC))
        palette.setColor(QPalette.Button, QColor(0x2D, 0x2D, 0x30))
        palette.setColor(QPalette.ButtonText, QColor(0xCC, 0xCC, 0xCC))
        palette.setColor(QPalette.BrightText, QColor(0xFF, 0x00, 0x00))
        palette.setColor(QPalette.Highlight, QColor(0x09, 0x47, 0x71))
        palette.setColor(QPalette.HighlightedText, QColor(0xFF, 0xFF, 0xFF))
        palette.setColor(QPalette.Link, QColor(0x00, 0xA0, 0xFF))
        palette.setColor(QPalette.LinkVisited, QColor(0x80, 0x60, 0xFF))
        palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(0x60, 0x60, 0x60))
        palette.setColor(QPalette.Disabled, QPalette.Text, QColor(0x60, 0x60, 0x60))
        palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(0x60, 0x60, 0x60))
        app.setPalette(palette)

    def _create_menu(self):
        mb = self.menuBar()

        file_menu = mb.addMenu("Файл")
        self._act_toggle = QAction("Сохранить на компьютере", self)
        self._act_toggle.setIcon(_svg_icon("loaded.svg", 24))
        self._act_toggle.triggered.connect(self._toggle_local_copy)
        file_menu.addAction(self._act_toggle)
        file_menu.addSeparator()
        self._act_copy = QAction("Копировать", self)
        self._act_copy.setIcon(_svg_icon("Copy.svg", 24))
        self._act_copy.setShortcut(QKeySequence("Ctrl+C"))
        self._act_copy.triggered.connect(self._copy_to_buffer)
        file_menu.addAction(self._act_copy)

        self._act_cut = QAction("Вырезать", self)
        self._act_cut.setIcon(_svg_icon("scissors-3.svg", 24))
        self._act_cut.setShortcut(QKeySequence("Ctrl+X"))
        self._act_cut.triggered.connect(self._cut_to_buffer)
        file_menu.addAction(self._act_cut)

        self._act_paste = QAction("Вставить", self)
        self._act_paste.setIcon(_svg_icon("paste.svg", 24))
        self._act_paste.setShortcut(QKeySequence("Ctrl+V"))
        self._act_paste.triggered.connect(self._paste_from_buffer)
        file_menu.addAction(self._act_paste)

        file_menu.addSeparator()

        self._act_delete = QAction("Удалить", self)
        self._act_delete.setIcon(_svg_icon("trash.svg", 24))
        self._act_delete.setShortcut(QKeySequence("Delete"))
        self._act_delete.triggered.connect(self._delete_selected)
        file_menu.addAction(self._act_delete)

        file_menu.addSeparator()
        self._act_refresh = QAction("Обновить", self)
        self._act_refresh.setIcon(_svg_icon("refresh.svg", 24))
        self._act_refresh.setShortcut(QKeySequence("F5"))
        self._act_refresh.triggered.connect(self.refresh_current)
        file_menu.addAction(self._act_refresh)

        file_menu.addSeparator()
        act_logout = QAction("Выйти из аккаунта", self)
        act_logout.setIcon(_svg_icon("log-out-svgrepo-com.svg", 24))
        act_logout.triggered.connect(self._logout_and_reauth)
        file_menu.addAction(act_logout)
        act_quit = QAction("Выход", self)
        act_quit.setIcon(_svg_icon("close-red.svg", 24))
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self._menu_quit)
        file_menu.addAction(act_quit)

        tools_menu = mb.addMenu("Инструменты")
        self._act_new_folder = QAction("Новая папка", self)
        self._act_new_folder.setIcon(_svg_icon("folder-add-yellow.svg", 24))
        self._act_new_folder.triggered.connect(self._create_folder)
        tools_menu.addAction(self._act_new_folder)

        tools_menu.addSeparator()
        self._act_get_link = QAction("Скопировать ссылку", self)
        self._act_get_link.setIcon(_svg_icon("link.svg", 24))
        self._act_get_link.triggered.connect(self._get_public_link)
        tools_menu.addAction(self._act_get_link)

        tools_menu.addSeparator()
        self._act_settings = QAction("Настройки", self)
        self._act_settings.setIcon(_svg_icon("settings.svg", 24))
        self._act_settings.triggered.connect(self._show_settings)
        tools_menu.addAction(self._act_settings)

        help_menu = mb.addMenu("Помощь")
        self._act_about = QAction("О программе", self)
        self._act_about.setIcon(_svg_icon("help-svgrepo-com.svg", 24))
        self._act_about.triggered.connect(self._show_about)
        help_menu.addAction(self._act_about)

    # ── Toolbar ────────────────────────────────────────────

    @staticmethod
    def _make_emoji_icon(emoji: str, size: int = 36) -> QIcon:
        """Создать QIcon из эмодзи (запасной метод, если SVG не загрузился)."""
        canvas = size + 8
        pixmap = QPixmap(canvas, canvas)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.TextAntialiasing)
        font = QFont("Segoe UI Emoji", size - 10)
        painter.setFont(font)
        painter.drawText(QRectF(0, 0, canvas, canvas), Qt.AlignCenter, emoji)
        painter.end()
        return QIcon(pixmap)

    def _create_toolbar(self):
        tb = QToolBar("Основная", self)
        tb.setIconSize(QSize(36, 36))
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        tb.setMovable(False)
        tb.setStyleSheet("""
            QToolBar {
                background: palette(Window);
                spacing: 0px;
                left: -4px;
            }
            QToolBar::separator {
                margin: 0 2px;
            }
            QToolButton {
                font-size: 12px; padding: 2px 6px;
                color: palette(WindowText);
                background: transparent;
            }
            QToolButton:hover {
                background: palette(AlternateBase);
            }
            QToolButton:pressed {
                background: palette(Midlight);
            }
            QToolButton:disabled {
                color: palette(Disabled, WindowText);
            }
            QLineEdit {
                background: palette(Base);
                color: palette(Text);
                border: 1px solid palette(Midlight);
                border-radius: 4px;
                padding: 5px 8px;
                min-height: 24px;
            }
            QLineEdit:focus {
                border: 2px solid palette(Highlight);
            }
        """)
        self.addToolBar(tb)

        def _nav_btn(svg_name, label, tip, cb):
            """Кнопка навигации без текста, только иконка."""
            icon = _svg_icon(svg_name, 36)
            a = QAction(icon, label, self)
            a.setToolTip(tip)
            a.triggered.connect(cb)
            tb.addAction(a)
            w = tb.widgetForAction(a)
            if w:
                w.setToolButtonStyle(Qt.ToolButtonIconOnly)
            return a

        def _icon_btn(svg_name, label, tip, cb, show_label=False):
            """Кнопка c SVG-иконкой."""
            icon = _svg_icon(svg_name, 36)
            if icon.isNull():
                icon = self._make_emoji_icon("⬜", 36)
            a = QAction(icon, label, self)
            a.setToolTip(tip)
            a.triggered.connect(cb)
            tb.addAction(a)
            w = tb.widgetForAction(a)
            if w:
                style = Qt.ToolButtonTextUnderIcon if show_label else Qt.ToolButtonIconOnly
                w.setToolButtonStyle(style)
            return a

        # Кнопки навигации — компактные иконки (small)
        self._tb_nav_back = _nav_btn("arrow-prev-svgrepo-com.svg",
                                     "Назад", "Назад (Alt+Left)", self._nav_back)
        self._tb_nav_forward = _nav_btn("arrow-next-svgrepo-com.svg",
                                        "Вперёд", "Вперёд (Alt+Right)", self._nav_forward)
        self._tb_nav_up = _nav_btn("arrow-up-svgrepo-com.svg",
                                   "Вверх", "На уровень вверх (Backspace)", self._nav_up)
        tb.addSeparator()

        # Настройки — иконка без подписи
        self._tb_settings = _icon_btn("settings.svg",
                                      "Настройки",
                                      "Настройки", self._show_settings)
        tb.addSeparator()

        # Скопировать ссылку — иконка + подпись
        self._tb_link = _icon_btn("link.svg",
                                  "Скопировать ссылку",
                                  "Скопировать ссылку", self._get_public_link,
                                  show_label=True)
        tb.addSeparator()

        # Кнопка-переключатель: Сохранить / Оставить только в облаке — иконка + подпись
        self._tb_toggle = _icon_btn("loaded.svg",
                                    "Сохранить на компьютере",
                                    "Сохранить выделенные на компьютер",
                                    self._toggle_local_copy,
                                    show_label=True)

        # Растягиваемый spacer — прижимает поиск к правому краю
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        # Поиск
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Поиск по имени…")
        self._search_edit.setMaximumWidth(220)
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._on_search)
        tb.addWidget(self._search_edit)

        # Отступ от правого края окна
        right_margin = QWidget()
        right_margin.setFixedWidth(6)
        tb.addWidget(right_margin)

        # Начальное состояние кнопок
        self._update_toolbar_buttons()

    # ── Toolbar: обновление иконок ─────────────────────────

    def _refresh_toolbar_icons(self):
        """Перезагрузить иконки тулбара при смене темы."""
        if not hasattr(self, '_tb_nav_back'):
            return
        self._tb_nav_back.setIcon(_svg_icon("arrow-prev-svgrepo-com.svg", 36))
        self._tb_nav_forward.setIcon(_svg_icon("arrow-next-svgrepo-com.svg", 36))
        self._tb_nav_up.setIcon(_svg_icon("arrow-up-svgrepo-com.svg", 36))
        self._tb_settings.setIcon(_svg_icon("settings.svg", 36))
        self._tb_link.setIcon(_svg_icon("link.svg", 36))
        self._tb_toggle.setIcon(_svg_icon("loaded.svg", 36))
        # Обновить состояние кнопки-переключателя (могла быть cloud.svg)
        self._update_toolbar_buttons()

    # ── Toolbar: состояние кнопок ──────────────────────────

    def _on_tree_selection_changed(self, selected, deselected):
        """При изменении выделения в дереве → снять выделение в таблице."""
        if self._selection_updating:
            return
        self._selection_updating = True
        self.table_view.clearSelection()
        self._selection_updating = False
        self._update_toolbar_buttons()

    def _on_table_selection_changed(self, selected, deselected):
        """При изменении выделения в таблице → снять выделение в дереве."""
        if self._selection_updating:
            return
        self._selection_updating = True
        self.tree_view.clearSelection()
        self._selection_updating = False
        self._update_toolbar_buttons()

    def _update_toolbar_buttons(self):
        """Включить/выключить кнопки в зависимости от выделения."""
        table_rows = self.table_view.selectionModel().selectedRows(0)
        tree_rows = self.tree_view.selectionModel().selectedRows(0)
        has_table_sel = bool(table_rows)
        has_tree_sel = bool(tree_rows)

        # Публичная ссылка: только для таблицы
        self._tb_link.setEnabled(has_table_sel)

        # Кнопки навигации
        self._update_nav_buttons()

        # Переключатель Сохранить/Оставить в облаке
        self._update_toggle_button(table_rows, has_tree_sel)

    def _update_toggle_button(self, table_rows, has_tree_sel):
        """Обновить текст, иконку и активность кнопки-переключателя.

        - Если все выделенные файлы `cloud_only` → ✅ Сохранить на компьютере
        - Если все выделенные файлы `downloaded`/`modified` → ☁️ Оставить только в облаке
        - Если смесь, или папки с разным статусом → disabled
        - Элемент ".." игнорируется
        """
        # Собираем статусы всех выделенных элементов
        statuses: set[str] = set()

        # Таблица
        for idx in table_rows:
            item = self._get_item(idx)
            if not item:
                continue
            if item.get("is_parent_nav"):
                continue  # ".." — не участвует
            if not item.get("is_dir"):
                statuses.add(item["status"])
            else:
                # Папка: определяем реальный статус через БД
                cp = item["cloud_path"]
                if self._db.is_folder_fully_synced(cp):
                    statuses.add("downloaded")
                else:
                    statuses.add("cloud_only")

        # Дерево папок: смотрим статус каждой выделенной папки
        if has_tree_sel:
            tree_rows = self.tree_view.selectionModel().selectedRows(0)
            for idx in tree_rows:
                cp = idx.data(Qt.UserRole)
                if cp:
                    if self._db.is_folder_fully_synced(cp):
                        statuses.add("downloaded")
                    else:
                        statuses.add("cloud_only")

        if not statuses:
            self._tb_toggle.setEnabled(False)
            self._act_toggle.setEnabled(False)
            return

        all_cloud = all(s == "cloud_only" for s in statuses)
        all_local = all(s == "downloaded" for s in statuses)

        if all_cloud:
            icon = _svg_icon("loaded.svg", 36)
            self._tb_toggle.setIcon(icon)
            self._tb_toggle.setText("Сохранить на компьютере")
            self._tb_toggle.setToolTip("Скачать выделенные файлы на компьютер")
            self._tb_toggle.setEnabled(True)
            self._act_toggle.setIcon(_svg_icon("loaded.svg", 24))
            self._act_toggle.setText("Сохранить на компьютере")
            self._act_toggle.setEnabled(True)
        elif all_local and not all_cloud:
            icon = _svg_icon("cloud.svg", 36)
            self._tb_toggle.setIcon(icon)
            self._tb_toggle.setText("Оставить только в облаке")
            self._tb_toggle.setToolTip("Удалить локальную копию, оставить в облаке")
            self._tb_toggle.setEnabled(True)
            self._act_toggle.setIcon(_svg_icon("cloud.svg", 24))
            self._act_toggle.setText("Оставить только в облаке")
            self._act_toggle.setEnabled(True)
        else:
            self._tb_toggle.setEnabled(False)
            self._act_toggle.setEnabled(False)

    # ── Tray ──────────────────────────────────────────────

    def _init_tray(self):
        self._tray = QSystemTrayIcon(self)
        self._update_app_icons(update_tray_only=True)
        self._tray.setToolTip("YaDisk Manager")

        self._tray_menu = QMenu(self)
        self._tray_act_show = QAction("Показать окно", self)
        self._tray_act_show.setIcon(_svg_icon("home-svgrepo-com.svg", 24))
        font = self._tray_act_show.font()
        font.setBold(True)
        self._tray_act_show.setFont(font)
        self._tray_act_show.triggered.connect(self._tray_show)
        self._tray_menu.addAction(self._tray_act_show)

        self._tray_menu.addSeparator()

        act_settings = QAction("Настройки", self)
        act_settings.setIcon(_svg_icon("settings.svg", 24))
        act_settings.triggered.connect(self._show_settings)
        self._tray_menu.addAction(act_settings)

        self._tray_menu.addSeparator()

        act_exit = QAction("Выход", self)
        act_exit.setIcon(_svg_icon("close-red.svg", 24))
        act_exit.triggered.connect(self._tray_exit)
        self._tray_menu.addAction(act_exit)

        self._tray.setContextMenu(self._tray_menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    # ── Иконки окна и трея ────────────────────────────

    @staticmethod
    def _is_windows_dark_mode() -> bool:
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return val == 0
        except Exception:
            return False

    @classmethod
    def _pick_app_icon(cls) -> QIcon:
        ico = "YD-NL-DT.ico" if cls._is_windows_dark_mode() else "YD-NL-LT.ico"
        path = os.path.join(os.path.dirname(__file__), ico)
        if os.path.isfile(path):
            return QIcon(path)
        return QIcon()

    def _update_app_icons(self, update_tray_only: bool = False):
        if not update_tray_only:
            self.setWindowIcon(self._pick_app_icon())
        if hasattr(self, "_tray") and self._tray:
            self._tray.setIcon(self._pick_app_icon())

    def changeEvent(self, event):
        if event.type() == QEvent.Type.PaletteChange:
            self._update_app_icons()
            # При смене системной темы — переприменить текущую тему оформления
            QTimer.singleShot(0, self._apply_theme)
        super().changeEvent(event)

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            # Одиночный клик ЛКМ — показать окно (если скрыто или свёрнуто)
            if not self.isVisible() or self.isMinimized():
                self._tray_show()
            elif not self.isActiveWindow():
                # Уже видимо, но за другим окном — просто поднять наверх
                self.raise_()
                self.activateWindow()
            # Если окно уже в фокусе — ничего не делаем
        # DoubleClick — игнорируем (вообще ничего не делаем)

    def _tray_show(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _tray_exit(self):
        """Полностью закрыть программу (Выход из трея)."""
        self._force_close = True
        self._tray.hide()
        self.close()

    def _menu_quit(self):
        """Полностью закрыть программу (Выход)."""
        self._force_close = True
        self._tray.hide()
        self.close()

    def _logout_and_reauth(self):
        """Выйти из аккаунта: очистить токен и показать диалог авторизации."""
        reply = QMessageBox.question(
            self, "Выйти из аккаунта",
            "Вы уверены, что хотите выйти из аккаунта?\n"
            "Токен будет удалён, потребуется повторная авторизация.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        # Очищаем старый токен
        db.set_token("")
        # Обнуляем API-клиент
        import disk_api
        token = db.get_token()
        if not token:
            # Показываем диалог авторизации
            auth = AuthDialog()
            if auth.exec() != AuthDialog.Accepted:
                # Пользователь отменил — выходим
                self._menu_quit()
                return
            token = auth.token
            db.set_token(token)
        self._api = disk_api.YaDiskAPI(token)
        # Обновляем дерево
        self._load_folder_async(self._current_path)
        logger.info("Account changed, token renewed")

    # ── Startup scan ───────────────────────────────────────

    def _startup_scan(self):
        """Сканирование локального кеша при запуске — фоновый поток."""
        cache_dir = _cache_dir()
        if not os.path.isdir(cache_dir):
            return

        logger.info("Startup scan: launching background thread...")
        self.statusBar().showMessage("Проверка локальных файлов...")

        # Добавляем ref на поток, чтобы не собрался GC
        thread = _StartupScanThread(self._db, cache_dir, self)
        thread.finished.connect(lambda stale, changed, new_files:
                                self._on_startup_scan_done(stale, changed, new_files))
        thread.finished.connect(thread.deleteLater)
        self._startup_scan_thread = thread
        thread.start()

    def _on_startup_scan_done(self, stale: set, changed: set, new_files: set):
        """Обработка результатов стартового сканирования (главный поток)."""
        if getattr(self, '_cache_was_restored', False) and stale:
            # Кеш был удалён, пользователь выбрал "Восстановить" —
            # ставим все файлы на перекачку (через очередь, без блокировки)
            self.statusBar().showMessage(f"Восстанавливаю {len(stale)} файлов...")
            for cp in stale:
                if cp in self._syncing:
                    continue
                local_path = _local_path(cp)
                self._syncing.add(cp)
                self._download_queue.append((cp, local_path))
                self.table_model.update_status(cp, "syncing")
            self._process_download_queue()
            logger.info("Restore: queued %d files for re-download", len(stale))
        else:
            # Stale — обновляем статус
            for cp in stale:
                self._db.set_cloud_only(cp)
                self.table_model.update_item_status(cp, "cloud_only")
                self._update_tree_status(cp)
                logger.info("Startup: local file missing, set cloud_only: %s", cp)

        # Changed — ставим на загрузку
        for cp in changed:
            self._pend_upload.add(cp)
            logger.info("Startup: local file changed, will upload: %s", cp)

        # New files — вычисляем cloud_path и ставим на загрузку
        cache_dir_norm = _cache_dir().replace("\\", "/")
        for fpath in new_files:
            rel = fpath[len(cache_dir_norm):].lstrip("/")
            cloud_path = "/" + rel
            self._pend_upload.add(cloud_path)
            logger.info("Startup: new local file detected, will upload: %s", cloud_path)

        if changed or new_files:
            QTimer.singleShot(2000, self._flush_pending_upload)

        # Проверка облака: новые файлы в полностью скачанных папках
        self.statusBar().showMessage("Проверка облака...")
        QTimer.singleShot(100, self._poll_cloud)

        if not stale and not changed and not new_files:
            logger.info("Startup scan: all clean")
            self.statusBar().clearMessage()

    # ── Проверка кеша при старте ──────────────────────────

    def _check_cache_dir(self):
        """Если папка кеша удалена, а в БД есть скачанные файлы — спросить пользователя."""
        cache_dir = _cache_dir()
        if os.path.isdir(cache_dir):
            self._cache_was_restored = False
            return

        downloaded = self._db.count_by_status("downloaded")
        if downloaded == 0:
            os.makedirs(cache_dir, exist_ok=True)
            self._cache_was_restored = False
            return

        # Показываем диалог
        msg = QMessageBox(self)
        msg.setWindowTitle("Локальный кеш не найден")
        msg.setText(
            f"Папка локального кеша не существует:\n{cache_dir}\n\n"
            f"В базе числится {downloaded} скачанных файлов.\n"
            f"Что делать?"
        )
        restore_btn = msg.addButton(
            "🔄 Восстановить — перекачать все файлы заново", QMessageBox.AcceptRole)
        clear_btn = msg.addButton(
            "🗑️ Очистить — сбросить статусы, начать с чистого листа", QMessageBox.RejectRole)
        cancel_btn = msg.addButton("Отмена", QMessageBox.NoRole)
        msg.setDefaultButton(restore_btn)
        msg.setDetailedText(
            "• Восстановить — все файлы, которые были скачаны, снова "
            "скачаются из облака. Долго, но данные не потеряются.\n"
            "• Очистить — статусы скачанных файлов сбрасываются, "
            "база очищается. Начать с чистого листа.\n"
            "• Отмена — программа создаст пустую папку, "
            "ничего скачивать не будет."
        )
        msg.exec()

        clicked = msg.clickedButton()

        if clicked == restore_btn:
            os.makedirs(cache_dir, exist_ok=True)
            self._cache_was_restored = True
            logger.info("Cache dir missing, user chose RESTORE (%d files)", downloaded)
        elif clicked == clear_btn:
            for rec in self._db.get_by_status("downloaded"):
                self._db.set_cloud_only(rec["cloud_path"])
            os.makedirs(cache_dir, exist_ok=True)
            self._cache_was_restored = False
            logger.info("Cache dir missing, user chose CLEAR (%d files reset)", downloaded)
        else:  # cancel
            os.makedirs(cache_dir, exist_ok=True)
            self._cache_was_restored = False
            logger.info("Cache dir missing, user chose CANCEL")

    # ── Watcher ───────────────────────────────────────────

    def _init_watcher(self):
        self._watcher = watcher.FileWatcher(
            _cache_dir(), self._on_local_file_event)
        self._watcher.start()

    def _on_local_file_event(self, event_type: str, path: str,
                            is_directory: bool = False, dest: str | None = None):
        """Вызывается из watchdog — кладём в очередь для main thread."""
        self._watcher_queue.append((event_type, path, dest, is_directory))

    def _flush_watcher_queue(self):
        """Раз в 200ms выбираем накопившиеся события из watcher-очереди (main thread)."""
        if not self._watcher_queue:
            return
        batch = list(self._watcher_queue)
        self._watcher_queue.clear()
        for event_type, local_path, dest_path, is_directory in batch:
            try:
                if event_type == "modified":
                    self._handle_file_modified(local_path)
                elif event_type == "created":
                    self._handle_file_created(local_path)
                elif event_type == "deleted":
                    self._handle_file_deleted(local_path, is_directory)
                elif event_type == "moved":
                    self._handle_file_moved(local_path, dest_path)  # src, dst
            except Exception as e:
                logger.warning("Watcher handler error: %s", e)

    def _handle_file_modified(self, local_path: str):
        """Локальный файл изменён — начать загрузку в облако."""
        rec = self._db.get_by_local_path(local_path)
        if not rec:
            # Неизвестный файл — возможно, только что создан и сразу изменён
            logger.info("Unknown local file modified, treating as created: %s", local_path)
            self._handle_file_created(local_path)
            return
        cloud_path = rec["cloud_path"]
        if cloud_path in self._syncing:
            return  # ложное срабатывание после скачивания
        self._pend_upload.add(cloud_path)
        QTimer.singleShot(2000, lambda: self._flush_pending_upload())

    def _handle_file_created(self, local_path: str):
        """Новый локальный файл — проверить, есть ли в облаке, если нет — загрузить."""
        rec = self._db.get_by_local_path(local_path)
        if rec:
            # Уже известен — значит это изменение, а не создание
            cloud_path = rec["cloud_path"]
            if cloud_path in self._syncing:
                return
            self._pend_upload.add(cloud_path)
            QTimer.singleShot(2000, lambda: self._flush_pending_upload())
            return

        # Новый файл в кеше — загружаем в облако (но не подпапки)
        cache_dir = _cache_dir().replace("\\", "/")
        local_path_posix = local_path.replace("\\", "/")
        if not local_path_posix.startswith(cache_dir):
            logger.debug("Created file outside cache: %s", local_path)
            return
        rel = local_path_posix[len(cache_dir):].lstrip("/")
        cloud_path = "/" + rel.replace("\\", "/")
        logger.info("New local file detected, uploading: %s → %s", local_path, cloud_path)
        self._pend_upload.add(cloud_path)
        QTimer.singleShot(2000, lambda: self._flush_pending_upload())

    def _handle_file_deleted(self, local_path: str, is_directory: bool = False):
        """Локальный файл/папка удалён — обновить статус в БД."""
        if is_directory:
            # Удалена целая папка — ищем все файлы в БД с этим префиксом
            dir_normalized = local_path.replace("\\", "/")
            affected = self._db.get_by_local_path_prefix(dir_normalized)
            if not affected:
                logger.info("Directory deleted (no tracked files): %s", local_path)
                return
            count = 0
            for rec in affected:
                cp = rec["cloud_path"]
                self._db.set_cloud_only(cp)
                self.table_model.update_item_status(cp, "cloud_only")
                self._update_tree_status(cp)
                count += 1
            logger.info("Directory deleted, %d files set to cloud_only: %s", count, local_path)
            self._update_toolbar_buttons()
            return

        # Одиночное удаление файла
        rec = self._db.get_by_local_path(local_path)
        if rec:
            cloud_path = rec["cloud_path"]
            self._db.set_cloud_only(cloud_path)
            self.table_model.update_item_status(cloud_path, "cloud_only")
            self._update_tree_status(cloud_path)
            self._update_toolbar_buttons()
            logger.info("Local file deleted, set cloud_only: %s", cloud_path)

    def _handle_file_moved(self, src_path: str, dst_path: str):
        """Локальный файл переименован/перемещён."""
        old_rec = self._db.get_by_local_path(src_path)
        if not old_rec:
            # Старый файл не отслеживался — может быть, новый появился
            logger.info("Moved from unknown path: %s → %s", src_path, dst_path)
            self._handle_file_created(dst_path)
            return
        cloud_path = old_rec["cloud_path"]
        new_rel = os.path.relpath(dst_path, _cache_dir()).replace("\\", "/")
        new_cloud = "/" + new_rel
        logger.info("File moved locally: %s → %s (cloud: %s → %s)",
                    src_path, dst_path, cloud_path, new_cloud)
        # Обновляем путь в БД
        self._db.remove_file(cloud_path)
        item_type = old_rec.get("type", "file")
        self._db.upsert_file(
            new_cloud, os.path.basename(dst_path), item_type,
            size=old_rec.get("size", 0),
            modified=old_rec.get("modified", ""),
            md5=old_rec.get("md5", ""),
        )
        self._db.set_downloaded(new_cloud, dst_path,
                                last_sync_md5=old_rec.get("last_sync_md5", ""))
        self._navigate_to_folder(self._current_path)

    def _flush_pending_upload(self):
        if not self._pend_upload:
            return
        batch = list(self._pend_upload)
        self._pend_upload.clear()
        for cp in batch:
            self._upload_single(cp)

    def _upload_single(self, cloud_path: str):
        info = self._db.get_file(cloud_path)
        if info and info.get("local_path") and os.path.exists(info["local_path"]):
            local_path = info["local_path"]
        else:
            # Новый файл (нет в БД) — вычисляем локальный путь напрямую
            local_path = _local_path(cloud_path)
        if not os.path.exists(local_path):
            return

        last_sync_md5 = ""
        if info:
            last_sync_md5 = info.get("last_sync_md5") or info.get("md5") or ""
        name = Path(cloud_path).name

        # ── показываем прогресс проверки ─────────────────
        self._show_progress_localized(f"Проверка {name}...")

        # ── запрашиваем метаданные в фоне ────────────────
        thread = _MetaFetchThread(self._api, cloud_path, self)
        thread.finished.connect(
            lambda result: self._on_meta_fetched(
                cloud_path, local_path, last_sync_md5, result
            )
        )
        thread.finished.connect(thread.deleteLater)
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()

    def _on_meta_fetched(self, cloud_path: str, local_path: str,
                          last_sync_md5: str, result: dict):
        """Обработка результата запроса метаданных (главный поток)."""
        self._hide_progress()

        cloud_md5 = result.get("cloud_md5", "")
        cloud_modified = result.get("cloud_modified", "")
        local_md5 = _md5_file(local_path)

        # Конфликт: облако изменилось с момента синхронизации
        if cloud_md5 and last_sync_md5 and cloud_md5 != last_sync_md5:
            self._handle_conflict(cloud_path, local_path, local_md5, cloud_md5)
            return

        # ── загружаем ────────────────────────────────────
        self._start_upload(cloud_path, local_path, local_md5)

    def _handle_conflict(self, cloud_path, local_path,
                         local_md5: str, cloud_md5: str):
        """Диалог конфликта — локальный и облачный файл изменились."""
        self._tray_show()  # показываем окно если свёрнуто

        reply = QMessageBox.question(
            self, "Конфликт",
            f"Файл изменился одновременно в облаке и локально:\n\n"
            f"  {cloud_path}\n\n"
            f"Что делать?\n\n"
            f"  • Да — оставить локальную версию (загрузить в облако)\n"
            f"  • Нет — скачать облачную версию (заменить локальную)\n"
            f"  • Отмена — ничего не делать",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )

        if reply == QMessageBox.Yes:
            # Сохраняем локальную, перезаписываем облачную
            self._start_upload(cloud_path, local_path, local_md5)
        elif reply == QMessageBox.No:
            # Скачиваем облачную, перезаписываем локальную
            self._start_download(cloud_path, local_path)
        # Cancel — ничего не делаем

    def _start_upload(self, cloud_path: str, local_path: str,
                      local_md5: str = ""):
        """Запустить upload в фоновом потоке."""
        if not local_md5:
            local_md5 = _md5_file(local_path)

        worker = UploadWorker(self._api, local_path, cloud_path)

        self._syncing.add(cloud_path)
        self._show_progress_localized(f"Загружаю {Path(cloud_path).name}")
        # Сразу показываем статус 🔄 в таблице
        self.table_model.update_status(cloud_path, "syncing")
        # Показываем статус 🔄 в дереве
        self._update_tree_status(cloud_path)

        def _ui_progress(done, total):
            if total > 0:
                self._status_progress.setRange(0, 100)
                self._status_progress.setValue(int(done / total * 100))
            else:
                self._status_progress.setRange(0, 0)

        def on_progress(done, total):
            QTimer.singleShot(0, lambda d=done, t=total: _ui_progress(d, t))

        def on_finished(cp):
            """Выполняется в рабочем потоке — кладём результат в потокобезопасную очередь."""
            local_md5 = _md5_file(local_path)
            self._download_results.append((cp, local_path, local_md5, "upload", False))

        def on_error(msg):
            self._download_results.append((cloud_path, "", "", "upload", True))

        # DirectConnection — on_finished/on_error выполняются в рабочем потоке.
        # Результаты читаются главным потоком через _process_result_queue.
        worker.progress.connect(on_progress, Qt.DirectConnection)
        worker.finished.connect(on_finished, Qt.DirectConnection)
        worker.error.connect(on_error, Qt.DirectConnection)

        thread = _WorkerThread(worker)
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()

    def _start_download(self, cloud_path: str, local_path: str):
        """Запустить download в фоновом потоке (с учётом лимита конкуренции)."""
        # Защита от повторного запуска для того же файла
        if cloud_path in self._syncing:
            logger.warning("start_download: already syncing %s", cloud_path)
            return

        self._syncing.add(cloud_path)
        # Сразу показываем статус 🔄 в таблице
        self.table_model.update_status(cloud_path, "syncing")
        # Показываем статус 🔄 в дереве для родительской папки
        self._update_tree_status(cloud_path)

        if self._active_downloads >= self._max_concurrent:
            # Ставим в очередь — освободится, когда один из активных закончит
            self._download_queue.append((cloud_path, local_path))
            logger.debug("start_download: queued %s (active=%d/%d)",
                        cloud_path, self._active_downloads, self._max_concurrent)
            return

        self._active_downloads += 1
        self._do_download(cloud_path, local_path)

    def _do_download(self, cloud_path: str, local_path: str):
        """Реально запустить download-воркер (без проверки лимита)."""
        logger.info("do_download: path=%s local=%s", cloud_path, local_path)
        # Берём дату изменения из облака (из модели или из БД)
        cloud_modified = ""
        for row in range(self.table_model.rowCount()):
            item = self.table_model.get_item(row)
            if item and item["cloud_path"] == cloud_path:
                cloud_modified = item.get("modified", "")
                break
        if not cloud_modified:
            db_row = self._db.get_file(cloud_path)
            if db_row:
                cloud_modified = db_row.get("modified", "")
        worker = DownloadWorker(self._api, cloud_path, local_path, cloud_modified)

        self._show_progress_localized(f"Скачиваю {Path(cloud_path).name}")

        def _ui_progress(done, total):
            if total > 0:
                self._status_progress.setRange(0, 100)
                self._status_progress.setValue(int(done / total * 100))
            else:
                self._status_progress.setRange(0, 0)

        def on_progress(done, total):
            QTimer.singleShot(0, lambda d=done, t=total: _ui_progress(d, t))

        def on_finished(lp):
            """Выполняется в рабочем потоке — кладёт результат в потокобезопасную очередь."""
            md5 = _md5_file(lp)
            self._download_results.append((cloud_path, lp, md5, "download", False))

        def on_error(msg):
            self._download_results.append((cloud_path, "", "", "download", True))

        # DirectConnection — on_finished/on_error выполняются в рабочем потоке.
        # Результаты читаются главным потоком через _process_result_queue.
        worker.progress.connect(on_progress, Qt.DirectConnection)
        worker.finished.connect(on_finished, Qt.DirectConnection)
        worker.error.connect(on_error, Qt.DirectConnection)

        thread = _WorkerThread(worker)
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()

    def _process_result_queue(self):
        """Обработать накопившиеся результаты скачиваний/загрузок (главный поток)."""
        if not self._download_results:
            return
        batch = list(self._download_results)
        self._download_results.clear()
        for cloud_path, local_path, md5, action, is_error in batch:
            try:
                if is_error:
                    self._syncing.discard(cloud_path)
                    self._hide_progress()
                    self._schedule_toolbar_update()
                    logger.warning("Worker error: %s %s", action, cloud_path)
                    continue

                if action == "download":
                    new_size = os.path.getsize(local_path)
                    # Берём mtime из файла (уже установлен DownloadWorker из cloud_modified)
                    try:
                        st = os.stat(local_path)
                        file_mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
                    except Exception:
                        file_mtime = datetime.now(timezone.utc).isoformat()
                    self._db.upsert_file(
                        cloud_path=cloud_path,
                        name=Path(cloud_path).name,
                        type_="file",
                        size=new_size,
                        modified=file_mtime,
                        md5=md5,
                    )
                    self._db.set_downloaded(cloud_path, local_path, last_sync_md5=md5)
                    self._syncing.discard(cloud_path)
                    self.table_model.update_item_after_download(
                        cloud_path, new_size, file_mtime, "downloaded")
                    self._update_tree_status(cloud_path)
                    self._hide_progress()
                    self._schedule_toolbar_update()
                    logger.info("Downloaded: %s → %s", cloud_path, local_path)
                    if cloud_path in self._open_after_download:
                        self._open_after_download.discard(cloud_path)
                        self._open_file(local_path)
                    # Освобождаем слот и запускаем следующий из очереди
                    self._active_downloads = max(0, self._active_downloads - 1)
                    self._process_download_queue()
                elif action == "upload":
                    self._db.upsert_file(
                        cloud_path=cloud_path,
                        name=Path(cloud_path).name,
                        type_="file",
                        size=os.path.getsize(local_path),
                        modified=datetime.now(timezone.utc).isoformat(),
                        md5=md5,
                    )
                    self._db.set_downloaded(cloud_path, local_path, last_sync_md5=md5)
                    self._syncing.discard(cloud_path)
                    self.table_model.update_status(cloud_path, "downloaded")
                    self._update_tree_status(cloud_path)
                    self._hide_progress()
                    self._schedule_toolbar_update()
                    logger.info("Uploaded: %s", cloud_path)
                    # Обновить родительскую папку — новый файл появился
                    QTimer.singleShot(200, self._refresh_folder_view)
            except Exception as e:
                logger.error("_process_result_queue failed: %s", e, exc_info=True)
                self._syncing.discard(cloud_path)
                self._hide_progress()
            finally:
                try:
                    threads_to_remove = [t for t in self._active_threads
                                         if not t.isRunning()]
                    for t in threads_to_remove:
                        self._active_threads.remove(t)
                except ValueError:
                    pass

        # После завершения всех загрузок — обновить статус папок в таблице
        if self._active_downloads == 0 and not self._download_queue:
            QTimer.singleShot(500, self._refresh_folder_view)

    def _refresh_folder_view(self):
        """Обновить текущий вид из БД — пересчитать статусы папок.
        Debounced: не чаще 1 раза в 500 мс."""
        if getattr(self, '_refresh_view_pending', False):
            return
        self._refresh_view_pending = True
        QTimer.singleShot(500, self._do_refresh_folder_view)

    def _do_refresh_folder_view(self):
        self._refresh_view_pending = False
        if hasattr(self, '_current_path') and self._current_path:
            self._load_folder_local(self._current_path)

    def _cleanup_thread(self, thread):
        if thread in self._active_threads:
            self._active_threads.remove(thread)

    def _schedule_toolbar_update(self):
        """Debounced обновление тулбара — не вызывать 100 раз за раз."""
        self._toolbar_timer.start(self._toolbar_debounce_ms)

    def _update_tree_status(self, cloud_path: str):
        """Обновить статус родительской папки в дереве после изменения статуса файла."""
        parent = os.path.dirname(cloud_path.rstrip("/")) or "/"
        self.tree_model.invalidate_status(parent)
        self.tree_model.emit_path_changed(parent)

    def _process_download_queue(self):
        """Запустить следующий скачивание из очереди, если есть свободные слоты."""
        while self._download_queue and self._active_downloads < self._max_concurrent:
            cp, lp = self._download_queue.pop(0)
            if cp in self._syncing:
                self._active_downloads += 1
                self._do_download(cp, lp)
            else:
                logger.debug("_process_download_queue: skipping cancelled %s", cp)

    def _show_progress_localized(self, text: str):
        self._status_label.setText(text)
        self._status_progress.setVisible(True)
        self._status_progress.setRange(0, 0)

    def _hide_progress(self):
        if not self._syncing:
            self._status_progress.setVisible(False)
            self._status_label.setText("")

    # ── Навигация ─────────────────────────────────────────

    def dragEnterEvent(self, event):
        """Принять перетаскивание файлов из Проводника."""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        """Разрешить перемещение по всей области окна."""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        """Обработать сброшенные файлы/папки — загрузить их в текущую папку."""
        urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
        if not urls:
            return
        event.acceptProposedAction()
        # Собираем все файлы (рекурсивно для папок)
        all_files: list[str] = []
        for url in urls:
            local_path = url.toLocalFile()
            if os.path.isfile(local_path):
                all_files.append(local_path)
            elif os.path.isdir(local_path):
                for root, dirs, files in os.walk(local_path):
                    for f in files:
                        all_files.append(os.path.join(root, f))
        if not all_files:
            return

        # ── Проверка коллизий имён ────────────────────────
        existing_names = []
        for src in all_files:
            name = os.path.basename(src)
            cloud_path = (self._current_path.rstrip("/") + "/" + name).replace("//", "/")
            if self._db.file_exists(cloud_path):
                existing_names.append(name)

        if existing_names:
            reply = QMessageBox.question(
                self, "Конфликт имён",
                f"Файл(ы) уже существуют в папке:\n"
                + "\n".join(f"  • {n}" for n in existing_names[:10])
                + ("\n  …" if len(existing_names) > 10 else "")
                + "\n\nПерезаписать?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self.statusBar().showMessage("❌ Загрузка отменена", 3000)
                return

        # ── Загрузка ──────────────────────────────────────
        logger.info("Drop: %d file(s) into %s", len(all_files), self._current_path)
        self.statusBar().showMessage(
            f"Загрузка {len(all_files)} файла(ов) в {self._current_path}...")
        for src in all_files:
            name = os.path.basename(src)
            cloud_path = (self._current_path.rstrip("/") + "/" + name).replace("//", "/")
            local_copy = _local_path(cloud_path)
            try:
                os.makedirs(os.path.dirname(local_copy), exist_ok=True)
                import shutil
                shutil.copy2(src, local_copy)
                self._start_upload(cloud_path, local_copy)
            except Exception as e:
                logger.error("Drop upload failed for %s: %s", src, e)
                self.statusBar().showMessage(f"❌ Ошибка загрузки {name}: {e}", 5000)

    def refresh_tree(self):
        """Обновить дерево папок (асинхронно)."""
        self.tree_model.refresh()
        self._show_tree_loading()
        self._fetch_folder_list("/", lambda items, err: self._on_tree_refreshed(items, err))

    def _on_tree_refreshed(self, items, error):
        if error:
            self.statusBar().showMessage(f"❌ Ошибка обновления дерева: {error}")
        else:
            self.tree_model.populate_children("/", items)
        self._hide_tree_loading()

    def refresh_files(self, path: str):
        """Обновить таблицу файлов для пути."""
        path = path or "/"
        self._load_folder_async(path)

    def refresh_current(self):
        self._navigate_to_folder(self._current_path)

    def eventFilter(self, obj, event):
        """Сброс выделения в дереве папок при клике по пустому месту.
        Сброс подсветки строки в таблице при уходе мыши.
        Клавиатурная навигация: Enter, Backspace, F2, type-ahead."""
        if (hasattr(self, 'tree_view') and self.tree_view is not None
                and obj is self.tree_view.viewport()
                and event.type() == QEvent.MouseButtonPress
                and event.button() == Qt.LeftButton):
            idx = self.tree_view.indexAt(event.pos())
            if not idx.isValid():
                # Пустое место в дереве: снимаем выделение в ОБЕИХ панелях.
                # clearSelection() без текущего выделения не эмитит signal →
                # cross-clear в _on_*_selection_changed может не сработать.
                self._selection_updating = True
                self.tree_view.clearSelection()
                self.table_view.clearSelection()
                self._selection_updating = False
                self._update_toolbar_buttons()
        if (hasattr(self, 'table_view') and self.table_view is not None
                and obj is self.table_view.viewport()
                and event.type() == QEvent.MouseButtonPress
                and event.button() == Qt.LeftButton):
            idx = self.table_view.indexAt(event.pos())
            if not idx.isValid():
                # Пустое место в таблице: снимаем выделение в ОБЕИХ панелях
                self._selection_updating = True
                self.tree_view.clearSelection()
                self.table_view.clearSelection()
                self._selection_updating = False
                self._update_toolbar_buttons()
        if (hasattr(self, 'table_view') and self.table_view is not None
                and obj is self.table_view.viewport()
                and event.type() == QEvent.Leave):
            # Сброс подсветки строки при уходе мыши из таблицы
            if hasattr(self, '_hover_delegate'):
                self._hover_delegate.hovered_row = -1
                self.table_view.viewport().update()
        # Клавиатурные события для таблицы
        if (hasattr(self, 'table_view') and self.table_view is not None
                and obj is self.table_view
                and event.type() == QEvent.KeyPress):
            return self._handle_table_key(event)
        return super().eventFilter(obj, event)

    def _on_folder_clicked(self, index: QModelIndex):
        path = index.data(Qt.UserRole) or "/"
        self._last_requested_path = path
        # Мгновенно из кеша, фоном — обновление из облака
        self._navigate_to_folder(path)

    def _get_selected_cloud_paths(self) -> list[str]:
        """Сохранить cloud_path выделенных строк в таблице."""
        paths = []
        sel = self.table_view.selectionModel()
        if sel:
            for idx in sel.selectedRows():
                src_idx = self.table_sort_model.mapToSource(idx)
                item = self.table_model.get_item(src_idx.row())
                if item:
                    paths.append(item["cloud_path"])
        return paths

    def _restore_selection(self, paths: list[str]):
        """Восстановить выделение строк по cloud_path (через proxy)."""
        if not paths:
            return
        sel = self.table_view.selectionModel()
        if not sel:
            return
        selection = QItemSelection()
        for row in range(self.table_model.rowCount()):
            item = self.table_model.get_item(row)
            if item and item["cloud_path"] in paths:
                src_idx = self.table_model.index(row, 0)
                proxy_idx = self.table_sort_model.mapFromSource(src_idx)
                if proxy_idx.isValid():
                    selection.select(proxy_idx, proxy_idx)
        if not selection.isEmpty():
            sel.select(selection, QItemSelectionModel.Select | QItemSelectionModel.Rows)

    # ── Navigation history ──────────────────────────────────

    def _update_nav_buttons(self):
        """Включить/выключить кнопки навигации в зависимости от истории."""
        self._tb_nav_back.setEnabled(self._nav_index > 0)
        self._tb_nav_forward.setEnabled(
            self._nav_index >= 0 and self._nav_index < len(self._nav_history) - 1)
        self._tb_nav_up.setEnabled(self._current_path != "/")

    def _nav_back(self):
        """Перейти назад по истории."""
        if self._nav_index > 0:
            self._nav_index -= 1
            target = self._nav_history[self._nav_index]
            self._nav_history_suppress = True
            self._navigate_to_folder(target)

    def _nav_forward(self):
        """Перейти вперёд по истории."""
        if self._nav_index < len(self._nav_history) - 1:
            self._nav_index += 1
            target = self._nav_history[self._nav_index]
            self._nav_history_suppress = True
            self._navigate_to_folder(target)

    def _nav_up(self):
        """Перейти на уровень вверх."""
        if self._current_path != "/":
            parent = "/".join(self._current_path.rstrip("/").split("/")[:-1]) or "/"
            self._navigate_to_folder(parent)

    def _navigate_to_folder(self, path: str):
        """Мгновенно показать папку из локального кеша, затем в фоне загрузить
        свежие данные из облака и незаметно обновить таблицу.

        Сохраняет путь в истории навигации для кнопок Назад/Вперёд.
        """
        # Не добавляем в историю если это та же папка или подавлено
        if path != self._current_path and not self._nav_history_suppress:
            # Обрезаем будущее, если мы были не в конце истории
            if self._nav_index >= 0 and self._nav_index < len(self._nav_history) - 1:
                self._nav_history = self._nav_history[:self._nav_index + 1]
            # Добавляем текущий путь в историю
            self._nav_history.append(self._current_path)
            self._nav_index = len(self._nav_history) - 1
            # Обновляем кнопки навигации
            self._update_nav_buttons()
        # Сброс флага подавления истории после любого перехода
        self._nav_history_suppress = False
        # Сбрасываем поиск при навигации — иначе остаточный фильтр скрывает файлы
        # и ломает прокси→source маппинг при F2
        self._search_edit.clear()
        # Сохраняем выделение перед навигацией
        saved_selection = self._get_selected_cloud_paths()
        self._current_path = path
        self._last_requested_path = path

        # Шаг 1: мгновенно из БД (без спиннера)
        self._load_folder_local(path)
        self._restore_selection(saved_selection)

        # Шаг 2: в фоне — API
        def on_refreshed(items, error):
            if error:
                if self._is_auth_error(error):
                    self._handle_auth_error()
                    return
                logger.warning("Background refresh failed for %s: %s", path, error)
                return
            # Защита от race: если пользователь уже ушёл в другую папку
            if self._last_requested_path != path:
                return
            # Тихо обновляем БД и таблицу (без спиннера)
            try:
                self._db.upsert_files_batch(items)
                self._sort_table_sync(path, items)
                self._restore_selection(self._get_selected_cloud_paths())
                self.statusBar().showMessage(f"{path} — {len(items)} эл., обновлено", 3000)
            except Exception as e:
                logger.warning("Background apply failed: %s", e)

        self._fetch_folder_list(path, on_refreshed)

    def _open_selected_item(self):
        """Открыть выделенный элемент в таблице (аналог двойного клика)."""
        rows = self.table_view.selectionModel().selectedRows(0)
        if rows:
            self._on_file_double_clicked(rows[0])

    def _open_selected_tree_item(self):
        """Открыть выделенную папку в дереве (навигация внутрь)."""
        rows = self.tree_view.selectionModel().selectedRows(0)
        if rows:
            path = rows[0].data(Qt.UserRole)
            if path:
                self._navigate_to_folder(path)

    def _create_local_copy(self):
        """Создать копию файла/папки на компьютере без изменения статуса."""
        focus = self.focusWidget()
        cloud_path = None
        is_dir = False
        if focus is self.tree_view:
            rows = self.tree_view.selectionModel().selectedRows(0)
            if rows:
                cloud_path = rows[0].data(Qt.UserRole)
                is_dir = True
        else:
            rows = self.table_view.selectionModel().selectedRows(0)
            if rows:
                item = self._get_item(rows[0])
                if item and not item.get("is_parent_nav"):
                    cloud_path = item["cloud_path"]
                    is_dir = item.get("is_dir", False)
        if not cloud_path:
            return
        name = os.path.basename(cloud_path.rstrip("/"))
        local_src = _local_path(cloud_path)
        if is_dir:
            dest = QFileDialog.getExistingDirectory(
                self, f"Куда сохранить копию «{name}»")
            if not dest:
                return
            dest_path = os.path.join(dest, name)
            if os.path.exists(dest_path):
                QMessageBox.warning(
                    self, "Ошибка", f"Папка «{name}» уже существует в выбранном месте")
                return
            if os.path.isdir(local_src):
                shutil.copytree(local_src, dest_path, dirs_exist_ok=False)
                QMessageBox.information(
                    self, "Готово", f"Копия папки сохранена в\n{dest_path}")
            else:
                self._start_download(cloud_path, dest_path)
        else:
            default_name = name
            dest, _ = QFileDialog.getSaveFileName(
                self, f"Куда сохранить копию «{name}»", default_name)
            if not dest:
                return
            if os.path.exists(local_src):
                shutil.copy2(local_src, dest)
                QMessageBox.information(
                    self, "Готово", f"Копия файла сохранена в\n{dest}")
            else:
                self._start_download(cloud_path, dest)

    def _load_folder_async(self, path: str):
        """Загрузить список файлов папки в фоне и обновить таблицу."""
        self._last_requested_path = path
        self.statusBar().showMessage(f"Загрузка {path}...")
        self._show_table_loading()
        def on_loaded(items, error):
            # Защита от race condition: если пользователь уже выбрал другую папку — игнорируем
            if self._last_requested_path != path:
                logger.debug("Ignored stale response for %s", path)
                return
            if error:
                if self._is_auth_error(error):
                    self._handle_auth_error()
                    return
                self.statusBar().showMessage(f"❌ {error}")
                self._hide_table_loading()
            else:
                self._apply_file_list(path, items)
                self._hide_table_loading()
        self._fetch_folder_list(path, on_loaded)

    def _load_folder_local(self, path: str):
        """Мгновенная навигация по локальному кешу (без вызова API)."""
        self._current_path = path
        db_records = self._db.get_children(path)
        items = [{
            "path": r["cloud_path"],
            "name": r["name"],
            "type": r["type"],
            "size": r["size"],
            "modified": r["modified"],
            "md5": r["md5"] or "",
            "mime_type": r["mime_type"] or "",
            # stale syncing → cloud_only, если файл сейчас реально не качается
            "status": "cloud_only" if (r["status"] == "syncing"
                                       and r["cloud_path"] not in self._syncing)
                      else (r["status"] or ""),
            "local_path": r["local_path"] or "",
        } for r in db_records if not _is_windows_reserved(r["name"])]
        self._sort_table_sync(path, items)
        self.statusBar().showMessage(f"{path} — {len(items)} эл.", 5000)
        self._schedule_toolbar_update()

    def _poll_cloud(self):
        """Проверка изменений — быстрый запрос недавно изменённых файлов."""
        if not self.isVisible() and not self._tray.isVisible():
            return
        thread = _RecentFilesThread(self._api, self)
        thread.finished.connect(self._on_poll_result)
        thread.start()

    def _on_poll_result(self, items):
        """Обработать результат poll'инга — обновить кеш, если есть изменения.
        Если в полностью скачанной папке появился новый файл — авто-загрузка.
        """
        if not items:
            return
        try:
            new_items = [it for it in items if not self._db.file_exists(it["path"])]
            if new_items:
                logger.info("Poll: %d new item(s)", len(new_items))
                auto_downloads = []
                for item in new_items:
                    cp = item["path"]
                    # Проверяем статус родительской папки ДО добавления
                    parts = cp.rstrip("/").split("/")
                    parent = "/".join(parts[:-1]) if len(parts) > 2 else "/"
                    was_synced = self._db.is_folder_fully_synced(parent)

                    self._db.upsert_file(
                        cp, item.get("name", ""),
                        item.get("type", "file"),
                        size=item.get("size", 0),
                        modified=item.get("modified", ""),
                        md5=item.get("md5", ""),
                    )

                    if was_synced and item.get("type") != "dir":
                        auto_downloads.append(cp)

                for cp in auto_downloads:
                    logger.info("Auto-download: %s (parent was fully synced)", cp)
                    self._start_download(cp, _local_path(cp))

                self._load_folder_async(self._current_path)
        except Exception as e:
            logger.warning("Poll error: %s", e)

    # ── Действия ──────────────────────────────────────────

    def _toggle_local_copy(self, download_mode: bool | None = None):
        """Переключатель: скачать / удалить локальную копию.

        - Если файл ☁️ cloud_only → ✅ скачать на компьютер
        - Если файл 💾 downloaded → ☁️ удалить локально
        - Папки: один фоновый запрос get_all_files() (не блокирует UI),
          затем рекурсивный сбор через БД

        Параметр download_mode:
        - True → скачивать (Сохранить на компьютере)
        - False → удалять (Оставить только в облаке)
        - None → авто-определение (из текста self._tb_toggle)
        """
        to_download: list[str] = []
        to_remove: list[str] = []
        pending_folders: list[str] = []

        # Работаем только с активной панелью (таблица XOR дерево)
        focus = self.focusWidget()
        if focus is self.tree_view:
            # Древо: обрабатываем выделенные папки рекурсивно
            tree_rows = self.tree_view.selectionModel().selectedRows(0)
            for idx in tree_rows:
                cloud_path = idx.data(Qt.UserRole)
                if cloud_path:
                    pending_folders.append(cloud_path)
        else:
            # Таблица: только выделенные строки
            rows = self.table_view.selectionModel().selectedRows(0)
            for idx in rows:
                item = self._get_item(idx)
                if not item:
                    continue
                if item.get("is_dir"):
                    pending_folders.append(item["cloud_path"])
                else:
                    if download_mode is True:
                        # Сохранить на компьютере — только нескачанные
                        if item["status"] == "cloud_only":
                            to_download.append(item["cloud_path"])
                    elif download_mode is False:
                        # Оставить только в облаке — только скачанные
                        if item["status"] == "downloaded":
                            to_remove.append(item["cloud_path"])
                    else:
                        # Авто-определение (тулбар)
                        if item["status"] == "cloud_only":
                            to_download.append(item["cloud_path"])
                        else:
                            to_remove.append(item["cloud_path"])

        if pending_folders:
            # Определяем режим: скачивание или удаление
            if download_mode is not None:
                dl_mode = download_mode
            else:
                dl_mode = self._tb_toggle.text() == "Сохранить на компьютере"
            # Папки: BFS-обход в фоновом потоке (не блокирует UI)
            for folder_path in pending_folders:
                self._db.set_status(folder_path, "syncing")
                self.table_model.update_status(folder_path, "syncing")
            # Немедленно помечаем все видимые элементы внутри папок как syncing
            for fp in pending_folders:
                prefix = fp.rstrip("/") + "/"
                for item_model_item in self.table_model._items:
                    cp = item_model_item["cloud_path"]
                    if cp.startswith(prefix) and not item_model_item.get("is_dir"):
                        self._db.set_status(cp, "syncing")
                        self.table_model.update_status(cp, "syncing")
            self.statusBar().showMessage("Сбор файлов для обработки...")
            thread = _FolderDownloadThread(
                self._api, self._db, pending_folders,
                download_mode=dl_mode, parent=self)
            thread.finished.connect(self._on_folder_download_ready)
            thread.finished.connect(thread.deleteLater)
            self._active_threads.append(thread)
            thread.finished.connect(lambda: self._cleanup_thread(thread))
            thread.start()
        else:
            # Только отдельные файлы — выполняем сразу
            for cp in to_download:
                self._db.set_status(cp, "syncing")
                self.table_model.update_status(cp, "syncing")
                self._start_download(cp, _local_path(cp))
            for cp in to_remove:
                self._remove_local_copy(cp)
            if not to_download and not to_remove:
                QMessageBox.information(
                    self, "Нет действий",
                    "Выделите файлы или папки для скачивания\n"
                    "или удаления локальной копии.")

    def _save_to_computer(self):
        """Сохранить выделенные файлы/папки на компьютер."""
        self._toggle_local_copy(download_mode=True)

    def _leave_only_in_cloud(self):
        """Удалить локальную копию выделенных файлов/папок."""
        self._toggle_local_copy(download_mode=False)

    def _on_folder_download_ready(self, to_download: list[str], to_remove: list[str]):
        """После BFS-обхода папок — запустить скачивание/удаление."""
        for cp in to_download:
            self._db.set_status(cp, "syncing")
            self.table_model.update_status(cp, "syncing")
            self._update_tree_status(cp)
            self._start_download(cp, _local_path(cp))
        for cp in to_remove:
            self._remove_local_copy(cp)
            self._update_tree_status(cp)
        # Обновляем дерево — возможно, статусы изменились и для «уже локальных» файлов
        if to_download or to_remove:
            self.tree_model.invalidate_status(None)
            self.tree_model.dataChanged.emit(QModelIndex(), QModelIndex(), [Qt.DecorationRole])
        if not to_download and not to_remove:
            QMessageBox.information(
                self, "Нет действий",
                "Выделите файлы или папки для скачивания\n"
                "или удаления локальной копии.")

    def _remove_local_copy(self, cloud_path: str):
        """Удалить локальную копию файла, оставить только в облаке."""
        info = self._db.get_file(cloud_path)
        if not info:
            return
        local_path = info.get("local_path") or _local_path(cloud_path)
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
                logger.info("Removed local copy: %s", local_path)
            else:
                logger.warning("Local file not found, updating DB: %s", local_path)
        except OSError as e:
            logger.warning("Cannot remove %s: %s", local_path, e)
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось удалить локальный файл:\\n{local_path}\\n{e}")
            return
        self._db.set_cloud_only(cloud_path)
        self.table_model.update_item_status(cloud_path, "cloud_only")
        self._update_tree_status(cloud_path)
        self._update_toolbar_buttons()
        logger.info("File set to cloud_only: %s", cloud_path)

    # ── Copy/Paste/Cut ──────────────────────────────────────

    def _copy_to_buffer(self, action: str = "copy"):
        """Скопировать или вырезать выделенные элементы во внутренний буфер.
        action='copy' — копировать; action='cut' — вырезать (с последующим перемещением при вставке).
        """
        items = []

        # Определяем активную панель (таблица XOR дерево)
        focus = self.focusWidget()
        if focus is self.tree_view:
            tree_rows = self.tree_view.selectionModel().selectedRows(0)
            for idx in tree_rows:
                cp = idx.data(Qt.UserRole) or ""
                if cp:
                    items.append({"cloud_path": cp})
        else:
            rows = self.table_view.selectionModel().selectedRows(0)
            for idx in rows:
                item = self._get_item(idx)
                if item and not item.get("is_parent_nav"):
                    items.append(item)

        if not items:
            return

        self._clipboard_buffer = [(it["cloud_path"], action) for it in items]
        names = [os.path.basename(cp.rstrip("/")) for cp, _ in self._clipboard_buffer]
        action_label = "скопирован" if action == "copy" else "вырезан"
        self.statusBar().showMessage(
            f"{action_label}: {'; '.join(names[:3])}{'...' if len(names) > 3 else ''}",
            5000,
        )
        self._schedule_toolbar_update()

    def _cut_to_buffer(self):
        """Вырезать выделенные элементы (Ctrl+X)."""
        self._copy_to_buffer(action="cut")

    def _paste_from_buffer(self):
        """Вставить элементы из буфера в текущую папку.
        Для copy — API copy; для cut — API move (перемещение).
        Если фокус на дереве — вставляем в выбранную папку, иначе — в текущую.
        """
        if not self._clipboard_buffer:
            return

        focus = self.focusWidget()
        dest = self._current_path
        if focus is self.tree_view:
            tree_rows = self.tree_view.selectionModel().selectedRows(0)
            if tree_rows:
                cp = tree_rows[0].data(Qt.UserRole) or ""
                if cp:
                    dest = cp
        errors = []
        success_count = 0

        for src_path, action in self._clipboard_buffer:
            name = os.path.basename(src_path.rstrip("/"))
            new_path = (dest.rstrip("/") + "/" + name).replace("//", "/")
            if new_path == src_path:
                continue  # нельзя копировать/перемещать в себя же
            try:
                if action == "copy":
                    self._api.copy(src_path, new_path, overwrite=False)
                elif action == "cut":
                    self._api.move(src_path, new_path, overwrite=False)
                success_count += 1
            except YaDiskError as e:
                if "already exists" in str(e).lower():
                    # Попробовать с overwrite=True — спросить пользователя
                    reply = QMessageBox.question(
                        self, "Конфликт",
                        f"Файл «{name}» уже существует в папке назначения.\n"
                        f"Перезаписать?",
                        QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                        QMessageBox.No,
                    )
                    if reply == QMessageBox.Yes:
                        try:
                            if action == "copy":
                                self._api.copy(src_path, new_path, overwrite=True)
                            else:
                                self._api.move(src_path, new_path, overwrite=True)
                            success_count += 1
                        except Exception as e2:
                            errors.append(f"{name}: {e2}")
                    elif reply == QMessageBox.Cancel:
                        break
                else:
                    errors.append(f"{name}: {e}")
            except Exception as e:
                errors.append(f"{name}: {e}")

        # Если хотя бы один элемент был cut — очищаем буфер
        if any(a == "cut" for _, a in self._clipboard_buffer):
            self._clipboard_buffer.clear()
            self._schedule_toolbar_update()

        # Итог
        action_label = "Скопировано" if action == "copy" else "Перемещено"
        if errors:
            QMessageBox.warning(
                self, f"{action_label} с ошибками",
                f"{action_label} {success_count} из {len(self._clipboard_buffer)} элемент(ов).\n"
                + "\n".join(f"  • {e}" for e in errors[:10])
                + ("\n  …" if len(errors) > 10 else ""),
            )
        else:
            self.statusBar().showMessage(
                f"✅ {action_label} {success_count} элемент(ов) в «{dest}»", 5000
            )

        # Обновляем текущий вид
        self.refresh_current()

    def _delete_selected(self):
        rows = self.table_view.selectionModel().selectedRows(0)
        if not rows:
            return
        names = [self._get_item(r)["name"] for r in rows]
        reply = QMessageBox.question(
            self, "Подтверждение",
            f"Удалить {len(rows)} файл(ов) из облака?\n"
            + "\n".join(f"  • {n}" for n in names),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        for idx in rows:
            item = self._get_item(idx)
            if item:
                try:
                    self._api.delete(item["cloud_path"])
                    self._db.remove_file(item["cloud_path"])
                    local = _local_path(item["cloud_path"])
                    if os.path.exists(local):
                        os.remove(local)
                except Exception as e:
                    QMessageBox.warning(
                        self, "Ошибка",
                        f"Не удалось удалить {item['name']}:\n{e}")
        self.refresh_current()

    def _create_folder(self):
        name, ok = QInputDialog.getText(self, "Новая папка", "Имя папки:")
        if not ok or not name.strip():
            return
        path = (self._current_path.rstrip("/") + "/" + name.strip()
                ).replace("//", "/")
        try:
            self._api.create_folder(path)
            self.refresh_current()
            self.refresh_tree()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", str(e))

    def _get_public_link(self):
        rows = self.table_view.selectionModel().selectedRows(0)
        if not rows:
            return
        item = self._get_item(rows[0])
        if not item:
            return
        self._get_public_link_for_path(item["cloud_path"])

    def _get_public_link_for_path(self, cloud_path: str):
        """Получить публичную ссылку для конкретного cloud_path (из контекстного меню дерева/таблицы)."""
        try:
            url = self._api.publish(cloud_path)
            QApplication.clipboard().setText(url)
            QMessageBox.information(
                self, "Скопировать ссылку",
                f"Ссылка скопирована в буфер обмена:\n{url}")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", str(e))

    def _download_folder_as_zip(self, cloud_path: str = ""):
        """Скачать папку как ZIP-архив в выбранное место на ПК.

        Вызывается из контекстного меню для папок (дерево / таблица).
        """
        if not cloud_path:
            # Берём из активного выделения
            focus = self.focusWidget()
            if focus is self.tree_view:
                idx = self.tree_view.selectionModel().selectedRows(0)
                if idx:
                    cloud_path = idx[0].data(Qt.UserRole) or ""
            else:
                rows = self.table_view.selectionModel().selectedRows(0)
                for idx in rows:
                    item = self._get_item(idx)
                    if item and item.get("is_dir"):
                        cloud_path = item["cloud_path"]
                        break
        if not cloud_path:
            QMessageBox.warning(self, "Ошибка",
                                "Выделите папку в дереве или таблице.")
            return

        folder_name = os.path.basename(cloud_path.rstrip("/")) or "disk"
        default_name = f"{folder_name}.zip"

        save_path, _ = QFileDialog.getSaveFileName(
            self, f"Сохранить ZIP: {folder_name}",
            os.path.join(os.path.expanduser("~"), "Downloads", default_name),
            "ZIP-архив (*.zip)",
        )
        if not save_path:
            return

        logger.info("ZIP download: %s → %s", cloud_path, save_path)
        self._start_zip_download(cloud_path, save_path)

    def _start_zip_download(self, cloud_path: str, local_path: str):
        """Запустить скачивание ZIP-архива в фоновом потоке."""
        worker = _ZipDownloadWorker(self._api, cloud_path, local_path)
        folder_name = os.path.basename(cloud_path.rstrip("/"))

        self._show_progress_localized(f"Скачиваю {folder_name}.zip...")

        def _ui_progress(done, total):
            if total > 0:
                self._status_progress.setRange(0, 100)
                self._status_progress.setValue(int(done / total * 100))
            else:
                self._status_progress.setRange(0, 0)

        def on_progress(done, total):
            QTimer.singleShot(0, lambda d=done, t=total: _ui_progress(d, t))

        def on_finished(lp):
            self._hide_progress()
            QMessageBox.information(
                self, "Готово",
                f"Архив сохранён:\n{lp}")
            logger.info("ZIP saved: %s", lp)

        def on_error(msg):
            self._hide_progress()
            QMessageBox.critical(
                self, "Ошибка",
                f"Не удалось скачать архив:\n{msg}")

        worker.progress.connect(on_progress, Qt.DirectConnection)
        worker.finished.connect(on_finished, Qt.DirectConnection)
        worker.error.connect(on_error, Qt.DirectConnection)

        thread = _WorkerThread(worker)
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()

    def _on_table_hovered(self, index: QModelIndex):
        """Обновить подсвеченную строку в таблице (через делегат RowHoverDelegate)."""
        delegate = self._hover_delegate
        old_row = delegate.hovered_row
        src_idx = self.table_sort_model.mapToSource(index)
        new_row = src_idx.row()
        if old_row != new_row:
            delegate.hovered_row = new_row
            # Перерисовать старую и новую строку
            self.table_view.update()

    def _on_file_double_clicked(self, index: QModelIndex):
        item = self._get_item(index)
        if not item:
            return
        if item.get("is_parent_nav"):
            self.tree_view.clearSelection()
            self._navigate_to_folder(item["cloud_path"])
            return
        if item["is_dir"]:
            self._navigate_to_folder(item["cloud_path"])
            return

        local_path = _local_path(item["cloud_path"])

        if item["status"] == "cloud_only" or not os.path.exists(local_path):
            self._open_after_download.add(item["cloud_path"])
            self._start_download(item["cloud_path"], local_path)
        else:
            self._open_file(local_path)

    # ── Keyboard navigation ────────────────────────────────

    def _handle_table_key(self, event) -> bool:
        """Обработать клавиатурное событие в таблице.
        Возвращает True если событие обработано (не передавать Qt)."""
        key = event.key()
        text = event.text()

        # Enter — открыть выделенный файл/папку (как двойной клик)
        if key in (Qt.Key_Return, Qt.Key_Enter):
            rows = self.table_view.selectionModel().selectedRows(0)
            if rows:
                self._on_file_double_clicked(rows[0])
            return True

        # Backspace — перейти в родительскую папку
        if key == Qt.Key_Backspace:
            if self._current_path != "/":
                parent = "/".join(self._current_path.rstrip("/").split("/")[:-1]) or "/"
                self.tree_view.clearSelection()
                self._navigate_to_folder(parent)
            return True

        # F2 — переименовать выделенный элемент
        if key == Qt.Key_F2:
            self._rename_selected()
            return True

        # Type-ahead: печатные символы
        if text and text.isprintable():
            self._typeahead_buf += text
            self._typeahead_timer.start(500)  # сброс через 500 мс
            self._select_by_typeahead()
            return True

        return False  # не обработано — пусть Qt разберётся

    def _clear_typeahead(self):
        """Сбросить буфер type-ahead (по таймауту)."""
        self._typeahead_buf = ""

    def _select_by_typeahead(self):
        """Найти и выделить строку, имя которой начинается с typeahead_buf."""
        search = self._typeahead_buf.lower()
        for row in range(self.table_model.rowCount()):
            item = self.table_model.get_item(row)
            if item and item.get("name", "").lower().startswith(search):
                src_idx = self.table_model.index(row, 0)
                proxy_idx = self.table_sort_model.mapFromSource(src_idx)
                if proxy_idx.isValid():
                    sel = self.table_view.selectionModel()
                    sel.clearSelection()
                    sel.select(
                        proxy_idx,
                        QItemSelectionModel.Select | QItemSelectionModel.Rows,
                    )
                    self.table_view.scrollTo(proxy_idx)
                break

    def _rename_selected(self):
        """Переименовать выделенный в таблице файл/папку (F2)."""
        rows = self.table_view.selectionModel().selectedRows(0)
        if not rows:
            return
        item = self._get_item(rows[0])
        if not item:
            return
        self._rename_item(item["cloud_path"], item["name"], item["type"],
                          size=item.get("size", 0),
                          modified=item.get("modified", ""),
                          md5=item.get("md5", ""))

    def _rename_item(self, cloud_path: str, old_name: str = "",
                     item_type: str = "file", **meta):
        """Переименовать файл/папку на Диске."""
        if not old_name:
            old_name = os.path.basename(cloud_path.rstrip("/"))
        parent = "/".join(cloud_path.rstrip("/").split("/")[:-1]) or "/"

        new_name, ok = QInputDialog.getText(
            self, "Переименовать",
            f"Новое имя для «{old_name}»:",
            QLineEdit.Normal, old_name)
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        new_name = new_name.strip()
        new_path = parent.rstrip("/") + "/" + new_name

        # Запоминаем старую запись БД до изменений
        old_rec = self._db.get_file(cloud_path)

        try:
            # 1. Переименовываем в облаке
            self._api.move(cloud_path, new_path)

            # 2. Переименовываем локальный файл, если он был скачан
            old_local = old_rec.get("local_path") if old_rec else None
            local_renamed = False
            if old_local and os.path.exists(old_local):
                new_local = os.path.join(
                    os.path.dirname(old_local), new_name)
                try:
                    os.makedirs(os.path.dirname(new_local), exist_ok=True)
                    if os.path.exists(new_local):
                        os.remove(new_local)
                    os.rename(old_local, new_local)
                    logger.info("Local renamed: %s → %s",
                                old_local, new_local)
                    local_renamed = True
                except Exception as e:
                    logger.error("Local rename failed: %s", e)

            # 3. Удаляем старую запись из БД
            self._db.remove_file(cloud_path)

            # 4. Создаём новую запись
            self._db.upsert_file(
                new_path, new_name, item_type,
                size=meta.get("size", 0),
                modified=meta.get("modified", ""),
                md5=meta.get("md5", ""),
            )

            # 5. Восстанавливаем статус, если файл был локальным
            if old_rec and old_rec.get("status") == "downloaded":
                if local_renamed:
                    self._db.set_downloaded(
                        new_path, new_local,
                        last_sync_md5=old_rec.get("last_sync_md5", ""),
                    )

            self.statusBar().showMessage(
                f"✅ Переименовано: {old_name} → {new_name}", 3000)
            self._navigate_to_folder(self._current_path)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка переименования",
                                 f"Не удалось переименовать «{old_name}»:\n{e}")

    def _open_file(self, local_path: str):
        QDesktopServices.openUrl(QUrl.fromLocalFile(local_path))

    def _open_in_explorer(self, local_path: str):
        """Открыть Проводник к файлу/папке, выделив его если это файл."""
        # explorer /select работает только для существующих файлов
        if os.path.isfile(local_path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(local_path)])
        elif os.path.isdir(local_path):
            subprocess.Popen(["explorer", os.path.normpath(local_path)])

    def _get_item(self, proxy_idx: QModelIndex) -> Optional[dict]:
        """Получить элемент модели по индексу из QTableView (через proxy)."""
        src_idx = self.table_sort_model.mapToSource(proxy_idx)
        if not src_idx.isValid():
            return None
        return self.table_model.get_item(src_idx.row())

    def _on_search(self, text: str):
        """Фильтрация таблицы по введённому тексту (поиск по имени)."""
        self.table_sort_model.set_search_text(text)

    def _show_settings(self):
        dlg = SettingsDialog(self, on_cache_changed=self._on_cache_changed)
        dlg.exec()

    def _on_cache_changed(self, old_dir: str, new_dir: str):
        """Обработчик смены папки кеша в настройках (без перезапуска)."""
        if old_dir == new_dir or not old_dir or not new_dir:
            return

        logger.info("Cache dir changed: %s → %s", old_dir, new_dir)

        # 1. Переместить файлы из старой папки в новую (быстро, локально)
        moved = sync.migrate_cache(old_dir, new_dir, self._db)
        self._cache_dir = new_dir

        # 2. Фоновая синхронизация с облаком
        def _after_sync(result):
            self._watcher.stop()
            self._init_watcher()
            self._load_folder_async(self._current_path)
            self.statusBar().showMessage(
                f"✅ Папка изменена: {new_dir} "
                f"(перемещено {moved} файлов, синхронизировано)", 5000)

        self._run_sync_threaded(new_dir, _after_sync)

    def _run_sync_threaded(self, cache_dir, on_done):
        """Запустить full_sync в фоновом потоке с callback."""
        thread = _SyncThread(self._api, self._db, cache_dir, self)
        thread.finished.connect(lambda r: on_done(r))
        thread.finished.connect(thread.deleteLater)
        thread.auth_error.connect(lambda msg: self._handle_auth_error())
        self._active_threads.append(thread)
        self.statusBar().showMessage("Синхронизация с облаком...")
        thread.start()

    def _show_about(self):
        QMessageBox.about(
            self, "YaDisk Manager",
            "Клиент Яндекс.Диска\nВерсия 0.6\n\n"
            "On-demand синхронизация:\n"
            "• Файлы видны в облаке, скачиваются по требованию\n"
            "• Двойной клик → скачать и открыть\n"
            "• Изменения → авто-загрузка в облако\n"
            "• Разрешение конфликтов\n"
            "• Системный трей\n"
            "• Автозапуск\n\n"
            "Написано на Python + PySide6",
        )

    def _on_context_menu(self, pos):
        index = self.table_view.indexAt(pos)
        menu = QMenu(self)

        if not index.isValid() or not self._get_item(index):
            # Клик по пустому месту — меню для текущей папки
            cp = self._current_path
            # Открыть текущую папку в Проводнике
            local_parent = _local_path(cp)
            if os.path.isdir(local_parent):
                act = menu.addAction(
                    "Открыть в Проводнике",
                    lambda p=local_parent: self._open_file(p))
                act.setIcon(_svg_icon("folder-yellow.svg", 24))
                menu.addSeparator()
            if db.get_zip_download_enabled():
                act = menu.addAction(
                    "Скачать как ZIP",
                    lambda cp=cp: self._download_folder_as_zip(cp))
                act.setIcon(_svg_icon("archive-svgrepo-com.svg", 24))
                menu.addSeparator()
            act = menu.addAction("Новая папка", self._create_folder)
            act.setIcon(_svg_icon("folder-add-yellow.svg", 24))
            menu.addSeparator()
            if self._clipboard_buffer:
                act = menu.addAction("Вставить", self._paste_from_buffer)
                act.setIcon(_svg_icon("paste.svg", 24))
                menu.addSeparator()
            act = menu.addAction("Обновить", self.refresh_current)
            act.setIcon(_svg_icon("refresh.svg", 24))
            menu.exec(self.table_view.viewport().mapToGlobal(pos))
            return

        item = self._get_item(index)
        # Выделяем только этот элемент
        self.table_view.selectionModel().select(
            index, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)

        is_dir = item.get("is_dir", False)
        cloud_path = item["cloud_path"]

        # 1. Открыть — жирным шрифтом
        act = menu.addAction("Открыть")
        fnt = act.font()
        fnt.setBold(True)
        act.setFont(fnt)
        act.triggered.connect(self._open_selected_item)

        # 2. Открыть в Проводнике
        local_path = _local_path(cloud_path)
        if os.path.exists(local_path):
            act = menu.addAction(
                "Открыть в Проводнике",
                lambda p=local_path: self._open_in_explorer(p))
            act.setIcon(_svg_icon("folder-yellow.svg", 24))

        # 3. Просмотреть на сайте
        web_url = f"https://disk.yandex.ru/client/disk/{cloud_path.lstrip('/')}"
        if is_dir:
            web_url += "/"
        act = menu.addAction("Просмотреть на сайте",
                       lambda u=web_url: QDesktopServices.openUrl(QUrl(u)))
        act.setIcon(_svg_icon("external-link.svg", 24))

        # 4. Создать копию на компьютере
        act = menu.addAction("Создать копию на компьютере", self._create_local_copy)

        menu.addSeparator()

        # 5. Скопировать ссылку
        act = menu.addAction("Скопировать ссылку", self._get_public_link)
        act.setIcon(_svg_icon("link.svg", 24))

        # 6. Скачать как ZIP (только для папок)
        if is_dir and db.get_zip_download_enabled():
            act = menu.addAction(
                "Скачать как ZIP",
                lambda cp=cloud_path: self._download_folder_as_zip(cp))
            act.setIcon(_svg_icon("archive-svgrepo-com.svg", 24))

        menu.addSeparator()

        # 7a. Сохранить на компьютере
        is_downloaded = item["status"] == "downloaded"
        act = menu.addAction("Сохранить на компьютере", self._save_to_computer)
        act.setIcon(_svg_icon("loaded.svg", 24))
        act.setEnabled(not is_downloaded)

        # 7b. Оставить только в облаке
        is_cloud = item["status"] == "cloud_only"
        act = menu.addAction("Оставить только в облаке", self._leave_only_in_cloud)
        act.setIcon(_svg_icon("cloud.svg", 24))
        act.setEnabled(not is_cloud)

        menu.addSeparator()

        # 8. Вырезать / Копировать / Вставить
        act = menu.addAction("Вырезать", self._cut_to_buffer)
        act.setIcon(_svg_icon("scissors-3.svg", 24))
        act = menu.addAction("Копировать", self._copy_to_buffer)
        act.setIcon(_svg_icon("Copy.svg", 24))
        if self._clipboard_buffer:
            act = menu.addAction("Вставить", self._paste_from_buffer)
            act.setIcon(_svg_icon("paste.svg", 24))

        menu.addSeparator()

        # 9. Удалить / Переименовать / Новая папка
        act = menu.addAction("Удалить", self._delete_selected)
        act.setIcon(_svg_icon("trash.svg", 24))
        act = menu.addAction("Переименовать", self._rename_selected)
        act.setIcon(_svg_icon("edit-colorful.svg", 24))
        act = menu.addAction("Новая папка", self._create_folder)
        act.setIcon(_svg_icon("folder-add-yellow.svg", 24))

        menu.exec(self.table_view.viewport().mapToGlobal(pos))

    def _on_tree_context_menu(self, pos):
        """Контекстное меню для дерева папок."""
        index = self.tree_view.indexAt(pos)
        if not index.isValid():
            return
        cloud_path = index.data(Qt.UserRole)
        if not cloud_path:
            return
        # Выделяем только эту папку
        self.tree_view.selectionModel().select(
            index, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        menu = QMenu(self)

        # 1. Открыть — жирным шрифтом
        act = menu.addAction("Открыть")
        fnt = act.font()
        fnt.setBold(True)
        act.setFont(fnt)
        act.triggered.connect(lambda p=cloud_path: self._navigate_to_folder(p))

        # 2. Открыть в Проводнике
        local_dir = _local_path(cloud_path)
        if os.path.isdir(local_dir):
            act = menu.addAction(
                "Открыть в Проводнике",
                lambda d=local_dir: self._open_in_explorer(d))
            act.setIcon(_svg_icon("folder-yellow.svg", 24))

        # 3. Просмотреть на сайте
        web_url = f"https://disk.yandex.ru/client/disk/{cloud_path.lstrip('/')}/"
        act = menu.addAction("Просмотреть на сайте",
                       lambda u=web_url: QDesktopServices.openUrl(QUrl(u)))
        act.setIcon(_svg_icon("external-link.svg", 24))

        # 4. Создать копию на компьютере
        act = menu.addAction("Создать копию на компьютере", self._create_local_copy)

        menu.addSeparator()

        # 5. Скопировать ссылку
        act = menu.addAction("Скопировать ссылку",
                       lambda cp=cloud_path: self._get_public_link_for_path(cp))
        act.setIcon(_svg_icon("link.svg", 24))

        # 6. Скачать как ZIP (только для папок)
        if db.get_zip_download_enabled():
            act = menu.addAction(
                "Скачать как ZIP",
                lambda cp=cloud_path: self._download_folder_as_zip(cp))
            act.setIcon(_svg_icon("archive-svgrepo-com.svg", 24))

        menu.addSeparator()

        # 7a. Сохранить на компьютере
        folder_agg = self._db.get_folder_aggregate_status(cloud_path)
        is_fully_local = folder_agg == "downloaded"
        act = menu.addAction("Сохранить на компьютере", self._save_to_computer)
        act.setIcon(_svg_icon("loaded.svg", 24))
        act.setEnabled(not is_fully_local)

        # 7b. Оставить только в облаке
        is_fully_cloud = folder_agg == "cloud_only"
        act = menu.addAction("Оставить только в облаке", self._leave_only_in_cloud)
        act.setIcon(_svg_icon("cloud.svg", 24))
        act.setEnabled(not is_fully_cloud)

        menu.addSeparator()

        # 8. Вырезать / Копировать / Вставить
        act = menu.addAction("Вырезать", self._cut_to_buffer)
        act.setIcon(_svg_icon("scissors-3.svg", 24))
        act = menu.addAction("Копировать", self._copy_to_buffer)
        act.setIcon(_svg_icon("Copy.svg", 24))
        if self._clipboard_buffer:
            act = menu.addAction("Вставить", self._paste_from_buffer)
            act.setIcon(_svg_icon("paste.svg", 24))

        menu.addSeparator()

        # 9. Удалить / Переименовать / Новая папка
        act = menu.addAction("Удалить", self._delete_selected)
        act.setIcon(_svg_icon("trash.svg", 24))
        act = menu.addAction(
            "Переименовать",
            lambda cp=cloud_path: self._rename_item(cp))
        act.setIcon(_svg_icon("edit-colorful.svg", 24))
        act = menu.addAction("Новая папка", self._create_folder)
        act.setIcon(_svg_icon("folder-add-yellow.svg", 24))

        menu.exec(self.tree_view.viewport().mapToGlobal(pos))

    def closeEvent(self, event):
        """Закрытие окна → сворачивание в трей (не завершение программы).
        При _force_close=True (Выход из меню/трея) — полное закрытие."""
        if self._force_close:
            self._delayed_close()
            event.accept()
            return
        if db.get_save_window_geometry():
            db.set_window_geometry(
                self.saveGeometry().toBase64().data().decode())
        self.hide()
        event.ignore()  # не закрывать — сворачиваем в трей
        # Уведомление при первом сворачивании
        if self._first_close_hint:
            self._first_close_hint = False
            self._tray.showMessage(
                "YaDisk Manager",
                "Окно свёрнуто в трей.\n"
                "Нажмите на значок программы, чтобы открыть его снова.",
                QSystemTrayIcon.Information, 5000)

    def _delayed_close(self):
        """Остановить потоки, сохранить настройки, закрыть БД — после скрытия окна."""
        if db.get_save_window_geometry():
            db.set_window_geometry(
                self.saveGeometry().toBase64().data().decode())
        for t in self._active_threads:
            try:
                t.cancel()
            except AttributeError:
                t.quit()
        for t in self._active_threads:
            t.wait(200)
        self._poll_timer.stop()
        self._watcher.stop()
        self._db.close()
        QApplication.quit()
