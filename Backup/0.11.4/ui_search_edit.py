"""
SearchEdit — строка поиска с автодополнением из истории.
"""

from PySide6.QtCore import Qt, QStringListModel
from PySide6.QtWidgets import QLineEdit, QCompleter


class SearchEdit(QLineEdit):
    """Строка поиска с авто-дополнением из истории запросов."""

    def __init__(self, db_instance, parent=None):
        super().__init__(parent)
        self._db = db_instance
        self._history_model = QStringListModel()
        self._completer = QCompleter(self._history_model, self)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchContains)
        self._completer.setMaxVisibleItems(10)
        self.setCompleter(self._completer)

    def refresh_history(self, limit: int = 30):
        """Загрузить историю запросов из БД в модель комплитера."""
        items = self._db.get_search_history(limit=limit)
        self._history_model.setStringList(items)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        # Показать историю при фокусе, если поле пустое
        if not self.text() and self._history_model.rowCount():
            self._completer.complete()
