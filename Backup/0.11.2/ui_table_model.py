"""
RowHoverDelegate, FileTableModel, FileTableSortModel — модель и делегат таблицы файлов.
"""

import logging
from datetime import datetime

from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, QSortFilterProxyModel
from PySide6.QtGui import QColor, QPalette, QPainter
from PySide6.QtWidgets import QApplication, QStyledItemDelegate, QStyle, QStyleOptionViewItem

import db
from ui_shared import (
    _svg_icon, _rotated_svg_icon, _human_size, _is_windows_reserved, _icon_for,
    STATUS_LABELS, STATUS_ICON, STATUS_COLOR, STATUS_COLOR_LIGHT,
    is_animated_status, get_rotation_angle,
)

logger = logging.getLogger("ui.table_model")


class RowHoverDelegate(QStyledItemDelegate):
    """Делегат для подсветки строки в таблице.

    Selected: palette(Midlight) + текст WindowText.
    Hover: computed hover-color.
    Колонка 1 (Имя) — ручной рендеринг с эмодзи.
    Остальные колонки — super().paint() без State_Selected,
    чтобы Fusion не рисовал синее.
    """

    hovered_row = -1

    @staticmethod
    def _hover_color(base: QColor) -> QColor:
        if base.lightness() > 128:
            return QColor(max(0, base.red()-18), max(0, base.green()-18), max(0, base.blue()-18))
        else:
            return QColor(min(255, base.red()+10), min(255, base.green()+10), min(255, base.blue()+10))

    def paint(self, painter, option, index):
        is_selected = bool(option.state & QStyle.State_Selected)
        is_hover = (index.row() == self.hovered_row and not is_selected)

        # Фон: manual fill — без синего Fusion
        if is_selected:
            painter.fillRect(option.rect, option.palette.color(QPalette.Midlight))
        elif is_hover:
            painter.fillRect(option.rect, self._hover_color(option.palette.color(QPalette.Base)))

        if index.column() == 1:
            self._paint_name_cell(painter, option, index)
        elif is_selected or is_hover:
            # super().paint() без State_Selected/State_MouseOver.
            # Qt сам рисует иконку+текст — те же отступы, что и в normal,
            # иконка/текст не дёргаются.
            opt = QStyleOptionViewItem(option)
            opt.state &= ~(QStyle.State_Selected | QStyle.State_MouseOver)
            opt.palette.setColor(QPalette.HighlightedText,
                                 option.palette.color(QPalette.WindowText))
            opt.palette.setColor(QPalette.Text,
                                 option.palette.color(QPalette.WindowText))
            super().paint(painter, opt, index)
        else:
            super().paint(painter, option, index)

    def _paint_name_cell(self, painter, option, index):
        """Колонка «Имя»: эмодзи-иконка + название (элизия). Никогда не красит фон."""
        painter.save()
        painter.setPen(option.palette.color(QPalette.WindowText))

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

        text_rect = option.rect.adjusted(4, 0, -4, 0)
        elided = option.fontMetrics.elidedText(display_text, Qt.ElideRight,
                                                text_rect.width())
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)
        painter.restore()


