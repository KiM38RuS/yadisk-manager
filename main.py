#!/usr/bin/env python3
"""
☁️ YaDisk Manager — клиент Яндекс.Диска с on-demand синхронизацией.

Файлы видны в облаке, скачиваются по требованию (двойной клик).
Изменения отслеживаются → авто-загрузка в облако.
Системный трей, автозапуск, прогресс, разрешение конфликтов.
"""

import os
import sys
import logging
import traceback
import warnings
from logging.handlers import RotatingFileHandler
from typing import Optional

from PySide6.QtCore import QTimer, QThread, Signal, qInstallMessageHandler
from PySide6.QtWidgets import (
    QApplication, QMessageBox, QFileDialog,
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton,
    QCheckBox, QDialogButtonBox, QStyleFactory,
)

import sync  # noqa: E402  (sync логика)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# ── Файловый лог — критично для exe без консоли ───────────
# Ротация: 1 МБ × 3 бэкапа → максимум ~4 МБ, не растёт бесконечно
try:
    _log_dir = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")), "yadisk-client"
    )
    os.makedirs(_log_dir, exist_ok=True)
    _fh = RotatingFileHandler(
        os.path.join(_log_dir, "yadisk.log"),
        maxBytes=1_000_000,   # 1 МБ на файл
        backupCount=3,        # yadisk.log + .1/.2/.3
        encoding="utf-8",
    )
    _fh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")
    )
    logging.getLogger().addHandler(_fh)
except Exception:
    pass  # файловый лог — бонус, не блокируем запуск
logger = logging.getLogger("main")
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

# ── Перехват всех «невидимых» ошибок в лог ────────────────

def _log_excepthook(etype, value, tb):
    """Необработанные исключения — в лог через logging.critical."""
    msg = "".join(traceback.format_exception(etype, value, tb))
    logging.getLogger("unhandled").critical(msg)

def _qt_message_handler(mode, context, message):
    """Qt C++ warning/error.

    Пишем в stderr, а НЕ через logging: LogHandler трогает Qt-виджеты окна
    лога (_append_log → appendHtml), а _qt_message_handler вызывается ИЗ
    C++-кода Qt (например, из парсера QSS при setStyleSheet). Синхронный
    вход в Qt-виджеты из такого колбэка = реентерабельность →
    segfault / STATUS_HEAP_CORRUPTION.
    """
    # QtMsgType: 0=Debug, 1=Warning, 2=Critical, 3=Fatal, 4=Info
    prefix = {0: "Qt debug", 1: "Qt warning", 2: "Qt critical",
              3: "Qt fatal", 4: "Qt info"}.get(mode, "Qt")
    print(f"{prefix}: {message}", file=sys.stderr)


