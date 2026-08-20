#!/usr/bin/env python3
"""☁️ YaDisk Manager — Test C: No tray icon

Гипотеза: QSystemTrayIcon.show() на этой системе может вешать UI.
Проверка: пропускаем _init_tray().
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import ui
ui.MainWindow._init_tray = lambda self: setattr(self, '_tray', None)

import main
main.main()