class FileTableModel(QAbstractTableModel):
    COLUMNS = ["Статус", "Имя", "Размер", "Изменён", "Расположение"]

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

        # Предзагружаем aggregate статусы папок одним запросом
        dir_paths = []
        for item in api_items:
            if _is_windows_reserved(item.get("name", "")):
                continue
            if item["type"] == "dir" and ("status" not in item or not item.get("status")):
                path = item["path"]
                db_file = self._db.get_file(path)
                if db_file:
                    dir_paths.append(path)
        if dir_paths:
            batch_statuses = self._db.get_folder_batch_aggregate_status(cloud_path, dir_paths)
        else:
            batch_statuses = {}

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
                    if path in batch_statuses:
                        status = batch_statuses[path]
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
                "parent_path": "",
            })
        self.endResetModel()

    @property
    def current_paths(self) -> set:
        """Множество cloud_path, отображаемых в таблице (без '..')."""
        return {item["cloud_path"] for item in self._items
                if not item.get("is_parent_nav")}

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

    def refresh_statuses(self):
        """Обновить статусы всех элементов модели из БД (без API-запросов).

        Для папок используется get_folder_aggregate_status,
        для файлов — прямой запрос get_file().
        Эмитит dataChanged только для строк, где статус изменился.
        """
        changed_rows: list[int] = []
        for i, item in enumerate(self._items):
            if item.get("is_parent_nav"):
                continue
            cloud_path = item["cloud_path"]
            is_dir = item.get("is_dir", False)
            old_status = self._items[i]["status"]
            if is_dir:
                new_status = self._db.get_folder_aggregate_status(cloud_path)
            else:
                db_file = self._db.get_file(cloud_path)
                new_status = db_file["status"] if db_file else "cloud_only"
            if new_status != old_status:
                self._items[i]["status"] = new_status
                changed_rows.append(i)
        if changed_rows:
            min_r = min(changed_rows)
            max_r = max(changed_rows)
            self.dataChanged.emit(
                self.index(min_r, 0), self.index(max_r, 3), [Qt.DisplayRole])

    def refresh_animated_icons(self):
        """Обновить иконки строк с анимированным статусом (syncing/unknown).

        Эмитит dataChanged только для колонки 0 (иконка) строк, у которых
        статус входит в _ANIMATED_STATUSES. Значительно легче полного repaint.
        """
        # Ищем строки с animated-статусом
        rows: list[int] = []
        for i, item in enumerate(self._items):
            if item.get("is_parent_nav"):
                continue
            if is_animated_status(item.get("status", "")):
                rows.append(i)
        if rows:
            # Эмитим для колонки 0 (иконка) — Qt перерисовывает только DecorationRole
            first = self.index(rows[0], 0)
            last = self.index(rows[-1], 0)
            self.dataChanged.emit(first, last, [Qt.DecorationRole])

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
            elif col == 4:
                return item.get("parent_path", "")
        if role == Qt.DecorationRole and col == 0:
            s = item["status"]
            icon_name = STATUS_ICON.get(s, "")
            if icon_name:
                if is_animated_status(s):
                    return _rotated_svg_icon(icon_name, 20, angle=get_rotation_angle())
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

    def get_item(self, row: int) -> dict | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def set_search_results(self, items: list[dict]):
        """Показать результаты глобального поиска (без элемента '..')."""
        self.beginResetModel()
        self._current_path = "__search__"
        self._items = []
        for item in items:
            is_dir = item["type"] == "dir"
            cp = item["cloud_path"]
            parent = "/".join(cp.rstrip("/").split("/")[:-1]) or "/"
            self._items.append({
                "cloud_path": cp,
                "name": item["name"],
                "type": item["type"],
                "size": item.get("size", 0),
                "modified": item.get("modified", ""),
                "md5": item.get("md5", ""),
                "mime_type": item.get("mime_type", ""),
                "status": item.get("status", "cloud_only"),
                "local_path": item.get("local_path", ""),
                "is_dir": is_dir,
                "parent_path": parent,
            })
        self.endResetModel()

    # ── Drag & Drop support ─────────────────────────────

    def mimeTypes(self):
        return ['application/x-yadisk-cloud-paths']

    def mimeData(self, indexes):
        import json
        from PySide6.QtCore import QMimeData
        paths = []
        seen = set()
        for idx in indexes:
            if idx.isValid():
                cp = idx.data(Qt.UserRole)
                if cp and cp not in seen:
                    # Пропускаем ".."
                    item = self._items[idx.row()] if 0 <= idx.row() < len(self._items) else None
                    if item and item.get("is_parent_nav"):
                        continue
                    paths.append(cp)
                    seen.add(cp)
        if not paths:
            return None
        mime = QMimeData()
        mime.setData('application/x-yadisk-cloud-paths',
                      json.dumps(paths).encode('utf-8'))
        return mime


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

    # ── Drag & Drop: делегируем source-модели ────────────

    def mimeTypes(self):
        src = self.sourceModel()
        if src and hasattr(src, 'mimeTypes'):
            return src.mimeTypes()
        return ['application/x-yadisk-cloud-paths']

    def mimeData(self, indexes):
        """Переводим proxy-индексы в source-индексы и вызываем source.mimeData()."""
        src = self.sourceModel()
        if not src or not hasattr(src, 'mimeData'):
            return super().mimeData(indexes)
        source_indexes = [self.mapToSource(idx) for idx in indexes if idx.isValid()]
        return src.mimeData(source_indexes)

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
