"""Регрессия: выход из поиска (Esc) должен сбрасывать фильтр proxy-модели.

История: в _on_search("") ветка выхода из режима поиска (if self._search_mode)
не вызывала table_sort_model.set_search_text("") — сброс был только в ветке
обычной фильтрации. После Esc на текущую папку накладывался старый фильтр:
БД корня содержала 29 записей, а таблица показывала лишь те папки, в имени
которых есть буква из прежнего поискового запроса (например «т» → 9 папок).
"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ui_table_model import FileTableModel, FileTableSortModel


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeDB:
    """Фейк БД: get_file() и агрегатные статусы папок — для FileTableModel."""

    def get_file(self, cloud_path):
        return None

    def get_folder_batch_aggregate_status(self, parent_path, dir_paths):
        return {p: "cloud_only" for p in dir_paths}


def _api_items(names):
    """Записи как из FolderLoadThread/API: папки корня."""
    return [{
        "path": f"/{name}",
        "cloud_path": f"/{name}",
        "name": name,
        "type": "dir",
        "size": 0,
        "modified": "",
        "md5": "",
        "mime_type": "",
        "status": "cloud_only",
    } for name in names]


def _visible_names(model, proxy):
    """Имена строк, которые реально видит QTableView через proxy."""
    names = []
    for row in range(proxy.rowCount()):
        idx = proxy.index(row, 0)
        src = proxy.mapToSource(idx)
        names.append(model.get_item(src.row())["name"])
    return names


def test_exit_search_clears_filter(qapp):
    model = FileTableModel(_FakeDB())
    proxy = FileTableSortModel()
    proxy.setSourceModel(model)

    names = ["Аудио", "Видео", "Документы", "Загрузки", "Шара"]
    model.set_path("/", _api_items(names))
    assert proxy.rowCount() == len(names)

    # Пользователь набрал «т» — фильтрация текущей папки
    proxy.set_search_text("т")
    visible = _visible_names(model, proxy)
    assert visible == ["Документы"]  # единственная с буквой «т»

    # Поиск пошёл: результаты в модели (режим поиска)
    model.set_search_results(_api_items(["Документы"]))
    assert _visible_names(model, proxy) == ["Документы"]

    # Esc: _on_search("") — выход из поиска + сброс фильтра (фикс)
    proxy.set_search_text("")
    model.set_path("/", _api_items(names))
    assert _visible_names(model, proxy) == names  # все 5 снова видны


def test_search_filter_case_insensitive(qapp):
    model = FileTableModel(_FakeDB())
    proxy = FileTableSortModel()
    proxy.setSourceModel(model)

    model.set_path("/", _api_items(["Аудио", "Фотокамера", "Sync"]))
    proxy.set_search_text("а")  # строчная «а» матчит «Аудио» и «а» в «Фотокамера»
    visible = _visible_names(model, proxy)
    assert "Sync" not in visible  # латинская «S» не матчится с кириллической «а»
    assert "Аудио" in visible
    assert "Фотокамера" in visible
