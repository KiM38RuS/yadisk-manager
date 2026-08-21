"""
PySide6 GUI — двухпанельный файловый менеджер для Яндекс.Диска.
Фичи: on-demand sync, трей, автозапуск, прогресс, разрешение конфликтов.
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue, Empty
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QSize, QEvent, QRect, QPoint, QModelIndex, QByteArray, QItemSelection, QItemSelectionModel, QUrl, QRectF, QMimeData, QStringListModel
from PySide6.QtGui import (
    QAction, QIcon, QFont, QColor, QPalette, QBrush,
    QFontDatabase, QShortcut, QKeySequence, QPixmap,
    QPainter, QLinearGradient, QMovie, QGuiApplication,
    QDesktopServices, QDrag, QCursor, QImage, qRgba,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTreeView, QTableView, QHeaderView,
    QPushButton, QToolBar, QMenu, QMenuBar, QStatusBar,
    QLabel, QMessageBox, QSystemTrayIcon, QStyle, QProgressBar,
    QAbstractItemView, QFileDialog, QInputDialog,
    QToolButton, QListView, QListWidget, QSizePolicy, QFrame,
    QLineEdit, QStackedWidget, QSlider,
)

import db
import disk_api
import sync
import watcher
from ipc import send_ipc_command, IPC_ENABLED

from ui_shared import (
    ASSETS_DIR,
    _svg_icon, _cache_dir, _local_path, _icon_for, _human_size, _md5_file,
    _clear_svg_icon_cache,
    _is_windows_reserved, WINDOWS_RESERVED,
    ICONS, STATUS_LABELS, STATUS_ICON, STATUS_COLOR, STATUS_COLOR_LIGHT,
    POLL_INTERVAL_MS,
    _autorun_is_enabled, _autorun_set,
    advance_rotation,
)
from ui_braille_spinner import BrailleSpinner as _BrailleSpinner
from ui_wave_spinner import WaveSpinner as _WaveSpinner
from ui_log import LogSignal, LogHandler, LogWindow
from ui_workers import DownloadWorker, UploadWorker, ZipDownloadWorker as _ZipDownloadWorker
from ui_tree_model import FolderTreeItem, FolderTreeModel
from ui_table_model import RowHoverDelegate, FileTableModel, FileTableSortModel
from ui_dialogs import AuthDialog, SettingsDialog, UpdateDialog
from ui_threads import (
    WorkerThread as _WorkerThread,
    SyncThread as _SyncThread,
    ApiListThread as _ApiListThread,
    FolderLoadThread as _FolderLoadThread,
    AllFilesThread as _AllFilesThread,
    FolderDownloadThread as _FolderDownloadThread,
    RecentFilesThread as _RecentFilesThread,
    SearchThread as _SearchThread,
    MetaFetchThread as _MetaFetchThread,
    StartupScanThread as _StartupScanThread,
    AutoDownloadThread as _AutoDownloadThread,
    DropUploadThread as _DropUploadThread,
    DeleteFilesThread as _DeleteFilesThread,
    InternalDropThread as _InternalDropThread,
    PasteFilesThread as _PasteFilesThread,
    DirPathsThread,
)
from ui_search_edit import SearchEdit
from ui_breadcrumbs import BreadcrumbBar

logger = logging.getLogger(__name__)

from _version import VERSION
import updater

# Интервал полного опроса всех файлов для детекции изменений в облаке (15 мин)
BULK_SYNC_INTERVAL_MS = 900000

# Map QEvent.Type int → name for diagnostics
_EVENT_NAMES: dict[int, str] | None = None
def event_type_name(et: int) -> str:
    global _EVENT_NAMES
    if _EVENT_NAMES is None:
        _EVENT_NAMES = {v.value: k for k, v in vars(QEvent.Type).items()
                        if isinstance(v, QEvent.Type)}
    return _EVENT_NAMES.get(et, f"UNKNOWN({et})")

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
        self._recently_downloaded: set[str] = set()
        self._pend_upload: set[str] = set()
        self._recently_deleted: set[str] = set()  # пути, удалённые пользователем (< 30 сек)
        self._pending_selection: list | None = None  # выделение для восстановления после async-загрузки
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

        # Drag-and-drop: отслеживание начала перетаскивания
        self._drag_start_pos: QPoint | None = None
        self._drag_start_view: str | None = None  # 'tree' or 'table'
        self._pending_drag_button: int | None = None  # LMB/RMB для старта drag
        self._drag_mouse_button: int | None = None  # кнопка активного drag
        self._pending_internal_drop: tuple | None = None  # (paths, target) для RMB-drag
        self._internal_drop_op_id: int | None = None  # ID pending_op для внутреннего DnD
        self._delete_op_id: int | None = None  # ID pending_op для удаления

        # Лимит одновременных скачиваний — чтобы не захламлять пул соединений
        self._max_concurrent = 4
        self._download_queue: list[tuple[str, str]] = []  # (cloud_path, local_path)
        self._active_downloads = 0

        # Лимит одновременных загрузок — чтобы не захламлять пул соединений
        self._max_upload_concurrent = 4
        self._upload_queue: list[tuple[str, str, str]] = []  # (cloud_path, local_path, local_md5)
        self._active_uploads = 0

        # Debounce обновления тулбара — _update_toolbar_buttons вызывается
        # до 100 раз за раз, группируем в один
        self._toolbar_timer = QTimer(self)
        self._toolbar_timer.setSingleShot(True)
        self._toolbar_timer.timeout.connect(self._update_toolbar_buttons)
        self._toolbar_debounce_ms = 50

        # Потокобезопасная очередь для результатов из рабочих потоков
        self._download_results: Queue = Queue()
        self._result_timer = QTimer(self)
        self._result_timer.timeout.connect(self._process_result_queue)
        self._result_timer.start(100)  # poll every 100ms

        # Очередь и лимит для MetaFetch (проверка метаданных + MD5)
        self._meta_fetch_queue: list[tuple[str, str, str]] = []  # (cloud_path, local_path, last_sync_md5)
        self._active_meta_fetches = 0
        self._max_meta_concurrent = 8

        # Очередь для обратной MetaFetch (облако→локальный кеш, reverse sync)
        self._reverse_meta_queue: list[tuple[str, str, str]] = []

        self._watcher_queue: list[tuple[str, str, str | None, bool]] = []
        self._watcher_timer = QTimer(self)
        self._watcher_timer.timeout.connect(self._flush_watcher_queue)
        self._watcher_timer.start(200)  # poll watcher queue every 200ms

        # ── вращение иконок syncing / unknown ──────────────
        self._spin_timer = QTimer(self)
        self._spin_timer.timeout.connect(self._on_spin_tick)
        # ОТКЛЮЧЕНО для теста: постоянный 60fps-таймер дёргает модели 60 раз/с
        # (refresh_animated_icons обходит все строки таблицы и дерева),
        # что даёт SLOW WINDOW EVENT / FREEZE и, возможно, повреждение кучи
        # при смене темы. Для включения: раскомментировать start(16).
        # self._spin_timer.start(16)  # ~60fps — плавное вращение

        # ── type-ahead для таблицы ────────────────────────
        self._typeahead_buf = ""
        self._typeahead_timer = QTimer(self)
        self._typeahead_timer.setSingleShot(True)
        self._typeahead_timer.timeout.connect(self._clear_typeahead)

        # ── глобальный поиск ──────────────────────────────
        self._search_mode = False           # True = в таблице результаты поиска
        self._search_query = ""             # последний поисковый запрос
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.timeout.connect(self._do_search)
        self._search_debounce_ms = 300      # ждать 300ms паузы ввода перед запуском поиска
        # История поиска: сохранять запрос только при Enter или паузе >2с,
        # а не на каждый введённый символ (в истории не копятся обрывки)
        self._search_history_timer = QTimer(self)
        self._search_history_timer.setSingleShot(True)
        self._search_history_timer.timeout.connect(self._save_search_history)
        self._search_history_ms = 2000

        # ── проверка интернет-соединения ──────────────────
        self._online = True  # считаем что онлайн до первой проверки
        self._connectivity_timer = QTimer(self)
        self._connectivity_timer.timeout.connect(self._check_connectivity)
        self._connectivity_timer.start(30000)  # проверять каждые 30 секунд

        # Предотвращение рекурсии при перекрёстном снятии выделения
        self._selection_updating = False

        # ── Окно лога (должен быть создан до _init_ui, где вызывается _update_log_label_style) ─
        self._log_signal = LogSignal()
        self._log_handler = LogHandler(self._log_signal)
        # Прикрепить к корневому логгеру СРАЗУ, а не в main.py после __init__,
        # чтобы все сообщения внутри __init__ тоже попадали в лог.
        logging.getLogger().addHandler(self._log_handler)
        self._log_window = LogWindow(self._log_signal, self)
        self._log_window.setVisible(False)
        self._log_visible_before_minimize = False
        self._log_window.restore_position()

        self._init_ui()
        # Фокус на дерево папок с самого старта (дерево отображается пустым сразу)
        self.tree_view.setFocus()
        # Загрузить историю поиска для поискового поля
        self._search_edit.refresh_history()
        self._init_tray()
        self._init_updates()
        self._check_cache_dir()
        # Watcher НЕ запускаем здесь — он стартует после startup_scan,
        # чтобы вотчер не успел поймать файловые события во время
        # стартовой проверки и не начал ложную выгрузку всех файлов в облако.

        # Авто-показ окна лога при запуске (если включено в настройках).
        # НЕ вызываем _sync_log_zorder(): Менеджер ещё не показан (show()
        # вызывается в main.py после __init__) и не активен — иначе лог
        # сразу ушёл бы на дно z-order (ветка деактивации) и остался
        # невидимым. Менеджер показывается позже и ложится поверх лога.
        # В silent-режиме (автозапуск) окно лога не показываем — только
        # по клику «Показать лог» / пункту трея.
        if db.get_show_log_on_startup() and "--silent" not in sys.argv:
            self._log_window.setVisible(True)

        # Стартовое сканирование локального кеша + облака
        QTimer.singleShot(500, self._startup_scan)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_cloud)
        self._poll_timer.start(POLL_INTERVAL_MS)

        # Периодический bulk-опрос всех файлов (облако→локальный кеш)
        self._bulk_sync_timer = QTimer(self)
        self._bulk_sync_timer.timeout.connect(self._start_bulk_sync)
        self._bulk_sync_timer.start(BULK_SYNC_INTERVAL_MS)
        # Первый bulk sync — через 10 секунд после старта
        QTimer.singleShot(10000, self._start_bulk_sync)

        # Асинхронная загрузка данных — окно покажется сразу
        self._show_left_busy("Загрузка...")
        QTimer.singleShot(0, self._initial_load)

        # ── Freeze detector (heartbeat-based) ─────────────────────
        # QTimer каждые 100мс обновляет _heartbeat_ts в главном потоке.
        # Daemon-поток проверяет: если heartbeat не обновлялся >500ms —
        # значит главный поток заблокирован (Qt C++ paint / Python код).
        self._heartbeat_ts = time.monotonic()
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.timeout.connect(self._heartbeat_tick)
        self._heartbeat_timer.start(100)

        self._freeze_thread_running = True
        self._freeze_monitor_thread = threading.Thread(
            target=self._freeze_monitor, daemon=True, name="freeze-monitor")
        self._freeze_monitor_thread.start()

        # ── Event filter на QApplication для замера ВСЕХ событий ──
        _app = QApplication.instance()
        if _app and _app is not self:
            _app.installEventFilter(self)
        # Флаг, чтобы eventFilter различал app-level и widget-level вызовы.
        self._app_event_filter_installed = True

    def _heartbeat_tick(self):
        """Вызывается из QTimer в главном потоке — ставит метку, что UI жив."""
        self._heartbeat_ts = time.monotonic()

    def _freeze_monitor(self):
        """Daemon-поток: проверяет heartbeat главного потока.

        Если heartbeat не обновлялся >500ms — главный поток заблокирован
        (Qt C++ paint / layout / Python-код). Логирует stacktrace.
        """
        while getattr(self, '_freeze_thread_running', True):
            time.sleep(0.2)
            now = time.monotonic()
            elapsed_ms = (now - self._heartbeat_ts) * 1000
            if elapsed_ms > 500:
                # Достаём стек главного потока
                main_id = threading.main_thread().ident
                frames = sys._current_frames().get(main_id)
                stack = ""
                if frames:
                    stack = "\n" + "".join(
                        traceback.format_stack(frames))
                logger.warning(
                    "⚠️ FREEZE: UI blocked for ~%d ms%s",
                    int(elapsed_ms), stack)

    def event(self, event):
        """Замер времени обработки каждого события MainWindow."""
        start = time.monotonic()
        result = super().event(event)
        elapsed = (time.monotonic() - start) * 1000
        if elapsed > 200:
            logger.warning("⚠️ SLOW WINDOW EVENT: type=%d(%s) took %dms",
                          event.type(), event_type_name(event.type()),
                          int(elapsed))
        return result

    def _reauthorize(self) -> bool:
        """Показать диалог авторизации и обновить API-клиент.

        Возвращает True, если токен получен, False если пользователь отменил.
        """
        auth = AuthDialog(self)
        auth.setWindowModality(Qt.WindowModal)
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
        msg.setWindowModality(Qt.WindowModal)
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
        self._show_loading("Загрузка папок...")
        self._show_tree_loading()
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
            logger.info("Tree loaded: %d top-level folders",
                        len(self.tree_model._visible_root.children))
            # Корень «Яндекс Диск» всегда раскрыт
            self._expand_tree_root()
        self._hide_tree_loading()
        # Фокус на дерево папок (а не на строку поиска),
        # только когда оно реально видимо после снятия loading-оверлея
        self.tree_view.setFocus()
        # После дерева — загружаем файлы (API + SQLite в фоновом потоке)
        self._show_loading("Загрузка файлов...")
        self._show_table_loading()
        self._start_folder_load("/", use_api=True, after_all_files=True)

    def _start_folder_load(self, path: str, use_api: bool = True,
                           after_all_files: bool = False):
        """Загрузить содержимое папки в фоновом потоке (API + SQLite + fs).

        Все запросы к БД выполняются в FolderLoadThread — главный поток
        только применяет готовые данные к модели таблицы (v0.11.5,
        устранение FREEZE UI при старте/навигации на БД 100К+ файлов).
        """
        thread = _FolderLoadThread(
            self._api, self._db, path, use_api=use_api,
            recently_deleted=self._recently_deleted,
            syncing=self._syncing, parent=self)
        thread.finished.connect(
            lambda p, i, f, r, e, _aaf=after_all_files:
                self._on_folder_loaded(p, i, f, r, e, _aaf))
        thread.finished.connect(thread.deleteLater)
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()

    def _on_folder_loaded(self, path: str, items: list, fixed_paths: list,
                          reverse_checks: list, error: str,
                          after_all_files: bool = False):
        """Применить результат FolderLoadThread к UI (только быстрые операции)."""
        if error:
            logger.error("Folder load failed for %s: %s", path, error)
            if self._is_auth_error(error):
                self._handle_auth_error()
                return
            self._hide_left_busy()
            self.statusBar().showMessage(f"❌ {error}")
            self._hide_table_loading()
            # Крошки не должны показывать несостоявшийся путь
            self._breadcrumb_bar.set_path(
                getattr(self, "_last_good_path", "/"))
            return
        # Защита от race: пользователь уже ушёл в другую папку
        if path != self._current_path:
            logger.debug("Ignored stale folder load for %s (current %s)",
                         path, self._current_path)
            return
        # Папка загружена — запоминаем как последний хороший путь (для отката)
        self._last_good_path = path
        # Nav-fix: файлы найдены локально → поставить на загрузку и обновить дерево
        for cp in fixed_paths:
            self._pend_upload.add(cp)
            self._update_tree_status(cp)
        # Lazy reverse sync: поставить в очередь MetaFetch
        for cp, lp, lsmd5 in reverse_checks:
            self._queue_reverse_meta_fetch(cp, lp, lsmd5)
        # Если набор путей не изменился — пропускаем перерисовку (нет мигания)
        new_paths = {i["path"] for i in items}
        if new_paths != self.table_model.current_paths:
            self._sort_table_sync(path, items)
        # Колонка «Расположение» — только в режиме поиска
        self._update_location_column()
        self._hide_left_busy()
        self.statusBar().showMessage(f"{path} — {len(items)} эл.", 5000)
        self._schedule_toolbar_update()
        self._hide_table_loading()
        # Восстановление выделения после навигации (если запрашивалось).
        # Снимок НЕ сбрасывается: Шаг 2 навигации (API) может перерисовать
        # таблицу повторно — выделение восстановится снова (идемпотентно).
        # Новый снимок ставится при следующей навигации (_navigate_to_folder).
        if self._pending_selection is not None:
            self._restore_selection(self._pending_selection)
        if after_all_files:
            self._start_all_files_load()

    def _start_all_files_load(self):
        """Запустить фоновую загрузку полного списка файлов (для локальной навигации)."""
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
            self._hide_loading()
            self._hide_left_busy()
            self.statusBar().showMessage(f"Загружено {count} файлов")
        else:
            self._hide_loading()
            self._hide_left_busy()
            self.statusBar().showMessage("Список файлов загружен не полностью")
        # Обновляем текущий вид из БД (мгновенно, без API) — даже частичный список полезен
        self._load_folder_local(self._current_path)

    def _on_all_files_progress(self, offset: int, count: int):
        """Обновление прогресса загрузки всех файлов в статус-баре."""
        self._show_loading(f"Загрузка файлов: {count} ({offset} обработано)")

    # ── loading helpers ─────────────────────────────────

    def _show_tree_loading(self):
        self._tree_spinner.show()

    def _hide_tree_loading(self):
        self._tree_spinner.hide()

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

    def _expand_tree_root(self):
        """Развернуть корневой узел «Яндекс Диск» (индекс 0,0)."""
        root_idx = self.tree_model.index(0, 0)
        if root_idx.isValid():
            self.tree_view.expand(root_idx)

    def _on_tree_collapsed(self, index: QModelIndex):
        """Корень «Яндекс Диск» всегда раскрыт — сворачивание отменяется."""
        if not index.isValid():
            return
        item: FolderTreeItem = index.internalPointer()
        if item is self.tree_model._visible_root:
            self.tree_view.expand(index)

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
        # В режиме поиска индикатор сортировки скрыт — вернуть его при навигации
        self.table_view.horizontalHeader().setSortIndicatorShown(True)

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
        self._hide_left_busy()
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
            QTreeView {
                color: #000000;
            }
            QTreeView::item {
                border-radius: 4px;
                padding: 1px 4px;
                border: none;
            }
            QTreeView::item:selected {
                background: palette(Midlight);
                color: #000000;
            }
            QTreeView::item:selected:!active {
                background: palette(AlternateBase);
                color: #000000;
            }
            QTreeView::item:hover:!selected {
                background: rgba(128, 128, 128, 0.06);
            }
        """)
        # Локальная палитра больше не устанавливается — все цвета в явном hex в QSS.
        # При смене темы _apply_theme() полностью переписывает QSS с hex-цветами темы.
        # _apply_theme() также убирает palette(AlternateBase/Midlight) — заменяет на hex.
        self.tree_view.clicked.connect(self._on_folder_clicked)
        self.tree_view.expanded.connect(self._on_tree_item_expanded)
        self.tree_view.collapsed.connect(self._on_tree_collapsed)
        self.tree_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree_view.customContextMenuRequested.connect(
            self._on_tree_context_menu)
        self.tree_view.selectionModel().selectionChanged.connect(
            self._on_tree_selection_changed)
        # Корень «Яндекс Диск» всегда раскрыт: при любом сбросе модели
        # (refresh/перезагрузка дерева) снова разворачиваем его
        self.tree_model.modelReset.connect(self._expand_tree_root)
        # Клик по пустому месту — сброс выделения
        self.tree_view.viewport().installEventFilter(self)
        self.tree_view.viewport().setAcceptDrops(True)
        self.tree_view.setDragEnabled(True)

        # ── Дерево папок (контейнер + плавающий BrailleSpinner поверх) ──
        self._tree_container = QWidget()
        self._tree_layout = QVBoxLayout(self._tree_container)
        self._tree_layout.setContentsMargins(0, 0, 0, 0)
        self._tree_layout.addWidget(self.tree_view)
        self._tree_spinner = _BrailleSpinner(self._tree_container)
        self._tree_spinner.hide()
        splitter.addWidget(self._tree_container)

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
        self.table_view.setColumnWidth(4, 100)  # Изменён
        self.table_view.setColumnWidth(5, 180)  # Расположение
        # Колонка «Расположение» видна только в результатах поиска
        self.table_view.setColumnHidden(5, True)
        self.table_view.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Fixed)
        self.table_view.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents)
        self.table_view.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeToContents)
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
                background: palette(Midlight);
                color: #000000;
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
        self.table_view.viewport().setAcceptDrops(True)
        self.table_view.setDragEnabled(True)
        self.table_view.installEventFilter(self)

        # ── Таблица файлов (QStackedWidget: loading-страница со спиннером / таблица) ──
        self._table_loading_page = QWidget()
        table_loading_layout = QVBoxLayout(self._table_loading_page)
        table_loading_layout.setAlignment(Qt.AlignCenter)
        self._table_spinner = _BrailleSpinner(self._table_loading_page, fill_parent=False)
        table_loading_layout.addWidget(self._table_spinner)

        self._table_stack = QStackedWidget()
        self._table_stack.addWidget(self._table_loading_page)  # index 0
        self._table_stack.addWidget(self.table_view)           # index 1
        self._table_stack.setCurrentIndex(0)  # показываем спиннер с самого старта
        splitter.addWidget(self._table_stack)

        splitter.setSizes([220, 680])
        self.setCentralWidget(splitter)

        # Меню
        self._create_menu()
        self._create_toolbar()

        # Drag-and-drop из Проводника
        self.setAcceptDrops(True)

        # Статусбар
        # Волновой спиннер — показывается перед надписью
        # («Закрытие программы...», «Перезагрузка программы...»).
        # Обычный QLabel из глифов Брайля: не раздувает высоту панели
        # (брайлевский спиннер-виджет с запасом +20px в sizeHint давал 51px).
        self._status_spinner = _WaveSpinner()
        self._status_spinner.hide()
        self.statusBar().insertWidget(0, self._status_spinner)

        # Счётчик множественного выделения («Выбрано: N») — слева в статус-баре,
        # сразу после спиннера (перенесено из контекстного меню). Виден только
        # когда выделено несколько элементов.
        self._status_selection_label = QLabel("")
        self._status_selection_label.hide()
        self.statusBar().insertWidget(1, self._status_selection_label)

        self._status_label = QLabel("")
        self.statusBar().addWidget(self._status_label, 1)

        # Ссылка «Показать лог» и прогресс-бар — в одном контейнере,
        # чтобы скрытие прогресс-бара не сдвигало надпись (ПЛАНЫ 6.1).
        # Индикатор загрузки (спиннер Брайля + текст) — тоже здесь, справа:
        # левая зона остаётся чистой для перезагрузки/выключения.
        # Прогресс-бар живёт в слоте фиксированной ширины, поэтому его
        # появление/скрытие не сдвигает ни индикатор загрузки, ни «Показать лог».
        self._status_right = QWidget()
        self._status_right_layout = QHBoxLayout(self._status_right)
        self._status_right_layout.setContentsMargins(0, 0, 0, 0)
        self._status_right_layout.setSpacing(8)

        # Индикатор загрузки папок/файлов: спиннер + надпись справа,
        # чтобы не пересекаться с левой зоной (спиннер и надпись
        # перезагрузки/выключения)
        self._loading_spinner = _WaveSpinner()
        self._loading_spinner.hide()
        self._status_right_layout.addWidget(self._loading_spinner)

        self._loading_label = QLabel("")
        self._loading_label.hide()
        self._status_right_layout.addWidget(self._loading_label)

        self._log_label = QLabel("Показать лог")
        self._log_label.setCursor(Qt.PointingHandCursor)
        self._log_label.mousePressEvent = lambda e: self._toggle_log_window()
        self._update_log_label_style()
        self._status_right_layout.addWidget(self._log_label)

        # Слот прогресс-бара: контейнер фиксированной ширины (200 px —
        # максимум бара) всегда в раскладке, даже когда сам бар скрыт.
        # Без этого появление бара меняло ширину правого контейнера
        # и сдвигало надписи («Загрузка файлов...», «Показать лог»).
        self._status_progress_slot = QWidget()
        self._status_progress_slot.setFixedWidth(200)
        _progress_slot_layout = QHBoxLayout(self._status_progress_slot)
        _progress_slot_layout.setContentsMargins(0, 0, 0, 0)
        _progress_slot_layout.setSpacing(0)

        self._status_progress = QProgressBar()
        self._status_progress.setVisible(False)
        _progress_slot_layout.addWidget(self._status_progress)
        self._status_right_layout.addWidget(self._status_progress_slot)

        self.statusBar().addPermanentWidget(self._status_right)

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

        # Ctrl+F — фокус в строку поиска
        self._shortcut_search = QAction("Поиск", self)
        self._shortcut_search.setShortcut(QKeySequence("Ctrl+F"))
        self._shortcut_search.triggered.connect(self._focus_search)
        self.addAction(self._shortcut_search)

        # Ctrl+L / Alt+D — фокус в адресную строку (как в Проводнике)
        for _seq in ("Ctrl+L", "Alt+D"):
            _act_addr = QAction("Адресная строка", self)
            _act_addr.setShortcut(QKeySequence(_seq))
            _act_addr.triggered.connect(self._focus_address_bar)
            self.addAction(_act_addr)

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

        Обёртка с защитой от реентерабельности: применение темы вызывает
        setPalette/setStyleSheet, что порождает PaletteChange-события; если
        changeEvent или другой обработчик вызовет _apply_theme повторно во
        время применения — Qt повредит кучу (STATUS_HEAP_CORRUPTION).
        """
        if getattr(self, '_applying_theme', False):
            logger.warning("_apply_theme: reentrant call skipped (already applying)")
            return
        self._applying_theme = True
        try:
            logger.info("Applying theme...")
            self._apply_theme_impl()
            logger.info("Theme applied OK")
        finally:
            self._applying_theme = False

    def _apply_theme_impl(self):
        """Применить тему (system / light / dark) из конфига.

        Fusion style уже установлен в main.py (до создания виджетов).
        Здесь только меняем QPalette — мгновенно, без QSS.
        """
        # Сброс кеша иконок: при смене темы иконки пересоздаются
        # с новым is_dark (старые не копятся в памяти).
        _clear_svg_icon_cache()
        theme = db.get_theme()
        app = QApplication.instance()
        is_dark = theme == "dark"
        logger.info("Applying theme: config=%r is_dark=%s (windows_dark=%s)",
                    theme, is_dark, self._is_windows_dark_mode() if hasattr(self, '_is_windows_dark_mode') else '?')

        if theme == "light":
            palette = self._make_palette_light()
            app.setPalette(palette)
        elif theme == "dark":
            palette = self._make_palette_dark()
            app.setPalette(palette)
        elif self._is_windows_dark_mode():
            self._apply_dark_system_palette(app)
            is_dark = True
        else:
            app.setPalette(app.style().standardPalette())

        # ── Единый QSS: ОДИН app.setStyleSheet вместо каскада реполошингов ──
        # Раньше: app.setStyleSheet("") + unpolish/polish всех виджетов + tooltip +
        # tree + table + toolbar отдельными setStyleSheet = 5-6 ПОЛНЫХ реполошингов
        # приложения по 500-700 мс каждый (FREEZE). При реальном вводе это давало
        # многомиллисекундное окно, в котором ввод пересекался с repolish →
        # heap corruption. Теперь — один реполошинг, в ~6 раз быстрее.
        fg = "#FFFFFF" if is_dark else "#000000"
        bg_sel = "#3C3C3C" if is_dark else "#D0D0D0"
        bg_sel_inactive = "#2A2A2E" if is_dark else "#F5F5F5"
        border = "#3C3C3C" if is_dark else "#D0D0D0"

        if is_dark:
            tooltip_bg, tooltip_text, tooltip_border = "#383838", "#FFFFFF", "#555555"
        else:
            tooltip_bg, tooltip_text, tooltip_border = "#ffffe5", "#000000", "#C0C0C0"

        # Popup истории поиска: фон в тон строке поиска тулбара
        popup_bg = "#252526" if is_dark else "#FFFFFF"

        if is_dark:
            tb_qss = """
                QToolBar {
                    background: #1E1E1E;
                    spacing: 0px;
                    left: -4px;
                }
                QToolBar::separator {
                    margin: 0 2px;
                    width: 1px;
                    height: 20px;
                    background: #3C3C3C;
                }
                QToolButton {
                    font-size: 12px; padding: 2px 10px;
                    color: #FFFFFF;
                    background: transparent;
                    border: none;
                    border-radius: 4px;
                }
                QToolButton:hover {
                    background: rgba(64, 150, 255, 0.12);
                }
                QToolButton:pressed {
                    background: rgba(64, 150, 255, 0.25);
                }
                QToolButton:disabled {
                    color: #606060;
                }
                QToolBar QLineEdit {
                    background: #252526;
                    color: #FFFFFF;
                    border: 1px solid #3C3C3C;
                    border-radius: 4px;
                    padding: 5px 8px;
                    min-height: 24px;
                }
                QToolBar QLineEdit:focus {
                    border: 2px solid #094771;
                }
            """
        else:
            tb_qss = """
                QToolBar {
                    background: #F0F0F0;
                    spacing: 0px;
                    left: -4px;
                }
                QToolBar::separator {
                    margin: 0 2px;
                    width: 1px;
                    height: 20px;
                    background: #D0D0D0;
                }
                QToolButton {
                    font-size: 12px; padding: 2px 10px;
                    color: #000000;
                    background: transparent;
                    border: none;
                    border-radius: 4px;
                }
                QToolButton:hover {
                    background: rgba(64, 150, 255, 0.12);
                }
                QToolButton:pressed {
                    background: rgba(64, 150, 255, 0.25);
                }
                QToolButton:disabled {
                    color: #808080;
                }
                QToolBar QLineEdit {
                    background: #FFFFFF;
                    color: #000000;
                    border: 1px solid #D0D0D0;
                    border-radius: 4px;
                    padding: 5px 8px;
                    min-height: 24px;
                }
                QToolBar QLineEdit:focus {
                    border: 2px solid #0066FF;
                }
            """

        qss = (
            f"QToolTip {{ background-color: {tooltip_bg}; color: {tooltip_text}; "
            f"border: 1px solid {tooltip_border}; padding: 2px 4px; }}\n"
            f"QTreeView {{\n"
            f"    color: {fg};\n"
            f"}}\n"
            f"QTreeView::item {{\n"
            f"    border-radius: 4px;\n"
            f"    padding: 1px 4px;\n"
            f"    border: none;\n"
            f"}}\n"
            f"QTreeView::item:selected {{\n"
            f"    background: {bg_sel};\n"
            f"    color: {fg};\n"
            f"}}\n"
            f"QTreeView::item:selected:!active {{\n"
            f"    background: {bg_sel_inactive};\n"
            f"    color: {fg};\n"
            f"}}\n"
            f"QTreeView::item:hover:!selected {{\n"
            f"    background: rgba(128, 128, 128, 0.06);\n"
            f"}}\n"
            f"QTableView::item:selected {{\n"
            f"    background: {bg_sel};\n"
            f"    color: {fg};\n"
            f"}}\n"
            f"QHeaderView::section {{\n"
            f"    font-weight: normal;\n"
            f"    font-size: 12px;\n"
            f"    padding: 3px 6px;\n"
            f"    border: none;\n"
            f"    border-right: 1px solid {border};\n"
            f"    border-bottom: 1px solid {border};\n"
            f"}}\n"
            f"QHeaderView::section:last {{\n"
            f"    border-right: none;\n"
            f"}}\n"
            f"QHeaderView::section:highlighted {{\n"
            f"    font-weight: normal;\n"
            f"}}\n"
            # Popup истории поиска (QListView#search_history_popup):
            # hover-подсветка в акцент темы + выделение как у дерева/таблицы
            f"QListView#search_history_popup {{\n"
            f"    background-color: {popup_bg};\n"
            f"    color: {fg};\n"
            f"    border: 1px solid {border};\n"
            f"    padding: 2px;\n"
            f"}}\n"
            f"QListView#search_history_popup::item {{\n"
            f"    padding: 4px 8px;\n"
            f"    border-radius: 4px;\n"
            f"}}\n"
            f"QListView#search_history_popup::item:hover {{\n"
            f"    background: rgba(64, 150, 255, 0.12);\n"
            f"}}\n"
            f"QListView#search_history_popup::item:selected {{\n"
            f"    background: {bg_sel};\n"
            f"    color: {fg};\n"
            f"}}\n"
            + tb_qss
        )
        app.setStyleSheet(qss)

        # Тулбар: widget-level QSS (приоритетнее app-level, иначе светлый
        # стиль из конструктора перекрывает тёмную тему). Один setStyleSheet
        # на QToolBar = repolish только тулбара, дёшево.
        for w in self.findChildren(QToolBar):
            w.setStyleSheet(tb_qss)
            break

        # Дерево: widget-level QSS (конструктор задаёт чёрный текст — без
        # перезаписи он останется чёрным в тёмной теме)
        if hasattr(self, 'tree_view'):
            self.tree_view.setStyleSheet(f"""
                QTreeView {{
                    color: {fg};
                }}
                QTreeView::item {{
                    border-radius: 4px;
                    padding: 1px 4px;
                    border: none;
                }}
                QTreeView::item:selected {{
                    background: {bg_sel};
                    color: {fg};
                }}
                QTreeView::item:selected:!active {{
                    background: {bg_sel_inactive};
                    color: {fg};
                }}
                QTreeView::item:hover:!selected {{
                    background: rgba(128, 128, 128, 0.06);
                }}
            """)

        # Таблица: widget-level QSS (конструктор задаёт чёрный текст)
        if hasattr(self, 'table_view'):
            self.table_view.setStyleSheet(f"""
                QTableView::item:selected {{
                    background: {bg_sel};
                    color: {fg};
                }}
                QHeaderView::section {{
                    font-weight: normal;
                    font-size: 12px;
                    padding: 3px 6px;
                    border: none;
                    border-right: 1px solid {border};
                    border-bottom: 1px solid {border};
                }}
                QHeaderView::section:last {{
                    border-right: none;
                }}
                QHeaderView::section:highlighted {{
                    font-weight: normal;
                }}
            """)

        # Иконки окна/трея и тулбара — с актуальным is_dark
        self._update_app_icons()
        self._refresh_toolbar_icons()

        # Обновить цвета надписи «Показать лог»
        self._update_log_label_style()

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
        palette.setColor(QPalette.Midlight, QColor(0xD0, 0xD0, 0xD0))
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
        palette.setColor(QPalette.WindowText, QColor(0xFF, 0xFF, 0xFF))
        palette.setColor(QPalette.Base, QColor(0x25, 0x25, 0x26))
        palette.setColor(QPalette.AlternateBase, QColor(0x2A, 0x2A, 0x2E))
        palette.setColor(QPalette.ToolTipBase, QColor(0x38, 0x38, 0x38))
        palette.setColor(QPalette.ToolTipText, QColor(0xFF, 0xFF, 0xFF))
        palette.setColor(QPalette.Text, QColor(0xFF, 0xFF, 0xFF))
        palette.setColor(QPalette.Button, QColor(0x2D, 0x2D, 0x30))
        palette.setColor(QPalette.ButtonText, QColor(0xFF, 0xFF, 0xFF))
        palette.setColor(QPalette.BrightText, QColor(0xFF, 0x00, 0x00))
        palette.setColor(QPalette.Midlight, QColor(0x3C, 0x3C, 0x3C))
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
        palette.setColor(QPalette.WindowText, QColor(0xFF, 0xFF, 0xFF))
        palette.setColor(QPalette.Base, QColor(0x25, 0x25, 0x26))
        palette.setColor(QPalette.AlternateBase, QColor(0x2A, 0x2A, 0x2E))
        palette.setColor(QPalette.ToolTipBase, QColor(0x38, 0x38, 0x38))
        palette.setColor(QPalette.ToolTipText, QColor(0xFF, 0xFF, 0xFF))
        palette.setColor(QPalette.Text, QColor(0xFF, 0xFF, 0xFF))
        palette.setColor(QPalette.Button, QColor(0x2D, 0x2D, 0x30))
        palette.setColor(QPalette.ButtonText, QColor(0xFF, 0xFF, 0xFF))
        palette.setColor(QPalette.BrightText, QColor(0xFF, 0x00, 0x00))
        palette.setColor(QPalette.Midlight, QColor(0x3C, 0x3C, 0x3C))
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

        # ── Файл ──────────────────────────────────────────
        file_menu = mb.addMenu("Файл")
        self._act_save = QAction("Сохранить на компьютере", self)
        self._act_save.setIcon(_svg_icon("loaded.svg", 24))
        self._act_save.triggered.connect(self._save_to_computer)
        file_menu.addAction(self._act_save)

        self._act_leave = QAction("Оставить только в облаке", self)
        self._act_leave.setIcon(_svg_icon("cloud.svg", 24))
        self._act_leave.triggered.connect(self._leave_only_in_cloud)
        file_menu.addAction(self._act_leave)

        file_menu.addSeparator()

        self._act_refresh = QAction("Обновить", self)
        self._act_refresh.setIcon(_svg_icon("refresh.svg", 24))
        self._act_refresh.setShortcut(QKeySequence("F5"))
        self._act_refresh.triggered.connect(self.refresh_current)
        file_menu.addAction(self._act_refresh)

        file_menu.addSeparator()

        self._act_settings = QAction("Настройки", self)
        self._act_settings.setIcon(_svg_icon("settings-grey.svg", 24))
        self._act_settings.triggered.connect(self._show_settings)
        file_menu.addAction(self._act_settings)

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

        # ── Правка ────────────────────────────────────────
        edit_menu = mb.addMenu("Правка")

        self._act_copy = QAction("Копировать", self)
        self._act_copy.setIcon(_svg_icon("Copy.svg", 24))
        self._act_copy.setShortcut(QKeySequence("Ctrl+C"))
        self._act_copy.triggered.connect(self._copy_to_buffer)
        edit_menu.addAction(self._act_copy)

        self._act_cut = QAction("Вырезать", self)
        self._act_cut.setIcon(_svg_icon("scissors-3.svg", 24))
        self._act_cut.setShortcut(QKeySequence("Ctrl+X"))
        self._act_cut.triggered.connect(self._cut_to_buffer)
        edit_menu.addAction(self._act_cut)

        self._act_paste = QAction("Вставить", self)
        self._act_paste.setIcon(_svg_icon("paste.svg", 24))
        self._act_paste.setShortcut(QKeySequence("Ctrl+V"))
        self._act_paste.triggered.connect(self._paste_from_buffer)
        edit_menu.addAction(self._act_paste)

        self._act_delete = QAction("Удалить", self)
        self._act_delete.setIcon(_svg_icon("trash.svg", 24))
        self._act_delete.setShortcut(QKeySequence("Delete"))
        self._act_delete.triggered.connect(self._delete_selected)
        edit_menu.addAction(self._act_delete)

        edit_menu.addSeparator()

        self._act_new_folder = QAction("Новая папка", self)
        self._act_new_folder.setIcon(_svg_icon("folder-add-yellow.svg", 24))
        self._act_new_folder.triggered.connect(self._create_folder)
        edit_menu.addAction(self._act_new_folder)

        self._act_get_link = QAction("Скопировать ссылку", self)
        self._act_get_link.setIcon(_svg_icon("link.svg", 24))
        self._act_get_link.triggered.connect(self._get_public_link)
        edit_menu.addAction(self._act_get_link)

        edit_menu.addSeparator()

        self._act_restart = QAction("Перезапустить", self)
        self._act_restart.setIcon(_svg_icon("refresh.svg", 24))
        self._act_restart.setShortcut(QKeySequence("Shift+F5"))
        self._act_restart.triggered.connect(self._restart_app)
        edit_menu.addAction(self._act_restart)

        # ── Справка ───────────────────────────────────────
        help_menu = mb.addMenu("Справка")
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
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        tb.setMovable(False)
        tb.setStyleSheet("""
            QToolBar {
                background: #F0F0F0;
                spacing: 0px;
                left: -4px;
            }
            QToolBar::separator {
                margin: 0 2px;
                width: 1px;
                height: 20px;
                background: #D0D0D0;
            }
            QToolButton {
                font-size: 12px; padding: 2px 10px;
                color: #000000;
                background: transparent;
                border: none;
                border-radius: 4px;
            }
            QToolButton:hover {
                background: rgba(64, 150, 255, 0.12);
            }
            QToolButton:pressed {
                background: rgba(64, 150, 255, 0.25);
            }
            QToolButton:disabled {
                color: #808080;
            }
            .QLineEdit {
                background: #FFFFFF;
                color: #000000;
                border: 1px solid #D0D0D0;
                border-radius: 4px;
                padding: 5px 8px;
                min-height: 24px;
            }
            .QLineEdit:focus {
                border: 2px solid #0066FF;
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
                style = Qt.ToolButtonTextBesideIcon if show_label else Qt.ToolButtonIconOnly
                w.setToolButtonStyle(style)
            return a

        # Кнопки навигации — компактные иконки (small)
        self._tb_nav_back = _nav_btn("arrow-left-1.svg",
                                     "Назад", "Назад (Alt+Left)", self._nav_back)
        self._tb_nav_forward = _nav_btn("arrow-right-1.svg",
                                        "Вперёд", "Вперёд (Alt+Right)", self._nav_forward)
        self._tb_nav_up = _nav_btn("arrow-up-svgrepo-com.svg",
                                   "Вверх", "На уровень вверх (Backspace)", self._nav_up)

        # Настройки — иконка без подписи
        self._tb_settings = _icon_btn("settings-grey.svg",
                                      "Настройки",
                                      "Настройки", self._show_settings)
        self._sep_after_settings = tb.addSeparator()

        # Скопировать ссылку — иконка + подпись
        self._tb_link = _icon_btn("link.svg",
                                  "Скопировать ссылку",
                                  "Скопировать ссылку", self._get_public_link,
                                  show_label=True)

        # Сохранить на компьютере — иконка + подпись
        self._tb_save = _icon_btn("loaded.svg",
                                  "Сохранить на компьютере",
                                  "Скачать выделенные на компьютер",
                                  self._save_to_computer,
                                  show_label=True)

        # Оставить только в облаке — иконка + подпись
        self._tb_leave = _icon_btn("cloud.svg",
                                   "Оставить только в облаке",
                                   "Удалить локальную копию, оставить в облаке",
                                   self._leave_only_in_cloud,
                                   show_label=True)

        # Растягиваемый spacer — прижимает поиск к правому краю
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        # Поиск
        self._search_edit = SearchEdit(self._db)
        self._search_edit.setPlaceholderText("Поиск по имени…")
        self._search_edit.setMaximumWidth(220)
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._on_search)
        self._search_edit.returnPressed.connect(self._on_search_enter)
        self._search_edit.escapePressed.connect(self._exit_search)
        tb.addWidget(self._search_edit)

        # Отступ от правого края окна
        right_margin = QWidget()
        right_margin.setFixedWidth(6)
        tb.addWidget(right_margin)

        # Адресная строка (breadcrumbs) — отдельная полоса под основным тулбаром
        self.addToolBarBreak(Qt.TopToolBarArea)
        addr_tb = QToolBar("Адрес", self)
        addr_tb.setMovable(False)
        self._breadcrumb_bar = BreadcrumbBar()
        addr_tb.addWidget(self._breadcrumb_bar)
        self.addToolBar(addr_tb)
        self._breadcrumb_bar.navigate.connect(self._on_breadcrumb_navigate)
        self._breadcrumb_bar.editor_shown.connect(self._refresh_path_completer)

        # Начальное состояние кнопок
        self._update_toolbar_buttons()

    # ── Toolbar: обновление иконок ─────────────────────────

    def _refresh_toolbar_icons(self):
        """Перезагрузить иконки тулбара при смене темы."""
        if not hasattr(self, '_tb_nav_back'):
            return
        self._tb_nav_back.setIcon(_svg_icon("arrow-left-1.svg", 36))
        self._tb_nav_forward.setIcon(_svg_icon("arrow-right-1.svg", 36))
        self._tb_nav_up.setIcon(_svg_icon("arrow-up-svgrepo-com.svg", 36))
        self._tb_settings.setIcon(_svg_icon("settings-grey.svg", 36))
        self._tb_link.setIcon(_svg_icon("link.svg", 36))
        self._tb_save.setIcon(_svg_icon("loaded.svg", 36))
        self._tb_leave.setIcon(_svg_icon("cloud.svg", 36))
        # Обновить состояние кнопок
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
        self._update_selection_counter()

    def _on_table_selection_changed(self, selected, deselected):
        """При изменении выделения в таблице → снять выделение в дереве."""
        if self._selection_updating:
            return
        self._selection_updating = True
        self.tree_view.clearSelection()
        self._selection_updating = False
        self._update_toolbar_buttons()
        self._update_selection_counter()

    def _update_selection_counter(self):
        """Счётчик «Выбрано: N» в статус-баре — виден при множественном выделении.

        Вызывается при каждом изменении выделения (таблица + дерево).
        """
        n = len(self.table_view.selectionModel().selectedRows(0))
        n += len(self.tree_view.selectionModel().selectedRows(0))
        if n >= 2:
            self._status_selection_label.setText(
                f"Выбрано: {n} "
                f"{self._plural_ru(n, 'элемент', 'элемента', 'элементов')}")
            self._status_selection_label.show()
        else:
            self._status_selection_label.hide()

    @staticmethod
    def _plural_ru(n: int, one: str, few: str, many: str) -> str:
        """Русское склонение: 1 элемент, 2 элемента, 5 элементов."""
        n10, n100 = n % 10, n % 100
        if n10 == 1 and n100 != 11:
            return one
        if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
            return few
        return many

    def _update_toolbar_buttons(self):
        """Включить/выключить кнопки в зависимости от выделения."""
        table_rows = self.table_view.selectionModel().selectedRows(0)
        tree_rows = self.tree_view.selectionModel().selectedRows(0)
        has_table_sel = bool(table_rows)
        has_tree_sel = bool(tree_rows)

        # Публичная ссылка: скрыта если нет выделения
        show_link = has_table_sel or has_tree_sel
        self._tb_link.setVisible(show_link)

        # Определяем статусы всех выделенных элементов
        all_downloaded = False
        has_valid_sel = False
        if has_table_sel or has_tree_sel:
            statuses = self._collect_selection_statuses(table_rows, tree_rows)
            if statuses:
                has_valid_sel = True
                all_downloaded = all(s == "downloaded" for s in statuses)

        # Две отдельные кнопки: показываем только одну, вторую прячем
        self._tb_save.setVisible(has_valid_sel and not all_downloaded)
        self._tb_leave.setVisible(has_valid_sel and all_downloaded)

        # Меню: обе видимы, активна только релевантная
        self._act_save.setEnabled(has_valid_sel and not all_downloaded)
        self._act_leave.setEnabled(has_valid_sel and all_downloaded)

        # Разделитель перед контекстными кнопками
        self._sep_after_settings.setVisible(
            show_link or self._tb_save.isVisible() or self._tb_leave.isVisible())

        # Кнопки навигации
        self._update_nav_buttons()

    def _collect_selection_statuses(self, table_rows, tree_rows) -> set[str]:
        """Собрать статусы всех выделенных элементов (файлов и папок).

        Для папок определяем реальный статус через БД (downloaded / cloud_only).
        Папки батчатся в один get_folder_batch_aggregate_status запрос.
        Элемент \"..\" игнорируется.
        """
        statuses: set[str] = set()
        folder_paths: list[str] = []

        for idx in table_rows:
            item = self._get_item(idx)
            if not item:
                continue
            if item.get("is_parent_nav"):
                continue
            if not item.get("is_dir"):
                statuses.add(item["status"])
            else:
                folder_paths.append(item["cloud_path"])

        if tree_rows:
            for idx in tree_rows:
                cp = idx.data(Qt.UserRole)
                if cp and cp != "/":
                    folder_paths.append(cp)

        if folder_paths:
            batch = self._db.get_folder_batch_aggregate_status("/", folder_paths)
            for fp in folder_paths:
                agg = batch.get(fp, "cloud_only")
                statuses.add("downloaded" if agg == "downloaded" else "cloud_only")

        return statuses

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
        act_settings.setIcon(_svg_icon("settings-grey.svg", 24))
        act_settings.triggered.connect(self._show_settings)
        self._tray_menu.addAction(act_settings)

        self._tray_menu.addSeparator()

        act_check_updates = QAction("Проверить обновления…", self)
        act_check_updates.triggered.connect(self._check_updates_manual)
        self._tray_menu.addAction(act_check_updates)

        self._tray_menu.addSeparator()

        act_exit = QAction("Выход", self)
        act_exit.setIcon(_svg_icon("close-red.svg", 24))
        act_exit.triggered.connect(self._tray_exit)
        self._tray_menu.addAction(act_exit)

        self._tray.setContextMenu(self._tray_menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.messageClicked.connect(self._on_tray_message_clicked)
        self._tray.show()

    # ── Иконки окна и трея ────────────────────────────

    _dark_mode_cache = None  # кеш на сеанс: реестр не меняется, а winreg.OpenKey
                             # блокирует UI-поток (~0.5 с) при каждом вызове

    @staticmethod
    def _is_windows_dark_mode() -> bool:
        """Тёмная тема Windows (реестр). Кешируется на сеанс."""
        if MainWindow._dark_mode_cache is not None:
            return MainWindow._dark_mode_cache
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            MainWindow._dark_mode_cache = (val == 0)
        except Exception:
            MainWindow._dark_mode_cache = False
        return MainWindow._dark_mode_cache

    @classmethod
    def _pick_window_icon(cls) -> QIcon:
        """Иконка для заголовка окна и панели задач — icon-main.ico (всегда одна)."""
        path = os.path.join(os.path.dirname(__file__), "icon-main.ico")
        if os.path.isfile(path):
            return QIcon(path)
        return QIcon()

    @classmethod
    def _pick_tray_icon(cls, force_offline: bool = False) -> QIcon:
        """Иконка для системного трея — 16px с учётом темы (светлая/тёмная).
        Если force_offline=True — возвращает полупрозрачную версию иконки."""
        ico = "icon-light-16.ico" if cls._is_windows_dark_mode() else "icon-16.ico"
        path = os.path.join(os.path.dirname(__file__), "Assets", ico)
        base_path = path if os.path.isfile(path) else None
        
        # fallback на icon-main.ico если 16px нет
        if base_path is None:
            fallback = os.path.join(os.path.dirname(__file__), "icon-main.ico")
            if os.path.isfile(fallback):
                base_path = fallback
        
        if base_path is None:
            return QIcon()
        
        if not force_offline:
            return QIcon(base_path)
        
        # Создаём полупрозрачную версию иконки
        pixmap = QPixmap(base_path)
        if pixmap.isNull():
            return QIcon(base_path)
        
        # Конвертируем в QImage для манипуляции с альфа-каналом
        image = pixmap.toImage().convertToFormat(QImage.Format_ARGB32)
        
        # Делаем изображение полупрозрачным (альфа-канал ~50%)
        for y in range(image.height()):
            for x in range(image.width()):
                pixel = image.pixel(x, y)
                a = qRgba(pixel >> 24 & 0xFF, pixel >> 16 & 0xFF, pixel >> 8 & 0xFF, pixel & 0xFF)
                alpha = (pixel & 0xFF)  # текущий альфа-канал
                new_alpha = int(alpha * 0.5)  # уменьшаем до 50%
                image.setPixel(x, y, qRgba(pixel >> 24 & 0xFF, pixel >> 16 & 0xFF, pixel >> 8 & 0xFF, new_alpha))
        
        return QIcon(QPixmap.fromImage(image))

    def _update_app_icons(self, update_tray_only: bool = False):
        if not update_tray_only:
            self.setWindowIcon(self._pick_window_icon())
        if hasattr(self, "_tray") and self._tray:
            self._tray.setIcon(self._pick_tray_icon(force_offline=not self._online))
        # Обновляем иконки в меню трея (после смены темы)
        # Обновление выполняется только если не update_tray_only=False (т.е. при полной смене темы),
        # чтобы избежать лишних обновлений при проверке подключения
        if not update_tray_only:
            if hasattr(self, "_tray_act_show"):
                self._tray_act_show.setIcon(_svg_icon("home-svgrepo-com.svg", 24))
            # Находим действие "Настройки" в меню и обновляем его иконку
            if hasattr(self, "_tray_menu"):
                for action in self._tray_menu.actions():
                    if action.text() == "Настройки":
                        action.setIcon(_svg_icon("settings-grey.svg", 24))
                        break

    def _check_connectivity(self):
        """Проверка интернет-соединения через запрос к yandex.ru.
        При изменении статуса обновляет иконку в трее."""
        import socket
        
        old_online = self._online
        try:
            # Пробуем подключиться к yandex.ru:443 (HTTPS)
            sock = socket.create_connection(("yandex.ru", 443), timeout=5)
            sock.close()
            self._online = True
        except (socket.timeout, socket.gaierror, OSError, Exception):
            self._online = False
        
        # Если статус изменился — обновляем иконку
        if old_online != self._online:
            logger.info(f"Статус подключения изменился: {'онлайн' if self._online else 'офлайн'}")
            self._update_app_icons(update_tray_only=True)

    def _sync_log_zorder(self):
        """Спутник окна лога: при активации Менеджера (Alt+Tab, значок на
        панели задач, клик по окну) лог поднимается на передний план сразу
        за Менеджером. При потере фокуса лог НЕ опускается и НЕ прячется:
        переключение в другое окно не должно делать лог невидимым (раньше
        lw.lower() уводил его на дно z-order, под окно Менеджера); активное
        чужое окно накроет лог само, если окна перекрываются. Лог прячется
        только вместе со сворачиванием Менеджера (_on_manager_state_change).
        Клик по логу поднимает его над Менеджером — «всегда поверх» нет."""
        lw = getattr(self, '_log_window', None)
        if lw is None or not lw.isVisible() or self.isMinimized():
            return
        if self.isActiveWindow():
            lw.raise_()
            self.raise_()
            # Подстраховка: raise() меняет z-order без смены фокуса, но если
            # Windows всё же отдал фокус логу — вернуть его Менеджеру.
            if QApplication.activeWindow() is lw:
                QTimer.singleShot(0, self.activateWindow)

    def _on_manager_state_change(self):
        """При сворачивании Менеджера прятать окно лога (у него нет значка
        на панели задач — вернуть его можно только вместе с Менеджером)."""
        lw = getattr(self, '_log_window', None)
        if lw is None:
            return
        if self.isMinimized():
            if lw.isVisible():
                self._log_visible_before_minimize = True
                lw.hide()
        else:
            if getattr(self, '_log_visible_before_minimize', False):
                self._log_visible_before_minimize = False
                lw.show()
                lw.raise_()
                self.raise_()

    def _sync_log_zorder(self):
        """Спутник окна лога: при активации Менеджера (Alt+Tab, значок на
        панели задач, клик по окну) лог поднимается на передний план сразу
        за Менеджером. При потере фокуса лог НЕ опускается и НЕ прячется:
        переключение в другое окно не должно делать лог невидимым (раньше
        lw.lower() уводил его на дно z-order, под окно Менеджера); активное
        чужое окно накроет лог само, если окна перекрываются. Лог прячется
        только вместе со сворачиванием Менеджера (_on_manager_state_change).
        Клик по логу поднимает его над Менеджером — «всегда поверх» нет."""
        lw = getattr(self, '_log_window', None)
        if lw is None or not lw.isVisible() or self.isMinimized():
            return
        if self.isActiveWindow():
            lw.raise_()
            self.raise_()
            # Подстраховка: raise() меняет z-order без смены фокуса, но если
            # Windows всё же отдал фокус логу — вернуть его Менеджеру.
            if QApplication.activeWindow() is lw:
                QTimer.singleShot(0, self.activateWindow)

    def _on_manager_state_change(self):
        """При сворачивании Менеджера прятать окно лога (у него нет значка
        на панели задач — вернуть его можно только вместе с Менеджером)."""
        lw = getattr(self, '_log_window', None)
        if lw is None:
            return
        if self.isMinimized():
            if lw.isVisible():
                self._log_visible_before_minimize = True
                lw.hide()
        else:
            if getattr(self, '_log_visible_before_minimize', False):
                self._log_visible_before_minimize = False
                lw.show()
                lw.raise_()
                self.raise_()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.ActivationChange:
            self._sync_log_zorder()
        elif event.type() == QEvent.Type.WindowStateChange:
            self._on_manager_state_change()
        elif event.type() == QEvent.Type.PaletteChange:
            # НЕ вызываем _update_app_icons() на каждый PaletteChange:
            # Qt шлёт PaletteChange при открытии/закрытии КАЖДОГО модального
            # диалога, и пересоздание иконок окна/трея в обработчике события
            # (setWindowIcon + QSystemTrayIcon.setIcon → новые QIcon/QPixmap)
            # накапливает аллокации в Qt-куче и при повторных применениях
            # темы повреждает её (STATUS_HEAP_CORRUPTION, 0xc0000374).
            # Иконки обновляются только при реальной смене темы
            # (в _apply_theme_impl) — этого достаточно.
            dark_now = self._is_windows_dark_mode()
            if dark_now != getattr(self, '_last_system_dark', None):
                self._last_system_dark = dark_now
                self._update_app_icons()
                QTimer.singleShot(0, self._apply_theme)
        super().changeEvent(event)

    # ── Обновления ─────────────────────────────────────────

    def _init_updates(self):
        """Авто-проверка обновлений: отложенный старт + периодический таймер."""
        self._update_thread = None
        self._update_dialog = None
        self._pending_update = None   # найденное обновление (UpdateInfo)
        self._update_offered = False  # в этой сессии уже предлагали
        self._pending_update_path = None  # скачанный exe для бесшумного обновления
        # Первая проверка — через 20 сек (не мешает стартовой загрузке)
        QTimer.singleShot(updater.UPDATE_CHECK_DELAY_MS, self._check_updates_auto)
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._check_updates_auto)
        self._update_timer.start(updater.UPDATE_CHECK_INTERVAL_MS)

    def _check_updates_auto(self):
        """Тихая авто-проверка: уведомление в трее, если есть обновление.

        Если включён «бесшумный» режим (auto_update_enabled) — обновление
        скачивается в фоне без диалога и применяется при перезапуске.
        """
        if not db.get_check_updates_enabled() and not db.get_auto_update_enabled():
            return
        if self._update_thread is not None or self._update_dialog is not None:
            return  # уже идёт проверка/диалог
        self._start_update_check(auto=True)

    def _check_updates_manual(self):
        """Ручная проверка из меню трея: диалог при наличии обновления."""
        if self._update_thread is not None:
            return
        self._start_update_check(auto=False)

    def _start_update_check(self, auto: bool):
        if not updater.UPDATE_REPO:
            if not auto:
                self._tray_show()
                QMessageBox.information(
                    self, "Проверка обновлений",
                    "Проверка обновлений ещё не настроена\n"
                    "(UPDATE_REPO в updater.py не заполнен).")
            return
        self._update_thread = updater.UpdateCheckThread(
            updater.UPDATE_REPO, VERSION,
            include_prerelease=db.get_update_beta_enabled(), parent=self)
        self._update_thread.finished.connect(
            lambda info, err, _a=auto: self._on_update_checked(info, err, _a))
        self._update_thread.finished.connect(self._update_thread.deleteLater)
        self._update_thread.start()

    def _on_update_checked(self, info, error, auto: bool):
        self._update_thread = None
        if error:
            logger.warning("Update check error: %s", error)
            return
        if info is None:
            if not auto:
                self._tray_show()
                self._tray.showMessage(
                    "Обновлений нет",
                    "Установлена последняя версия.",
                    QSystemTrayIcon.Information, 5000)
            return
        self._pending_update = info
        # Бесшумный режим (ЯД 3.0): сразу качаем, без диалога и уведомлений
        if db.get_auto_update_enabled():
            if self._pending_update_path is not None:
                return  # уже скачано в этой сессии
            self._auto_install_update(info)
            return
        if not auto:
            self._show_update_dialog()
            return
        # Авто: ненавязчивое уведомление в трее — один раз за сессию
        if self._update_offered:
            return
        self._update_offered = True
        self._tray.showMessage(
            "Доступно обновление",
            f"YaDisk Manager {info.version}. Нажмите, чтобы обновить "
            f"(займёт меньше минуты).",
            QSystemTrayIcon.Information, 15000)
        logger.info("Update available: %s", info.version)

    def _auto_install_update(self, info):
        """Бесшумное обновление (ЯД 3.0): скачивание в фоне без диалога.

        Готовый exe применяется при следующем выходе/перезапуске
        (_maybe_apply_pending_update), чтобы не прерывать работу.
        """
        logger.info("Auto-update: downloading %s in background", info.version)
        try:
            self._tray.showMessage(
                "Обновление",
                f"Скачивание обновления {info.version}…",
                QSystemTrayIcon.Information, 5000)
        except Exception:
            pass  # трей недоступен — не критично
        self._update_download = updater.UpdateDownloadThread(info, parent=self)
        self._update_download.finished.connect(self._on_auto_update_downloaded)
        self._update_download.finished.connect(
            self._update_download.deleteLater)
        self._update_download.start()

    def _on_auto_update_downloaded(self, path, error):
        self._update_download = None
        if error or not path:
            logger.warning("Auto-update download failed: %s", error)
            return
        self._pending_update_path = path
        try:
            self._tray.showMessage(
                "Обновление готово",
                "Будет применено при следующем перезапуске.",
                QSystemTrayIcon.Information, 8000)
        except Exception:
            pass
        logger.info("Auto-update: downloaded %s", path)

    def _maybe_apply_pending_update(self):
        """Применить скачанное обновление перед выходом (бесшумный режим)."""
        if not self._pending_update_path:
            return
        path, self._pending_update_path = self._pending_update_path, None
        if not updater.apply_update(path):
            logger.warning("Auto-update: apply skipped")
        else:
            logger.info("Auto-update: apply_update launched")

    def _on_tray_message_clicked(self):
        """Клик по уведомлению в трее → окно обновления."""
        if self._pending_update is not None:
            self._show_update_dialog()

    def _show_update_dialog(self):
        if self._update_dialog is not None:
            return
        self._tray_show()
        dlg = UpdateDialog(self._pending_update, parent=self)
        self._update_dialog = dlg
        dlg.finished.connect(self._on_update_dialog_closed)
        dlg.show()

    def _on_update_dialog_closed(self, _result: int):
        self._update_dialog = None
        # Если пользователь обновился — приложение закрыто (QApplication.quit);
        # здесь только чистим ссылку. Если нажал «Позже» — уведомление не
        # повторяется в этой сессии (_update_offered уже True).

    # ── Иконки окна и трея ────────────────────────────

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
        self._show_status_spinner("Закрытие программы...")
        self._maybe_apply_pending_update()  # бесшумное обновление
        self._force_close = True
        self._tray.hide()
        QApplication.processEvents()
        self._tray.deleteLater()
        self.close()

    def _menu_quit(self, show_spinner_text: bool = True):
        """Полностью закрыть программу (Выход).

        show_spinner_text=False — надпись уже показана вызывающим
        (перезагрузка: «Перезагрузка программы...»), не перезаписывать.
        """
        if getattr(self, "_quitting", False):
            return
        self._quitting = True
        if show_spinner_text:
            self._show_status_spinner("Закрытие программы...")
        self._maybe_apply_pending_update()  # бесшумное обновление
        self._force_close = True
        try:
            if self._tray:
                self._tray.hide()
        except RuntimeError:
            pass
        QApplication.processEvents()
        try:
            if self._tray:
                self._tray.deleteLater()
        except RuntimeError:
            pass
        self.close()

    # ── Log window toggle ────────────────────────────────

    def _toggle_log_window(self):
        """Показать/скрыть окно лога."""
        visible = not self._log_window.isVisible()
        self._log_window.setVisible(visible)
        if visible:
            self._log_window.raise_()
            self._log_window.activateWindow()

    def _update_log_label_style(self):
        """Обновить стиль ссылки «Показать лог» в статус-баре (адаптируется к теме)."""
        visible = self._log_window.isVisible()
        font = self._log_label.font()
        font.setBold(visible)
        self._log_label.setFont(font)
        pal = QApplication.instance().palette()
        text_color = pal.color(QPalette.WindowText).name()
        muted = pal.color(QPalette.Disabled, QPalette.Text).name()
        color = muted  # всегда muted, не зависит от видимости окна лога
        self._log_label.setStyleSheet(
            f"QLabel {{ color: {color}; padding: 0 4px; }} "
            f"QLabel:hover {{ color: {text_color}; text-decoration: underline; }}"
        )

    # ── Restart ──────────────────────────────────────────

    def _restart_app(self):
        """Перезапустить программу без подтверждения."""
        logger.info("Restarting application...")
        self._show_status_spinner("Перезагрузка программы...")
        # Сохраняем положения окон
        if db.get_save_window_geometry():
            db.set_window_geometry(
                self.saveGeometry().toBase64().data().decode())
            self._log_window.save_position()
        # Запускаем новый процесс
        # Флаг --restart: новый экземпляр должен ПОДОЖДАТЬ, пока текущий
        # закроется (single-instance guard иначе сочтёт его дубликатом и
        # выйдет — Менеджер просто закроется без нового окна).
        script = os.path.join(os.path.dirname(__file__), "main.py")
        subprocess.Popen([sys.executable, script, "--restart"])
        # Закрываем текущий (надпись «Перезагрузка программы...» сохраняется)
        self._force_close = True
        self._menu_quit(show_spinner_text=False)

    def _logout_and_reauth(self):
        """Выйти из аккаунта: очистить токен и показать диалог авторизации."""
        msg = QMessageBox(self)
        msg.setWindowModality(Qt.WindowModal)
        msg.setWindowTitle("Выйти из аккаунта")
        msg.setIcon(QMessageBox.Question)
        msg.setText(
            "Вы уверены, что хотите выйти из аккаунта?\n"
            "Токен будет удалён, потребуется повторная авторизация."
        )
        btn_yes = msg.addButton("Да", QMessageBox.YesRole)
        btn_no = msg.addButton("Нет", QMessageBox.NoRole)
        msg.setDefaultButton(btn_no)
        msg.exec()
        if msg.clickedButton() != btn_yes:
            return
        # Очищаем старый токен
        db.set_token("")
        # Обнуляем API-клиент
        import disk_api
        token = db.get_token()
        if not token:
            # Показываем диалог авторизации
            auth = AuthDialog(self)
            auth.setWindowModality(Qt.WindowModal)
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
        self._show_left_busy("Проверка локальных файлов...")

        # Добавляем ref на поток, чтобы не собрался GC
        thread = _StartupScanThread(self._api, self._db, cache_dir, self)
        thread.finished.connect(self._on_startup_scan_done)
        thread.finished.connect(thread.deleteLater)
        self._startup_scan_thread = thread
        thread.start()

    def _on_startup_scan_done(self):
        """Обработка результатов стартового сканирования (главный поток)."""
        # Вся обработка уже выполнена в фоне, нужно только обновить UI и запустить загрузку изменённых файлов
        logger.info("Startup scan: done, refreshing UI...")
        
        # Обновляем текущий вид из БД, чтобы подхватить изменённые статусы
        QTimer.singleShot(100, lambda: self._load_folder_local(self._current_path))
        
        # Запускаем загрузку изменённых файлов (если есть)
        QTimer.singleShot(2000, self._flush_pending_upload)
        
        # Проверка облака: новые файлы в полностью скачанных папках
        self._show_left_busy("Проверка облака...", timeout=8000)
        QTimer.singleShot(100, self._poll_cloud)

        # Вотчер запускаем только после завершения стартового сканирования,
        # чтобы не поймать ложные файловые события во время проверки кеша
        self._init_watcher()

        # Возобновляем незавершённые операции с прошлого запуска
        self._retry_pending_ops()


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
        msg.setWindowModality(Qt.WindowModal)
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

    # ── Drag-and-drop ────────────────────────────────

    def _on_drop_complete(self, results: list[tuple[str, str, str]]):
        """Обработка результатов DropUploadThread: регистрация файлов в БД."""
        if not results:
            self._hide_left_busy()
            self.statusBar().showMessage("❌ Ничего не перетащено", 3000)
            return
        count = len(results)
        logger.info("Drop: %d file(s) copied to cache", count)
        for cloud_path, local_path, local_md5 in results:
            try:
                self._register_new_file(local_path, cloud_path)
            except Exception as e:
                logger.warning("Drop: failed to register %s — %s", cloud_path, e)
        self._navigate_to_folder(self._current_path)
        self._hide_left_busy()
        self.statusBar().showMessage(
            f"✅ Перетащено: {count} файл(ов) в {self._current_path}", 5000)
        QTimer.singleShot(2000, self._flush_pending_upload)

    def _handle_internal_drop(self, paths: list[str], dest: str, is_move: bool):
        """Обработка внутреннего DnD: копирование/перемещение облачных путей."""
        if not paths or not dest:
            return
        action_name = "перемещение" if is_move else "копирование"
        logger.info("Internal DnD: %s %d items to %s", action_name, len(paths), dest)

        # Собираем пары (src, dest)
        items: list[tuple[str, str]] = []
        for src in paths:
            name = os.path.basename(src.rstrip("/"))
            new_path = (dest.rstrip("/") + "/" + name).replace("//", "/")
            if new_path == src:
                continue
            items.append((src, new_path))
        if not items:
            self._hide_left_busy()
            return

        # Немедленно показываем "syncing" для каждого назначения
        for src, new_path in items:
            name = os.path.basename(src.rstrip("/"))
            # Создаём заглушку в БД со статусом syncing
            self._db.upsert_file(
                new_path, name, "file",
                size=0, modified="", md5="")
            self._db.set_status(new_path, "syncing")
        self.table_model.refresh_statuses()
        self.tree_model.emit_path_changed(dest)
        self._show_left_busy(f"📦 {action_name} {len(items)} элемент(ов)...")

        logger.info("📦 %s %d элемент(ов) → %s",
                     action_name, len(items), dest)
        op_id = self._db.save_pending_op(
            "move" if is_move else "copy", dest,
            ",".join(f"{s}→{d}" for s, d in items))
        self._internal_drop_op_id = op_id

        # Запускаем фоновый поток
        thread = _InternalDropThread(self._api, self._db, items, is_move, self)
        thread.finished.connect(self._on_internal_drop_finished)
        thread.finished.connect(thread.deleteLater)
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()

    def _on_internal_drop_finished(self, result: dict):
        """Обработка завершения фонового DnD."""
        success = result.get("success", 0)
        errors = result.get("errors", [])
        processed = result.get("processed", [])
        is_move = result.get("is_move", False)
        action_name = "перемещение" if is_move else "копирование"

        logger.info("📦 %s завершено: %d успешно, %d ошибок",
                     action_name, success, len(errors))

        # Завершаем pending-операцию (создана в _handle_internal_drop, строка 1814)
        op_id = getattr(self, '_internal_drop_op_id', None)
        if op_id is not None:
            try:
                self._db.complete_pending_op(op_id, "done" if not errors else "failed")
            except Exception as e:
                logger.warning("DnD: complete_pending_op failed: %s", e)
            self._internal_drop_op_id = None

        # Мигрируем БД для перемещённых элементов
        for src_p, new_p in processed:
            if is_move:
                try:
                    self._db.migrate_path(src_p, new_p)
                except Exception as e:
                    logger.warning("DnD: migrate_path %s → %s failed: %s", src_p, new_p, e)

        if errors:
            self._hide_left_busy()
            self.statusBar().showMessage(
                f"⚠️ {action_name}: {success} успешно, {len(errors)} ошибок", 5000)
            for err in errors[:3]:
                logger.warning("DnD error: %s", err)
        else:
            self._hide_left_busy()
            self.statusBar().showMessage(
                f"✅ {action_name}: {len(processed)} элемент(ов)", 5000)

        # Обновляем отображение из БД
        QTimer.singleShot(300, lambda: self._navigate_to_folder(self._current_path))

    def _finish_drag(self, drag, mime_paths: list[str]):
        """Общий хвост QDrag: exec, затем для RMB — меню «Копировать/Переместить».

        Пока drag живёт, eventFilter принимает Drop: для LMB — сразу обрабатывает
        (Shift = перемещение), для RMB — откладывает в _pending_internal_drop,
        а выбор действия делает пользователь в этом меню.
        """
        btn = getattr(self, '_pending_drag_button', Qt.LeftButton)
        self._drag_mouse_button = btn
        try:
            action = drag.exec(Qt.CopyAction | Qt.MoveAction)
        finally:
            self._drag_mouse_button = None
        if btn != Qt.RightButton or action == Qt.IgnoreAction:
            return
        # RMB-drag: после отпускания — меню выбора действия
        pending = getattr(self, '_pending_internal_drop', None)
        self._pending_internal_drop = None
        if not pending:
            return
        paths, target = pending
        menu = QMenu(self)
        act_copy = menu.addAction("Копировать")
        act_copy.setIcon(_svg_icon("Copy.svg", 24))
        act_move = menu.addAction("Переместить")
        act_move.setIcon(_svg_icon("scissors-3.svg", 24))
        chosen = menu.exec(QCursor.pos())
        if chosen is None:
            # Отмена меню (Esc / клик мимо) — операция не запускается
            return
        is_move = (chosen == act_move)
        self._handle_internal_drop(paths, target, is_move)

    def _start_drag_from_tree(self):
        """Инициировать QDrag из дерева папок."""
        sel = self.tree_view.selectionModel()
        if not sel:
            return
        indexes = sel.selectedRows(0)
        if not indexes:
            return
        mime = self.tree_model.mimeData(indexes)
        if not mime:
            return
        drag = QDrag(self)
        drag.setMimeData(mime)
        # Пиктограмма: текст с количеством элементов
        cnt = len(indexes)
        pix = QPixmap(120, 24)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setPen(QColor(255, 255, 255))
        p.setBrush(QColor(80, 80, 100, 200))
        p.drawRoundedRect(0, 0, 120, 24, 6, 6)
        p.setPen(Qt.white)
        p.drawText(pix.rect(), Qt.AlignCenter, f"📄 {cnt} элемент(ов)")
        p.end()
        drag.setPixmap(pix)
        drag.setHotSpot(QPoint(60, 12))
        self._finish_drag(drag, [])

    def _start_drag_from_table(self):
        """Инициировать QDrag из таблицы файлов."""
        sel = self.table_view.selectionModel()
        if not sel:
            return
        indexes = sel.selectedRows(0)
        if not indexes:
            return
        # Индексы от прокси — mimeData у прокси переводит их в source
        mime = self.table_sort_model.mimeData(indexes)
        if not mime:
            return
        drag = QDrag(self)
        drag.setMimeData(mime)
        cnt = len(indexes)
        pix = QPixmap(120, 24)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setPen(QColor(255, 255, 255))
        p.setBrush(QColor(80, 80, 100, 200))
        p.drawRoundedRect(0, 0, 120, 24, 6, 6)
        p.setPen(Qt.white)
        p.drawText(pix.rect(), Qt.AlignCenter, f"📄 {cnt} элемент(ов)")
        p.end()
        drag.setPixmap(pix)
        drag.setHotSpot(QPoint(60, 12))
        self._finish_drag(drag, [])

    # ── Watcher ───────────────────────────────────────────


    def _retry_pending_ops(self):
        """Возобновить незавершённые операции с прошлого запуска."""
        pending = self._db.get_pending_ops()
        if not pending:
            return
        logger.info("🔄 Found %d pending operations from last session", len(pending))
        for op in pending:
            op_type = op["op_type"]
            src = op["src_path"]
            dst = op.get("dst_path", "")
            try:
                if op_type == "delete" and dst:
                    # dst contains comma-separated cloud_paths to delete
                    paths = [p.strip() for p in dst.split(",") if p.strip()]
                    for p in paths:
                        try:
                            self._api.delete(p)
                            self._db.remove_file(p)
                        except Exception:
                            pass
                    logger.info("🔄 Retry delete: %d paths", len(paths))
                elif op_type in ("copy", "move") and dst:
                    for pair in dst.split(","):
                        if "→" in pair:
                            s, d = pair.split("→", 1)
                            if op_type == "copy":
                                self._api.copy(s, d, overwrite=True)
                            else:
                                self._api.move(s, d, overwrite=True)
                            logger.info("🔄 Retry %s: %s → %s", op_type, s, d)
                elif op_type == "rename" and dst:
                    self._api.move(src, dst)
                    logger.info("🔄 Retry rename: %s → %s", src, dst)
                self._db.complete_pending_op(op["id"], "done")
            except Exception as e:
                logger.warning("🔄 Retry %s failed: %s", op_type, e)
                self._db.complete_pending_op(op["id"], "failed")

    def _init_watcher(self):
        self._watcher = watcher.FileWatcher(
            _cache_dir(), self._on_local_file_event)
        self._watcher.start()

    def _on_local_file_event(self, event_type: str, path: str,
                            is_directory: bool = False, dest: str | None = None):
        """Вызывается из watchdog — кладём в очередь для main thread."""
        self._watcher_queue.append((event_type, path, dest, is_directory))

    def _flush_watcher_queue(self):
        """Раз в 200ms выбираем накопившиеся события из watcher-очереди (main thread).

        На Windows cut+paste папки генерирует пары deleted/created для каждого
        файла вместо moved. Здесь объединяем такие пары: если файл удалён и
        создан с тем же размером/MD5 за <1с — трактуем как перемещение.
        """
        if not self._watcher_queue:
            return
        batch = list(self._watcher_queue)
        self._watcher_queue.clear()

        # Группируем deleted/created по basename для поиска пар
        by_name: dict[str, list] = {}
        for ev in batch:
            if ev[0] in ("deleted", "created"):
                name = os.path.basename(ev[1])
                by_name.setdefault(name, []).append(ev)

        # Ищем пары deleted+created с одинаковым размером/MD5 за <1с
        paired = set()
        moves = []
        for name, events in by_name.items():
            deleted = [e for e in events if e[0] == "deleted"]
            created = [e for e in events if e[0] == "created"]
            for d in deleted:
                for c in created:
                    # Проверяем время (в очереди порядок — по времени поступления)
                    # и размер/MD5 файла на диске
                    if self._files_match(d[1], c[1]):
                        moves.append(("moved", d[1], c[1], d[3]))
                        paired.add(id(d))
                        paired.add(id(c))
                        break

        # Обрабатываем найденные moved
        for ev in moves:
            try:
                self._handle_file_moved(ev[1], ev[2])  # src, dst
            except Exception as e:
                logger.warning("Watcher moved handler error: %s", e)

        # Остальные события — по порядку
        for i, (event_type, local_path, dest_path, is_directory) in enumerate(batch):
            if id(batch[i]) in paired:
                continue
            try:
                if event_type == "modified":
                    self._handle_file_modified(local_path)
                elif event_type == "created":
                    self._handle_file_created(local_path)
                elif event_type == "deleted":
                    self._handle_file_deleted(local_path, is_directory)
                elif event_type == "moved":
                    self._handle_file_moved(local_path, dest_path)
            except Exception as e:
                logger.warning("Watcher handler error: %s", e)

    def _files_match(self, path1: str, path2: str) -> bool:
        """Проверить, что два файла идентичны (размер + MD5)."""
        try:
            st1 = os.stat(path1)
            st2 = os.stat(path2)
            if st1.st_size != st2.st_size:
                return False
            # Быстрая проверка MD5
            return _md5_file(path1) == _md5_file(path2)
        except OSError:
            return False

    def _register_new_file(self, local_path: str, cloud_path: str):
        """Зарегистрировать новый локальный файл для загрузки в облако.

        Немедленно вносит файл в БД со статусом 'syncing' и отображает
        в интерфейсе — до завершения upload-а. Обновляет статус
        родительских папок.
        """
        if cloud_path in self._syncing:
            return  # уже обрабатывается

        try:
            st = os.stat(local_path)
            size = st.st_size
            modified = datetime.fromtimestamp(
                st.st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            size = 0
            modified = datetime.now(timezone.utc).isoformat()

        # Вносим в БД со статусом syncing
        self._db.set_syncing(cloud_path, local_path, size, modified)
        # НЕ добавляем в self._syncing — _start_upload сделает это,
        # когда реально начнёт загрузку. Иначе start_upload проверяет
        # self._syncing и отказывается запускать.
        self._syncing.discard(cloud_path)

        # Обновляем отображение
        parent = "/".join(cloud_path.rstrip("/").split("/")[:-1]) or "/"
        if hasattr(self, '_current_path') and parent == self._current_path:
            self._refresh_folder_view()
        self._update_tree_status(cloud_path)

        # Ставим в очередь загрузки
        self._pend_upload.add(cloud_path)

    def _handle_file_modified(self, local_path: str):
        """Локальный файл изменён — начать загрузку в облако."""
        rec = self._db.get_by_local_path(local_path)
        if not rec:
            # Неизвестный файл — возможно, только что создан и сразу изменён
            logger.info("Unknown local file modified, treating as created: %s", local_path)
            self._handle_file_created(local_path)
            return
        cloud_path = rec["cloud_path"]
        if cloud_path in self._syncing or cloud_path in self._recently_downloaded:
            return  # ложное срабатывание после скачивания
        # Немедленно показываем syncing
        self._db.set_status(cloud_path, "syncing")
        # НЕ добавляем в self._syncing — _start_upload сделает это
        self._syncing.discard(cloud_path)
        parent = "/".join(cloud_path.rstrip("/").split("/")[:-1]) or "/"
        if hasattr(self, '_current_path') and parent == self._current_path:
            self._refresh_folder_view()
        self._update_tree_status(cloud_path)
        self._pend_upload.add(cloud_path)
        QTimer.singleShot(2000, lambda: self._flush_pending_upload())

    def _handle_file_created(self, local_path: str):
        """Новый локальный файл — проверить, есть ли в облаке, если нет — загрузить."""
        rec = self._db.get_by_local_path(local_path)
        if rec:
            # Уже известен — значит это изменение, а не создание
            cloud_path = rec["cloud_path"]
            if cloud_path in self._syncing or cloud_path in self._recently_downloaded:
                return
            self._register_new_file(local_path, cloud_path)
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
        self._register_new_file(local_path, cloud_path)
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

        # ── лимит одновременных MetaFetch ────────────────
        if self._active_meta_fetches >= self._max_meta_concurrent:
            self._meta_fetch_queue.append((cloud_path, local_path, last_sync_md5))
            return

        self._do_meta_fetch(cloud_path, local_path, last_sync_md5)

    def _do_meta_fetch(self, cloud_path: str, local_path: str,
                       last_sync_md5: str):
        """Запустить MetaFetchThread (без проверки лимита)."""
        self._active_meta_fetches += 1
        name = Path(cloud_path).name
        self._show_progress_localized(f"Проверка {name}...")

        thread = _MetaFetchThread(self._api, cloud_path, local_path, self)
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
        self._active_meta_fetches = max(0, self._active_meta_fetches - 1)
        self._process_meta_fetch_queue()
        self._process_reverse_meta_queue()  # освободился слот — дренируем reverse очередь

        cloud_md5 = result.get("cloud_md5", "")
        cloud_modified = result.get("cloud_modified", "")
        cloud_size = result.get("cloud_size", 0)
        local_md5 = result.get("local_md5", "")

        # Конфликт: облако изменилось с момента синхронизации
        if cloud_md5 and last_sync_md5 and cloud_md5 != last_sync_md5:
            self._handle_conflict(cloud_path, local_path, local_md5,
                                  cloud_md5, cloud_size, cloud_modified)
            return

        # Файл уже синхронизирован с облаком — пропускаем загрузку
        if cloud_md5 and cloud_md5 == local_md5:
            logger.debug("File already in sync, skipping upload: %s", cloud_path)
            # Обновляем last_sync_md5 на случай, если он был пуст
            # (миграция со старых версий, где last_sync_md5 не записывался)
            self._db.update_last_sync_md5(cloud_path, cloud_md5)
            return

        # ── загружаем ────────────────────────────────────
        self._start_upload(cloud_path, local_path, local_md5)

    def _handle_conflict(self, cloud_path, local_path,
                         local_md5: str, cloud_md5: str,
                         cloud_size: int = 0, cloud_modified: str = ""):
        """Диалог конфликта — локальный и облачный файл изменились."""
        # Offscreen / headless (интеграционные тесты) — авто-отмена, не блокировать
        if QGuiApplication.platformName() == "offscreen":
            logger.warning(
                "Conflict auto-cancelled (offscreen): %s local=%s cloud=%s",
                cloud_path, local_md5[:8] if local_md5 else "-",
                cloud_md5[:8] if cloud_md5 else "-"
            )
            self._db.update_last_sync_md5(cloud_path, cloud_md5)
            return

        self._tray_show()  # показываем окно если свёрнуто

        try:
            st = os.stat(local_path)
            local_size = st.st_size
            local_mtime = datetime.fromtimestamp(st.st_mtime).strftime("%d.%m.%Y %H:%M")
        except OSError:
            local_size = 0
            local_mtime = "—"

        cloud_time_str = "—"
        if cloud_modified:
            try:
                dt = datetime.fromisoformat(cloud_modified.replace("Z", "+00:00"))
                cloud_time_str = dt.strftime("%d.%m.%Y %H:%M")
            except Exception:
                cloud_time_str = cloud_modified[:19].replace("T", " ")

        # Определяем, какая версия новее — через Unix timestamps
        # (избегаем проблем naive vs aware datetime)
        try:
            local_ts = os.stat(local_path).st_mtime  # всегда UTC-based
        except OSError:
            local_ts = 0

        cloud_ts = 0
        if cloud_modified:
            try:
                cd = datetime.fromisoformat(
                    cloud_modified.replace("Z", "+00:00"))
                if cd.tzinfo is None:
                    # Нет таймзоны — считаем что это UTC
                    cd = cd.replace(tzinfo=timezone.utc)
                cloud_ts = cd.timestamp()
            except Exception:
                pass

        local_size_str = _human_size(local_size)
        cloud_size_str = _human_size(cloud_size)

        if local_ts > cloud_ts:
            local_label = f"Локальная версия ({local_size_str}): {local_mtime} (новее)"
            cloud_label = f"Облачная версия ({cloud_size_str}): {cloud_time_str}"
        elif cloud_ts > local_ts:
            local_label = f"Локальная версия ({local_size_str}): {local_mtime}"
            cloud_label = f"Облачная версия ({cloud_size_str}): {cloud_time_str} (новее)"
        else:
            local_label = f"Локальная версия ({local_size_str}): {local_mtime}"
            cloud_label = f"Облачная версия ({cloud_size_str}): {cloud_time_str}"

        msg = QMessageBox(self)
        msg.setWindowModality(Qt.WindowModal)
        msg.setWindowTitle("Конфликт")
        msg.setIcon(QMessageBox.Question)
        msg.setText(
            f"Файл изменился одновременно в облаке и локально:\n\n"
            f"  {cloud_path}\n\n"
            f"{local_label}\n"
            f"{cloud_label}\n\n"
            f"Что делать?\n\n"
            f"  • Да — оставить локальную версию (загрузить в облако)\n"
            f"  • Нет — скачать облачную версию (заменить локальную)\n"
            f"  • Отмена — ничего не делать"
        )
        btn_yes = msg.addButton("Да", QMessageBox.YesRole)
        btn_no = msg.addButton("Нет", QMessageBox.NoRole)
        btn_cancel = msg.addButton("Отмена", QMessageBox.RejectRole)
        msg.setDefaultButton(btn_cancel)

        msg.exec()

        if msg.clickedButton() == btn_yes:
            # Сохраняем локальную, перезаписываем облачную
            # Немедленно обновляем last_sync_md5, чтобы повторный MetaFetch
            # не показал тот же конфликт ещё раз
            if local_md5:
                self._db.update_last_sync_md5(cloud_path, local_md5)
            self._start_upload(cloud_path, local_path, local_md5)
        elif msg.clickedButton() == btn_no:
            # Скачиваем облачную, перезаписываем локальную
            # Немедленно обновляем last_sync_md5 = cloud_md5, чтобы
            # любые последующие MetaFetch видели файл синхронизированным
            self._db.update_last_sync_md5(cloud_path, cloud_md5)
            self._start_download(cloud_path, local_path)
        # Отмена — ничего не делаем

    def _start_upload(self, cloud_path: str, local_path: str,
                      local_md5: str = ""):
        """Запустить upload в фоновом потоке (с учётом лимита конкуренции)."""
        if not local_md5:
            logger.debug("_start_upload: no local_md5 provided, computing in main thread — this may freeze UI")
            local_md5 = _md5_file(local_path)

        # Защита от повторного запуска для того же файла
        if cloud_path in self._syncing:
            logger.warning("start_upload: already syncing %s", cloud_path)
            return

        self._syncing.add(cloud_path)
        # Сразу показываем статус 🔄 в таблице
        self.table_model.update_status(cloud_path, "syncing")
        # Показываем статус 🔄 в дереве
        self._update_tree_status(cloud_path)

        # ── concurrency limit ───────────────────────────────────
        if self._active_uploads >= self._max_upload_concurrent:
            self._upload_queue.append((cloud_path, local_path, local_md5))
            logger.debug("_start_upload: queued %s (active=%d/%d)",
                         cloud_path, self._active_uploads, self._max_upload_concurrent)
            return

        self._active_uploads += 1
        self._uploads_refreshed = False
        self._do_upload(cloud_path, local_path, local_md5)

    def _do_upload(self, cloud_path: str, local_path: str,
                   local_md5: str):
        """Реально запустить upload-воркер (без проверки лимита)."""
        worker = UploadWorker(self._api, local_path, cloud_path)

        self._show_progress_localized(f"Загружаю {Path(cloud_path).name}")

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
            self._download_results.put((cp, local_path, local_md5, "upload", False))

        def on_error(msg):
            logger.warning("Worker error: upload %s — %s", cloud_path, msg)
            self._download_results.put((cloud_path, "", "", "upload", True))

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
            self._download_results.put((cloud_path, lp, md5, "download", False))

        def on_error(msg):
            logger.warning("Worker error: download %s — %s", cloud_path, msg)
            self._download_results.put((cloud_path, "", "", "download", True))

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
        batch: list[tuple] = []
        while True:
            try:
                batch.append(self._download_results.get_nowait())
            except Empty:
                break
        if not batch:
            return
        for cloud_path, local_path, md5, action, is_error in batch:
            try:
                if is_error:
                    self._syncing.discard(cloud_path)
                    self._db.set_cloud_only(cloud_path)  # сброс статуса в БД
                    self.table_model.update_item_status(cloud_path, "cloud_only")
                    self._hide_progress()
                    self._schedule_toolbar_update()
                    logger.warning("Worker error: %s %s — reset to cloud_only", action, cloud_path)
                    if action == "upload":
                        self._active_uploads = max(0, self._active_uploads - 1)
                        self._process_upload_queue()
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
                    # Защита от ложного срабатывания watcher: не даём перезагрузить
                    # только что скачанный файл обратно в облако
                    self._recently_downloaded.add(cloud_path)
                    QTimer.singleShot(
                        5000,
                        lambda cp=cloud_path: self._recently_downloaded.discard(cp)
                    )
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
                    # Освобождаем слот и запускаем следующий из очереди
                    self._active_uploads = max(0, self._active_uploads - 1)
                    self._process_upload_queue()
                    # Если файл в текущей папке — обновляем вид
                    if hasattr(self, '_current_path') and self._current_path:
                        parent = "/".join(cloud_path.rstrip("/").split("/")[:-1]) or "/"
                        if parent == self._current_path:
                            self._refresh_folder_view()
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

        # После завершения всех загрузок или выгрузок — обновить статусы в таблице и дереве
        if (self._active_downloads == 0 and not self._download_queue) or \
           (self._active_uploads == 0 and not self._upload_queue):
            self.table_model.refresh_statuses()
            self.tree_model.invalidate_status(None)
            self.tree_model.layoutChanged.emit()
            # После последнего upload-а — перезагрузить текущую папку из API,
            # чтобы новые файлы появились в таблице (однократно)
            if self._active_uploads == 0 and not self._upload_queue \
               and not getattr(self, '_uploads_refreshed', False) \
               and hasattr(self, '_current_path') and self._current_path:
                self._uploads_refreshed = True
                self._navigate_to_folder(self._current_path)

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

    def _on_spin_tick(self):
        """Обновить угол вращения и перерисовать анимированные иконки.

        Без viewport().update() — только dataChanged на строках с syncing/unknown.
        Qt перерисовывает только ячейку иконки, ничего больше.
        """
        if not hasattr(self, 'tree_view') or not self.tree_view:
            return
        advance_rotation(-6)  # 6°/тик = 360°/с при 60fps, против часовой
        self.tree_model.refresh_animated_icons()
        if hasattr(self, 'table_model') and self.table_model:
            self.table_model.refresh_animated_icons()

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

    def _process_upload_queue(self):
        """Запустить следующую загрузку из очереди, если есть свободные слоты."""
        while self._upload_queue and self._active_uploads < self._max_upload_concurrent:
            cp, lp, md5 = self._upload_queue.pop(0)
            if cp in self._syncing:
                self._active_uploads += 1
                self._uploads_refreshed = False
                self._do_upload(cp, lp, md5)
            else:
                logger.debug("_process_upload_queue: skipping cancelled %s", cp)

    def _process_meta_fetch_queue(self):
        """Запустить отложенные MetaFetch-запросы (очередь, лимит max_meta_concurrent)."""
        while (self._meta_fetch_queue
               and self._active_meta_fetches < self._max_meta_concurrent):
            cp, lp, last_sync_md5 = self._meta_fetch_queue.pop(0)
            self._do_meta_fetch(cp, lp, last_sync_md5)

    def _show_progress_localized(self, text: str):
        self.statusBar().clearMessage()
        self._status_label.setText(text)
        self._status_progress.setVisible(True)
        self._status_progress.setRange(0, 0)

    def _hide_progress(self):
        if not self._syncing:
            self._status_progress.setVisible(False)
            self._status_label.setText("")
            # Спиннер долгой операции тоже прячем — прогресс-бар
            # забрал левую зону себе (см. _show_progress_localized).
            self._status_spinner.hide()

    # ── Индикатор загрузки (правая зона статус-бара) ─────

    def _show_loading(self, text: str):
        """Показать справа индикатор загрузки (брайль-спиннер + текст).

        Левая зона остаётся свободной для сообщений перезагрузки/
        выключения: «Загрузка файлов...» больше не пересекается со
        спиннером перезагрузки в левой части статус-бара.
        """
        self.statusBar().clearMessage()
        self._loading_spinner.show()
        self._loading_label.setText(text)
        self._loading_label.show()

    def _hide_loading(self):
        self._loading_spinner.hide()
        self._loading_label.hide()

    # ── Долгие операции (левая зона: спиннер + текст) ─────

    def _show_left_busy(self, text: str, timeout: int = 0):
        """Показать слева спиннер и текст долгой фоновой операции.

        Текст ставится в _status_label (рядом со спиннером), а не через
        showMessage — чтобы индикатор крутился прямо перед надписью.
        timeout > 0 — автоскрытие через N мс (для коротких проверок,
        у которых нет явного финального колбэка).
        """
        self.statusBar().clearMessage()
        self._busy_token = getattr(self, "_busy_token", 0) + 1
        token = self._busy_token
        self._status_spinner.show()
        self._status_label.setText(text)
        if timeout > 0:
            QTimer.singleShot(timeout, lambda t=token: self._hide_left_busy(t))

    def _hide_left_busy(self, token: int | None = None):
        """Скрыть спиннер/текст долгой операции (по завершении).

        token — защита от гонки: устаревший таймер автоскрытия не
        должен прятать индикатор новой операции.
        """
        if token is not None and token != getattr(self, "_busy_token", 0):
            return
        self._status_spinner.hide()
        self._status_label.setText("")

    def _show_status_spinner(self, text: str):
        """Показать в статус-баре волновой спиннер и текст.

        Используется при необратимых действиях (закрытие/перезагрузка),
        поэтому сразу прокручиваем события — надпись должна быть видна
        ещё до блокирующих операций (применение обновления и т.п.).
        """
        # Убрать «застрявшую» надпись загрузки и индикатор справа —
        # спиннер и текст перезагрузки/выключения остаются одни
        self.statusBar().clearMessage()
        self._hide_loading()
        self._status_spinner.show()
        self._status_label.setText(text)
        QApplication.processEvents()

    def _hide_status_spinner(self):
        self._status_spinner.hide()
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
        """Обработать сброшенные файлы/папки — загрузить их в текущую папку (асинхронно)."""
        urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
        if not urls:
            return
        event.acceptProposedAction()

        # ── Проверка коллизий имён (только верхний уровень, быстро) ──
        existing_names = []
        for url in urls:
            local_path = url.toLocalFile()
            name = os.path.basename(local_path)
            cloud_path = (self._current_path.rstrip("/") + "/" + name).replace("//", "/")
            if self._db.file_exists(cloud_path):
                existing_names.append(name)

        if existing_names:
            msg = QMessageBox(self)
            msg.setWindowModality(Qt.WindowModal)
            msg.setWindowTitle("Конфликт имён")
            msg.setIcon(QMessageBox.Question)
            msg.setText(
                f"Файл(ы) уже существуют в папке:\n"
                + "\n".join(f"  • {n}" for n in existing_names[:10])
                + ("\n  …" if len(existing_names) > 10 else "")
                + "\n\nПерезаписать?"
            )
            btn_yes = msg.addButton("Да", QMessageBox.YesRole)
            btn_no = msg.addButton("Нет", QMessageBox.NoRole)
            msg.setDefaultButton(btn_no)
            msg.exec()
            if msg.clickedButton() != btn_yes:
                self.statusBar().showMessage("❌ Загрузка отменена", 3000)
                return

        # ── Асинхронная загрузка ────────────────────────────
        paths = [url.toLocalFile() for url in urls]
        logger.info("Drop: %d file(s) into %s (async)", len(paths), self._current_path)
        self._show_left_busy(
            f"Загрузка {len(paths)} файла(ов) в {self._current_path}...")

        thread = _DropUploadThread(paths, self._current_path, self)
        thread.finished.connect(self._on_drop_upload_ready)
        thread.finished.connect(thread.deleteLater)
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()

    def _on_drop_upload_ready(self, results: list[tuple[str, str, str]]):
        """DropUploadThread завершил копирование — запустить загрузку в облако."""
        count = len(results)
        logger.info("Drop upload ready: %d file(s) copied, starting upload", count)
        if not results:
            self._hide_left_busy()
            self.statusBar().showMessage("❌ Нет файлов для загрузки", 3000)
            return
        for cloud_path, local_path, local_md5 in results:
            self._start_upload(cloud_path, local_path, local_md5)
        self._hide_left_busy()
        self.statusBar().showMessage(
            f"✅ Загрузка {count} файла(ов) запущена", 5000)

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
        Клавиатурная навигация: Enter, Backspace, F2, type-ahead.
        Замер времени обработки событий (app-level).
        Drag-and-drop: ручной старт QDrag при LMB+движение (обход Qt-selection)."""
        _start = time.monotonic()
        try:
            # ── MouseButtonPress: сброс выделения в пустом месте + трекинг drag ─
            if (event.type() == QEvent.MouseButtonPress
                    and event.button() in (Qt.LeftButton, Qt.RightButton)):
                obj_is_tree = (hasattr(self, 'tree_view') and self.tree_view is not None
                               and obj is self.tree_view.viewport())
                obj_is_table = (hasattr(self, 'table_view') and self.table_view is not None
                                and obj is self.table_view.viewport())

                if obj_is_tree:
                    idx = self.tree_view.indexAt(event.pos())
                    if idx.isValid():
                        # Клик на элементе → готовимся к возможному DnD
                        self._drag_start_pos = event.pos()
                        self._drag_start_view = 'tree'
                    else:
                        # Пустое место → сброс
                        self._drag_start_pos = None
                        self._drag_start_view = None
                        self._selection_updating = True
                        self.tree_view.clearSelection()
                        self.table_view.clearSelection()
                        self._selection_updating = False
                        self._update_toolbar_buttons()
                        self._update_selection_counter()
                elif obj_is_table:
                    idx = self.table_view.indexAt(event.pos())
                    if idx.isValid():
                        self._drag_start_pos = event.pos()
                        self._drag_start_view = 'table'
                    else:
                        self._drag_start_pos = None
                        self._drag_start_view = None
                        self._selection_updating = True
                        self.tree_view.clearSelection()
                        self.table_view.clearSelection()
                        self._selection_updating = False
                        self._update_toolbar_buttons()
                        self._update_selection_counter()

            # ── MouseMove: DnD старт при превышении threshold (LMB или RMB) ────
            if (event.type() == QEvent.MouseMove
                    and event.buttons() & (Qt.LeftButton | Qt.RightButton)
                    and self._drag_start_pos is not None):
                # Проверяем дистанцию
                delta = (event.pos() - self._drag_start_pos).manhattanLength()
                if delta >= QApplication.startDragDistance():
                    # Какая кнопка тащит (для RMB после отпускания — меню Copy/Move)
                    self._pending_drag_button = (
                        Qt.LeftButton if event.buttons() & Qt.LeftButton
                        else Qt.RightButton)
                    # Запускаем QDrag
                    if self._drag_start_view == 'tree':
                        self._start_drag_from_tree()
                    elif self._drag_start_view == 'table':
                        self._start_drag_from_table()
                    self._pending_drag_button = None
                    self._drag_start_pos = None
                    self._drag_start_view = None
                    return True

            # ── MouseButtonRelease: сброс трекинга drag ─────────────────────---
            if (event.type() == QEvent.MouseButtonRelease
                    and event.button() == Qt.LeftButton):
                self._drag_start_pos = None
                self._drag_start_view = None

            # ── Leave: сброс подсветки строки ──────────────────────────────────
            if (hasattr(self, 'table_view') and self.table_view is not None
                    and obj is self.table_view.viewport()
                    and event.type() == QEvent.Leave):
                if hasattr(self, '_hover_delegate'):
                    self._hover_delegate.hovered_row = -1
                    self.table_view.viewport().update()

            # ── Клавиатурные события для таблицы ───────────────────────────────
            if (hasattr(self, 'table_view') and self.table_view is not None
                    and obj is self.table_view
                    and event.type() == QEvent.KeyPress):
                return self._handle_table_key(event)

            # ── Drag-and-drop (внешний + внутренний) ───────────────────────────
            _INTERNAL_MIME = 'application/x-yadisk-cloud-paths'
            etype = event.type()
            if etype == QEvent.DragEnter or etype == QEvent.DragMove:
                mime = event.mimeData()
                if not mime:
                    return False
                # Внутренний DnD (из дерева/таблицы): НЕ съедаем событие —
                # возвращаем False, чтобы сам QTreeView/QTableView обработал его
                # (canDropMimeData в модели) и нарисовал drop-индикатор на папке
                if mime.hasFormat(_INTERNAL_MIME):
                    return False
                # Внешний DnD (из Проводника) — view его не примет, accept здесь
                if mime.hasUrls() and any(u.isLocalFile() for u in mime.urls()):
                    event.acceptProposedAction()
                    return True
                return False
            if etype == QEvent.Drop:
                mime = event.mimeData()
                if not mime:
                    return False
                # ── Внутренний DnD (облачные пути) ──────────────
                if mime.hasFormat(_INTERNAL_MIME):
                    import json
                    try:
                        paths = json.loads(bytes(mime.data(_INTERNAL_MIME)).decode('utf-8'))
                    except Exception:
                        return False
                    if not paths:
                        return False
                    # Определяем папку назначения
                    if obj is self.tree_view.viewport():
                        target_path = self._current_path
                        idx = self.tree_view.indexAt(event.pos())
                        if idx.isValid():
                            item = self.tree_model._find_item(
                                idx.data(Qt.UserRole) or "")
                            if item and item.cloud_path:
                                target_path = item.cloud_path
                    elif obj is self.table_view.viewport():
                        target_path = self._current_path
                        # Как в Проводнике: drop на строку-папку нацеливается
                        # на эту папку (а не на текущую)
                        idx = self.table_view.indexAt(event.pos())
                        if idx.isValid():
                            item = self._get_item(idx)
                            if item and item.get("is_dir") \
                                    and not item.get("is_parent_nav"):
                                target_path = item["cloud_path"]
                    else:
                        return False
                    # RMB-drag: не обрабатываем drop сразу — откладываем,
                    # действие выберет пользователь в меню после отпускания
                    if getattr(self, '_drag_mouse_button', None) == Qt.RightButton:
                        self._pending_internal_drop = (paths, target_path)
                        event.acceptProposedAction()
                        return True
                    mods = event.keyboardModifiers()
                    is_move = bool(mods & Qt.ShiftModifier)
                    QTimer.singleShot(0, lambda p=paths, d=target_path, m=is_move:
                        self._handle_internal_drop(p, d, m))
                    event.acceptProposedAction()
                    label = "перемещение" if is_move else "копирование"
                    self._show_left_busy(
                        f"📦 {label} {len(paths)} элемент(ов)...")
                    return True
                # ── Внешний DnD (локальные файлы из Проводника) ──
                if mime.hasUrls():
                    urls = [u.toLocalFile() for u in mime.urls()
                            if u.isLocalFile() and u.toLocalFile()]
                    if not urls:
                        return False
                    if obj is self.tree_view.viewport():
                        target_path = self._current_path
                        idx = self.tree_view.indexAt(event.pos())
                        if idx.isValid():
                            item = self.tree_model._find_item(
                                idx.data(Qt.UserRole) or "")
                            if item and item.cloud_path:
                                target_path = item.cloud_path
                    elif obj is self.table_view.viewport():
                        target_path = self._current_path
                    else:
                        return False
                    thread = _DropUploadThread(urls, target_path, self)
                    thread.finished.connect(self._on_drop_complete)
                    thread.finished.connect(thread.deleteLater)
                    thread.start()
                    event.acceptProposedAction()
                    self._show_left_busy(
                        f"📥 Копирование {len(urls)} файлов...")
                    return True
            return super().eventFilter(obj, event)
        finally:
            elapsed = (time.monotonic() - _start) * 1000
            if elapsed > 200:
                # Логируем только widget-level события (где это наш дочерний виджет)
                obj_name = obj.objectName() or type(obj).__name__
                if (isinstance(obj, QWidget) and
                        (self.isAncestorOf(obj) if hasattr(self, 'isAncestorOf') else
                        obj is self or (hasattr(self, 'tree_view') and
                        obj in (self.tree_view, self.tree_view.viewport(),
                                self.table_view, self.table_view.viewport())))):
                    logger.warning(
                        "⚠️ SLOW EVENT: type=%d(%s) on %s took %dms",
                        event.type(), event_type_name(event.type()),
                        obj_name, int(elapsed))

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
        self._search_mode = False
        self._search_query = ""
        self._search_history_timer.stop()
        self._search_edit.clear()
        self._update_location_column()
        # Сохраняем выделение перед навигацией
        saved_selection = self._get_selected_cloud_paths()
        self._current_path = path
        self._last_requested_path = path
        # Адресная строка следует за навигацией (закрывает открытый редактор)
        self._breadcrumb_bar.set_path(path)

        # Шаг 1: из БД (без спиннера; SQLite в фоновом потоке)
        self._pending_selection = saved_selection
        self._load_folder_local(path)

        # Шаг 2: в фоне — API (обновление БД; перерисовка только если
        # набор путей изменился — см. _on_folder_loaded)
        self._start_folder_load(path, use_api=True)

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
                # Корень диска не копируется целиком
                if cloud_path == "/":
                    cloud_path = None
                else:
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
        """Загрузить список файлов папки в фоне (API + SQLite) и обновить таблицу."""
        self._last_requested_path = path
        self._show_left_busy(f"Загрузка {path}...")
        self._show_table_loading()
        self._start_folder_load(path, use_api=True)

    def _load_folder_local(self, path: str):
        """Навигация по локальному кешу (без API) — SQLite в фоновом потоке."""
        self._current_path = path
        self._start_folder_load(path, use_api=False)

    # ── Обратная синхронизация (облако → локальный кеш) ──────

    def _start_bulk_sync(self):
        """Запустить полный опрос всех файлов (AllFilesThread) для детекции изменений в облаке."""
        if getattr(self, '_bulk_sync_running', False):
            return
        self._bulk_sync_running = True
        logger.info("Bulk sync: starting full file scan...")
        thread = _AllFilesThread(self._api, self._db, self)
        thread.finished.connect(self._on_bulk_sync_complete)
        thread.finished.connect(thread.deleteLater)
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()

    def _on_bulk_sync_complete(self, total: int):
        """После AllFilesThread — найти файлы, изменившиеся в облаке."""
        self._bulk_sync_running = False
        if total == 0:
            logger.info("Bulk sync: no files loaded, nothing to check")
            return
        changed = self._db.get_changed_downloaded_files()
        if not changed:
            logger.info("Bulk sync: all %d downloaded files match cloud MD5", total)
            return
        logger.info("Bulk sync: %d downloaded file(s) changed in cloud",
                     len(changed))
        for rec in changed:
            cp = rec["cloud_path"]
            lp = rec.get("local_path") or _local_path(cp)
            if not lp or not os.path.exists(lp):
                continue
            last_sync_md5 = rec.get("last_sync_md5") or ""
            if not last_sync_md5:
                continue
            self._queue_reverse_meta_fetch(cp, lp, last_sync_md5)

    def _queue_reverse_meta_fetch(self, cloud_path: str, local_path: str,
                                   last_sync_md5: str):
        """Поставить файл в очередь MetaFetch для проверки обратной синхронизации."""
        if cloud_path in self._syncing:
            return
        if self._active_meta_fetches >= self._max_meta_concurrent:
            self._reverse_meta_queue.append((cloud_path, local_path, last_sync_md5))
        else:
            self._do_reverse_meta_fetch(cloud_path, local_path, last_sync_md5)

    def _do_reverse_meta_fetch(self, cloud_path: str, local_path: str,
                                last_sync_md5: str):
        """Запустить MetaFetchThread для обратной синхронизации."""
        self._active_meta_fetches += 1
        thread = _MetaFetchThread(self._api, cloud_path, local_path, self)
        thread.finished.connect(
            lambda result: self._on_reverse_meta_fetched(
                cloud_path, local_path, last_sync_md5, result
            )
        )
        thread.finished.connect(thread.deleteLater)
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()

    def _process_reverse_meta_queue(self):
        """Обработать очередь обратной MetaFetch."""
        while (self._reverse_meta_queue
               and self._active_meta_fetches < self._max_meta_concurrent):
            cp, lp, lsmd5 = self._reverse_meta_queue.pop(0)
            self._do_reverse_meta_fetch(cp, lp, lsmd5)

    def _on_reverse_meta_fetched(self, cloud_path: str, local_path: str,
                                  last_sync_md5: str, result: dict):
        """Обработка MetaFetch для обратной синхронизации (облако→локальный)."""
        self._active_meta_fetches = max(0, self._active_meta_fetches - 1)
        self._process_reverse_meta_queue()

        cloud_md5 = result.get("cloud_md5", "")
        local_md5 = result.get("local_md5", "")

        if not cloud_md5 or not last_sync_md5:
            return
        if cloud_md5 == last_sync_md5:
            return  # облако не изменилось

        # Облако изменилось — решаем, что делать
        if local_md5 == last_sync_md5:
            # Локально не менялось — безопасно скачиваем
            logger.info("Reverse sync: downloading %s (cloud changed)", cloud_path)
            self._start_download(cloud_path, local_path)
        else:
            # Файл изменился и локально, и в облаке — конфликт
            logger.info("Reverse sync: conflict %s (both changed)", cloud_path)
            self._handle_conflict(cloud_path, local_path, local_md5,
                                  cloud_md5, result.get("cloud_size", 0),
                                  result.get("cloud_modified", ""))

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
        Если существующий downloaded файл изменился в облаке — авто-синхронизация.
        """
        if not items:
            return
        try:
            all_paths = [it["path"] for it in items]
            existing_paths = self._db.file_exists_batch(all_paths)
            path_to_item = {it["path"]: it for it in items}

            new_items = [path_to_item[p] for p in all_paths if p not in existing_paths]

            changed_items = []
            if existing_paths:
                existing_records = self._db.get_files_batch(list(existing_paths))
                for cp in existing_paths:
                    rec = existing_records.get(cp)
                    if rec and rec.get("status") == "downloaded":
                        it = path_to_item[cp]
                        cloud_md5 = it.get("md5", "")
                        last_sync = rec.get("last_sync_md5") or ""
                        if cloud_md5 and last_sync and cloud_md5 != last_sync:
                            changed_items.append(it)
            if changed_items:
                logger.info("Poll: %d downloaded file(s) changed in cloud",
                            len(changed_items))
                for item in changed_items:
                    cp = item["path"]
                    lp = _local_path(cp)
                    if os.path.exists(lp):
                        self._queue_reverse_meta_fetch(
                            cp, lp, self._db.get_file(cp).get("last_sync_md5") or "")

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

    def _toggle_local_copy(self, download_mode: bool = True):
        """Переключатель: скачать / удалить локальную копию.

        - download_mode=True → скачивать (Сохранить на компьютере)
        - download_mode=False → удалять (Оставить только в облаке)

        Вызывается через _save_to_computer() / _leave_only_in_cloud() из тулбара,
        меню и контекстного меню.
        """
        to_download: list[str] = []
        to_remove: list[str] = []
        pending_folders: list[str] = []

        # Определяем активную панель по выделению, а не по фокусу —
        # кнопка тулбара крадёт фокус, и focusWidget() не находит таблицу/дерево
        table_rows = self.table_view.selectionModel().selectedRows(0)
        tree_rows = self.tree_view.selectionModel().selectedRows(0)

        if tree_rows and not table_rows:
            # Только дерево имеет выделение → обрабатываем папки рекурсивно
            for idx in tree_rows:
                cloud_path = idx.data(Qt.UserRole)
                # Корень диска («Яндекс Диск») — только навигация:
                # «Сохранить на компьютере» скачала бы весь диск,
                # «Оставить только в облаке» удалила бы весь локальный кеш
                if cloud_path and cloud_path != "/":
                    pending_folders.append(cloud_path)
        elif table_rows:
            # Таблица имеет выделение → обрабатываем строки
            for idx in table_rows:
                item = self._get_item(idx)
                if not item:
                    continue
                if item.get("is_dir"):
                    pending_folders.append(item["cloud_path"])
                else:
                    if download_mode is True:
                        # Сохранить на компьютере — только нескачанные
                        if item["status"] in ("cloud_only", "unknown"):
                            to_download.append(item["cloud_path"])
                    elif download_mode is False:
                        # Оставить только в облаке — только скачанные
                        if item["status"] == "downloaded":
                            to_remove.append(item["cloud_path"])
                    else:
                        # Авто-определение (тулбар)
                        if item["status"] in ("cloud_only", "unknown"):
                            to_download.append(item["cloud_path"])
                        else:
                            to_remove.append(item["cloud_path"])

        if pending_folders:
            # Определяем режим: скачивание или удаление
            dl_mode = download_mode
            # Сохраняем контекст для обработчика
            self._folder_download_mode = dl_mode
            self._pending_folder_paths = pending_folders[:]
            # Папки: BFS-обход в фоновом потоке (не блокирует UI).
            # Ставим syncing только самой папке — для немедленной обратной связи.
            # Индивидуальные файлы внутри пометит BFS-поток (чтобы не испортить
            # pre_status кеш классификации).
            for folder_path in pending_folders:
                self._db.set_status(folder_path, "syncing")
                self.table_model.update_status(folder_path, "syncing")
                self._update_tree_status(folder_path)
            self._show_left_busy("Сбор файлов для обработки...")
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
        self._hide_left_busy()
        for cp in to_download:
            self._db.set_status(cp, "syncing")
            self.table_model.update_status(cp, "syncing")
            self._update_tree_status(cp)
            self._start_download(cp, _local_path(cp))
        for cp in to_remove:
            self._remove_local_copy(cp)
            self._update_tree_status(cp)
        # Обновляем статусы в таблице и дереве из БД
        self.table_model.refresh_statuses()
        self.tree_model.invalidate_status(None)
        self.tree_model.layoutChanged.emit()

        if not to_download and not to_remove:
            # Возможно, выбраны пустые папки (ни одного файла внутри)
            folders = getattr(self, '_pending_folder_paths', [])
            dl_mode = getattr(self, '_folder_download_mode', None)
            handled_any = False
            for fp in folders:
                if not self._db.get_children(fp):
                    handled_any = True
                    local_dir = _local_path(fp)
                    if dl_mode is True:
                        # «Сохранить на компьютере» — создать пустую локальную папку
                        os.makedirs(local_dir, exist_ok=True)
                        self._db.upsert_file(
                            fp, Path(fp).name, "dir",
                            size=0,
                            modified=datetime.now(timezone.utc).isoformat(),
                            md5="",
                        )
                        self._db.set_downloaded(fp, local_dir)
                        self.table_model.update_status(fp, "downloaded")
                        logger.info("Empty folder created locally: %s → %s", fp, local_dir)
                    else:
                        # «Оставить только в облаке» — удалить локальную, если существует
                        if os.path.isdir(local_dir):
                            shutil.rmtree(local_dir, ignore_errors=True)
                            logger.info("Empty folder removed locally: %s", local_dir)
                        if self._db.get_file(fp):
                            self._db.set_cloud_only(fp)
                            self.table_model.update_item_status(fp, "cloud_only")
                    self._update_tree_status(fp)
            if handled_any:
                self.table_model.refresh_statuses()
                self.tree_model.invalidate_status(None)
                self.tree_model.layoutChanged.emit()
                self.statusBar().showMessage("✅ Пустая папка обработана", 3000)
                return
            # Нет ни файлов, ни папок — показываем подсказку
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
                # Корень диска не копируется/не вырезается
                if cp and cp != "/":
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

        Предварительная проверка конфликтов (через БД, без API) в главном потоке,
        затем API-вызовы copy/move в фоновом потоке PasteFilesThread.
        """
        if not self._clipboard_buffer:
            return
        logger.info("📋 Вставка: %d элемент(ов) → %s",
                     len(self._clipboard_buffer), self._current_path)

        focus = self.focusWidget()
        dest = self._current_path
        if focus is self.tree_view:
            tree_rows = self.tree_view.selectionModel().selectedRows(0)
            if tree_rows:
                cp = tree_rows[0].data(Qt.UserRole) or ""
                if cp:
                    dest = cp

        any_copy = any(a == "copy" for _, a in self._clipboard_buffer)

        # ── Pre-check: собираем все пары + проверяем конфликты по БД ──
        conflicts: list[tuple[str, str, str, bool, bool]] = []  # (src, dest, action, overwrite_default, is_dir_src)
        no_conflict_items: list[tuple[str, str, str]] = []
        for src_path, action in list(self._clipboard_buffer):
            name = os.path.basename(src_path.rstrip("/"))
            new_path = (dest.rstrip("/") + "/" + name).replace("//", "/")
            if new_path == src_path:
                continue  # нельзя копировать/перемещать в себя же
            dst_rec = self._db.get_file(new_path)
            exists = dst_rec is not None
            if exists:
                src_rec = self._db.get_file(src_path)
                src_is_dir = src_rec and src_rec.get("type") == "dir"
                dst_has_local = dst_rec.get("local_path", "")
                conflicts.append((src_path, new_path, action, dst_has_local, src_is_dir))
            else:
                no_conflict_items.append((src_path, new_path, action))

        # ── Диалог конфликта (один на все) ──
        overwrite_mode = None  # None=cancel, False=skip_all, True=overwrite_all
        if conflicts:
            names_list = "\n".join(
                f"  • {os.path.basename(s.rstrip('/'))}" for s, _, _, _, _ in conflicts[:10])
            has_extra_warning = any(dst_hl for _, _, _, dst_hl, _ in conflicts)
            extra_warning = ""
            if has_extra_warning:
                extra_warning = (
                    "\n\n⚠️ Некоторые конфликтующие папки содержат скачанные файлы. "
                    "Замена удалит их в облаке."
                )
            msg = QMessageBox(self)
            msg.setWindowModality(Qt.WindowModal)
            msg.setWindowTitle("Конфликт")
            msg.setIcon(QMessageBox.Question)
            msg.setText(
                f"{len(conflicts)} элемент(ов) уже существует в папке назначения:\n\n"
                f"{names_list}\n\n"
                f"Что делать?"
                + extra_warning
            )
            btn_overwrite = msg.addButton("✅ Заменить всё", QMessageBox.YesRole)
            btn_skip = msg.addButton("⏭️ Пропустить все", QMessageBox.NoRole)
            btn_cancel = msg.addButton("Отмена", QMessageBox.RejectRole)
            msg.setDefaultButton(btn_skip)
            msg.exec()
            if msg.clickedButton() == btn_overwrite:
                overwrite_mode = True
            elif msg.clickedButton() == btn_skip:
                overwrite_mode = False
            else:
                return  # cancel

        # ── Собираем финальный список ──
        items: list[tuple[str, str, str, bool]] = []
        for src_path, new_path, action in no_conflict_items:
            items.append((src_path, new_path, action, False))
        if overwrite_mode is True:
            for src_path, new_path, action, _, _ in conflicts:
                items.append((src_path, new_path, action, True))

        if not items:
            self.statusBar().showMessage("❌ Ничего не сделано", 3000)
            return

        self._show_left_busy(
            f"📋 Вставка {len(items)} элемент(ов)...")

        # ── Запускаем фоновый поток ──
        thread = _PasteFilesThread(self._api, items, self)
        thread.finished.connect(
            lambda sc, pc, errs, ac: self._on_paste_finished(
                sc, pc, errs, ac, dest))
        thread.finished.connect(thread.deleteLater)
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()

    def _on_paste_finished(self, success_count: int, processed_cut: list[tuple[str, str]],
                           errors: list[str], any_copy: bool, dest: str):
        """Обработка завершения PasteFilesThread (главный поток)."""
        total_attempted = len(processed_cut) + (success_count - len(processed_cut)) + len(errors)

        # Мигрируем БД для успешно выполненных cut-операций
        for src_p, new_p in processed_cut:
            try:
                self._db.migrate_path(src_p, new_p)
            except Exception as e:
                logger.warning("DB migrate_path(%s→%s) failed: %s", src_p, new_p, e)

        # Очистка буфера: для cut удаляем только успешно обработанные
        if processed_cut or any_copy:
            remaining = [
                (sp, a) for sp, a in self._clipboard_buffer
                if not (a == "cut" and any(
                    sp == cut_src for cut_src, _ in processed_cut
                ))
            ]
            self._clipboard_buffer = remaining
            self._schedule_toolbar_update()

        # Итог
        action_label = "Перемещено" if processed_cut else "Скопировано"
        logger.info("📋 %s %d/%d элемент(ов) в «%s»%s",
                     action_label, success_count, total_attempted, dest,
                     f", ошибки: {len(errors)}" if errors else "")
        if errors:
            QMessageBox.warning(
                self, f"{action_label} с ошибками",
                f"{action_label} {success_count} из {total_attempted} элемент(ов).\n"
                + "\n".join(f"  • {e}" for e in errors[:10])
                + ("\n  …" if len(errors) > 10 else ""),
            )
        elif success_count > 0:
            self._hide_left_busy()
            self.statusBar().showMessage(
                f"✅ {action_label} {success_count} элемент(ов) в «{dest}»", 5000
            )
        else:
            self._hide_left_busy()
            self.statusBar().showMessage("❌ Ничего не сделано", 5000)

        # Обновляем текущий вид
        self.refresh_current()

    def _delete_selected(self):
        items = []
        names = []

        # Собираем выделенные элементы из таблицы
        rows = self.table_view.selectionModel().selectedRows(0)
        for idx in rows:
            item = self._get_item(idx)
            if item and not item.get("is_parent_nav"):
                items.append(item)
                names.append(item["name"])

        # Если из таблицы пусто — из дерева
        if not items:
            tree_rows = self.tree_view.selectionModel().selectedRows(0)
            for idx in tree_rows:
                cp = idx.data(Qt.UserRole)
                if cp:
                    name = os.path.basename(cp.rstrip("/")) or "/"
                    items.append({
                        "cloud_path": cp, "name": name, "is_dir": True,
                    })
                    names.append(name)

        if not items:
            return

        # Диалог подтверждения
        msg = QMessageBox(self)
        msg.setWindowModality(Qt.WindowModal)
        msg.setWindowTitle("Подтверждение")
        msg.setIcon(QMessageBox.Question)
        msg.setText(
            f"Удалить {len(items)} файл(ов) из облака?\n"
            + "\n".join(f"  • {n}" for n in names)
        )
        btn_yes = msg.addButton("Да", QMessageBox.YesRole)
        btn_no = msg.addButton("Нет", QMessageBox.NoRole)
        msg.setDefaultButton(btn_no)
        msg.exec()
        if msg.clickedButton() != btn_yes:
            return

        # Собираем все пути: выделенные + их дети (для папок)
        all_paths = []
        for item in items:
            cp = item["cloud_path"]
            all_paths.append(cp)
            if item.get("is_dir"):
                # Получаем детей из БД
                children = self._db.get_children(cp)
                for child in children:
                    all_paths.append(child["cloud_path"])

        # Немедленно ставим статус "deleting" на все пути
        self._db.set_status_batch(all_paths, "deleting")
        # Обновляем модель таблицы и дерева (не только БД)
        for cp in all_paths:
            self.table_model.update_item_status(cp, "deleting")
        self.table_model.refresh_animated_icons()
        logger.info("🗑️ Удаление: %d элемент(ов) из «%s»",
                     len(all_paths), self._current_path)
        op_id = self._db.save_pending_op("delete", self._current_path,
                                        ",".join(all_paths))
        self._delete_op_id = op_id

        # Запоминаем удалённые пути (чёрный список на 30 сек)
        self._recently_deleted.update(all_paths)
        def _clear_deleted():
            self._recently_deleted -= set(all_paths)
        QTimer.singleShot(30000, _clear_deleted)

        # Немедленно обновляем вид: иконки в таблице и дереве
        self.table_model.refresh_animated_icons()
        self.tree_model.emit_path_changed(self._current_path)
        self._show_left_busy(
            f"🗑️ Удаление {len(items)} файл(ов)...")

        # Запускаем фоновое удаление
        thread = _DeleteFilesThread(self._api, self._db, items, self)
        thread.finished.connect(self._on_delete_finished)
        thread.finished.connect(thread.deleteLater)
        self._active_threads.append(thread)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        thread.start()

    def _on_delete_finished(self, result: dict):
        """Обработка завершения фонового удаления."""
        success = result.get("success", 0)
        errors = result.get("errors", [])

        logger.info("🗑️ Удаление завершено: %d успешно, %d ошибок",
                     success, len(errors))

        # Завершаем pending-операцию
        if self._delete_op_id is not None:
            try:
                self._db.complete_pending_op(self._delete_op_id,
                                             "done" if not errors else "failed")
            except Exception as e:
                logger.warning("Delete: complete_pending_op failed: %s", e)
            self._delete_op_id = None

        if errors:
            self._hide_left_busy()
            names = [e.split(":")[0] for e in errors[:3]]
            self.statusBar().showMessage(
                f"⚠️ Удалено: {success}, ошибки: {', '.join(names)}", 5000)
            for err in errors:
                logger.warning("Delete error: %s", err)
        else:
            self._hide_left_busy()
            self.statusBar().showMessage(
                f"✅ Удалено: {success} файл(ов)", 5000)

        # Обновляем отображение
        self.refresh_current()
        self.tree_model.emit_path_changed(self._current_path)

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
        if rows:
            item = self._get_item(rows[0])
            if item:
                self._get_public_link_for_path(item["cloud_path"])
                return
        # Если в таблице ничего нет — пробуем дерево
        tree_rows = self.tree_view.selectionModel().selectedRows(0)
        if tree_rows:
            cloud_path = tree_rows[0].data(Qt.UserRole)
            if cloud_path:
                self._get_public_link_for_path(cloud_path)

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
        # Режим поиска: папка → переход внутрь; файл → открыть (скачать из
        # облака при необходимости). Результаты поиска при этом не сбрасываются,
        # чтобы можно было сразу открыть следующий результат.
        if self._search_mode:
            if item["is_dir"]:
                self._navigate_to_folder(item["cloud_path"])
                return
            local_path = _local_path(item["cloud_path"])
            if item["status"] == "cloud_only" or not os.path.exists(local_path):
                self._open_after_download.add(item["cloud_path"])
                self._start_download(item["cloud_path"], local_path)
            else:
                self._open_file(local_path)
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
            logger.info("✏️ Переименование: %s → %s", cloud_path, new_path)
            op_id = self._db.save_pending_op("rename", cloud_path, new_path)
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

                    self._db.complete_pending_op(op_id, "done")
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
        """Ввод текста в строку поиска — если ≥2 символов, глобальный поиск по БД."""
        text = text.strip()
        if not text or len(text) < 2:
            # Очистка: выходим из режима поиска, показываем текущую папку
            if self._search_mode:
                self._search_mode = False
                self._search_query = text
                self._search_history_timer.stop()
                # Сбрасываем фильтр proxy-модели — иначе после Esc на
                # папку накладывается старый фильтр поиска (таблица
                # показывает лишь часть записей)
                self.table_sort_model.set_search_text("")
                self._load_folder_local(self._current_path)
                self._update_location_column()
                self._schedule_toolbar_update()
            else:
                # 1 символ (или пусто) — обычная фильтрация текущей папки
                self._search_query = text
                self.table_sort_model.set_search_text(text)
            return

        self._search_query = text
        # История: перезапускаем таймер паузы — запрос сохранится,
        # только если пользователь перестал печатать на >2с (или нажал Enter)
        self._search_history_timer.start(self._search_history_ms)
        # Debounce: перезапускаем таймер при каждом вводе
        self._search_debounce.start(self._search_debounce_ms)

    def _on_search_enter(self):
        """Enter в строке поиска — немедленный поиск + запись в историю."""
        q = self._search_edit.text().strip()
        if len(q) < 2:
            return
        self._search_history_timer.stop()
        self._save_search_query(q)
        self._search_query = q
        self._search_debounce.stop()
        self._do_search()

    def _save_search_history(self):
        """Таймер паузы >2с сработал — сохранить текущий запрос в историю."""
        q = self._search_edit.text().strip()
        if len(q) >= 2:
            self._save_search_query(q)

    def _save_search_query(self, q: str):
        """Сохранить запрос в историю поиска (дубликаты обновляют timestamp)."""
        self._db.add_search_query(q)
        self._search_edit.refresh_history()

    def _exit_search(self):
        """Esc в строке поиска — очистить строку и выйти из режима поиска."""
        if self._search_edit.text():
            self._search_edit.clear()  # textChanged → _on_search("") → выход из поиска
        elif self._search_mode:
            self._on_search("")
        self.table_view.setFocus()

    def _update_location_column(self):
        """Колонка «Расположение» видна только в результатах поиска."""
        self.table_view.setColumnHidden(5, not self._search_mode)

    def _focus_search(self):
        """Перевести фокус в строку поиска (Ctrl+F)."""
        self._search_edit.setFocus()
        self._search_edit.selectAll()

    # ── Адресная строка (breadcrumbs) ─────────────────────

    def _focus_address_bar(self):
        """Ctrl+L / Alt+D: войти в режим ввода пути."""
        self._breadcrumb_bar.show_editor()

    def _on_breadcrumb_navigate(self, path: str):
        """Переход из адресной строки с мгновенной проверкой существования.

        Ограничение: папки, созданные в облаке в обход Менеджера и ещё не
        попавшие в локальную БД, потребуют обычной навигации (дерево/поиск) —
        они отклоняются здесь как «не найденные» до ближайшей синхронизации.
        """
        if path != "/" and not self._db.get_file(path):
            self.statusBar().showMessage(f"Папка не найдена: {path}", 5000)
            self._breadcrumb_bar.show_editor_with(path)
            return
        self._navigate_to_folder(path)

    def _refresh_path_completer(self):
        """Перечитать список папок из БД для автодополнения адресной строки."""
        t = DirPathsThread(self._db, self)

        def _done(paths, err):
            if err:
                logger.warning("Path completer refresh failed: %s", err)
                return
            self._breadcrumb_bar.set_completer_model(
                QStringListModel(paths, self))

        t.finished.connect(_done)
        t.finished.connect(t.deleteLater)
        t.start()

    def _do_search(self):
        """Запустить фоновый поток поиска (после debounce)."""
        q = self._search_query
        if len(q) < 2:
            return
        self._show_left_busy(f"🔍 Поиск: {q}…")
        thread = _SearchThread(self._db, q, self)
        thread.finished.connect(self._on_search_results)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_search_results(self, query: str, results: list[dict]):
        """Показать результаты поиска в таблице."""
        # Защита от гонки: результат устаревшего запроса (пользователь уже
        # очистил строку или ввёл новый текст) не должен перекрывать папку
        if query != self._search_query:
            logger.debug("Ignored stale search results for %r (current %r)",
                         query, self._search_query)
            return
        if not results:
            self._hide_left_busy()
            self.statusBar().showMessage(f"«{query}» — ничего не найдено", 3000)
            return
        self._search_mode = True
        self.table_model.set_search_results(results)
        # Результаты уже отсортированы по релевантности — отключаем
        # proxy-сортировку (sort(-1)), иначе она переставит строки по имени
        self.table_sort_model.setDynamicSortFilter(False)
        self.table_sort_model.sort(-1, Qt.AscendingOrder)
        self.table_sort_model.setDynamicSortFilter(True)
        self.table_view.horizontalHeader().setSortIndicatorShown(False)
        self._update_location_column()
        self._hide_left_busy()
        self.statusBar().showMessage(f"🔍 Найдено: {len(results)} эл. по запросу «{query}»")
        self._schedule_toolbar_update()

    def _show_settings(self):
        dlg = SettingsDialog(self, on_cache_changed=self._on_cache_changed)
        # WindowModal вместо дефолтного ApplicationModal (его даёт exec()):
        # блокируется только окно Менеджера, а окно лога (без Qt-родителя)
        # остаётся видимым и интерактивным, пока открыты Настройки.
        dlg.setWindowModality(Qt.WindowModal)
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
            self._hide_left_busy()
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
        self._show_left_busy("Синхронизация с облаком...")
        thread.start()

    def _show_about(self):
        QMessageBox.about(
            self, "YaDisk Manager",
            f"Клиент Яндекс.Диска\nВерсия {VERSION}\n\n"
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
        # Стандартное поведение Windows: ПКМ по невыделенному элементу выделяет его,
        # ПКМ по уже выделенному (в т.ч. в составе множественного выделения)
        # сохраняет выделение как есть — без этого ПКМ схлопывал мультивыделение
        # до одного элемента под курсором
        if not self.table_view.selectionModel().isSelected(index):
            self.table_view.selectionModel().select(
                index, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)

        # Множественное выделение — общее меню (как в Проводнике): только операции,
        # работающие со всем набором; single-операции (ссылка, переименовать, ZIP…)
        # показываются только для одиночного выделения
        if len(self.table_view.selectionModel().selectedRows(0)) > 1:
            self._build_multi_context_menu(menu)
            menu.exec(self.table_view.viewport().mapToGlobal(pos))
            return

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

    def _build_multi_context_menu(self, menu: QMenu):
        """Меню для множественного выделения в таблице.

        Показывается одно и то же меню для любого набора (файлы / папки / смешанное),
        как в Проводнике Windows: только операции, применимые ко всему выделению.
        """
        items = []
        for idx in self.table_view.selectionModel().selectedRows(0):
            it = self._get_item(idx)
            if it and not it.get("is_parent_nav"):
                items.append(it)
        if not items:
            return

        # Есть ли среди выделенных ещё не скачанные / уже оставленные в облаке
        any_not_downloaded = any(
            it.get("status") != "downloaded" for it in items)
        any_not_cloud_only = any(
            it.get("status") != "cloud_only" for it in items)

        act = menu.addAction("Сохранить на компьютере", self._save_to_computer)
        act.setIcon(_svg_icon("loaded.svg", 24))
        act.setEnabled(any_not_downloaded)
        act = menu.addAction("Оставить только в облаке", self._leave_only_in_cloud)
        act.setIcon(_svg_icon("cloud.svg", 24))
        act.setEnabled(any_not_cloud_only)

        menu.addSeparator()

        act = menu.addAction("Вырезать", self._cut_to_buffer)
        act.setIcon(_svg_icon("scissors-3.svg", 24))
        act = menu.addAction("Копировать", self._copy_to_buffer)
        act.setIcon(_svg_icon("Copy.svg", 24))
        if self._clipboard_buffer:
            act = menu.addAction("Вставить", self._paste_from_buffer)
            act.setIcon(_svg_icon("paste.svg", 24))

        menu.addSeparator()

        act = menu.addAction("Удалить", self._delete_selected)
        act.setIcon(_svg_icon("trash.svg", 24))

    def _on_tree_context_menu(self, pos):
        """Контекстное меню для дерева папок."""
        index = self.tree_view.indexAt(pos)
        if not index.isValid():
            return
        cloud_path = index.data(Qt.UserRole)
        if not cloud_path:
            return
        # Стандартное поведение Windows: ПКМ по невыделенной папке выделяет её,
        # по выделенной — сохраняет текущее выделение
        if not self.tree_view.selectionModel().isSelected(index):
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

        # Корень диска («Яндекс Диск») — только навигация и вставка:
        # операции (копия/ссылка/ZIP/сохранить/оставить/вырезать) по всему
        # диску опасны или бессмысленны, а агрегация статуса "/" сканирует
        # всю БД и зависла бы UI
        if cloud_path == "/":
            if self._clipboard_buffer:
                menu.addSeparator()
                act = menu.addAction("Вставить", self._paste_from_buffer)
                act.setIcon(_svg_icon("paste.svg", 24))
            menu.exec(self.tree_view.viewport().mapToGlobal(pos))
            return

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
            self._log_window.save_position()
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
        self._freeze_thread_running = False
        # Сначала останавливаем таймеры, чтобы не стартовали новые операции
        self._result_timer.stop()
        self._watcher_timer.stop()
        self._poll_timer.stop()
        self._heartbeat_timer.stop()
        self._watcher.stop()
        QApplication.processEvents()
        # Затем ждём завершения потоков
        for t in self._active_threads:
            try:
                t.cancel()
            except AttributeError:
                t.quit()
            t.wait(2000)  # 2 секунды на каждый поток
            if t.isRunning():
                # Поток висит — terminate (крайняя мера)
                try:
                    t.terminate()
                    t.wait(500)
                except AttributeError:
                    pass
        self._active_threads.clear()
        if db.get_save_window_geometry():
            db.set_window_geometry(
                self.saveGeometry().toBase64().data().decode())
            self._log_window.save_position()
        # Отключаем лог-обработчик до разрушения MainWindow,
        # чтобы LogSignal не оказался удалён в момент позднего лога
        logging.getLogger().removeHandler(self._log_handler)
        self._db.close()
        QApplication.quit()
