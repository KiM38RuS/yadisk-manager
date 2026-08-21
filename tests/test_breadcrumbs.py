# tests/test_breadcrumbs.py
"""Тесты адресной строки: нормализация путей, переполнение, сегменты."""
import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_normalize_basic():
    from ui_shared import normalize_cloud_path
    assert normalize_cloud_path("/Загрузки/Отчёты/") == "/Загрузки/Отчёты"
    assert normalize_cloud_path("/Загрузки/Отчёты") == "/Загрузки/Отчёты"  # канонический passthrough
    assert normalize_cloud_path("//a///b//") == "/a/b"
    assert normalize_cloud_path("") == "/"
    assert normalize_cloud_path("   ") == "/"
    assert normalize_cloud_path("relative/path") == "/relative/path"
    assert normalize_cloud_path("/") == "/"


def test_normalize_backslashes():
    """Вставка пути из Проводника Windows: '\\' → '/'."""
    from ui_shared import normalize_cloud_path
    assert normalize_cloud_path("Документы\\Фото\\2024") == "/Документы/Фото/2024"
    assert normalize_cloud_path("\\\\a\\b\\") == "/a/b"


def test_compute_overflow():
    from ui_breadcrumbs import compute_overflow
    # Всё помещается
    assert compute_overflow(300, 400, [100, 100, 100]) == 0
    # Нужно убрать один средний
    assert compute_overflow(350, 300, [100, 100, 100, 50]) == 1
    # Два сегмента не трогаем никогда
    assert compute_overflow(500, 50, [250, 250]) == 0


def test_split_segments(qapp):
    from ui_breadcrumbs import BreadcrumbBar
    segs = BreadcrumbBar._split_segments("/Загрузки/Отчёты")
    assert segs == [
        ("Яндекс Диск", "/"),
        ("Загрузки", "/Загрузки"),
        ("Отчёты", "/Загрузки/Отчёты"),
    ]


def test_set_path_builds_buttons(qapp):
    from ui_breadcrumbs import BreadcrumbBar
    bar = BreadcrumbBar()
    bar._crumbs.setFixedWidth(2000)  # исключаем переполнение (offscreen-ширина)
    bar.set_path("/Загрузки/Отчёты")
    # Кнопки: Яндекс Диск, Загрузки, Отчёты (+ разделители QLabel)
    from PySide6.QtWidgets import QToolButton
    btns = [w for w in bar._crumbs.findChildren(QToolButton)]
    labels = [b.text() for b in btns]
    assert labels == ["Яндекс Диск", "Загрузки", "Отчёты"]
    # Последняя кнопка жирная
    assert btns[-1].font().bold() is True


def test_navigate_signal_on_crumb_click(qapp):
    from ui_breadcrumbs import BreadcrumbBar
    bar = BreadcrumbBar()
    bar._crumbs.setFixedWidth(2000)  # исключаем переполнение
    bar.set_path("/Загрузки/Отчёты")
    got = []
    bar.navigate.connect(got.append)
    from PySide6.QtWidgets import QToolButton
    btns = bar._crumbs.findChildren(QToolButton)
    btns[1].click()  # «Загрузки»
    assert got == ["/Загрузки"]


def test_editor_roundtrip(qapp):
    from ui_breadcrumbs import BreadcrumbBar
    bar = BreadcrumbBar()
    bar.set_path("/A")
    got = []
    bar.navigate.connect(got.append)
    bar.show_editor()
    # isHidden(): явное скрытие; в offscreen isVisible() всегда False,
    # т.к. родительское окно не показано
    assert bar._editor.isHidden() is False   # редактор активен
    assert bar._crumbs.isHidden() is True    # крошки спрятаны
    bar._editor.setText("/B/")
    bar._on_edit_done()
    assert got == ["/B"]                     # нормализовано: хвостовой слэш снят
    assert bar._editor.isHidden() is True    # вернулись к крошкам
    assert bar._crumbs.isHidden() is False


# ── Интеграция с MainWindow (без инстанцирования: unbound-вызов + моки) ──

def _fake_mainwindow(db_get_file):
    """Заглушка MainWindow: только то, что трогает _on_breadcrumb_navigate."""
    from unittest.mock import MagicMock
    mw = MagicMock()
    mw._db.get_file.side_effect = db_get_file
    return mw


def test_breadcrumb_navigate_rejects_unknown_path(qapp):
    """Несуществующий в БД путь: навигация не запускается, редактор возвращается."""
    import ui as ui_mod
    mw = _fake_mainwindow(lambda p: None)
    ui_mod.MainWindow._on_breadcrumb_navigate(mw, "/Нет такой")
    mw._navigate_to_folder.assert_not_called()
    mw._breadcrumb_bar.show_editor_with.assert_called_once_with("/Нет такой")


