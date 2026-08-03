"""
LogSignal, LogHandler, LogWindow — потокобезопасное логирование в GUI.
"""

import logging

from PySide6.QtCore import Qt, QTimer, QByteArray, QEvent, QObject, Signal
from PySide6.QtGui import QFont, QColor, QPalette, QGuiApplication, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPlainTextEdit,
    QPushButton, QScrollBar,
)

import db
from ui_shared import _svg_icon


# ── Log signal (thread-safe) ──────────────────────────────


class LogSignal(QObject):
    """Сигнал для передачи лог-сообщений из любого потока в GUI."""
    message = Signal(str, int)  # formatted_text, levelno


class LogHandler(logging.Handler):
    """Логирование в GUI — потокобезопасно через Qt-сигнал.

    Устанавливается на корневой логгер в main.py.
    """
    def __init__(self, signal: LogSignal):
        super().__init__(level=logging.NOTSET)
        self._signal = signal
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record):
        try:
            msg = self.format(record)
            self._signal.message.emit(msg, record.levelno)
        except Exception:
            self.handleError(record)


# ── Log window (отдельное окно) ───────────────────────────


class LogWindow(QWidget):
    """Отдельное окно для цветного лога.

    Авто-прокрутка: пока ползунок внизу — новые строки приходят вниз.
    Если пользователь прокрутил вверх — авто-прокрутка отключается.
    """
    def __init__(self, signal: LogSignal, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Лог YaDisk Manager")
        # Dialog вместо Tool — Qt.Tool блокирует клавиатурные события на Windows
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        self.resize(720, 400)
        self._auto_scroll = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Текстовое поле — чёрный фон, моноширинный шрифт
        self._text = QPlainTextEdit(self)
        self._text.setReadOnly(True)
        self._text.setUndoRedoEnabled(False)
        self._text.setMaximumBlockCount(5000)

        pal = self._text.palette()
        pal.setColor(QPalette.Base, QColor(0, 0, 0))
        pal.setColor(QPalette.Text, QColor(0xCC, 0xCC, 0xCC))
        self._text.setPalette(pal)

        font = QFont("Consolas", 9)
        font.setStyleHint(QFont.Monospace)
        self._text.setFont(font)

        # Раскраска скроллбара через палитру, а не QSS —
        # QSS на QScrollBar ломает треугольники и перекрытие
        vsb = self._text.verticalScrollBar()
        pal = vsb.palette()
        pal.setColor(QPalette.Window, QColor(40, 40, 40))       # трек
        pal.setColor(QPalette.Button, QColor(120, 120, 120))    # ползунок
        pal.setColor(QPalette.Highlight, QColor(160, 160, 160))  # ползунок при наведении
        vsb.setPalette(pal)

        # Auto-scroll: следим за положением ползунка
        vsb = self._text.verticalScrollBar()
        vsb.valueChanged.connect(self._on_vscroll)

        layout.addWidget(self._text)

        # Кнопка «Копировать всё» — поверх текста, правый нижний угол
        self._copy_btn = QPushButton(self._text)
        self._copy_btn.setIcon(_svg_icon("Copy-light.svg", 20))
        if self._copy_btn.icon().isNull():
            self._copy_btn.setText("📋")
        self._copy_btn.setToolTip("Копировать всё")
        self._copy_btn.setFixedSize(40, 40)
        self._copy_btn.clicked.connect(self._copy_all)
        self._copy_btn.setFocusPolicy(Qt.NoFocus)
        self._copy_btn.setStyleSheet("""
            QPushButton {
                background: rgba(30,30,30,160);
                border: 1px solid rgba(255,255,255,30);
                border-radius: 8px;
                padding: 4px;
            }
            QPushButton:hover {
                background: rgba(60,60,60,200);
                border: 1px solid rgba(255,255,255,60);
            }
        """)
        # Позиционируем поверх текста — надо будет переставить при изменении размера
        self._position_copy_btn()
        self._text.resizeEvent = lambda e: (
            QPlainTextEdit.resizeEvent(self._text, e),
            self._position_copy_btn()
        )

        # Shift+F5 — перезапуск (работает и когда окно лога в фокусе)
        QShortcut(QKeySequence("Shift+F5"), self, self._parent_restart)

        # Перехват Ctrl+C на уровне QApplication (Qt.Tool блокирует все другие методы)
        QApplication.instance().installEventFilter(self)

        # Подключаем сигнал лога (может приходить из любого потока)
        signal.message.connect(self._append_log)

    def _position_copy_btn(self):
        """Разместить кнопку в правом нижнем углу текстового поля."""
        x = self._text.viewport().width() - self._copy_btn.width() - 12
        y = self._text.viewport().height() - self._copy_btn.height() - 12
        self._copy_btn.move(x, y)

    def _parent_restart(self):
        """Перезапуск через родительское окно."""
        parent = self.parent()
        if parent and hasattr(parent, "_restart_app"):
            parent._restart_app()

    def eventFilter(self, obj, event):
        """Перехват Ctrl+C на уровне QApplication."""
        if event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_C and event.modifiers() == Qt.ControlModifier:
                focused = QApplication.focusWidget()
                target = focused
                while target:
                    if isinstance(target, QPlainTextEdit):
                        cursor = target.textCursor()
                        if cursor.hasSelection():
                            sel = cursor.selectedText()
                            QApplication.clipboard().setText(sel)
                            return True
                    target = target.parent()
        return super().eventFilter(obj, event)

    def _copy_all(self):
        """Скопировать весь текст лога в буфер обмена — показать галочку на 2 сек."""
        QApplication.clipboard().setText(self._text.toPlainText())
        self._copy_btn.setIcon(_svg_icon("checkmark-light.svg", 20))
        if self._copy_btn.icon().isNull():
            self._copy_btn.setText("✔")
        self._copy_btn.setStyleSheet("""
            QPushButton {
                background: rgba(30,30,30,160);
                border: 1px solid rgba(76,175,80,80);
                border-radius: 8px;
                padding: 4px;
            }
        """)
        QTimer.singleShot(2000, self._restore_copy_button)

    def _restore_copy_button(self):
        """Вернуть кнопку копирования в исходное состояние."""
        self._copy_btn.setIcon(_svg_icon("Copy-light.svg", 20))
        if self._copy_btn.icon().isNull():
            self._copy_btn.setText("📋")
        self._copy_btn.setStyleSheet("""
            QPushButton {
                background: rgba(30,30,30,160);
                border: 1px solid rgba(255,255,255,30);
                border-radius: 8px;
                padding: 4px;
            }
            QPushButton:hover {
                background: rgba(60,60,60,200);
                border: 1px solid rgba(255,255,255,60);
            }
        """)

    def _on_vscroll(self, value):
        vsb = self._text.verticalScrollBar()
        self._auto_scroll = (value >= vsb.maximum() - 3)

    def _append_log(self, text: str, levelno: int):
        """Добавить строку в лог (вызывается из любого потока через сигнал)."""
        color = self._level_color(levelno, text)
        # Экранируем HTML-спецсимволы, чтобы < > не ломали разметку
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html = f'<p style="margin:0; color:{color}; white-space:pre-wrap;">{safe}</p>'
        self._text.appendHtml(html)
        if self._auto_scroll:
            vsb = self._text.verticalScrollBar()
            vsb.setValue(vsb.maximum())

    @staticmethod
    def _level_color(levelno: int, text: str) -> str:
        if levelno >= logging.ERROR:
            return "#ff4444"
        if levelno >= logging.WARNING:
            return "#ffaa00"
        if levelno >= logging.INFO:
            # Зелёный для успешных действий
            if any(kw in text for kw in (
                "Uploaded", "Downloaded", "✅", "успешно",
                "загружено", "скачан", "создан",
            )):
                return "#44cc44"
            return "#cccccc"
        return "#888888"

    def restore_position(self):
        """Восстановить положение окна из конфига (по умолчанию — левый нижний угол)."""
        data = db.get_log_window_geometry()
        if data:
            try:
                self.restoreGeometry(QByteArray.fromBase64(data.encode()))
                return
            except Exception:
                pass
        screen = QGuiApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.left() + 10, geo.bottom() - self.height() - 50)

    def save_position(self):
        """Сохранить положение окна в конфиг."""
        geo = self.saveGeometry().toBase64().data().decode()
        db.set_log_window_geometry(geo)

    def showEvent(self, event):
        super().showEvent(event)
        parent = self.parent()
        if parent and hasattr(parent, '_update_log_label_style'):
            parent._update_log_label_style()

    def hideEvent(self, event):
        super().hideEvent(event)
        parent = self.parent()
        if parent and hasattr(parent, '_update_log_label_style'):
            parent._update_log_label_style()
