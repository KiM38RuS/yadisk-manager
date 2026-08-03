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
    "cloud": "☁️", "local": "💾", "modified": "✏️",
    "folder": "📁", "file": "📄", "image": "🖼️",
    "video": "🎬", "audio": "🎵", "archive": "📦",
    "pdf": "📕", "code": "📝",
}
STATUS_CHAR = {
    "cloud_only": "☁️", "downloaded": "💾", "modified": "✏️",
}
STATUS_COLOR = {
    "cloud_only": QColor("#999"),
    "downloaded": QColor("#2a2"),
    "modified": QColor("#e80"),
}
STATUS_COLOR_LIGHT = {
    "cloud_only": QColor("#999"),
    "downloaded": QColor("#1B7A1B"),
    "modified": QColor("#C65300"),
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


# ── Модель для дерева папок ──────────────────────────────

class FolderTreeItem:
    def __init__(self, name: str, cloud_path: str, parent=None):
        self.name = name
        self.cloud_path = cloud_path
        self.parent = parent
        self.children: list["FolderTreeItem"] = []
        self.loaded = False
        self._has_children = True  # assume has children until proven otherwise

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

    def __init__(self, api: disk_api.YaDiskAPI, parent=None):
        super().__init__(parent)
        self._api = api
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
            return item.name
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


# ── Модель для таблицы файлов ────────────────────────────

class RowHoverDelegate(QStyledItemDelegate):
    """Делегат для подсветки целой строки в таблице при наведении.

    По умолчанию QTableView подсвечивает только ячейку под курсором.
    Этот делегат красит фон всей строки, в которой стоит курсор мыши.
    Цвет подсветки адаптируется к текущей палитре (светлая/тёмная тема).
    """

    hovered_row = -1

    def paint(self, painter, option, index):
        if index.row() == self.hovered_row and not (option.state & QStyle.State_Selected):
            # Используем цвет Highlight из палитры с прозрачностью ~30%
            hl = option.palette.color(QPalette.Highlight)
            hl.setAlpha(50)
            painter.fillRect(option.rect, hl)
        super().paint(painter, option, index)


class FileTableModel(QAbstractTableModel):
    COLUMNS = ["Имя", "Размер", "Изменён", "Статус"]

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
            is_dir = item["type"] == "dir"
            path = item["path"]
            db_file = self._db.get_file(path)
            if is_dir:
                # Для папок вычисляем агрегированный статус из БД
                if db_file:
                    # Папка уже есть в БД — проверяем её children
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
                idx = self.index(i, 3)
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
                # Оповестить колонки 2 (Изменён) и 3 (Статус)
                idx2 = self.index(i, 2)
                idx3 = self.index(i, 3)
                self.dataChanged.emit(idx2, idx3, [Qt.DisplayRole])
                break

    def update_item_status(self, cloud_path: str, new_status: str):
        """Обновить статус файла в модели (после toggle local copy)."""
        for i, item in enumerate(self._items):
            if item["cloud_path"] == cloud_path:
                self._items[i]["status"] = new_status
                idx3 = self.index(i, 3)
                self.dataChanged.emit(idx3, idx3, [Qt.DisplayRole])
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
                icon = _icon_for(item["name"], item["is_dir"])
                return f"{icon} {item['name']}"
            elif col == 1:
                return _human_size(item["size"]) if not item["is_dir"] else ""
            elif col == 2:
                mod = item["modified"]
                if mod:
                    try:
                        dt = datetime.fromisoformat(mod.replace("Z", "+00:00"))
                        return dt.strftime("%d.%m.%Y %H:%M")
                    except Exception:
                        return mod[:19].replace("T", " ")
                return ""
            elif col == 3:
                s = item["status"]
                _labels = {
                    "cloud_only": "в облаке",
                    "downloaded": "на компьютере",
                    "modified": "изменён",
                }
                return f"{STATUS_CHAR.get(s, '')} {_labels.get(s, s)}"
        if role == Qt.ForegroundRole and col == 3:
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
            return (f"{item['cloud_path']}\nСтатус: {item['status']}")
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COLUMNS[section]
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

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        col = self.sortColumn()
        src = self.sourceModel()
        l_item = src._items[left.row()]
        r_item = src._items[right.row()]

        # Папки всегда сверху при asc, снизу при desc
        if l_item["is_dir"] != r_item["is_dir"]:
            return l_item["is_dir"]

        if col == 0:  # Имя
            l_name = l_item["name"].lower()
            r_name = r_item["name"].lower()
            return l_name < r_name
        elif col == 1:  # Размер
            return l_item["size"] < r_item["size"]
        elif col == 2:  # Изменён
            return (l_item["modified"] or "") < (r_item["modified"] or "")
        elif col == 3:  # Статус
            order = {"downloaded": 0, "modified": 1, "cloud_only": 2}
            return order.get(l_item["status"], 99) < order.get(r_item["status"], 99)
        return super().lessThan(left, right)

    def sort(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder):
        """Default: col 2 (Изменён) сортируется по убыванию при первом клике."""
        if order == Qt.AscendingOrder and column == 2:
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
        <h2>🔑 Первый запуск</h2>
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

        check_btn = QPushButton("✓ Проверить и сохранить", self)
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
        self.setWindowTitle("⚙ Настройки")
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
    """Фоновый поток для загрузки полного списка ВСЕХ файлов с Диска + вставка в БД."""
    finished = Signal(int)  # count of files loaded

    def __init__(self, api, database, parent=None):
        super().__init__(parent)
        self._api = api
        self._database = database

    def run(self):
        logger.info("AllFilesThread: loading all files...")
        try:
            files = self._api.get_all_files()
            # Вставка в БД в фоновом потоке — не блокирует главный
            if files:
                self._database.upsert_files_batch(files)
            logger.info("AllFilesThread: %d files loaded", len(files))
            self.finished.emit(len(files))
        except Exception as e:
            logger.error("AllFilesThread failed: %s", e)
            self.finished.emit(0)


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

        # Потокобезопасная очередь для результатов из рабочих потоков (вместо QueuedConnection)
        self._download_results: list[tuple[str, str, str, str, bool]] = []  # (cloud_path, local_path, md5, action, is_error)
        self._result_timer = QTimer(self)
        self._result_timer.timeout.connect(self._process_result_queue)
        self._result_timer.start(100)  # poll every 100ms

        self._watcher_queue: list[str] = []
        self._watcher_timer = QTimer(self)
        self._watcher_timer.timeout.connect(self._flush_watcher_queue)
        self._watcher_timer.start(200)  # poll watcher queue every 200ms

        self._init_ui()
        self._init_tray()
        self._init_watcher()

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
        reply = QMessageBox.warning(
            self, "Требуется авторизация",
            "Токен доступа недействителен или истёк.\n"
            "Хотите ввести новый токен?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return False

        if self._reauthorize():
            self.statusBar().showMessage("✅ Токен обновлён, перезагружаю...")
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
            self.statusBar().showMessage(f"❌ Ошибка загрузки папок: {error}")
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
            self.statusBar().showMessage(f"❌ Ошибка загрузки файлов: {error}")
        else:
            self._apply_file_list("/", items)
            logger.info("File list loaded: %d items", len(items))
        self._hide_table_loading()
        # Загружаем полный список всех файлов в фоне (для локальной навигации)
        thread = _AllFilesThread(self._api, self._db, self)
        thread.finished.connect(self._on_all_files_loaded)
        thread.finished.connect(thread.deleteLater)
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()
        # Фоновая синхронизация
        QTimer.singleShot(100, self._run_full_sync)

    def _on_all_files_loaded(self, count: int):
        """Полный список файлов загружен — переключаемся в локальный режим."""
        if count:
            logger.info("Local cache ready: %d files", count)
        # Обновляем текущий вид из БД (мгновенно, без API)
        self._load_folder_local(self._current_path)

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
        label = QLabel(f"⏳ {text}")
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

    def _apply_file_list(self, path: str, items: list):
        """Применить список файлов к БД и таблице (главный поток)."""
        # Массовая вставка в одной транзакции — без тормозов
        self._db.upsert_files_batch(items)
        self.table_model.set_path(path, items)
        self._current_path = path
        self.statusBar().showMessage(f"📂 {path} — {len(items)} эл.", 5000)

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
        self.tree_model = FolderTreeModel(self._api, self)
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
            self._update_toolbar_buttons)
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
        self.table_view.horizontalHeader().setSortIndicatorShown(True)
        self.table_view.sortByColumn(0, Qt.AscendingOrder)
        self.table_view.selectionModel().selectionChanged.connect(
            self._update_toolbar_buttons)
        # Resizable столбцы
        for c in range(self.table_model.columnCount()):
            self.table_view.horizontalHeader().setSectionResizeMode(
                c, QHeaderView.Interactive)
        # Ширина "Статус" по умолчанию — чтобы влезало "в облаке"
        self.table_view.setColumnWidth(0, 300)
        self.table_view.setColumnWidth(3, 100)
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
        """)

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
            app.setPalette(palette)
        elif theme == "dark":
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

        self._update_app_icons()

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

        file_menu = mb.addMenu("📁 Файл")
        self._act_toggle = QAction("✅ Сохранить на компьютере", self)
        self._act_toggle.triggered.connect(self._toggle_local_copy)
        file_menu.addAction(self._act_toggle)
        file_menu.addSeparator()
        self._act_delete = QAction("🗑 Удалить", self)
        self._act_delete.setShortcut(QKeySequence("Delete"))
        self._act_delete.triggered.connect(self._delete_selected)
        file_menu.addAction(self._act_delete)

        file_menu.addSeparator()
        self._act_refresh = QAction("🔄 Обновить", self)
        self._act_refresh.setShortcut(QKeySequence("F5"))
        self._act_refresh.triggered.connect(self.refresh_current)
        file_menu.addAction(self._act_refresh)

        tools_menu = mb.addMenu("🔧 Инструменты")
        self._act_new_folder = QAction("📂 Новая папка", self)
        self._act_new_folder.triggered.connect(self._create_folder)
        tools_menu.addAction(self._act_new_folder)

        tools_menu.addSeparator()
        self._act_get_link = QAction("🔗 Публичная ссылка", self)
        self._act_get_link.triggered.connect(self._get_public_link)
        tools_menu.addAction(self._act_get_link)

        tools_menu.addSeparator()
        self._act_settings = QAction("⚙ Настройки", self)
        self._act_settings.triggered.connect(self._show_settings)
        tools_menu.addAction(self._act_settings)

        help_menu = mb.addMenu("❓ Помощь")
        self._act_about = QAction("О программе", self)
        self._act_about.triggered.connect(self._show_about)
        help_menu.addAction(self._act_about)

    # ── Toolbar ────────────────────────────────────────────

    @staticmethod
    def _make_emoji_icon(emoji: str, size: int = 36) -> QIcon:
        """Создать QIcon из эмодзи для тулбара."""
        h = size + 4  # немного выше, чтобы эмодзи не обрезались снизу
        pixmap = QPixmap(size, h)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.TextAntialiasing)
        font = QFont("Segoe UI Emoji", size - 8)
        painter.setFont(font)
        painter.drawText(QRectF(0, 0, size, h), Qt.AlignCenter, emoji)
        painter.end()
        return QIcon(pixmap)

    def _create_toolbar(self):
        tb = QToolBar("Основная", self)
        tb.setIconSize(QSize(36, 40))
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        tb.setStyleSheet("""
            QToolBar {
                background: palette(Window);
                spacing: 2px;
            }
            QToolButton {
                font-size: 12px; padding: 2px 8px;
                color: palette(WindowText);
                background: transparent;
            }
            QToolButton:hover {
                background: palette(Midlight);
            }
            QToolButton:pressed {
                background: palette(Dark);
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

        def _btn(emoji, label, tip, cb):
            icon = self._make_emoji_icon(emoji, 36)
            a = QAction(icon, label, self)
            a.setToolTip(tip)
            a.triggered.connect(cb)
            tb.addAction(a)
            return a

        def _icon_btn(emoji, tip, cb):
            """Кнопка только с иконкой (без подписи)."""
            a = _btn(emoji, "", tip, cb)
            w = tb.widgetForAction(a)
            if w:
                w.setToolButtonStyle(Qt.ToolButtonIconOnly)
            return a

        self._tb_refresh = _icon_btn("🔄",
                                     "Обновить (F5)", self.refresh_current)
        tb.addSeparator()
        self._tb_new_folder = _icon_btn("📂",
                                        "Новая папка", self._create_folder)
        self._tb_delete = _icon_btn("🗑",
                                    "Удалить выделенные", self._delete_selected)
        tb.addSeparator()
        self._tb_settings = _icon_btn('⚙',
                                      'Настройки', self._show_settings)

        tb.addSeparator()
        self._tb_link = _btn('🔗', 'Скопировать ссылку',
                             'Публичная ссылка', self._get_public_link)
        link_w = tb.widgetForAction(self._tb_link)
        if link_w:
            link_w.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)

        # Кнопка-переключатель: Сохранить / Оставить только в облаке
        self._tb_toggle = _btn('✅', 'Сохранить на компьютере',
                               'Сохранить выделенные на компьютер',
                               self._toggle_local_copy)
        toggle_w = tb.widgetForAction(self._tb_toggle)
        if toggle_w:
            toggle_w.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)

        # Растягиваемый spacer — прижимает поиск к правому краю
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        # Поиск
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("🔍 Поиск по имени…")
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

    # ── Toolbar: состояние кнопок ──────────────────────────

    def _update_toolbar_buttons(self):
        """Включить/выключить кнопки в зависимости от выделения."""
        table_rows = self.table_view.selectionModel().selectedRows(0)
        tree_rows = self.tree_view.selectionModel().selectedRows(0)
        has_table_sel = bool(table_rows)
        has_tree_sel = bool(tree_rows)

        # Удаление: активно если что-то выделено
        self._tb_delete.setEnabled(has_table_sel or has_tree_sel)

        # Публичная ссылка: только для таблицы
        self._tb_link.setEnabled(has_table_sel)

        # Переключатель Сохранить/Оставить в облаке
        self._update_toggle_button(table_rows, has_tree_sel)

    def _update_toggle_button(self, table_rows, has_tree_sel):
        """Обновить текст, иконку и активность кнопки-переключателя.

        - Если все выделенные файлы `cloud_only` → ✅ Сохранить на компьютере
        - Если все выделенные файлы `downloaded`/`modified` → ☁️ Оставить только в облаке
        - Если смесь или ничего → disabled
        """
        # Собираем статусы всех выделенных элементов
        statuses: set[str] = set()
        for idx in table_rows:
            item = self._get_item(idx)
            if item and not item.get("is_dir"):
                statuses.add(item["status"])
            elif item and item.get("is_dir"):
                # Папка считается "загружаемой" — установим placeholder
                statuses.add("_folder")
        if has_tree_sel:
            statuses.add("_folder")

        if not statuses:
            self._tb_toggle.setEnabled(False)
            return

        all_cloud = statuses <= {"cloud_only", "_folder"}
        all_local = statuses <= {"downloaded", "modified", "_folder"}

        if all_cloud:
            self._tb_toggle.setIcon(self._make_emoji_icon("✅", 36))
            self._tb_toggle.setText("Сохранить на компьютере")
            self._tb_toggle.setToolTip("Скачать выделенные файлы на компьютер")
            self._tb_toggle.setEnabled(True)
        elif all_local and not all_cloud:
            self._tb_toggle.setIcon(self._make_emoji_icon("☁️", 36))
            self._tb_toggle.setText("Оставить только в облаке")
            self._tb_toggle.setToolTip("Удалить локальную копию, оставить в облаке")
            self._tb_toggle.setEnabled(True)
        else:
            self._tb_toggle.setEnabled(False)

    # ── Tray ──────────────────────────────────────────────

    def _init_tray(self):
        self._tray = QSystemTrayIcon(self)
        self._update_app_icons(update_tray_only=True)
        self._tray.setToolTip("☁️ YaDisk Manager")

        menu = QMenu(self)
        act_show = QAction("📂 Показать", self)
        act_show.triggered.connect(self._tray_show)
        menu.addAction(act_show)

        act_hide = QAction("🔽 Скрыть", self)
        act_hide.triggered.connect(self._tray_hide)
        menu.addAction(act_hide)

        menu.addSeparator()

        act_settings = QAction("⚙ Настройки", self)
        act_settings.triggered.connect(self._show_settings)
        menu.addAction(act_settings)

        menu.addSeparator()

        act_exit = QAction("❌ Выход", self)
        act_exit.triggered.connect(self._tray_exit)
        menu.addAction(act_exit)

        self._tray.setContextMenu(menu)
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
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            if self.isVisible():
                self._tray_hide()
            else:
                self._tray_show()

    def _tray_show(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _tray_hide(self):
        self.hide()

    def _tray_exit(self):
        self._tray.hide()
        self.close()

    # ── Watcher ───────────────────────────────────────────

    def _init_watcher(self):
        self._watcher = watcher.FileWatcher(
            _cache_dir(), self._on_local_file_changed)
        self._watcher.start()

    def _on_local_file_changed(self, local_path: str):
        """Вызывается из watchdog-потока — просто кладём в очередь для main thread."""
        self._watcher_queue.append(local_path)

    def _flush_watcher_queue(self):
        """Раз в 200ms выбираем накопившиеся события из watcher-очереди (main thread)."""
        if not self._watcher_queue:
            return
        batch = list(self._watcher_queue)
        self._watcher_queue.clear()
        for local_path in batch:
            try:
                self._handle_file_changed(local_path)
            except Exception as e:
                logger.warning("Watcher handler error: %s", e)

    def _handle_file_changed(self, local_path: str):
        rec = self._db.get_by_local_path(local_path)
        if not rec:
            logger.info("Unknown local file changed: %s", local_path)
            return
        cloud_path = rec["cloud_path"]

        if cloud_path in self._syncing:
            return
        self._pend_upload.add(cloud_path)
        QTimer.singleShot(2000, lambda: self._flush_pending_upload())

    def _flush_pending_upload(self):
        if not self._pend_upload:
            return
        batch = list(self._pend_upload)
        self._pend_upload.clear()
        for cp in batch:
            self._upload_single(cp)

    def _upload_single(self, cloud_path: str):
        info = self._db.get_file(cloud_path)
        if not info or not info["local_path"]:
            return
        local_path = info["local_path"]
        if not os.path.exists(local_path):
            return

        last_sync_md5 = info.get("last_sync_md5") or info.get("md5") or ""
        name = Path(cloud_path).name

        # ── показываем прогресс проверки ─────────────────
        self._show_progress_localized(f"⏳ Проверка {name}...")

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
            self, "⚠️ Конфликт",
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
        self._show_progress_localized(f"⬆ Загружаю {Path(cloud_path).name}")

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
        """Запустить download в фоновом потоке."""
        # Защита от повторного запуска для того же файла
        if cloud_path in self._syncing:
            logger.warning("start_download: already syncing %s", cloud_path)
            return
        logger.info("start_download: path=%s local=%s", cloud_path, local_path)
        worker = DownloadWorker(self._api, cloud_path, local_path)

        self._syncing.add(cloud_path)
        self._show_progress_localized(f"⬇ Скачиваю {Path(cloud_path).name}")

        def _ui_progress(done, total):
            if total > 0:
                self._status_progress.setRange(0, 100)
                self._status_progress.setValue(int(done / total * 100))
            else:
                self._status_progress.setRange(0, 0)

        def on_progress(done, total):
            QTimer.singleShot(0, lambda d=done, t=total: _ui_progress(d, t))

        def on_finished(lp):
            """Выполняется в рабочем потоке — кладём результат в потокобезопасную очередь."""
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
                    self._update_toolbar_buttons()
                    logger.warning("Worker error: %s %s", action, cloud_path)
                    continue

                if action == "download":
                    new_size = os.path.getsize(local_path)
                    new_modified = datetime.now(timezone.utc).isoformat()
                    # Сначала upsert (создаёт запись если её нет), потом статус!
                    self._db.upsert_file(
                        cloud_path=cloud_path,
                        name=Path(cloud_path).name,
                        type_="file",
                        size=new_size,
                        modified=new_modified,
                        md5=md5,
                    )
                    self._db.set_downloaded(cloud_path, local_path, last_sync_md5=md5)
                    self._syncing.discard(cloud_path)
                    self.table_model.update_item_after_download(
                        cloud_path, new_size, new_modified, "downloaded")
                    self._hide_progress()
                    self._update_toolbar_buttons()
                    logger.info("Downloaded: %s → %s", cloud_path, local_path)
                    # Авто-открытие, если запрошено (двойной клик)
                    if cloud_path in self._open_after_download:
                        self._open_after_download.discard(cloud_path)
                        self._open_file(local_path)
                elif action == "upload":
                    # Сначала upsert (создаёт запись если её нет), потом статус!
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
                    self._hide_progress()
                    self._update_toolbar_buttons()
                    logger.info("Uploaded: %s", cloud_path)
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

    def _cleanup_thread(self, thread):
        if thread in self._active_threads:
            self._active_threads.remove(thread)

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
        self._load_folder_async(self._current_path)

    def eventFilter(self, obj, event):
        """Сброс выделения в дереве папок при клике по пустому месту."""
        if (obj is self.tree_view.viewport()
                and event.type() == QEvent.MouseButtonPress
                and event.button() == Qt.LeftButton):
            idx = self.tree_view.indexAt(event.pos())
            if not idx.isValid():
                self.tree_view.clearSelection()
                self._update_toolbar_buttons()
        return super().eventFilter(obj, event)

    def _on_folder_clicked(self, index: QModelIndex):
        path = index.data(Qt.UserRole) or "/"
        self._last_requested_path = path
        # Всегда используем API для навигации — так таблица показывает точное
        # содержимое папки (файлы + папки), а БД используем только для статусов.
        self._load_folder_async(path)

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
        } for r in db_records]
        self.table_model.set_path(path, items)
        self.statusBar().showMessage(f"📂 {path} — {len(items)} эл.", 5000)

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

    def _toggle_local_copy(self):
        """Переключатель: скачать / удалить локальную копию.

        - Если файл ☁️ cloud_only → ✅ скачать на компьютер
        - Если файл 💾 downloaded / ✏️ modified → ☁️ удалить локально
        - Папки обрабатываются рекурсивно через БД
        """
        to_download: list[str] = []
        to_remove: list[str] = []

        # Работаем только с активной панелью (таблица XOR дерево)
        focus = self.focusWidget()
        if focus is self.tree_view:
            # Древо: обрабатываем выделенные папки рекурсивно
            tree_rows = self.tree_view.selectionModel().selectedRows(0)
            for idx in tree_rows:
                cloud_path = idx.data(Qt.UserRole)
                if cloud_path:
                    self._collect_toggle_recursive(
                        cloud_path, to_download, to_remove)
        else:
            # Таблица: только выделенные строки
            rows = self.table_view.selectionModel().selectedRows(0)
            for idx in rows:
                item = self._get_item(idx)
                if not item:
                    continue
                if item.get("is_dir"):
                    self._collect_toggle_recursive(
                        item["cloud_path"], to_download, to_remove)
                else:
                    if item["status"] == "cloud_only":
                        to_download.append(item["cloud_path"])
                    else:
                        to_remove.append(item["cloud_path"])

        for cp in to_download:
            self._start_download(cp, _local_path(cp))
        for cp in to_remove:
            self._remove_local_copy(cp)

        if not to_download and not to_remove:
            QMessageBox.information(
                self, "Нет действий",
                "Выделите файлы или папки для скачивания\n"
                "или удаления локальной копии.")

    def _collect_toggle_recursive(self, cloud_path: str,
                                   download_list: list[str],
                                   remove_list: list[str]) -> None:
        """Рекурсивно собрать файлы под cloud_path для toggle."""
        for f in self._db.get_children(cloud_path):
            if f["type"] == "dir":
                self._collect_toggle_recursive(
                    f["cloud_path"], download_list, remove_list)
            elif f["status"] == "cloud_only":
                download_list.append(f["cloud_path"])
            else:
                remove_list.append(f["cloud_path"])

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
        self._update_toolbar_buttons()
        logger.info("File set to cloud_only: %s", cloud_path)

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
        try:
            url = self._api.publish(item["cloud_path"])
            QApplication.clipboard().setText(url)
            QMessageBox.information(
                self, "Публичная ссылка",
                f"Ссылка скопирована в буфер обмена:\n{url}")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", str(e))

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
            self._load_folder_async(item["cloud_path"])
            return
        if item["is_dir"]:
            self._load_folder_async(item["cloud_path"])
            return

        local_path = _local_path(item["cloud_path"])

        if item["status"] == "cloud_only" or not os.path.exists(local_path):
            self._open_after_download.add(item["cloud_path"])
            self._start_download(item["cloud_path"], local_path)
        else:
            self._open_file(local_path)

    def _open_file(self, local_path: str):
        QDesktopServices.openUrl(QUrl.fromLocalFile(local_path))

    def _get_item(self, proxy_idx: QModelIndex) -> Optional[dict]:
        """Получить элемент модели по индексу из QTableView (через proxy)."""
        src_idx = self.table_sort_model.mapToSource(proxy_idx)
        return self.table_model.get_item(src_idx.row())

    def _on_search(self, text: str):
        """Фильтрация таблицы по введённому тексту (поиск по имени)."""
        self.table_sort_model.setFilterFixedString(text)

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
            self, "☁️ YaDisk Manager",
            "Клиент Яндекс.Диска\\nВерсия 0.6\\n\\n"
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
        if not index.isValid():
            return
        item = self._get_item(index)
        if not item:
            return
        # Выделяем только этот элемент
        self.table_view.selectionModel().select(
            index, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        menu = QMenu(self)
        is_cloud = item["status"] == "cloud_only"
        if is_cloud:
            menu.addAction("✅ Сохранить на компьютере", self._toggle_local_copy)
        else:
            menu.addAction("☁️ Оставить только в облаке", self._toggle_local_copy)
        menu.addSeparator()
        if not item["is_dir"]:
            menu.addAction("🔗 Публичная ссылка", self._get_public_link)
            menu.addSeparator()
            menu.addAction(
                "📂 Открыть папку",
                lambda: self._open_file(
                    os.path.dirname(_local_path(item["cloud_path"]))))
        menu.addSeparator()
        menu.addAction("🗑 Удалить", self._delete_selected)
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
        menu.addAction("✅ Сохранить на компьютере", self._toggle_local_copy)
        menu.addSeparator()
        menu.addAction("📂 Новая папка", self._create_folder)
        menu.addSeparator()
        menu.addAction("🗑 Удалить", self._delete_selected)
        menu.exec(self.tree_view.viewport().mapToGlobal(pos))

    def closeEvent(self, event):
        # Сохранить положение окна
        if db.get_save_window_geometry():
            db.set_window_geometry(
                self.saveGeometry().toBase64().data().decode())
        for t in self._active_threads:
            try:
                t.cancel()
            except AttributeError:
                t.quit()
                t.wait(2000)
        self._poll_timer.stop()
        self._watcher.stop()
        self._db.close()
        super().closeEvent(event)
