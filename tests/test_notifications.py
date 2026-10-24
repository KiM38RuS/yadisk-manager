"""PLANS 4.5 — уведомления Windows после скачивания/загрузки.

Одно уведомление на пачку (bulk-очередь не должна спамить трей
сотней сообщений), отложенный показ через 1.5 с после последнего файла.
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


# ── текст уведомления (чистая функция) ────────────────────────────

def test_single_file_notification_text():
    from ui_shared import build_transfer_notification
    assert build_transfer_notification(["отчёт.docx"], "download") == \
        ("Скачано", "отчёт.docx")
    assert build_transfer_notification(["отчёт.docx"], "upload") == \
        ("Загружено", "отчёт.docx")


def test_bulk_notification_text_is_aggregated():
    from ui_shared import build_transfer_notification
    title, body = build_transfer_notification(
        ["a.txt", "b.txt", "c.txt", "d.txt"], "download")
    assert title == "Скачано"
    assert body == "4 файла: a.txt, b.txt, c.txt…"


def test_bulk_two_files_plural():
    from ui_shared import build_transfer_notification
    assert build_transfer_notification(["a", "b"], "upload")[1] == "2 файла: a, b"


# ── очередь уведомлений в MainWindow ──────────────────────────────

def test_schedule_accumulates_and_flushes_once(qapp):
    import ui as ui_mod

    mw = MagicMock()
    mw._pending_download_notifs = []
    mw._pending_upload_notifs = []

    ui_mod.MainWindow._schedule_transfer_notification(
        mw, "/Документы/отчёт.docx", "download")
    ui_mod.MainWindow._schedule_transfer_notification(
        mw, "/Документы/смета.xlsx", "download")
    mw._notif_timer.start.assert_called_with(1500)

    ui_mod.MainWindow._flush_transfer_notifications(mw)

    mw._notify.assert_called_once_with(
        "Скачано", "2 файла: отчёт.docx, смета.xlsx")
    assert mw._pending_download_notifs == []
    # Пустой второй вызов ничего не показывает
    mw._notify.reset_mock()
    ui_mod.MainWindow._flush_transfer_notifications(mw)
    mw._notify.assert_not_called()


def test_download_and_upload_are_reported_separately(qapp):
    import ui as ui_mod

    mw = MagicMock()
    mw._pending_download_notifs = ["скачан.txt"]
    mw._pending_upload_notifs = ["загружен.txt"]

    ui_mod.MainWindow._flush_transfer_notifications(mw)

    titles = [c.args[0] for c in mw._notify.call_args_list]
    assert titles == ["Скачано", "Загружено"]


def test_notify_respects_disabled_state_and_missing_tray(qapp):
    """Отключённые уведомления и отсутствие трея — молча ничего не делаем."""
    import ui as ui_mod

    mw = MagicMock()
    mw._tray = None
    ui_mod.MainWindow._notify(mw, "Скачано", "a.txt")   # трея нет — не падаем
    # showMessage не должен быть вызван ни у чего
    assert not mw.method_calls  # mw сам не трогали в недопустимом виде

    mw2 = MagicMock()
    ui_mod.MainWindow._notify(mw2, "Скачано", "a.txt")
    mw2._tray.showMessage.assert_called_once()
