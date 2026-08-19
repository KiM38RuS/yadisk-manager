#!/usr/bin/env python3
"""Тест стилей кнопок тулбара — 5 вариантов с подписями и корректной сменой темы."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QToolBar, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QStyleFactory,
)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QAction, QIcon, QPalette, QColor, QPixmap, QPainter


def _make_test_icon(char, size=36):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.TextAntialiasing)
    p.setPen(Qt.gray)
    p.drawText(pm.rect(), Qt.AlignCenter, char)
    p.end()
    return QIcon(pm)


# Стили тулбаров — каждый со своим QToolBar QSS
TOOLBAR_QSS = {}

TOOLBAR_QSS["Текущий"] = """
QToolBar {
    background: palette(Window); spacing: 2px; left: 0px; margin: 0;
}
QToolButton {
    font-size: 12px; padding: 2px 8px;
    color: palette(WindowText);
    background: transparent;
    border: none;
}
QToolButton:hover {
    background: palette(AlternateBase);
}
QToolButton:pressed {
    background: palette(Midlight);
}
"""

TOOLBAR_QSS["macOS pill"] = """
QToolBar {
    background: palette(Window); spacing: 2px; left: 0px; margin: 0;
}
QToolButton {
    font-size: 12px; padding: 2px 10px;
    color: palette(WindowText);
    background: transparent;
    border: none;
    border-radius: 18px;
}
QToolButton:hover {
    background: rgba(128,128,128,0.15);
}
QToolButton:pressed {
    background: rgba(128,128,128,0.30);
}
"""

TOOLBAR_QSS["VS Code"] = """
QToolBar {
    background: palette(Window); spacing: 2px; left: 0px; margin: 0;
}
QToolButton {
    font-size: 12px; padding: 2px 8px 2px 8px;
    color: palette(WindowText);
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
}
QToolButton:hover {
    background: rgba(128,128,128,0.08);
    border-bottom: 2px solid palette(Highlight);
}
QToolButton:pressed {
    background: rgba(128,128,128,0.15);
    border-bottom: 2px solid palette(Highlight);
}
"""

TOOLBAR_QSS["Material"] = """
QToolBar {
    background: palette(Window); spacing: 2px; left: 0px; margin: 0;
}
QToolButton {
    font-size: 12px; padding: 2px 10px;
    color: palette(WindowText);
    background: transparent;
    border: none;
    border-radius: 4px;
}
QToolButton:hover {
    background: rgba(64,150,255,0.12);
}
QToolButton:pressed {
    background: rgba(64,150,255,0.25);
}
"""

TOOLBAR_QSS["Windows 11"] = """
QToolBar {
    background: palette(Window); spacing: 2px; left: 0px; margin: 0;
}
QToolButton {
    font-size: 12px; padding: 2px 10px;
    color: palette(WindowText);
    background: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
}
QToolButton:hover {
    background: rgba(128,128,128,0.08);
    border: 1px solid rgba(128,128,128,0.25);
}
QToolButton:pressed {
    background: rgba(128,128,128,0.14);
    border: 1px solid rgba(128,128,128,0.35);
}
"""

TOOLBAR_QSS["macOS pill (dark)"] = """
QToolBar {
    background: palette(Window); spacing: 2px; left: 0px; margin: 0;
}
QToolButton {
    font-size: 12px; padding: 2px 10px;
    color: palette(WindowText);
    background: transparent;
    border: none;
    border-radius: 18px;
}
QToolButton:hover {
    background: rgba(128,128,128,0.15);
    border: 1px solid rgba(128,128,128,0.20);
}
QToolButton:pressed {
    background: rgba(128,128,128,0.30);
    border: 1px solid rgba(128,128,128,0.35);
}
"""


class StyleDemo(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Toolbar Styles — наведи курсор и нажми")
        self.resize(950, 620)

        self._widgets_to_refresh = []  # виджеты с palette()-зависимым QSS
        self._toolbar_widgets = []      # (toolbar, qss_template) для переприменения

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(2)
        layout.setContentsMargins(8, 8, 8, 8)

        # Инструкция
        self._instr = QLabel(
            "Наведи на кнопки и нажми — сравни поведение. "
            "Тема меняет всё: фон, текст, QSS пересчитывается.")
        self._instr.setWordWrap(True)
        layout.addWidget(self._instr)
        self._widgets_to_refresh.append(self._instr)

        # Кнопка переключения темы
        theme_row = QHBoxLayout()
        self._theme_btn = QPushButton("Переключить на тёмную тему")
        self._theme_btn.clicked.connect(self._toggle_theme)
        theme_row.addWidget(self._theme_btn)
        theme_row.addStretch()
        layout.addLayout(theme_row)

        # Тулбары с подписями
        for name, qss in TOOLBAR_QSS.items():
            row = QHBoxLayout()
            row.setSpacing(6)

            label = QLabel(name)
            label.setFixedWidth(120)
            label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            label.setStyleSheet("font-size: 12px; font-weight: bold;")
            row.addWidget(label)
            self._widgets_to_refresh.append(label)

            tb = QToolBar(self)
            tb.setMovable(False)
            tb.setIconSize(QSize(28, 28))
            tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            tb.setStyleSheet(qss)
            self._toolbar_widgets.append((tb, qss))

            for i, (btn_label, tip) in enumerate([
                ("Обновить", "F5"),
                ("Копировать", "Ctrl+C"),
                ("Удалить", "Del"),
            ]):
                a = QAction(_make_test_icon(str(i + 1), 28), btn_label, self)
                a.setToolTip(f"{btn_label} ({tip})")
                tb.addAction(a)

            row.addWidget(tb, 1)
            layout.addLayout(row)

        layout.addStretch()
        self._refresh_styles()

    def _is_light(self):
        return self.palette().window().color().lightness() > 128

    def _refresh_styles(self):
        """Переприменить QSS ко всем тулбарам после смены палитры."""
        for tb, qss in self._toolbar_widgets:
            tb.setStyleSheet(qss)
        for w in self._widgets_to_refresh:
            ss = w.styleSheet()
            if ss:
                w.setStyleSheet(ss)  # триггер пересчёта palette()

    def _toggle_theme(self):
        app = QApplication.instance()
        pal = app.palette()
        if self._is_light():
            pal.setColor(QPalette.Window, QColor(30, 30, 30))
            pal.setColor(QPalette.WindowText, QColor(220, 220, 220))
            pal.setColor(QPalette.Base, QColor(42, 42, 42))
            pal.setColor(QPalette.AlternateBase, QColor(50, 50, 50))
            pal.setColor(QPalette.Text, QColor(220, 220, 220))
            pal.setColor(QPalette.Button, QColor(50, 50, 50))
            pal.setColor(QPalette.ButtonText, QColor(220, 220, 220))
            pal.setColor(QPalette.Highlight, QColor(64, 150, 255))
            pal.setColor(QPalette.HighlightedText, QColor(220, 220, 220))
            pal.setColor(QPalette.Mid, QColor(60, 60, 60))
            pal.setColor(QPalette.Midlight, QColor(55, 55, 55))
            pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor(80, 80, 80))
        else:
            pal.setColor(QPalette.Window, QColor(240, 240, 240))
            pal.setColor(QPalette.WindowText, QColor(30, 30, 30))
            pal.setColor(QPalette.Base, QColor(255, 255, 255))
            pal.setColor(QPalette.AlternateBase, QColor(230, 230, 230))
            pal.setColor(QPalette.Text, QColor(30, 30, 30))
            pal.setColor(QPalette.Button, QColor(230, 230, 230))
            pal.setColor(QPalette.ButtonText, QColor(30, 30, 30))
            pal.setColor(QPalette.Highlight, QColor(0, 120, 215))
            pal.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
            pal.setColor(QPalette.Mid, QColor(200, 200, 200))
            pal.setColor(QPalette.Midlight, QColor(220, 220, 220))
            pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor(160, 160, 160))
        app.setPalette(pal)
        self._refresh_styles()
        is_dark = not self._is_light()
        self._theme_btn.setText("Переключить на светлую тему" if is_dark else "Переключить на тёмную тему")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    fusion = QStyleFactory.create('Fusion')
    if fusion:
        app.setStyle(fusion)
    w = StyleDemo()
    w.show()
    sys.exit(app.exec())