def test_breadcrumb_navigate_known_path_delegates(qapp):
    """Существующая ПАПКА (и корень) → обычная навигация."""
    import ui as ui_mod
    mw = _fake_mainwindow(
        lambda p: {"cloud_path": p, "type": "dir"} if p == "/Есть" else None)
    ui_mod.MainWindow._on_breadcrumb_navigate(mw, "/Есть")
    ui_mod.MainWindow._on_breadcrumb_navigate(mw, "/")  # корень всегда разрешён
    assert mw._navigate_to_folder.call_args_list[0][0][0] == "/Есть"
    assert mw._navigate_to_folder.call_args_list[1][0][0] == "/"
    mw._breadcrumb_bar.show_editor_with.assert_not_called()


def test_breadcrumb_navigate_rejects_file_path(qapp):
    """Путь до ФАЙЛА проходит проверку существования, но не папка — отклоняем."""
    import ui as ui_mod
    mw = _fake_mainwindow(
        lambda p: {"cloud_path": p, "type": "file"} if p == "/файл.txt" else None)
    ui_mod.MainWindow._on_breadcrumb_navigate(mw, "/файл.txt")
    mw._navigate_to_folder.assert_not_called()
    mw._breadcrumb_bar.show_editor_with.assert_called_once_with("/файл.txt")


def test_focus_address_bar_shows_editor(qapp):
    """Ctrl+L / Alt+D → show_editor()."""
    from unittest.mock import MagicMock
    import ui as ui_mod
    mw = MagicMock()
    ui_mod.MainWindow._focus_address_bar(mw)
    mw._breadcrumb_bar.show_editor.assert_called_once()


# ── Синк-ходок дерева (_sync_tree_to_path): чистая логика шагов ──

def test_ancestor_paths(qapp):
    """Путь → цепочка предков от корня (включая сам путь)."""
    import ui as ui_mod
    assert ui_mod.MainWindow._ancestor_paths("/A/B/C") == \
        ["/", "/A", "/A/B", "/A/B/C"]
    assert ui_mod.MainWindow._ancestor_paths("/") == ["/"]


def test_tree_sync_step_expands_existing(qapp):
    """Все узлы пути есть в дереве и загружены → раскрыть без БД/API-ожидания."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    import ui as ui_mod

    mw = MagicMock()
    mw._tree_sync_queue = ["/", "/A", "/A/B"]
    mw._tree_sync_target = "/A/B"
    mw.tree_model._find_item.side_effect = lambda p: SimpleNamespace(loaded=True)

    ui_mod.MainWindow._tree_sync_step(mw, 1)

    assert mw._tree_sync_queue == []            # очередь исчерпана
    assert mw.tree_view.expand.call_count == 3  # каждый узел раскрыт
    mw._fetch_folder_list.assert_not_called()   # узлы уже loaded — API не нужен
    mw._tree_sync_finish.assert_called_once()


def test_tree_sync_step_fetches_db_children(qapp, monkeypatch):
    """Узла нет в дереве → ходок запрашивает детей родителя из БД и ждёт колбэк."""
    from unittest.mock import MagicMock
    import ui as ui_mod

    created = []

    class FakeThread:
        def __init__(self, db, parent_path, parent=None):
            created.append(parent_path)
            self.finished = MagicMock()
            self.deleteLater = lambda: None

        def start(self):
            pass

    monkeypatch.setattr(ui_mod, "DbChildrenThread", FakeThread)

    mw = MagicMock()
    mw._tree_sync_queue = ["/", "/A", "/A/B"]
    mw._tree_sync_target = "/A/B"
    # Корень есть, но ещё не загружен (loaded/db_loaded = False); /A отсутствует
    from types import SimpleNamespace
    mw.tree_model._find_item.side_effect = \
        lambda p: SimpleNamespace(loaded=False, db_loaded=False) if p == "/" else None

    ui_mod.MainWindow._tree_sync_step(mw, 1)

    assert created == ["/"]                # дети корня запрошены из БД
    assert mw._tree_sync_queue == ["/A/B"]  # очередь заморожена до колбэка
    mw._tree_sync_finish.assert_not_called()  # ходок ждёт колбэк


def test_tree_sync_db_children_callback(qapp):
    """Колбэк БД: успех → вставить детей и продолжить; ошибка → завершить."""
    from unittest.mock import MagicMock
    import ui as ui_mod

    # Успех: populate + продолжение ходока
    mw = MagicMock()
    ui_mod.MainWindow._on_db_children_for_sync(mw, "/", [], set(), "")
    mw.tree_model.populate_children_from_db.assert_called_once_with("/", [], set())
    mw._tree_sync_step.assert_called_once()

    # Ошибка: без вставки, ходок завершается
    mw2 = MagicMock()
    ui_mod.MainWindow._on_db_children_for_sync(mw2, "/", [], set(), "db error")
    mw2.tree_model.populate_children_from_db.assert_not_called()
    mw2._tree_sync_finish.assert_called_once()


def test_tree_sync_stale_gen_ignored(qapp):
    """Ответ от предыдущего хода (gen устарел) полностью игнорируется."""
    from unittest.mock import MagicMock
    import ui as ui_mod

    mw = MagicMock()
    mw._tree_sync_gen = 2  # уже шёл новый ходок
    ui_mod.MainWindow._on_db_children_for_sync(mw, "/", [], set(), "", 1)
    mw.tree_model.populate_children_from_db.assert_not_called()
    mw._tree_sync_step.assert_not_called()
    mw._tree_sync_finish.assert_not_called()


# ── select_after: выделение бывшей папки при подъёме вверх ──────────

def test_navigate_to_folder_select_after_overrides_selection(qapp):
    """select_after подменяет снимок выделения и уходит в ходок дерева."""
    from unittest.mock import MagicMock
    import ui as ui_mod

    mw = MagicMock()
    mw._current_path = "/A/B"
    mw._get_selected_cloud_paths.return_value = ["/A/B/старое.txt"]
    ui_mod.MainWindow._navigate_to_folder(mw, "/A", select_after="/A/B")

    # Таблица: после загрузки выделяется бывшая папка, а не старый снимок
    assert mw._pending_selection == ["/A/B"]
    # Дерево: ходок получил бывшую папку как цель подсветки
    mw._sync_tree_to_path.assert_called_once_with("/A", select_after="/A/B")


def test_navigate_to_folder_without_select_after_keeps_selection(qapp):
    """Обычная навигация: снимок выделения не подменяется, select_after=None."""
    from unittest.mock import MagicMock
    import ui as ui_mod

    mw = MagicMock()
    mw._get_selected_cloud_paths.return_value = ["/X/файл.txt"]
    ui_mod.MainWindow._navigate_to_folder(mw, "/Y")

    assert mw._pending_selection == ["/X/файл.txt"]
    mw._sync_tree_to_path.assert_called_once_with("/Y", select_after=None)


def test_sync_tree_to_path_stores_select_after(qapp):
    """Ходок запоминает select_after рядом с целью (для финального выделения)."""
    from unittest.mock import MagicMock
    import ui as ui_mod

    mw = MagicMock()
    mw._tree_sync_gen = 0
    ui_mod.MainWindow._sync_tree_to_path(mw, "/A", select_after="/A/B")

    assert mw._tree_sync_target == "/A"
    assert mw._tree_sync_select_after == "/A/B"
    mw._tree_sync_step.assert_called_once()


def test_tree_sync_finish_prefers_select_after(qapp):
    """Финал ходока: подсвечивается бывшая папка, а не пункт назначения."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    import ui as ui_mod

    mw = MagicMock()
    mw._tree_sync_target = "/A"
    mw._tree_sync_select_after = "/A/B"
    departed = SimpleNamespace()  # узел бывшей папки найден в дереве
    mw.tree_model._find_item.side_effect = \
        lambda p: departed if p == "/A/B" else None

    ui_mod.MainWindow._tree_sync_finish(mw)

    mw.tree_model._index_of.assert_called_once_with(departed)
    assert mw._selection_updating is False  # защита снята после выделения
    mw.tree_view.scrollTo.assert_called_once()


