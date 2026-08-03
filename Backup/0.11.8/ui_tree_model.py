"""
FolderTreeItem, FolderTreeModel — модель дерева папок для YaDisk Manager.
"""

import logging

from PySide6.QtCore import Qt, QAbstractItemModel, QModelIndex
from PySide6.QtGui import QIcon

import db
from ui_shared import _svg_icon, _rotated_svg_icon, STATUS_ICON, is_animated_status, get_rotation_angle, get_current_frame

logger = logging.getLogger("ui.tree_model")


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

    def __init__(self, api, database: db.Database = None, parent=None):
        super().__init__(parent)
        self._api = api
        self._db = database
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
        # Предзагружаем aggregate status для всех папок разом
        # (в data() это вызывало бы 20+ SQL-запросов во время Qt paint)
        if self._db is not None and folders:
            folder_paths = [f["path"] for f in folders]
            batch_statuses = self._db.get_folder_batch_aggregate_status(cloud_path, folder_paths)
            for f in folders:
                f["_status"] = batch_statuses.get(f["path"], "cloud_only")
        for f in folders:
            child = FolderTreeItem(
                name=f["name"],
                cloud_path=f["path"],
                parent=parent_item,
            )
            child.status = f.get("_status")
            parent_item.children.append(child)
        self.endInsertRows()

    def _find_item(self, cloud_path: str) -> FolderTreeItem | None:
        """Поиск узла по cloud_path (рекурсивно)."""
        if cloud_path in ("", "/"):
            return self._root
        return self._search_item(self._root, cloud_path)

    def _search_item(self, item: FolderTreeItem, cloud_path: str) -> FolderTreeItem | None:
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
                if is_animated_status(status):
                    return _rotated_svg_icon(STATUS_ICON[status], 20,
                                             angle=get_rotation_angle())
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
        """Испустить dataChanged для узла и всех его предков (чтобы дерево
        перерисовало иконки статуса на всех уровнях)."""
        item = self._find_item(cloud_path)
        while item and item is not self._root:
            idx = self._index_of(item)
            self.dataChanged.emit(idx, idx, [Qt.DecorationRole])
            item = item.parent

    def refresh_animated_icons(self):
        """Обновить иконки только для узлов с анимированным статусом.

        Эмитит dataChanged для каждого такого узла — Qt перерисует только его
        иконку, без полного repaint дерева. Вызывается из spin-таймера ~60 раз/с.
        """
        self._refresh_animated_recursive(self._root)

    def _refresh_animated_recursive(self, item: FolderTreeItem):
        if item is not self._root and item.status and is_animated_status(item.status):
            idx = self._index_of(item)
            self.dataChanged.emit(idx, idx, [Qt.DecorationRole])
        for child in item.children:
            self._refresh_animated_recursive(child)

    # ── Drag & Drop support ─────────────────────────────

    def mimeTypes(self):
        return ['application/x-yadisk-cloud-paths']

    def mimeData(self, indexes):
        import json
        from PySide6.QtCore import QMimeData
        paths = []
        seen = set()
        for idx in indexes:
            if idx.isValid() and idx.column() == 0:
                cp = idx.data(Qt.UserRole)
                if cp and cp not in seen:
                    paths.append(cp)
                    seen.add(cp)
        if not paths:
            return None
        mime = QMimeData()
        mime.setData('application/x-yadisk-cloud-paths',
                      json.dumps(paths).encode('utf-8'))
        return mime
