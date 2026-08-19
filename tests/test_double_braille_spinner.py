"""Тестовый скрипт: Double Braille Spinner — два подхода + слайдер расстояния.

Запуск:
    cd D:/Program_files/YaDiskManager
    python tests/test_double_braille_spinner.py
"""

import sys
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSlider, QGroupBox, QGridLayout,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QPainter, QPen, QColor


# ═══════════════════════════════════════════════════════════
# Double Braille Spinner (текстовый, через символы Брайля)
# ═══════════════════════════════════════════════════════════

class DoubleBrailleSpinner(QWidget):
    """Анимированный спиннер из двух символов Брайля — 3 точки бегут по периметру.

    Два QLabel, каждый для своего символа. Ручное позиционирование
    в resizeEvent — никакого QPainter, только шрифт.
    Нет дёрганья (каждый label содержит 1 символ, ширина стабильна).
    Нет обрезки (нет letter-spacing, правый символ в своём label).
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMinimumSize(120, 80)

        self._gap = -7  # px между центрами двух label
        self._char_w = None  # фикс. ширина одного символа (lazy)

        font = QFont()
        font.setPointSize(36)
        self.setFont(font)

        self._left = QLabel(self)
        self._left.setFont(font)
        self._left.setStyleSheet("color: palette(WindowText);")
        self._left.setAlignment(Qt.AlignCenter)

        self._right = QLabel(self)
        self._right.setFont(font)
        self._right.setStyleSheet("color: palette(WindowText);")
        self._right.setAlignment(Qt.AlignCenter)

        self._idx = 0
        self._update_text()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._next_frame)
        self._timer.setInterval(120)

    # ── internals ──────────────────────────────────────────

    def _frame_char(self, idx, side):
        bits_l, bits_r = self._FRAMES[idx % len(self._FRAMES)]
        return chr(0x2800 + (bits_l if side == 0 else bits_r))

    def _update_text(self):
        self._left.setText(self._frame_char(self._idx, 0))
        self._right.setText(self._frame_char(self._idx, 1))

    def _ensure_char_width(self):
        """Максимальная ширина одного Braille-глифа для всех кадров."""
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
        self._reposition()

    # ── public ─────────────────────────────────────────────

    def set_gap(self, px):
        self._gap = px
        self._reposition()

    # ── layout вручную ─────────────────────────────────────

    def _reposition(self):
        self._ensure_char_width()
        cw = self._char_w
        total_w = cw + self._gap + cw
        left_x = (self.width() - total_w) / 2
        right_x = left_x + cw + self._gap

        lh = self.fontMetrics().height()
        y = (self.height() - lh) / 2

        self._left.setGeometry(int(left_x), int(y), cw, lh)
        self._right.setGeometry(int(right_x), int(y), cw, lh)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition()

    # ── lifecycle ──────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()
        self._reposition()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()


# ═══════════════════════════════════════════════════════════
# Double Braille Spinner (QPainter, точки рисуются напрямую)
# ═══════════════════════════════════════════════════════════

class PainterBrailleSpinner(QWidget):
    """3 точки бегут по периметру поля 4x4. Рисуется QPainter'ом.

    Gap = 0 — левая и правая половины вплотную.
    Неактивные точки полностью прозрачны.
    """

    _PERIMETER = [
        (0, 0), (0, 1), (0, 2), (0, 3),
        (1, 3), (2, 3), (3, 3), (3, 2),
        (3, 1), (3, 0), (2, 0), (1, 0),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMinimumSize(120, 80)

        self._gap = 0
        self._dot_radius = 4
        self._cell_w = 14
        self._cell_h = 14
        self._margin = 12

        self._idx = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._next_frame)
        self._timer.setInterval(120)

    @property
    def _grid_width(self):
        return 3 * self._cell_w + self._gap + self._cell_w

    @property
    def _grid_height(self):
        return 3 * self._cell_h

    def _dot_center(self, row, col):
        x = self._margin + col * self._cell_w
        if col >= 2:
            x += self._gap
        y = self._margin + row * self._cell_h
        return x, y

    def _next_frame(self):
        self._idx = (self._idx + 1) % len(self._PERIMETER)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        n = len(self._PERIMETER)
        lit = {
            self._PERIMETER[(self._idx - 2) % n],
            self._PERIMETER[(self._idx - 1) % n],
            self._PERIMETER[self._idx],
        }

        dot_color = self.palette().color(self.foregroundRole())
        p.setPen(Qt.NoPen)

        for row in range(4):
            for col in range(4):
                cx, cy = self._dot_center(row, col)
                if (row, col) in lit:
                    p.setBrush(dot_color)
                else:
                    bg = QColor(dot_color)
                    bg.setAlpha(0)
                    p.setBrush(bg)
                p.drawEllipse(cx - self._dot_radius, cy - self._dot_radius,
                              self._dot_radius * 2, self._dot_radius * 2)

        p.end()

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()


# ═══════════════════════════════════════════════════════════
# Reference: оригинальный односимвольный Braille-спиннер
# ═══════════════════════════════════════════════════════════

class SingleBrailleSpinner(QWidget):
    """Односимвольный Braille-спиннер (как в ui.py сейчас)."""

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setContentsMargins(0, 0, 0, 0)

        self._label = QLabel(self._FRAMES[0])
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setFocusPolicy(Qt.NoFocus)

        font = self._label.font()
        font.setPointSize(36)
        self._label.setFont(font)
        self._label.setStyleSheet("color: palette(WindowText);")
        layout.addWidget(self._label)

        self._idx = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._next_frame)
        self._timer.setInterval(100)

    def _next_frame(self):
        self._idx = (self._idx + 1) % len(self._FRAMES)
        self._label.setText(self._FRAMES[self._idx])

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()


# ═══════════════════════════════════════════════════════════
# Окно для тестирования
# ═══════════════════════════════════════════════════════════

class TestWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Double Braille Spinner — тест")
        self.resize(720, 520)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(16)

        # ── Верхний ряд: три варианта спиннера ──
        spinners_row = QHBoxLayout()

        # 1) Text-based (основной, со слайдером gap)
        text_group = QGroupBox("Брайль через 2 QLabel (gap=-7), без дёрганья")
        t_layout = QVBoxLayout(text_group)
        self._text = DoubleBrailleSpinner(text_group)
        t_layout.addWidget(self._text)
        spinners_row.addWidget(text_group)

        # 2) Painter-based (для сравнения)
        paint_group = QGroupBox("QPainter (gap=0, неактивные прозрачные)")
        p_layout = QVBoxLayout(paint_group)
        self._painter = PainterBrailleSpinner(paint_group)
        p_layout.addWidget(self._painter)
        spinners_row.addWidget(paint_group)

        # 3) Single (оригинал)
        single_group = QGroupBox("Односимвольный (текущий в ui.py)")
        s_layout = QVBoxLayout(single_group)
        self._single = SingleBrailleSpinner(single_group)
        s_layout.addWidget(self._single)
        spinners_row.addWidget(single_group)

        main_layout.addLayout(spinners_row)

        # ── Слайдер для gap текстового спиннера ──
        slider_label = QLabel("Расстояние между символами Брайля (letter-spacing):")
        slider_label.setStyleSheet("color: palette(WindowText);")
        main_layout.addWidget(slider_label)

        slider_row = QHBoxLayout()
        slider_row.setAlignment(Qt.AlignCenter)

        lbl_min = QLabel("-20")
        lbl_min.setStyleSheet("color: palette(WindowText);")
        slider_row.addWidget(lbl_min)

        self._gap_slider = QSlider(Qt.Horizontal)
        self._gap_slider.setRange(-20, 40)
        self._gap_slider.setValue(-7)
        self._gap_slider.setTickPosition(QSlider.TicksBelow)
        self._gap_slider.setTickInterval(5)
        self._gap_slider.valueChanged.connect(self._on_gap_changed)
        slider_row.addWidget(self._gap_slider)

        lbl_max = QLabel("40px")
        lbl_max.setStyleSheet("color: palette(WindowText);")
        slider_row.addWidget(lbl_max)

        self._gap_label = QLabel("-7px")
        self._gap_label.setStyleSheet("color: palette(WindowText); font-weight: bold;")
        self._gap_label.setFixedWidth(60)
        self._gap_label.setAlignment(Qt.AlignCenter)
        slider_row.addWidget(self._gap_label)

        main_layout.addLayout(slider_row)

        # ── Кнопка показать/спрятать ──
        btn_row = QHBoxLayout()
        btn_row.setAlignment(Qt.AlignCenter)

        btn_hide = QPushButton("Спрятать / Показать все спиннеры")
        btn_hide.clicked.connect(self._toggle_spinners)
        btn_row.addWidget(btn_hide)

        self._status_label = QLabel("Спиннеры активны")
        self._status_label.setStyleSheet("color: palette(WindowText);")
        btn_row.addWidget(self._status_label)

        main_layout.addLayout(btn_row)

        self._visible = True

    def _on_gap_changed(self, value):
        self._text.set_gap(value)
        self._gap_label.setText(f"{value}px")

    def _toggle_spinners(self):
        self._visible = not self._visible
        self._text.setVisible(self._visible)
        self._painter.setVisible(self._visible)
        self._single.setVisible(self._visible)
        self._status_label.setText(
            "Спиннеры активны" if self._visible else "Спиннеры скрыты"
        )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    window = TestWindow()
    window.show()

    sys.exit(app.exec())
