#!/usr/bin/env python3
"""☁️ YaDisk Manager — Test D: No file watcher

Гипотеза: watchdog.Observer с recursive=True сканирует кеш-папку и вешает UI.
Проверка: пропускаем _init_watcher().
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import ui
ui.MainWindow._init_watcher = lambda self: setattr(self, '_watcher', None)

import main
main.main()
