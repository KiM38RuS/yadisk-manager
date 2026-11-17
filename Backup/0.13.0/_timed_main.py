
import sys, os, time
sys.path.insert(0, '.')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

real_init_ui = None
import ui
orig_init = ui.MainWindow.__init__
def traced_init(self, api, db, cache_dir=''):
    t0 = time.monotonic()
    print(f'TIMER: MainWindow.__init__ start t={t0:.3f}')
    orig_init(self, api, db, cache_dir)
    t1 = time.monotonic()
    print(f'TIMER: MainWindow.__init__ done in {t1-t0:.3f}s')
ui.MainWindow.__init__ = traced_init

import main
# Override _run_disk_check_threaded to add timing
def timed_disk_check(api):
    t0 = time.monotonic()
    print(f'TIMER: _run_disk_check_threaded started t={t0:.3f}')
    main._run_disk_check_threaded(api)
main._run_disk_check_threaded = timed_disk_check

main.main()
