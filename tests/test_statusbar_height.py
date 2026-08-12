"""Регрессия: спиннер статус-бара не должен раздувать высоту панели.

История: брайлевский спиннер (fill_parent=False, font_size=14) имел
sizeHint() = lh + 20 ≈ 46 px, а QStatusBar учитывает скрытые виджеты
в своём sizeHint → панель стала 51 px вместо ~22 px. Заменён на
WaveSpinner (обычный QLabel из глифов Брайля ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏) —
тест фиксирует это свойство.
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout,
    QLabel, QProgressBar,
)

from ui_wave_spinner import WaveSpinner


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _build_statusbar():
    """Статус-бар как в MainWindow._init_ui:
    спиннер+надпись слева (перезагрузка/выключение),
    справа — индикатор загрузки, «Показать лог», слот прогресс-бара.
    """
    win = QMainWindow()
    sb = win.statusBar()

    spinner = WaveSpinner()
    spinner.hide()
    sb.insertWidget(0, spinner)

    label = QLabel("")
    sb.addWidget(label, 1)

    right = QWidget()
    layout = QHBoxLayout(right)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)

    loading_spinner = WaveSpinner()
    loading_spinner.hide()
    layout.addWidget(loading_spinner)

    loading_label = QLabel("")
    loading_label.hide()
    layout.addWidget(loading_label)

    log_label = QLabel("Показать лог")
    layout.addWidget(log_label)

    # Слот прогресс-бара фиксированной ширины — всегда в раскладке
    slot = QWidget()
    slot.setFixedWidth(200)
    slot_layout = QHBoxLayout(slot)
    slot_layout.setContentsMargins(0, 0, 0, 0)
    slot_layout.setSpacing(0)
    progress = QProgressBar()
    progress.setVisible(False)
    slot_layout.addWidget(progress)
    layout.addWidget(slot)

    sb.addPermanentWidget(right)

    win.show()
    return win, sb, spinner, label, loading_spinner, loading_label, log_label


def test_statusbar_height_not_inflated_by_spinner(qapp):
    win, sb, spinner, label, loading_spinner, loading_label, log_label = _build_statusbar()
    try:
        # 51 px — был баг со старым спиннером; теперь панель ~22 px.
        # Порог 45 покрывает и повышенный DPI (~1.5x).
        assert sb.sizeHint().height() < 45
        assert sb.height() < 45

        # Индикатор загрузки (спиннер + надпись) тоже не раздувает панель
        loading_spinner.show()
        loading_label.setText("Загрузка файлов...")
        loading_label.show()
        qapp.processEvents()
        assert sb.height() < 45
    finally:
        win.close()


def test_status_spinner_shows_and_hides(qapp):
    win, sb, spinner, label, loading_spinner, loading_label, log_label = _build_statusbar()
    try:
        spinner.show()
        qapp.processEvents()
        assert spinner.isVisible()
        assert spinner.text()  # есть кадр

        spinner.hide()
        qapp.processEvents()
        assert not spinner.isVisible()
    finally:
        win.close()


def test_loading_indicator_right_zone(qapp):
    """Индикатор загрузки живёт в правой зоне (правее левой надписи),
    скрыт по умолчанию и переключается независимо от левого спиннера.
    """
    win, sb, spinner, label, loading_spinner, loading_label, log_label = _build_statusbar()
    try:
        # Скрыт по умолчанию
        assert not loading_spinner.isVisible()
        assert not loading_label.isVisible()

        # Показ индикатора загрузки
        loading_spinner.show()
        loading_label.setText("Загрузка файлов...")
        loading_label.show()
        qapp.processEvents()

        assert loading_spinner.isVisible()
        assert loading_label.isVisible()
        assert loading_label.text() == "Загрузка файлов..."

        # Правая зона правее левой (spinner + надпись слева не пересекаются)
        right = loading_label.parentWidget()
        assert right.geometry().left() >= label.geometry().right() or spinner.isVisible()

        # Скрытие индикатора — левый спиннер не тронут
        loading_spinner.hide()
        loading_label.hide()
        qapp.processEvents()
        assert not loading_spinner.isVisible()
        assert not loading_label.isVisible()
    finally:
        win.close()


def test_progress_slot_reserves_space(qapp):
    """Слот прогресс-бара фиксированной ширины: появление и скрытие бара
    не сдвигает надписи (индикатор загрузки, «Показать лог»).
    """
    win, sb, spinner, label, loading_spinner, loading_label, log_label = _build_statusbar()
    try:
        # Показываем индикатор загрузки, чтобы был «сдвигаемый» контент
        loading_spinner.show()
        loading_label.setText("Загрузка файлов...")
        loading_label.show()
        qapp.processEvents()

        left_before = (loading_label.geometry().left(),
                       log_label.geometry().left())

        # Слот зарезервирован даже при скрытом баре
        slot = loading_label.parentWidget().layout().itemAt(3).widget()
        assert slot.width() == 200

        progress = sb.findChild(QProgressBar)
        progress.setVisible(True)
        qapp.processEvents()

        assert (loading_label.geometry().left(),
                log_label.geometry().left()) == left_before

        progress.setVisible(False)
        qapp.processEvents()
        assert (loading_label.geometry().left(),
                log_label.geometry().left()) == left_before
    finally:
        win.close()


def test_left_busy_with_right_zone(qapp):
    """Левая зона долгой операции (спиннер + текст) и правая зона
    (индикатор загрузки) работают одновременно, не раздувая панель.
    Скрытие busy не трогает правую зону.
    """
    win, sb, spinner, label, loading_spinner, loading_label, log_label = _build_statusbar()
    try:
        # Левая busy-операция: спиннер + текст
        spinner.show()
        label.setText("🔍 Поиск: файлы…")
        qapp.processEvents()
        assert spinner.isVisible()
        assert label.text() == "🔍 Поиск: файлы…"
        assert sb.height() < 45

        # Правая зона в это же время: индикатор загрузки
        loading_spinner.show()
        loading_label.setText("Загрузка файлов...")
        loading_label.show()
        qapp.processEvents()
        assert loading_spinner.isVisible()
        assert loading_label.isVisible()
        assert sb.height() < 45

        # Скрытие левой busy — правая зона не тронута
        spinner.hide()
        label.setText("")
        qapp.processEvents()
        assert loading_spinner.isVisible()
        assert loading_label.isVisible()
    finally:
        win.close()
