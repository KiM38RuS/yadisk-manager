"""Трей/заголовок: имя, версия и статус с бейджем на иконке.

Ожидаемое поведение (инбокс PLANS):
* заголовок окна: «ЯДиск Менеджер <версия>»;
* тултип трея: три строки — имя, версия, «Статус: ...»;
* бейдж в правом нижнем углу иконки трея во всех состояниях, кроме готовности.
"""

import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _window(online=True, syncing=frozenset()):
    import ui as ui_mod

    mw = MagicMock()
    mw._tray = MagicMock()
    mw._online = online
    mw._syncing = set(syncing)
    mw._active_uploads = 0
    mw._active_downloads = 0
    mw._upload_queue = []
    mw._download_queue = []
    mw._bulk_sync_running = False
    mw._pending_conflicts = []
    mw._app_status_current = None
    # MagicMock подменяет методы-заглушки — проверяем реальную логику
    mw._app_status = lambda: ui_mod.MainWindow._app_status(mw)
    return mw


def _tooltip(mw):
    return mw._tray.setToolTip.call_args.args[0]


def test_title_and_tooltip_have_russian_name_and_version(qapp):
    from ui import MainWindow
    from _version import VERSION

    mw = _window()
    MainWindow._refresh_app_status(mw)

    mw.setWindowTitle.assert_called_with(f"ЯДиск Менеджер {VERSION}")
    tip = _tooltip(mw)
    assert tip.splitlines()[:2] == ["ЯДиск Менеджер", f"Версия: {VERSION}"]
    assert tip.splitlines()[2] == "Статус: готово"


def test_syncing_status_text(qapp):
    from ui import MainWindow

    mw = _window(syncing={"/a.txt"})
    MainWindow._refresh_app_status(mw)
    assert _tooltip(mw).splitlines()[2] == "Статус: синхронизация"


def test_conflict_status_text(qapp):
    from ui import MainWindow

    mw = _window()
    mw._pending_conflicts = [{"cloud_path": "/a.txt"}]
    MainWindow._refresh_app_status(mw)
    assert _tooltip(mw).splitlines()[2] == "Статус: ошибка синхронизации"


def test_offline_status_wins_over_syncing(qapp):
    from ui import MainWindow

    mw = _window(online=False, syncing={"/a.txt"})
    MainWindow._refresh_app_status(mw)
    assert _tooltip(mw).splitlines()[2] == "Статус: нет соединения"


def test_status_change_updates_tooltip_and_icon(qapp):
    """Повторный вызов с тем же статусом не пересоздаёт иконку (защита от утечки)."""
    from ui import MainWindow

    mw = _window()
    MainWindow._refresh_app_status(mw)
    calls_after_ready = mw._tray.setIcon.call_count
    assert calls_after_ready == 1

    mw._bulk_sync_running = True
    MainWindow._refresh_app_status(mw)
    assert calls_after_ready + 1 == mw._tray.setIcon.call_count
    assert _tooltip(mw).splitlines()[2] == "Статус: синхронизация"

    MainWindow._refresh_app_status(mw)  # статус не изменился
    assert mw._tray.setIcon.call_count == calls_after_ready + 1


def test_tray_icon_has_badge_only_for_non_ready_states(qapp):
    from ui import MainWindow

    ready = MainWindow._pick_tray_icon("ready").pixmap(32, 32).toImage()
    syncing = MainWindow._pick_tray_icon("syncing").pixmap(32, 32).toImage()
    offline = MainWindow._pick_tray_icon("offline").pixmap(32, 32).toImage()

    # левый верхний угол — базовая иконка, не тронутая бейджем
    assert ready.pixelColor(2, 2) == syncing.pixelColor(2, 2)
    assert ready.pixelColor(2, 2) == offline.pixelColor(2, 2)
    conflict = MainWindow._pick_tray_icon("conflict").pixmap(32, 32).toImage()

    # правый нижний угол (бейдж 1/2 размера) — меняется и у всех статусов
    assert ready.pixelColor(24, 24) != syncing.pixelColor(24, 24)
    assert ready.pixelColor(24, 24) != offline.pixelColor(24, 24)
    assert ready.pixelColor(24, 24) != conflict.pixelColor(24, 24)
    assert syncing.pixelColor(24, 24) != conflict.pixelColor(24, 24)
    assert offline.pixelColor(24, 24) != conflict.pixelColor(24, 24)

    # (20, 20) был вне старого бейджа 8×8 — рост до 16×16 должен менять и его
    assert ready.pixelColor(20, 20) != syncing.pixelColor(20, 20)
