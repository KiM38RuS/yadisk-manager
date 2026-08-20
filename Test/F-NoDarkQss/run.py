#!/usr/bin/env python3
"""☁️ YaDisk Manager — Test F: No dark QSS (palette only)

Единственное отличие от оригинала: _dark_qss() возвращает пустую строку.
QPalette, иконки, статусы, трей, вотчер — всё как в текущей версии.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import ui
# Переопределяем _dark_qss — возвращает пустую строку.
# Это единственная правка, всё остальное без изменений.
ui.MainWindow._dark_qss = staticmethod(lambda: "")

import main
main.main()
