"""
Braille loading spinner — анимированный спиннер из двух символов Брайля.
"""

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QWidget, QLabel


class BrailleSpinner(QWidget):
    """Анимированный спиннер из двух символов Брайля — 3 точки бегут по периметру.

    Два QLabel, каждый для своего символа. Ручное позиционирование.
    Ни дёрганья (каждый label содержит 1 символ, ширина стабильна),
    ни обрезки (нет letter-spacing, правый символ в своём label).

    Параметры:
        parent: родительский виджет
        fill_parent: если True (режим оверлея), при show() заполняет
                     весь родительский контейнер через setGeometry(parent.rect()).
                     Если False (режим вложенного виджета в layout),
                     размером управляет layout через sizeHint.
    """

    _FRAMES = [
        (0x09, 0x01),  # ⠉⠁
        (0x08, 0x09),  # ⠈⠉
        (0x00, 0x19),  # ⠀⠙
        (0x00, 0x38),  # ⠀⠸
        (0x00, 0xB0),  # ⠀⢰
        (0x00, 0xE0),  # ⠀⣠
        (0x80, 0xC0),  # ⢀⣀
        (0xC0, 0x40),  # ⣀⡀
        (0xC4, 0x00),  # ⣄⠀
        (0x46, 0x00),  # ⡆⠀
        (0x07, 0x00),  # ⠇⠀
        (0x0B, 0x00),  # ⠋⠀
    ]

    def __init__(self, parent=None, fill_parent=True):
        super().__init__(parent)
        self._fill_parent = fill_parent
        self._gap = -8          # px между центрами двух символов
        self._char_w = None     # макс. ширина одного глифа (lazy)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)

        font = QFont()
        font.setPointSize(28)
        self.setFont(font)

        self._left = QLabel(self)
        self._left.setFont(font)
        self._left.setStyleSheet("color: palette(WindowText); background: transparent;")
        self._left.setAlignment(Qt.AlignCenter)

        self._right = QLabel(self)
        self._right.setFont(font)
        self._right.setStyleSheet("color: palette(WindowText); background: transparent;")
        self._right.setAlignment(Qt.AlignCenter)

        self._idx = 0
        self._update_text()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._next_frame)
        self._timer.setInterval(120)

    # ── helpers ────────────────────────────────────────────

    def _frame_char(self, idx, side):
        bits_l, bits_r = self._FRAMES[idx % len(self._FRAMES)]
        return chr(0x2800 + (bits_l if side == 0 else bits_r))

    def _update_text(self):
        self._left.setText(self._frame_char(self._idx, 0))
        self._right.setText(self._frame_char(self._idx, 1))

    def _ensure_char_width(self):
        if self._char_w is not None:
            return
        fm = self.fontMetrics()
        glyphs = set()
        for bits_l, bits_r in self._FRAMES:
            glyphs.add(chr(0x2800 + bits_l))
            glyphs.add(chr(0x2800 + bits_r))
        self._char_w = max(fm.horizontalAdvance(g) for g in glyphs)

    def _next_frame(self):
        self._idx = (self._idx + 1) % len(self._FRAMES)
        self._update_text()

    def _reposition(self):
        """Позиционирует два label по центру виджета."""
        self._ensure_char_width()
        cw = self._char_w
        total_w = cw + self._gap + cw
        left_x = (self.width() - total_w) // 2
        right_x = left_x + cw + self._gap
        lh = self.fontMetrics().height()
        y = (self.height() - lh) // 2
        self._left.setGeometry(left_x, max(y, 0), cw, lh)
        self._right.setGeometry(right_x, max(y, 0), cw, lh)

    def _center(self):
        """Заполнить родительский контейнер (режим оверлея)."""
        if self.parent():
            self.setGeometry(self.parent().rect())

    # ── события ────────────────────────────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fill_parent:
            self._center()
        self._reposition()

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()
        if self._fill_parent:
            self._center()
        self._reposition()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def sizeHint(self):
        if not self._fill_parent:
            self._ensure_char_width()
            cw = self._char_w
            total_w = cw + self._gap + cw
            lh = self.fontMetrics().height()
            return QSize(total_w + 20, lh + 20)
        return super().sizeHint()

    def minimumSizeHint(self):
        return self.sizeHint()
