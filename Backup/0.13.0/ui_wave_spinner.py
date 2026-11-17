"""
Wave loading spinner — анимированный спиннер из глифов Брайля.

Простой QLabel + QTimer: кадры ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ (классическая
CLI-последовательность). Каждый кадр — один символ в своей ячейке Брайля,
ширина стабильна → без дёрганья. Без ручной геометрии и QPainter — размер
берётся из обычного шрифта, поэтому в статус-баре панель не раздувается
(в отличие от брайлевского спиннера-виджета с запасом +20 px в sizeHint,
который QStatusBar учитывает даже в скрытом состоянии).
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel


class WaveSpinner(QLabel):
    """Анимированный спиннер из глифов Брайля (⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏).

    Классическая CLI-последовательность «braille wave». Каждый кадр —
    один символ в своей ячейке Брайля: все кадры одинаковой ширины →
    без дёрганья. Обычный QLabel: размер такой же, как у текстовой
    надписи, поэтому в статус-баре панель не раздувается (в отличие от
    брайлевского спиннера-виджета с запасом +20 px в sizeHint, который
    QStatusBar учитывает даже в скрытом состоянии).
    """

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("color: palette(WindowText); background: transparent;")
        self._idx = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._next_frame)
        self._timer.setInterval(90)
        self._update_text()

    def _update_text(self):
        self.setText(self._FRAMES[self._idx])

    def _next_frame(self):
        self._idx = (self._idx + 1) % len(self._FRAMES)
        self._update_text()

    # Таймер живёт только пока спиннер виден
    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()