def main():
    # ── Single-instance guard ─────────────────────────────────
    # Если IPC-порт уже отвечает — работает другой экземпляр. Поднимаем его
    # окно и выходим: два экземпляра пишут в один config.json (WinError 32 на
    # os.replace), в одну SQLite и смотрят один кеш двумя watcher'ами.
    import time as _time
    import ipc as _ipc_guard
    is_restart = "--restart" in sys.argv
    if _ipc_guard.IPC_ENABLED:
        # Обычный запуск: 'raise' — поднять окно уже работающего экземпляра.
        # Рестарт: 'ping' — просто проверить живость, окно закрывается, дёргать
        # его не нужно.
        probe = 'ping' if is_restart else 'raise'
        resp = _ipc_guard.send_ipc_command(probe)
        if resp.get('status') == 'ok':
            if not is_restart:
                print("YaDisk Manager уже запущен — окно поднято, второй экземпляр не стартует.")
                return
            # restart: старый экземпляр ещё закрывается — ждём, пока порт
            # освободится (до 10 с), затем стартуем вместо выхода. Зонд 'ping'
            # — без побочных эффектов (в отличие от 'raise', который дёргал
            # окно закрывающегося экземпляра и засорял лог).
            for _ in range(20):
                _time.sleep(0.5)
                resp = _ipc_guard.send_ipc_command('ping')
                if resp.get('status') != 'ok':
                    break  # порт свободен — можно запускаться
        elif not is_restart:
            print("Warning: IPC не отвечает, но порт занят — запуск продолжается.", file=sys.stderr)

    # ЕДИНСТВЕННЫЙ QApplication на всё время жизни
    app = QApplication(sys.argv)
    app.setApplicationName("YaDisk Manager")
    app.setOrganizationName("YaDiskManager")
    app.setQuitOnLastWindowClosed(False)  # трей остаётся жить

    # ── Перехват «невидимых» ошибок ─────────────────────────
    sys.excepthook = _log_excepthook
    logging.captureWarnings(True)
    qInstallMessageHandler(_qt_message_handler)

    # Fusion style обязателен: с ним QPalette работает для всех виджетов
    # (WindowsVistaStyle игнорирует QPalette для меню, дерева, таблицы,
    # скроллбаров, хедеров — пришлось бы чинить тормозным QSS на старом CPU)
    fusion = QStyleFactory.create('Fusion')
    if fusion:
        app.setStyle(fusion)

    # Отключаем анимацию выпадающих списков (popup QComboBox): при live-смене
    # темы repolish всех виджетов в момент анимированного закрытия popup
    # повреждает кучу Qt (STATUS_HEAP_CORRUPTION, 0xc0000374).
    from PySide6.QtCore import Qt as _Qt
    app.setEffectEnabled(_Qt.UI_AnimateCombo, False)

    import disk_api
    import db
    import ui
    from _version import VERSION
    token = db.get_token()
    if not token:
        auth = ui.AuthDialog()
        if auth.exec() != ui.AuthDialog.Accepted:
            return
        token = auth.token
        db.set_token(token)

    # ── инициализация API (без блокирующих вызовов) ───────
    api = disk_api.YaDiskAPI(
        token, net_heal_enabled_fn=lambda: db.get_net_heal_enabled() is True)
    database = db.Database()

    # ── выбор папки кеша ───────────────────────────────────
    cache_dir = db.get_cache_dir()
    if not cache_dir:
        cache_dir = _choose_cache_dir()
        if cache_dir is None:
            return  # пользователь отменил
        db.set_cache_dir(cache_dir)
        logger.info("Cache dir set: %s", cache_dir)

    # ── GUI ────────────────────────────────────────────────
    window = ui.MainWindow(api, database, cache_dir)
    # Бесшумный запуск (--silent): только иконка в трее, без окна.
    # Автозапуск использует этот режим. Клик по трею покажет окно.
    is_silent = "--silent" in sys.argv
    if not is_silent:
        window.show()
    else:
        logger.info("Silent mode: window hidden, tray only")

    # ── IPC (управление из терминала) ──────────────────────
    import ipc as _ipc
    if _ipc.IPC_ENABLED:
        _ipc.start_ipc_server(window)
        _ipc_timer = QTimer(window)
        _ipc_timer.timeout.connect(lambda: _process_ipc_commands(window))
        _ipc_timer.start(200)

    # ── асинхронная проверка диска ─────────────────────────
    QTimer.singleShot(200, lambda: _run_disk_check_threaded(api))

    sys.exit(app.exec())


def _process_ipc_commands(window):
    """Проверить очередь IPC и выполнить команду (из главного потока Qt)."""
    import ipc as _ipc
    cmd = _ipc.poll_command()
    if cmd == "restart":
        logger.info("IPC: restart requested")
        window._restart_app()
    elif cmd == "ping":
        # Зонд живости (используется single-instance guard при --restart).
        # Сервер уже ответил {"status":"ok"} — здесь просто нечего делать.
        logger.debug("IPC: ping")
    elif cmd == "raise":
        logger.info("IPC: raise window requested")
        window.showNormal()
        window.raise_()
        window.activateWindow()
    elif cmd == "shutdown":
        logger.info("IPC: shutdown requested")
        # Асинхронный выход — не блокируем event loop из таймер-коллбека
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        QTimer.singleShot(0, lambda: _force_shutdown(window))


