"""
SearchEdit — строка поиска с автодополнением из истории.
"""

from PySide6.QtCore import Qt, QStringListModel, QTimer, Signal
from PySide6.QtWidgets import QLineEdit, QCompleter


# Перевод стандартных пунктов контекстного меню QLineEdit (Qt отдаёт их
# на английском, пока в приложение не подключён русский QTranslator).
_STANDARD_MENU_RU = {
    "undo": "Отменить",
    "redo": "Повторить",
    "cut": "Вырезать",
    "copy": "Копировать",
    "paste": "Вставить",
    "delete": "Удалить",
    "select all": "Выделить всё",
}


class SearchEdit(QLineEdit):
    """Строка поиска с авто-дополнением из истории запросов."""

    escapePressed = Signal()  # Esc — очистить строку и выйти из поиска

    def __init__(self, db_instance, parent=None):
        super().__init__(parent)
        self._db = db_instance
        self._history_model = QStringListModel()
        self._completer = QCompleter(self._history_model, self)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchContains)
        self._completer.setMaxVisibleItems(10)
        self._completer.setCompletionMode(QCompleter.PopupCompletion)
        self.setCompleter(self._completer)
        # Popup истории поиска: objectName для QSS-селектора из темы
        # (app-level QSS в ui.py: QListView#search_history_popup) — иначе
        # элементы списка не подсвечиваются при наведении ни в одной теме
        self._completer.popup().setObjectName("search_history_popup")

    def refresh_history(self, limit: int = 30):
        """Загрузить историю запросов из БД в модель комплитера."""
        items = self._db.get_search_history(limit=limit)
        self._history_model.setStringList(items)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        # Показать историю при фокусе, если поле пустое.
        # Откладываем через singleShot(0): клик, давший фокус, иначе
        # успевает закрыть popup — пользователь видел лишь 1-2 записи.
        if not self.text() and self._history_model.rowCount():
            QTimer.singleShot(0, self._show_history_popup)

    def _show_history_popup(self):
        if not self.text() and self._history_model.rowCount():
            self._completer.complete()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.escapePressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu()
        for action in menu.actions():
            key = action.text().replace("&", "").strip().lower()
            ru = _STANDARD_MENU_RU.get(key)
            if ru:
                action.setText(ru)
        menu.exec(event.globalPos())
