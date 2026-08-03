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
    QStackedWidget,
    QSystemTrayIcon,
)
from PySide6.QtCore import (
    Qt, QAbstractItemModel, QModelIndex, QAbstractTableModel,
    QThread, Signal, QObject, QTimer, QUrl, QSize, QPoint,
)
from PySide6.QtGui import (
    QAction, QKeySequence, QIcon, QColor, QFont,
    QDesktopServices, QPixmap, QPalette, QActionGroup,
)

import disk_api
import db
import watcher
import sync

logger = logging.getLogger(__name__)

# ── константы ────────────────────────────────────────────

def _cache_dir() -> str:
    return db.get_cache_dir() or os.path.join(os.path.expanduser("~"), ".yadisk-cache")
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
    "cloud_only": QColor("#888"),
    "downloaded": QColor("#2a2"),
    "modified": QColor("#e80"),
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
AUTORUN_NAME = "YandexDiskManager"


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

    def __init__(self, api: disk_api.YandexDiskAPI,
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
            resp = self.api._session.get(href, stream=True, timeout=120)
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0))
            os.makedirs(os.path.dirname(self.local_path), exist_ok=True)
            downloaded = 0
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
            self.error.emit(str(e))


class UploadWorker(QObject):
    """Загружает файл в облако в фоновом потоке с отчётом прогресса."""
    progress = Signal(int, int)    # uploaded, total
    finished = Signal(str)         # cloud_path
    error = Signal(str)
    conflict = Signal(str, str, str)  # cloud_path, local_md5, cloud_md5

    def __init__(self, api: disk_api.YandexDiskAPI,
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

    def child(self, row: int):
        return self.children[row] if 0 <= row < len(self.children) else None

    def row(self):
        if self.parent:
            return self.parent.children.index(self)
        return 0


class FolderTreeModel(QAbstractItemModel):
    """Модель дерева папок — НИКОГДА не делает API-вызовов синхронно.

    Дочерние узлы загружаются асинхронно через populate_children().
    hasChildren() всегда возвращает True для всех узлов (все — папки),
    поэтому Qt показывает стрелку разворачивания. Когда пользователь
    раскрывает узел, сигнал expanded запускает асинхронную загрузку.
    """

    def __init__(self, api: disk_api.YandexDiskAPI, parent=None):
        super().__init__(parent)
        self._api = api
        self._root = FolderTreeItem("root", "")
        self._root.loaded = False  # Корень НЕ загружен — populate_children сможет добавить детей

    # ── async population ────────────────────────────────

    def populate_children(self, cloud_path: str, items: list[dict]) -> None:
        """Асинхронно добавить children узлу (из главного потока)."""
        parent_item = self._find_item(cloud_path)
        if not parent_item or parent_item.loaded:
            return
        parent_item.loaded = True
        folders = [it for it in items if it.get("type") == "dir"]
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
        """Все узлы дерева — папки, показываем стрелку всегда."""
        if not parent.isValid():
            return len(self._root.children) > 0
        return True  # любой элемент в дереве — директория

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

class FileTableModel(QAbstractTableModel):
    COLUMNS = ["", "Имя", "Размер", "Изменён", "Статус"]

    def __init__(self, database: db.Database, parent=None):
        super().__init__(parent)
        self._db = database
        self._items: list[dict] = []
        self._current_path = "/"

    def set_path(self, cloud_path: str, api_items: list[dict]):
        self.beginResetModel()
        self._current_path = cloud_path
        self._items = []
        for item in api_items:
            is_dir = item["type"] == "dir"
            path = item["path"]
            db_file = self._db.get_file(path)
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
                idx = self.index(i, 4)
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
                return _icon_for(item["name"], item["is_dir"])
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
            elif col == 4:
                s = item["status"]
                return f"{STATUS_CHAR.get(s, '')} {s}"
        if role == Qt.ForegroundRole and col == 4:
            return STATUS_COLOR.get(item["status"])
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
            api = disk_api.YandexDiskAPI(token)
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
        self.setMinimumSize(400, 200)

        layout = QVBoxLayout(self)

        # Автозапуск
        grp = QGroupBox("Запуск", self)
        frm = QFormLayout(grp)
        self._chk_autorun = QCheckBox("Автозапуск при старте Windows")
        self._chk_autorun.setChecked(_autorun_is_enabled())
        frm.addRow(self._chk_autorun)
        layout.addWidget(grp)

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
            self, "Выберите папку для файлов",
            self._cache_edit.text(),
        )
        if chosen:
            self._cache_edit.setText(chosen)

    def _save(self):
        _autorun_set(self._chk_autorun.isChecked())
        new_cache = self._cache_edit.text().strip()
        if new_cache and new_cache != self._old_cache:
            os.makedirs(new_cache, exist_ok=True)
            db.set_cache_dir(new_cache)
            if self._on_cache_changed:
                self._on_cache_changed(self._old_cache, new_cache)
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
    """Фоновый поток для загрузки полного списка ВСЕХ файлов с Диска."""
    finished = Signal(list)

    def __init__(self, api, parent=None):
        super().__init__(parent)
        self._api = api

    def run(self):
        logger.info("AllFilesThread: loading all files...")
        try:
            files = self._api.get_all_files()
            logger.info("AllFilesThread: %d files loaded", len(files))
            self.finished.emit(files)
        except Exception as e:
            logger.error("AllFilesThread failed: %s", e)
            self.finished.emit([])


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


