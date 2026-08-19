"""Тестовая копия интерфейса YaDisk Manager — два спиннера + слайдер gap.

Воспроизводит точную раскладку ui.py:
  - Левая панель: QTreeView + _BrailleSpinner (overlay, fill_parent=True)
  - Правая панель: QTableView + Loading-страница со спиннером (fill_parent=False)
  - Слайдер под панелями управляет gap в ОБОИХ спиннерах одновременно

Запуск:
    cd D:/Program_files/YaDiskManager
    python tests/test_ui_spinner_layout.pyw
"""

import sys
import os

# Корень проекта для импорта ui.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTreeView, QTableView, QHeaderView, QLabel,
    QAbstractItemView, QStackedWidget, QSlider, QGroupBox,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QStandardItemModel, QStandardItem

# Импортируем production-спиннер
from ui import _BrailleSpinner


# ═══════════════════════════════════════════════════════════
# Тестовое окно — копия раскладки YaDisk Manager
# ═══════════════════════════════════════════════════════════

class TestLayoutWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YaDisk Manager — тест спиннеров")
        self.resize(800, 500)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(8)

        # ── Splitter: древо | контент ──────────────────────
        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter, 1)  # stretch=1

        # ── Левая панель: древо + спиннер (overlay) ────────
        self._tree_container = QWidget()
        self._tree_layout = QVBoxLayout(self._tree_container)
        self._tree_layout.setContentsMargins(0, 0, 0, 0)

        # Фейковое дерево (несколько строк)
        self.tree_view = QTreeView()
        tree_model = QStandardItemModel()
        tree_model.setHorizontalHeaderLabels(["Папки"])
        for name in ["Документы", "Фото", "Музыка", "Видео",
                      "Проекты", "Архив", "Загрузки"]:
            item = QStandardItem(name)
            for sub in ["sub1", "sub2", "sub3"]:
                item.appendRow(QStandardItem(sub))
            tree_model.appendRow(item)
        self.tree_view.setModel(tree_model)
        self.tree_view.expandAll()
        self._tree_layout.addWidget(self.tree_view)

        # Спиннер поверх дерева (overlay — fill_parent=True)
        self._tree_spinner = _BrailleSpinner(self._tree_container)
        self._tree_spinner.hide()
        splitter.addWidget(self._tree_container)

        # ── Правая панель: таблица + loading-страница ──────
        self._table_loading_page = QWidget()
        table_loading_layout = QVBoxLayout(self._table_loading_page)
        table_loading_layout.setAlignment(Qt.AlignCenter)
        # Спиннер внутри loading-страницы (fill_parent=False — layout управляет)
        self._table_spinner = _BrailleSpinner(self._table_loading_page, fill_parent=False)
        table_loading_layout.addWidget(self._table_spinner)

        # Фейковая таблица
        self.table_view = QTableView()
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_view.verticalHeader().hide()
        self.table_view.horizontalHeader().setStretchLastSection(True)

        table_model = QStandardItemModel()
        table_model.setHorizontalHeaderLabels(["Статус", "Имя", "Размер", "Изменён"])
        for i in range(15):
            table_model.appendRow([
                QStandardItem("✓"),
                QStandardItem(f"file_{i:03d}.txt"),
                QStandardItem(f"{i * 123} B"),
                QStandardItem("2026-07-16 13:40"),
            ])
        self.table_view.setModel(table_model)
        for c in range(4):
            self.table_view.horizontalHeader().setSectionResizeMode(
                c, QHeaderView.Interactive)
        self.table_view.setColumnWidth(0, 28)
        self.table_view.setColumnWidth(1, 250)
        self.table_view.setColumnWidth(2, 80)

        # ── QStackedWidget: loading-страница (index 0) / таблица (index 1) ──
        self._table_stack = QStackedWidget()
        self._table_stack.addWidget(self._table_loading_page)  # index 0
        self._table_stack.addWidget(self.table_view)           # index 1
        self._table_stack.setCurrentIndex(1)  # таблица видна по умолчанию
        splitter.addWidget(self._table_stack)

        splitter.setSizes([220, 580])

        # ── Панель управления ───────────────────────────────
        controls = QGroupBox("Управление спиннерами")
        ctrl_layout = QVBoxLayout(controls)
        ctrl_layout.setSpacing(6)

        # Кнопки показать/спрятать
        btn_row = QHBoxLayout()
        self._btn_toggle_tree = _styled_btn("Показать спиннер дерева")
        self._btn_toggle_tree.clicked.connect(self._toggle_tree_spinner)
        btn_row.addWidget(self._btn_toggle_tree)

        self._btn_toggle_table = _styled_btn("Показать спиннер таблицы")
        self._btn_toggle_table.clicked.connect(self._toggle_table_spinner)
        btn_row.addWidget(self._btn_toggle_table)

        ctrl_layout.addLayout(btn_row)

        # Слайдер gap для ОБОИХ спиннеров
        slider_label = QLabel("Расстояние между символами (gap):")
        slider_label.setStyleSheet("color: palette(WindowText);")
        ctrl_layout.addWidget(slider_label)

        slider_row = QHBoxLayout()
        slider_row.setAlignment(Qt.AlignCenter)

        lbl_min = QLabel("-20")
        lbl_min.setStyleSheet("color: palette(WindowText);")
        slider_row.addWidget(lbl_min)

        self._gap_slider = QSlider(Qt.Horizontal)
        self._gap_slider.setRange(-20, 40)
        self._gap_slider.setValue(-8)
        self._gap_slider.setTickPosition(QSlider.TicksBelow)
        self._gap_slider.setTickInterval(5)
        self._gap_slider.valueChanged.connect(self._on_gap_changed)
        slider_row.addWidget(self._gap_slider)

        lbl_max = QLabel("40px")
        lbl_max.setStyleSheet("color: palette(WindowText);")
        slider_row.addWidget(lbl_max)

        self._gap_label = QLabel("-8px")
        self._gap_label.setStyleSheet("color: palette(WindowText); font-weight: bold;")
        self._gap_label.setFixedWidth(60)
        self._gap_label.setAlignment(Qt.AlignCenter)
        slider_row.addWidget(self._gap_label)

        ctrl_layout.addLayout(slider_row)

        # Статус
        self._status_label = QLabel("Оба спиннера скрыты")
        self._status_label.setStyleSheet("color: palette(WindowText);")
        ctrl_layout.addWidget(self._status_label)

        main_layout.addWidget(controls)

        # ── Состояние ──
        self._tree_visible = False
        self._table_visible = False
        self._update_status()

    # ── toggle helpers ─────────────────────────────────────

    def _toggle_tree_spinner(self):
        self._tree_visible = not self._tree_visible
        self._tree_spinner.setVisible(self._tree_visible)
        self._btn_toggle_tree.setText(
            "Спрятать спиннер дерева" if self._tree_visible
            else "Показать спиннер дерева")
        self._update_status()

    def _toggle_table_spinner(self):
        self._table_visible = not self._table_visible
        # Табличный спиннер переключается через QStackedWidget (как в ui.py)
        self._table_stack.setCurrentIndex(0 if self._table_visible else 1)
        self._btn_toggle_table.setText(
            "Спрятать спиннер таблицы" if self._table_visible
            else "Показать спиннер таблицы")
        self._update_status()

    def _on_gap_changed(self, value):
        # Меняем gap в ОБОИХ спиннерах
        self._tree_spinner._gap = value
        self._table_spinner._gap = value
        self._tree_spinner._reposition()
        self._table_spinner._reposition()
        self._gap_label.setText(f"{value}px")

    def _update_status(self):
        parts = []
        if self._tree_visible:
            parts.append("дерево виден")
        if self._table_visible:
            parts.append("таблица виден")
        if parts:
            self._status_label.setText(f"Спиннеры: {' / '.join(parts)}")
        else:
            self._status_label.setText("Оба спиннера скрыты")


def _styled_btn(text: str):
    """Кнопка с единообразным стилем."""
    from PySide6.QtWidgets import QPushButton
    from PySide6.QtGui import QFont
    btn = QPushButton(text)
    btn.setStyleSheet("padding: 4px 12px;")
    return btn


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    window = TestLayoutWindow()
    window.show()

    sys.exit(app.exec())
