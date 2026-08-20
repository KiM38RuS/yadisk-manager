#!/usr/bin/env python3
"""☁️ YaDisk Manager — Test E: Минимальный старт

Гипотеза: комбинация QSS + трей + вотчер + диск-чек вешают UI.
Проверка: всё отключено — только голое окно с загрузкой данных.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import ui
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPalette

# 1. No QSS
ORIG_THEME = ui.MainWindow._apply_theme
def no_qss_theme(self):
    app = QApplication.instance()
    app.setStyleSheet("")
    app.setPalette(app.style().standardPalette())
    self._update_app_icons()
ui.MainWindow._apply_theme = no_qss_theme

# 2. No tray
ui.MainWindow._init_tray = lambda self: setattr(self, '_tray', None)

# 3. No watcher
ui.MainWindow._init_watcher = lambda self: setattr(self, '_watcher', None)

# 4. No disk info check at startup
import main
main._run_disk_check_threaded = lambda api: None

# 5. No poll timer (start in init but shorten interval)
ORIG_INIT = ui.MainWindow.__init__
def patched_init(self, api, db, cache_dir=''):
    ORIG_INIT(self, api, db, cache_dir)
    # Stop the poll timer, keep window responsive
    self._poll_timer.stop()
ui.MainWindow.__init__ = patched_init

main.main()
