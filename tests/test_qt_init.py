"""Test that the Qt GUI can initialize in offscreen mode."""
import os
import sys
import logging

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["DISPLAY"] = ""

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)
print("QApplication created OK", flush=True)

import db
import disk_api
from _version import VERSION
from ui import MainWindow

token = db.get_token()
api = disk_api.YaDiskAPI(token)
database = db.Database()
cache_dir = db.get_cache_dir()

window = MainWindow(api, database, cache_dir)
window.show()

# Process events briefly
for _ in range(5):
    app.processEvents()

print("MainWindow created and shown OK", flush=True)
print("Window visible:", window.isVisible(), flush=True)

# Test IPC server init
import ipc as _ipc
if _ipc.IPC_ENABLED:
    _ipc.start_ipc_server(window)
    print("IPC server started on port", _ipc.IPC_PORT, flush=True)

# Schedule quit
QTimer.singleShot(500, app.quit)
app.exec()

print("=== Qt GUI init PASSED ===", flush=True)
