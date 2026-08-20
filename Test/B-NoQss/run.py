#!/usr/bin/env python3
"""☁️ YaDisk Manager — Test B: No QSS stylesheet (palette only)

Гипотеза: тяжёлый тёмный QSS (dark_qss) на старом Xeon вызывает зависание.
Проверка: отключаем app.setStyleSheet() — оставляем только QPalette.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

# Patch _apply_theme to NEVER apply QSS
import ui

ORIG_APPLY_THEME = ui.MainWindow._apply_theme
def patched_apply_theme(self):
    """Только QPalette, без QSS."""
    from PySide6.QtGui import QPalette, QColor
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    app.setStyleSheet("")  # Clear any previous QSS
    theme = __import__('db').get_theme()
    if theme == "dark":
        dark_bg = QColor(0x2D, 0x2D, 0x2D)
        dark_surface = QColor(0x38, 0x38, 0x38)
        dark_text = QColor(0xE0, 0xE0, 0xE0)
        dark_highlight = QColor(0x00, 0x78, 0xD7)
        palette = QPalette()
        palette.setColor(QPalette.Window, dark_bg)
        palette.setColor(QPalette.WindowText, dark_text)
        palette.setColor(QPalette.Base, dark_surface)
        palette.setColor(QPalette.AlternateBase, QColor(0x42, 0x42, 0x42))
        palette.setColor(QPalette.ToolTipBase, QColor(0x50, 0x50, 0x50))
        palette.setColor(QPalette.ToolTipText, dark_text)
        palette.setColor(QPalette.Text, dark_text)
        palette.setColor(QPalette.Button, dark_bg)
        palette.setColor(QPalette.ButtonText, dark_text)
        palette.setColor(QPalette.BrightText, QColor(0xFF, 0x00, 0x00))
        palette.setColor(QPalette.Highlight, dark_highlight)
        palette.setColor(QPalette.HighlightedText, QColor(0xFF, 0xFF, 0xFF))
        palette.setColor(QPalette.Link, QColor(0x00, 0xA0, 0xFF))
        palette.setColor(QPalette.LinkVisited, QColor(0x80, 0x60, 0xFF))
        app.setPalette(palette)
    elif theme == "light" or not ui.MainWindow._is_windows_dark_mode():
        app.setPalette(app.style().standardPalette())
    else:
        # system dark — just palette, no QSS
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor(0x2D, 0x2D, 0x2D))
        palette.setColor(QPalette.WindowText, QColor(0xE0, 0xE0, 0xE0))
        app.setPalette(palette)
    self._update_app_icons()

ui.MainWindow._apply_theme = patched_apply_theme

import main
main.main()