def _force_shutdown(window):
    """Финальная стадия shutdown: завершить потоки и выйти.

    Вызывается из QTimer.singleShot(0), то есть вне IPC-таймера.
    Даём каждому потоку 100ms на quit() — достаточно, чтобы избежать
    "QThread: Destroyed while thread is still running", но не настолько
    много, чтобы вызвать многосекундный FREEZE.
    """
    from PySide6.QtWidgets import QApplication
    if hasattr(window, '_active_threads'):
        for t in window._active_threads[:]:
            if t.isRunning():
                t.quit()
                t.requestInterruption()
                if not t.wait(100):
                    t.terminate()
                    t.wait(50)
    QApplication.instance().quit()


def _run_disk_check_threaded(api):
    """Проверить API Яндекс.Диска в фоновом потоке (не блокирует UI)."""
    class _DiskInfoThread(QThread):
        disk_info_ready = Signal(dict, str)  # info, error
        def __init__(self, api):
            super().__init__()
            self._api = api
        def run(self):
            try:
                info = self._api.get_disk_info()
                self.disk_info_ready.emit(info, "")
            except Exception as e:
                self.disk_info_ready.emit({}, str(e))

    thread = _DiskInfoThread(api)
    thread.disk_info_ready.connect(lambda info, err: _on_disk_info_done(info, err))
    thread.disk_info_ready.connect(thread.deleteLater)
    # Keep a reference so GC doesn't collect the QThread while it's running
    thread._self_ref = thread
    thread.start()


def _on_disk_info_done(info: dict, error: str):
    if error:
        logger.error("Disk API check failed: %s", error)
        return
    total = info.get("total_space", 0)
    used = info.get("used_space", 0)
    logger.info(
        "Connected. Total: %.1f GB, Used: %.1f GB, Free: %.1f GB",
        total / 1e9, used / 1e9, (total - used) / 1e9,
    )


def _choose_cache_dir() -> Optional[str]:
    """Диалог выбора папки с опцией подпапки Yandex.Disk."""
    dlg = QDialog()
    dlg.setWindowTitle("Выбор папки для файлов")
    dlg.setMinimumSize(500, 180)
    layout = QVBoxLayout(dlg)

    layout.addWidget(QLabel("Выберите папку для скачивания файлов:"))

    path_layout = QHBoxLayout()
    path_edit = QLineEdit(os.path.expanduser("~"))
    browse_btn = QPushButton("Обзор…")
    path_layout.addWidget(path_edit, 1)
    path_layout.addWidget(browse_btn)
    layout.addLayout(path_layout)

    subfolder_cb = QCheckBox('Создать подпапку "Yandex.Disk"')
    subfolder_cb.setChecked(True)
    layout.addWidget(subfolder_cb)

    btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    btn_box.accepted.connect(dlg.accept)
    btn_box.rejected.connect(dlg.reject)
    layout.addWidget(btn_box)

    def browse():
        chosen = QFileDialog.getExistingDirectory(dlg, "Выберите папку",
                                                   path_edit.text())
        if chosen:
            path_edit.setText(chosen)

    browse_btn.clicked.connect(browse)

    if dlg.exec() != QDialog.Accepted:
        return None

    base_dir = path_edit.text().strip()
    if not base_dir:
        base_dir = os.path.expanduser("~")

    if subfolder_cb.isChecked():
        result = os.path.join(base_dir, "Yandex.Disk")
    else:
        result = base_dir

    os.makedirs(result, exist_ok=True)
    return result


if __name__ == "__main__":
    main()
