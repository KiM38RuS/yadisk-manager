"""
BreadcrumbBar — адресная строка с хлебными крошками для YaDisk Manager.

Крошки пути (кликабельные QToolButton) + режим ручного ввода (QLineEdit).
Переполнение при узком окне: средние сегменты сворачиваются в кнопку «…»
с выпадающим меню скрытых предков (как в Проводнике Windows).
"""

from PySide6.QtCore import Qt, QEvent, Signal, QStringListModel
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCompleter, QHBoxLayout, QLabel, QLineEdit, QMenu, QSizePolicy,
    QToolButton, QWidget,
)

from ui_shared import normalize_cloud_path


def compute_overflow(total_width: int, available: int,
                     widths: list[int]) -> int:
    """Сколько средних сегментов скрыть, чтобы суммарная ширина влезла.

    Первый и последний сегменты всегда видимы. Возвращает число скрытых
    сегментов подряд начиная с индекса 1.
    """
    if total_width <= available or len(widths) <= 2:
        return 0
    hidden = 0
    w = total_width
    for i in range(1, len(widths) - 1):
        if w <= available:
            break
        w -= widths[i]
        hidden += 1
    return hidden


class _CrumbsArea(QWidget):
    """Контейнер крошек: клик по пустому месту → запрос режима ввода."""
    empty_clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.empty_clicked.emit()
        super().mousePressEvent(event)