# ── Главное окно ────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, api: disk_api.YandexDiskAPI,
                 database: db.Database,
                 cache_dir: str = ""):
        super().__init__()
        self._api = api
        self._db = database
        self._cache_dir = cache_dir or _cache_dir()
        self._current_path = "/"
        self._local_mode = False  # После загрузки всех файлов — локальная навигация
        self._last_requested_path = "/"
        self._syncing: set[str] = set()
        self._pend_upload: set[str] = set()
        self._active_threads: list[_WorkerThread] = []

        self._init_ui()
        self._init_tray()
        self._init_watcher()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_cloud)
        self._poll_timer.start(POLL_INTERVAL_MS)

        # Асинхронная загрузка данных — окно покажется сразу
        self.statusBar().showMessage("Загрузка...")
        QTimer.singleShot(0, self._initial_load)

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

    def _on_tree_loaded(self, items, error):
        """Обновить дерево папок после фоновой загрузки."""
        if error:
            logger.error("Tree load failed: %s", error)
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
            self.statusBar().showMessage(f"❌ Ошибка загрузки файлов: {error}")
        else:
            self._apply_file_list("/", items)
            logger.info("File list loaded: %d items", len(items))
        self._hide_table_loading()
        # Загружаем полный список всех файлов в фоне (для локальной навигации)
        thread = _AllFilesThread(self._api, self)
        thread.finished.connect(self._on_all_files_loaded)
        thread.start()
        # Фоновая синхронизация
        QTimer.singleShot(100, self._run_full_sync)

    def _on_all_files_loaded(self, all_files):
        """Полный список файлов загружен — переключаемся в локальный режим."""
        if all_files:
            self._db.upsert_files_batch(all_files)
            logger.info("Local cache ready: %d files", len(all_files))
        self._local_mode = True
        # Обновляем текущий вид из локального кеша
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
        label.setStyleSheet("font-size: 14px; color: #666;")
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
        self.statusBar().showMessage(
            f"Синхронизация: {result.get('matched', 0)} совпало, "
            f"{result.get('uploaded', 0)} загружено, "
            f"{result.get('downloaded', 0)} скачано, "
            f"{result.get('moved', 0)} перемещено, "
            f"{result.get('deleted', 0)} удалено", 8000)
        if self._local_mode:
            self._load_folder_local(self._current_path)
        else:
            self._load_folder_async(self._current_path)

    # ── UI setup ──────────────────────────────────────────

    def _init_ui(self):
        self.setWindowTitle("☁️ Yandex Disk Manager")
        self.setMinimumSize(900, 600)
        self.resize(1100, 700)
        self.setWindowIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_DriveHDIcon))

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

        self._tree_stack = QStackedWidget()
        self._tree_stack.addWidget(self._make_loading_widget("Загрузка папок..."))  # 0
        self._tree_stack.addWidget(self.tree_view)                                    # 1
        self._tree_stack.setCurrentIndex(0)
        splitter.addWidget(self._tree_stack)

        # ── Таблица файлов (с loading overlay) ────────────
        self.table_model = FileTableModel(self._db, self)
        self.table_view = QTableView()
        self.table_view.setModel(self.table_model)
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table_view.setAlternatingRowColors(True)
        self.table_view.verticalHeader().hide()
        self.table_view.horizontalHeader().setStretchLastSection(True)
        self.table_view.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self.table_view.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents)
        self.table_view.setColumnWidth(0, 36)
        self.table_view.doubleClicked.connect(self._on_file_double_clicked)
        self.table_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(
            self._on_context_menu)

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

        # Статусбар
        self._status_label = QLabel("")
        self.statusBar().addWidget(self._status_label, 1)
        self._status_progress = QProgressBar()
        self._status_progress.setMaximumWidth(200)
        self._status_progress.setMinimumWidth(120)
        self._status_progress.setVisible(False)
        self.statusBar().addPermanentWidget(self._status_progress)

    def _create_menu(self):
        mb = self.menuBar()

        file_menu = mb.addMenu("📁 Файл")
        self._act_download = QAction("⬇ Скачать", self)
        self._act_download.setShortcut(QKeySequence("Ctrl+D"))
        self._act_download.triggered.connect(self._download_selected)
        file_menu.addAction(self._act_download)

        self._act_upload = QAction("⬆ Загрузить", self)
        self._act_upload.setShortcut(QKeySequence("Ctrl+U"))
        self._act_upload.triggered.connect(self._upload_selected)
        file_menu.addAction(self._act_upload)

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

    def _create_toolbar(self):
        tb = QToolBar("Основная", self)
        tb.setIconSize(QSize(20, 20))
        self.addToolBar(tb)

        def _btn(text, tip, cb):
            a = QAction(text, self)
            a.setToolTip(tip)
            a.triggered.connect(cb)
            tb.addAction(a)
            return a

        _btn("🔄", "Обновить (F5)", self.refresh_current)
        tb.addSeparator()
        _btn("⬇", "Скачать выделенные", self._download_selected)
        _btn("⬆", "Загрузить выделенные", self._upload_selected)
        tb.addSeparator()
        _btn("📂", "Новая папка", self._create_folder)
        _btn("🗑", "Удалить выделенные", self._delete_selected)
        tb.addSeparator()
        _btn("🔗", "Публичная ссылка", self._get_public_link)

    # ── Tray ──────────────────────────────────────────────

    def _init_tray(self):
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_DriveHDIcon))
        self._tray.setToolTip("☁️ Yandex Disk Manager")

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
        QTimer.singleShot(0, lambda: self._handle_file_changed(local_path))

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

        # ── проверка на конфликт ─────────────────────────
        try:
            cloud_meta = self._api.get_meta(cloud_path)
            cloud_md5 = cloud_meta.get("md5", "")
            cloud_modified = cloud_meta.get("modified", "")
        except Exception:
            cloud_md5 = ""
            cloud_modified = ""

        last_sync_md5 = info.get("last_sync_md5") or info.get("md5") or ""
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
        thread = _WorkerThread(worker)
        self._active_threads.append(thread)

        self._syncing.add(cloud_path)
        self._show_progress_localized(f"⬆ Загружаю {Path(cloud_path).name}")

        def on_progress(done, total):
            if total > 0:
                pct = int(done / total * 100)
                self._status_progress.setRange(0, 100)
                self._status_progress.setValue(pct)
            else:
                self._status_progress.setRange(0, 0)

        def on_finished(cp):
            self._syncing.discard(cp)
            self._db.set_downloaded(cp, local_path, last_sync_md5=local_md5)
            self._db.upsert_file(
                cloud_path=cp, name=Path(cp).name, type_="file",
                size=os.path.getsize(local_path),
                modified=datetime.now(timezone.utc).isoformat(),
                md5=local_md5,
            )
            self._table_model.update_status(cp, "downloaded")
            self._hide_progress()
            logger.info("Uploaded: %s", cp)
            self._active_threads.remove(thread)

        def on_error(msg):
            self._syncing.discard(cloud_path)
            self._hide_progress()
            logger.error("Upload failed %s: %s", cloud_path, msg)
            QTimer.singleShot(0, lambda: QMessageBox.critical(
                self, "Ошибка загрузки",
                f"Не удалось загрузить {Path(cloud_path).name}:\n{msg}"))
            self._active_threads.remove(thread)

        worker.progress.connect(on_progress)
        worker.finished.connect(on_finished)
        worker.error.connect(on_error)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()

    def _start_download(self, cloud_path: str, local_path: str):
        """Запустить download в фоновом потоке."""
        worker = DownloadWorker(self._api, cloud_path, local_path)
        thread = _WorkerThread(worker)
        self._active_threads.append(thread)

        self._syncing.add(cloud_path)
        self._show_progress_localized(f"⬇ Скачиваю {Path(cloud_path).name}")

        def on_progress(done, total):
            if total > 0:
                pct = int(done / total * 100)
                self._status_progress.setRange(0, 100)
                self._status_progress.setValue(pct)
            else:
                self._status_progress.setRange(0, 0)

        def on_finished(lp):
            md5 = _md5_file(lp)
            self._db.set_downloaded(cloud_path, lp, last_sync_md5=md5)
            self._db.upsert_file(
                cloud_path=cloud_path,
                name=Path(cloud_path).name,
                type_="file",
                size=os.path.getsize(lp),
                modified=datetime.now(timezone.utc).isoformat(),
                md5=md5,
            )
            self._syncing.discard(cloud_path)
            self._table_model.update_status(cloud_path, "downloaded")
            self._hide_progress()
            logger.info("Downloaded: %s → %s", cloud_path, lp)
            self._active_threads.remove(thread)

        def on_error(msg):
            self._syncing.discard(cloud_path)
            self._hide_progress()
            logger.error("Download failed %s: %s", cloud_path, msg)
            QTimer.singleShot(0, lambda: QMessageBox.critical(
                self, "Ошибка скачивания",
                f"Не удалось скачать {Path(cloud_path).name}:\n{msg}"))
            self._active_threads.remove(thread)

        worker.progress.connect(on_progress, Qt.QueuedConnection)
        worker.finished.connect(on_finished, Qt.QueuedConnection)
        worker.error.connect(on_error, Qt.QueuedConnection)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()

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
        if self._local_mode:
            self._load_folder_local(path)
        else:
            self._load_folder_async(path)

    def refresh_current(self):
        if self._local_mode:
            self._load_folder_local(self._current_path)
        else:
            self._load_folder_async(self._current_path)

    def _on_folder_clicked(self, index: QModelIndex):
        path = index.data(Qt.UserRole) or "/"
        self._last_requested_path = path
        if self._local_mode:
            self._load_folder_local(path)
        else:
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
        """Обработать результат poll'инга — обновить кеш, если есть изменения."""
        if not items:
            return
        try:
            new_items = [it for it in items if not self._db.file_exists(it["path"])]
            if new_items:
                logger.info("Poll: %d new item(s)", len(new_items))
                self._db.upsert_files_batch(new_items)
                if self._local_mode:
                    self._load_folder_local(self._current_path)
                else:
                    self._load_folder_async(self._current_path)
        except Exception:
            pass

    # ── Действия ──────────────────────────────────────────

    def _download_selected(self):
        rows = self.table_view.selectionModel().selectedRows(0)
        for idx in rows:
            item = self.table_model.get_item(idx.row())
            if item and not item["is_dir"] and item["status"] != "downloaded":
                self._start_download(
                    item["cloud_path"],
                    os.path.join(_cache_dir(), item["cloud_path"].lstrip("/")),
                )

    def _upload_selected(self):
        rows = self.table_view.selectionModel().selectedRows(0)
        for idx in rows:
            item = self.table_model.get_item(idx.row())
            if item and not item["is_dir"]:
                if item["status"] == "cloud_only":
                    QMessageBox.information(
                        self, "Нет локальной копии",
                        f"Файл {item['name']} не скачан локально.\n"
                        f"Сначала скачайте его (двойной клик).")
                    continue
                self._upload_single(item["cloud_path"])

    def _delete_selected(self):
        rows = self.table_view.selectionModel().selectedRows(0)
        if not rows:
            return
        names = [self.table_model.get_item(r.row())["name"] for r in rows]
        reply = QMessageBox.question(
            self, "Подтверждение",
            f"Удалить {len(rows)} файл(ов) из облака?\n"
            + "\n".join(f"  • {n}" for n in names),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        for idx in rows:
            item = self.table_model.get_item(idx.row())
            if item:
                try:
                    self._api.delete(item["cloud_path"])
                    self._db.remove_file(item["cloud_path"])
                    local = os.path.join(
                        _cache_dir(), item["cloud_path"].lstrip("/"))
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
        item = self.table_model.get_item(rows[0].row())
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

    def _on_file_double_clicked(self, index: QModelIndex):
        item = self.table_model.get_item(index.row())
        if not item:
            return
        if item["is_dir"]:
            if self._local_mode:
                self._load_folder_local(item["cloud_path"])
            else:
                self._load_folder_async(item["cloud_path"])
            return

        local_path = os.path.join(
            _cache_dir(), item["cloud_path"].lstrip("/"))

        if item["status"] == "cloud_only" or not os.path.exists(local_path):
            self._start_download(item["cloud_path"], local_path)
        else:
            self._open_file(local_path)

    def _open_file(self, local_path: str):
        QDesktopServices.openUrl(QUrl.fromLocalFile(local_path))

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
            if self._local_mode:
                self._load_folder_local(self._current_path)
            else:
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
        self._active_threads.append(thread)
        self.statusBar().showMessage("Синхронизация с облаком...")
        thread.start()

    def _show_about(self):
        QMessageBox.about(
            self, "☁️ Yandex Disk Manager",
            "Клиент Яндекс.Диска\nВерсия 2.0\n\n"
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
        item = self.table_model.get_item(index.row())
        if not item:
            return
        menu = QMenu(self)
        if not item["is_dir"]:
            menu.addAction("⬇ Скачать", self._download_selected)
            if item["status"] != "cloud_only":
                menu.addAction("⬆ Загрузить изменения",
                               self._upload_selected)
            menu.addSeparator()
            menu.addAction("🔗 Публичная ссылка", self._get_public_link)
            menu.addSeparator()
            menu.addAction(
                "📂 Открыть папку",
                lambda: self._open_file(
                    os.path.dirname(
                        os.path.join(_cache_dir(),
                                     item["cloud_path"].lstrip("/")))))
        menu.addSeparator()
        menu.addAction("🗑 Удалить", self._delete_selected)
        menu.exec(self.table_view.viewport().mapToGlobal(pos))

    def closeEvent(self, event):
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