def test_tree_sync_finish_falls_back_to_target(qapp):
    """Бывшей папки нет в дереве → откат на подсветку пункта назначения."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    import ui as ui_mod

    mw = MagicMock()
    mw._tree_sync_target = "/A"
    mw._tree_sync_select_after = "/A/Исчезнувшая"
    dest = SimpleNamespace()
    mw.tree_model._find_item.side_effect = \
        lambda p: dest if p == "/A" else None

    ui_mod.MainWindow._tree_sync_finish(mw)

    mw.tree_model._index_of.assert_called_once_with(dest)


def test_nav_up_passes_departed_folder(qapp):
    """Кнопка «Вверх» / Alt+Up: бывшая папка уходит в select_after."""
    from unittest.mock import MagicMock
    import ui as ui_mod

    mw = MagicMock()
    mw._current_path = "/A/B"
    ui_mod.MainWindow._nav_up(mw)

    mw._navigate_to_folder.assert_called_once_with("/A", select_after="/A/B")


def test_backspace_passes_departed_folder(qapp):
    """Backspace в таблице: бывшая папка уходит в select_after."""
    from unittest.mock import MagicMock
    from PySide6.QtCore import Qt
    import ui as ui_mod

    mw = MagicMock()
    mw._current_path = "/A/B"
    ev = MagicMock()
    ev.key.return_value = Qt.Key_Backspace

    handled = ui_mod.MainWindow._handle_table_key(mw, ev)

    assert handled is True
    mw._navigate_to_folder.assert_called_once_with("/A", select_after="/A/B")


def test_parent_nav_row_passes_departed_folder(qapp):
    """Двойной клик по строке «..»: бывшая папка уходит в select_after."""
    from unittest.mock import MagicMock
    import ui as ui_mod

    mw = MagicMock()
    mw._search_mode = False
    mw._current_path = "/A/B"
    mw._get_item.return_value = {"is_parent_nav": True, "cloud_path": "/A"}

    ui_mod.MainWindow._on_file_double_clicked(mw, MagicMock())

    mw._navigate_to_folder.assert_called_once_with("/A", select_after="/A/B")