class BreadcrumbBar(QWidget):
    """Адресная строка: крошки пути + ручной ввод облачного пути."""

    navigate = Signal(str)       # выбран путь (клик по крошке или Enter)
    editor_shown = Signal()      # вошли в режим ввода (для обновления completer)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._path = "/"
        self._segments: list[tuple[str, str]] = []
        self._editing = False
        # Кеш метрик последней сборки (для пропуска лишних rebuild при resize)
        self._widths_cache: tuple[int, list[int]] | None = None
        self._last_hidden = 0

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)

        self._crumbs = _CrumbsArea()
        self._crumbs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._crumbs_layout = QHBoxLayout(self._crumbs)
        self._crumbs_layout.setContentsMargins(0, 0, 0, 0)
        self._crumbs_layout.setSpacing(2)
        self._crumbs.empty_clicked.connect(self.show_editor)
        lay.addWidget(self._crumbs)

        self._editor = QLineEdit()
        self._editor.hide()
        self._editor.setPlaceholderText("Путь, например: /Загрузки/Отчёты")
        self._editor.returnPressed.connect(self._on_edit_done)
        self._editor.installEventFilter(self)
        lay.addWidget(self._editor)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._rebuild()

    # ── публичный API ──────────────────────────────────────

    def set_path(self, path: str) -> None:
        """Отобразить путь крошками (вызывать при каждой навигации).

        Закрывает режим ввода: внешняя навигация (дерево/таблица)
        всегда важнее открытого редактора с устаревшим путём.
        """
        self.cancel_edit()
        self._path = normalize_cloud_path(path)
        self._segments = self._split_segments(self._path)
        self._rebuild()

    def show_editor(self) -> None:
        """Режим ввода: скрыть крошки, показать QLineEdit с полным путём."""
        self._editing = True
        self._crumbs.hide()
        self._editor.show()
        self._editor.setText(self._path)
        self._editor.selectAll()
        self._editor.setFocus()
        self.editor_shown.emit()

    def show_editor_with(self, text: str) -> None:
        """Режим ввода с подставленным текстом (повтор ввода неверного пути)."""
        self.show_editor()
        self._editor.setText(text)
        self._editor.selectAll()

    def set_completer_model(self, model: QStringListModel) -> None:
        """Подменить модель автодополнения (список папок из БД)."""
        completer = QCompleter(model, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        # PopupCompletion: PySide6 не поддерживает «|» для CompletionMode
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self._editor.setCompleter(completer)

    # ── внутреннее ─────────────────────────────────────────

    @staticmethod
    def _split_segments(path: str) -> list[tuple[str, str]]:
        """Путь → [(подпись, cloud_path)], первый сегмент — корень диска."""
        parts = [p for p in path.split("/") if p]
        segs = [("Яндекс Диск", "/")]
        cur = ""
        for p in parts:
            cur += "/" + p
            segs.append((p, cur))
        return segs

    def _make_button(self, label: str, path: str, current: bool) -> QToolButton:
        b = QToolButton()          # у QToolButton нет конструктора с текстом
        b.setText(label)
        b.setAutoRaise(True)
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(path)
        if current:
            f = QFont(b.font())
            f.setBold(True)
            b.setFont(f)
        else:
            b.clicked.connect(lambda checked=False, p=path: self.navigate.emit(p))
        return b

    @staticmethod
    def _make_sep() -> QLabel:
        s = QLabel("▸")
        s.setStyleSheet("color: #909090;")
        return s

    def _rebuild(self) -> None:
        """Перестроить крошки с учётом доступной ширины (переполнение → «…»)."""
        while self._crumbs_layout.count():
            it = self._crumbs_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

        n = len(self._segments)
        buttons = [self._make_button(lbl, p, i == n - 1)
                   for i, (lbl, p) in enumerate(self._segments)]
        seps = [self._make_sep() for _ in range(max(0, n - 1))]

        widths = [b.sizeHint().width() for b in buttons]
        seps_w = sum(s.sizeHint().width() + 2 for s in seps)
        total = sum(widths) + seps_w
        avail = max(80, self._crumbs.width() - 8)
        hidden = compute_overflow(total, avail, widths)
        self._widths_cache = (total, widths)
        self._last_hidden = hidden

        if hidden > 0 and n > 2:
            dots = QToolButton()
            dots.setText("…")
            dots.setAutoRaise(True)
            dots.setToolTip("Промежуточные папки")
            menu = QMenu(dots)
            for lbl, p in self._segments[1:1 + hidden]:
                menu.addAction(lbl, lambda checked=False, pp=p: self.navigate.emit(pp))
            dots.setMenu(menu)
            dots.setPopupMode(QToolButton.InstantPopup)
            self._crumbs_layout.addWidget(buttons[0])
            self._crumbs_layout.addWidget(self._make_sep())
            self._crumbs_layout.addWidget(dots)
            self._crumbs_layout.addWidget(self._make_sep())
            self._crumbs_layout.addWidget(buttons[-1])
        else:
            for i, b in enumerate(buttons):
                if i:
                    self._crumbs_layout.addWidget(seps[i - 1])
                self._crumbs_layout.addWidget(b)
        self._crumbs_layout.addStretch(1)

    def _on_edit_done(self) -> None:
        """Enter в редакторе: нормализовать путь и сообщить о навигации."""
        raw = self._editor.text()
        self.cancel_edit()
        self.navigate.emit(normalize_cloud_path(raw))

    def cancel_edit(self) -> None:
        """Выйти из режима ввода без навигации (Esc или внешняя навигация)."""
        if self._editing:
            self._editing = False
            self._editor.hide()
            self._crumbs.show()
            self._rebuild()

    def eventFilter(self, obj, event):
        if obj is self._editor and event.type() == QEvent.KeyPress \
                and event.key() == Qt.Key_Escape:
            self.cancel_edit()
            return True
        return super().eventFilter(obj, event)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._editing:
            return
        # Ширина _crumbs ещё не обновлена внутри resizeEvent — считаем
        # доступную ширину из события (минус поля 4+4), иначе отстаём на тик.
        avail = max(80, ev.size().width() - 16)
        if self._widths_cache is None:
            self._rebuild()
            return
        total, widths = self._widths_cache
        hidden = compute_overflow(total, avail, widths)
        if hidden != self._last_hidden:
            self._rebuild()
