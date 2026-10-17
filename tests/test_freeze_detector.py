"""Регрессия: монитор зависаний не должен ронять процесс.

v0.12.2 на чужой машине умирала нативным исключением (0xc0000005) сразу
после строки «⚠️ FREEZE: UI blocked for ~578 ms» — монитор собирал стек
главного потока через `sys._current_frames()` + `traceback.format_stack()`
в тот момент, когда главный поток держал GIL (там шла сортировка таблицы
+ запрос в SQLite). Теперь стек всех потоков пишет `faulthandler`
(C-уровень, не строит Python-объекты чужого потока), а сообщение
логируется один раз за эпизод зависания.

Монитор вызывается как обычная функция с подставной «оболочкой окна»:
поднимать настоящий MainWindow нельзя — его фоновые потоки текут между
тестами (см. `isolate_real_app_state` в conftest).
"""

import logging
import sys
import threading
import time
from collections import deque

import ui


class _FakeWindow:
    """Заглушка MainWindow: монитору нужны только heartbeat и флаг."""

    def __init__(self, heartbeat_ages_ms, checks=None):
        self._ages = deque(heartbeat_ages_ms)
        self._checks = len(self._ages) if checks is None else checks

    @property
    def _heartbeat_ts(self):
        return time.monotonic() - self._ages.popleft() / 1000.0

    @property
    def _freeze_thread_running(self):
        self._checks -= 1
        return self._checks >= 0


def _record_has(record, *markers) -> bool:
    """Маркер есть в сообщении (в любом языке)."""
    msg = record.getMessage()
    return any(m in msg for m in markers)


def _freeze_records(caplog):
    """WARNING'и монитора зависаний.

    l10n-форматтер, установленный другими тестами, подменяет msg на русский
    вариант — поэтому ищем маркер в любом из двух языков.
    """
    return [r for r in caplog.records if _record_is_freeze(r)]


def _ended_records(caplog):
    return [r for r in caplog.records
            if r.levelno == logging.INFO
            and _record_has(r, "FREEZE ENDED", "ЗАВИСАНИЕ ЗАКОНЧИЛОСЬ")]


def _record_is_freeze(record) -> bool:
    return (record.levelno == logging.WARNING and record.name == "ui"
            and _record_has(record, "UI blocked", "заблокирован"))


def test_freeze_logs_once_per_episode_and_dumps_all_threads(
        monkeypatch, tmp_path, caplog):
    """Два замера >500 мс внутри одного зависания → одно сообщение + дамп."""
    dump = tmp_path / "freeze.log"
    monkeypatch.setattr(ui, "_freeze_dump_path", lambda: str(dump))

    stop = threading.Event()

    def _spin():
        stop.wait(5)

    worker = threading.Thread(target=_spin, name="ydm-dump-test", daemon=True)
    worker.start()
    try:
        with caplog.at_level(logging.WARNING, logger="ui"):
            # 5000, 5000 — всё ещё зависло; 0 — интерфейс ожил.
            ui.MainWindow._freeze_monitor(_FakeWindow([5000, 5000, 0]))
    finally:
        stop.set()
        worker.join(timeout=5)

    assert len(_freeze_records(caplog)) == 1, "одно сообщение на эпизод"
    text = dump.read_text(encoding="utf-8")
    assert "FREEZE dump" in text
    # all_threads=True: в дампе есть и посторонний поток теста.
    assert "_spin" in text


def test_freeze_recovery_is_reported_once(monkeypatch, tmp_path, caplog):
    dump = tmp_path / "freeze.log"
    monkeypatch.setattr(ui, "_freeze_dump_path", lambda: str(dump))

    with caplog.at_level(logging.INFO, logger="ui"):
        ui.MainWindow._freeze_monitor(_FakeWindow([5000, 5000, 0]))

    ended = _ended_records(caplog)
    assert len(ended) == 1
    assert len(_freeze_records(caplog)) == 1


def test_monitor_never_walks_foreign_frames(monkeypatch, tmp_path):
    """Старый код звал sys._current_frames() — источник нативного крэша."""
    dump = tmp_path / "freeze.log"
    monkeypatch.setattr(ui, "_freeze_dump_path", lambda: str(dump))

    def _boom():
        raise AssertionError(
            "sys._current_frames() не должен вызываться: "
            "именно он ронял процесс (0xc0000005)")

    monkeypatch.setattr(sys, "_current_frames", _boom)
    try:
        ui.MainWindow._freeze_monitor(_FakeWindow([5000]))
    finally:
        monkeypatch.undo()  # вернуть sys._current_frames сразу же

    assert dump.exists()


def test_no_freeze_no_dump(monkeypatch, tmp_path, caplog):
    dump = tmp_path / "freeze.log"
    monkeypatch.setattr(ui, "_freeze_dump_path", lambda: str(dump))

    with caplog.at_level(logging.WARNING, logger="ui"):
        ui.MainWindow._freeze_monitor(_FakeWindow([0]))

    assert not _freeze_records(caplog)
    assert not dump.exists()


def test_dump_failure_is_not_fatal(monkeypatch, tmp_path, caplog):
    """Недоступный путь к дампу не должен валить монитор (и весь GUI)."""
    blocker = tmp_path / "blocked"
    blocker.write_text("занято", encoding="utf-8")
    monkeypatch.setattr(ui, "_freeze_dump_path",
                        lambda: str(blocker / "freeze.log"))

    with caplog.at_level(logging.WARNING, logger="ui"):
        ui.MainWindow._freeze_monitor(_FakeWindow([5000]))

    assert len(_freeze_records(caplog)) == 1  # предупреждение всё равно есть
