# Explorer Navigation (Группа А) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Навигация как в Проводнике Windows: адресная строка с крошками (1.1), мгновенная синхронизация дерева из БД при навигации (1.2), выделение бывшей папки при возврате вверх (1.10).

**Architecture:** Единая точка навигации `_navigate_to_folder()` расширяется параметром `select_after` и вызовом синк-ходока `_sync_tree_to_path()`. Ходок раскрывает дерево лениво: недостающие дети берутся из SQLite фоновым потоком (`DbChildrenThread`) — мгновенно, без API; API-сверка догоняет асинхронно через `merge_children_from_api()`. Адресная строка — самостоятельный виджет `BreadcrumbBar` (новый файл) во втором тулбаре.

**Tech Stack:** Python 3.12, PySide6, SQLite. Тесты: pytest, QT_QPA_PLATFORM=offscreen.

**Спека:** `docs/superpowers/specs/2026-08-21-explorer-navigation-design.md`

**Запуск тестов:** `.venv\Scripts\python -m pytest tests/<file> -v` (из корня проекта)

---

### Task 1: `db.get_dir_tree_levels()` + `db.get_all_dir_paths()`

**Files:**
- Modify: `db.py` (класс `Database`, после метода `get_children`, ~строка 854)
- Test: `tests/test_dir_tree_levels.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dir_tree_levels.py
"""Тесты двухуровневой выборки подпапок из БД (для дерева и автодополнения)."""


def test_levels_root(populated_db):
    folders, subs = populated_db.get_dir_tree_levels("/")
    names = [f["name"] for f in folders]
    assert names == ["Документы", "Музыка", "Фото"]
    # /Фото имеет подпапку /Фото/2024, остальные — нет
    assert subs == {"/Фото"}


def test_levels_nested(populated_db):
    folders, subs = populated_db.get_dir_tree_levels("/Фото")
    assert [f["path"] for f in folders] == ["/Фото/2024"]
    assert subs == set()


def test_levels_empty_folder(populated_db):
    folders, subs = populated_db.get_dir_tree_levels("/Несуществует")
    assert folders == []
    assert subs == set()


def test_all_dir_paths(populated_db):
    paths = populated_db.get_all_dir_paths()
    assert "/Документы" in paths
    assert "/Фото/2024" in paths
    assert paths == sorted(paths)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_dir_tree_levels.py -v`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'get_dir_tree_levels'`

- [ ] **Step 3: Write minimal implementation**

Добавить в класс `Database` в `db.py` сразу после метода `get_children` (после строки 854):

```python
    def get_dir_tree_levels(self, parent_path: str) -> tuple[list[dict], set[str]]:
        """Прямые подпапки parent_path + множество тех, у кого есть свои подпапки.

        Один SQL-запрос достаёт ВСЕ dir-пути под parent_path; группировка по
        первому сегменту даёт оба уровня сразу (точные стрелки дерева из БД).

        Возвращает (folders, has_subdirs):
          folders     — [{"path": ..., "name": ...}] прямые подпапки, по алфавиту
          has_subdirs — cloud_path папок, у которых есть хотя бы одна подпапка
        """
        prefix = parent_path.rstrip("/")
        pattern = (prefix + "/%") if prefix else "/%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT cloud_path FROM files "
                "WHERE type = 'dir' AND cloud_path LIKE ?",
                (pattern,),
            ).fetchall()
        base_len = len(prefix) + 1 if prefix else 1  # срез после "prefix/" или "/"
        level1: dict[str, dict] = {}
        has_subdirs: set[str] = set()
        for (cp,) in rows:
            rest = cp[base_len:]
            if "/" in rest:
                name, _ = rest.split("/", 1)
                child_path = (prefix + "/" + name) if prefix else "/" + name
                has_subdirs.add(child_path)
                level1.setdefault(child_path, {"path": child_path, "name": name})
            else:
                level1.setdefault(cp, {"path": cp, "name": rest})
        folders = sorted(level1.values(), key=lambda d: d["name"].lower())
        return folders, has_subdirs

    def get_all_dir_paths(self) -> list[str]:
        """Все облачные пути папок (для автодополнения адресной строки)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT cloud_path FROM files "
                "WHERE type = 'dir' ORDER BY cloud_path"
            ).fetchall()
        return [r["cloud_path"] for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_dir_tree_levels.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_dir_tree_levels.py
git commit -m "feat(db): get_dir_tree_levels + get_all_dir_paths for instant tree sync"
```

---

### Task 2: Потоки `DbChildrenThread` и `DirPathsThread`

**Files:**
- Modify: `ui_threads.py` (после класса `ApiListThread`, ~строка 94)
- Test: `tests/test_dir_tree_levels.py` (добавить)

- [ ] **Step 1: Write the failing test**

Дописать в конец `tests/test_dir_tree_levels.py`:

```python
def test_db_children_thread_run(populated_db):
    """run() синхронно: сигнал finished отдаёт папки и множество стрелок."""
    from ui_threads import DbChildrenThread

    result = {}

    def on_done(folders, subs, err):
        result.update(folders=folders, subs=subs, err=err)

    t = DbChildrenThread(populated_db, "/")
    t.finished.connect(on_done)
    t.run()  # синхронный вызов, без start()
    assert result["err"] == ""
    assert [f["name"] for f in result["folders"]] == ["Документы", "Музыка", "Фото"]
    assert result["subs"] == {"/Фото"}


def test_dir_paths_thread_run(populated_db):
    from ui_threads import DirPathsThread

    result = {}
    t = DirPathsThread(populated_db)
    t.finished.connect(lambda paths, err: result.update(paths=paths, err=err))
    t.run()
    assert result["err"] == ""
    assert "/Фото/2024" in result["paths"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_dir_tree_levels.py -v`
Expected: FAIL — `ImportError: cannot import name 'DbChildrenThread'`

- [ ] **Step 3: Write minimal implementation**

В `ui_threads.py` после класса `ApiListThread` (перед `FolderLoadThread`):

```python
class DbChildrenThread(QThread):
    """Фоновое чтение подпапок из БД: уровень 1 + наличие подпапок (стрелки).

    Мгновенная альтернатива ApiListThread для раскрытия дерева: локальный
    SQLite-запрос вместо сетевого. Объекты возвращаются как есть:
    finished(list[dict], set[str], str)
    """
    finished = Signal(list, object, str)  # folders, has_subdirs, error_msg

    def __init__(self, database, parent_path: str, parent=None):
        super().__init__(parent)
        self._database = database
        self._parent_path = parent_path

    def run(self):
        try:
            folders, has_subdirs = self._database.get_dir_tree_levels(self._parent_path)
            self.finished.emit(folders, has_subdirs, "")
        except Exception as e:
            logger.error("DbChildrenThread: %s FAILED: %s", self._parent_path, e)
            self.finished.emit([], set(), str(e))


class DirPathsThread(QThread):
    """Фоновое чтение всех облачных путей папок (автодополнение адресной строки)."""
    finished = Signal(list, str)  # paths, error_msg

    def __init__(self, database, parent=None):
        super().__init__(parent)
        self._database = database

    def run(self):
        try:
            self.finished.emit(self._database.get_all_dir_paths(), "")
        except Exception as e:
            logger.error("DirPathsThread FAILED: %s", e)
            self.finished.emit([], str(e))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_dir_tree_levels.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add ui_threads.py tests/test_dir_tree_levels.py
git commit -m "feat(threads): DbChildrenThread + DirPathsThread for DB-first tree"
```

---

### Task 3: Модель — `populate_children_from_db()` и точные стрелки

**Files:**
- Modify: `ui_tree_model.py` (`FolderTreeItem.__init__` стр. 30–37; `hasChildren` стр. 153–160; новый метод после `populate_children` стр. 102)
- Test: `tests/test_tree_model_db.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tree_model_db.py
"""Тесты DB-first наполнения FolderTreeModel и точных стрелок раскрытия."""
import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def model(qapp, populated_db):
    from ui_tree_model import FolderTreeModel
    # Корень «Яндекс Диск» существует сразу после __init__ (visible_root);
    # НЕ вызываем populate_children — он пометил бы корень loaded=True
    return FolderTreeModel(None, populated_db)


def test_populate_from_db_exact_arrows(model):
    folders, subs = model._db.get_dir_tree_levels("/")
    model.populate_children_from_db("/", folders, subs)

    # Три дочерних узла корня
    assert model.rowCount(model.index(0, 0)) == 3

    # Стрелки точные: у /Фото есть подпапки, у остальных нет
    photo_idx = model.index(2, 0, model.index(0, 0))   # sorted: Документы, Музыка, Фото
    docs_idx = model.index(0, 0, model.index(0, 0))
    assert model.hasChildren(photo_idx) is True
    assert model.hasChildren(docs_idx) is False


def test_populate_from_db_sets_flag(model):
    folders, subs = model._db.get_dir_tree_levels("/")
    model.populate_children_from_db("/", folders, subs)
    item = model._find_item("/")
    assert item.db_loaded is True
    assert item.loaded is False  # API ещё не сверял


def test_populate_twice_is_noop(model):
    folders, subs = model._db.get_dir_tree_levels("/")
    model.populate_children_from_db("/", folders, subs)
    model.populate_children_from_db("/", folders, subs)  # второй вызов — no-op
    assert model.rowCount(model.index(0, 0)) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_tree_model_db.py -v`
Expected: FAIL — `AttributeError: 'FolderTreeModel' object has no attribute 'populate_children_from_db'`

- [ ] **Step 3: Write minimal implementation**

3a. В `FolderTreeItem.__init__` (ui_tree_model.py:30) добавить два поля после `self._has_children = True`:

```python
        self.db_loaded = False    # дети получены из SQLite (мгновенно)
        self.arrow_known = False  # наличие подпапок известно из БД (точная стрелка)
```

3b. Заменить `hasChildren` (строки 153–160):

```python
    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        """True если у узла должны быть дети (рисуется стрелка раскрытия).

        Приоритет источников: API-сверка > знание из БД > оптимистичное True.
        """
        if not parent.isValid():
            return True
        item: FolderTreeItem = parent.internalPointer()
        if item.loaded:
            return item._has_children
        if item.db_loaded:
            return len(item.children) > 0
        if item.arrow_known:
            return item._has_children
        return True  # ничего не знаем → показываем стрелку
```

3c. Новый метод после `populate_children` (после строки 102):

```python
    def populate_children_from_db(self, cloud_path: str, folders: list[dict],
                                  with_subdirs: set[str]) -> None:
        """Мгновенно вставить детей из SQLite (вызов только из главного потока).

        Узел получает db_loaded=True: дети и стрелки видны сразу, без API.
        API-сверка досинхронизируется позже через merge_children_from_api().
        Повторный вызов — no-op.
        """
        parent_item = self._find_item(cloud_path)
        if not parent_item or parent_item.loaded or parent_item.db_loaded:
            return
        parent_item.db_loaded = True
        parent_item._has_children = len(folders) > 0
        if not folders:
            return
        parent_index = self._index_of(parent_item)
        self.beginInsertRows(parent_index, 0, len(folders) - 1)
        if self._db is not None:
            folder_paths = [f["path"] for f in folders]
            batch_statuses = self._db.get_folder_batch_aggregate_status(
                cloud_path, folder_paths)
        else:
            batch_statuses = {}
        for f in folders:
            child = FolderTreeItem(
                name=f["name"], cloud_path=f["path"], parent=parent_item)
            child.status = batch_statuses.get(f["path"], "cloud_only")
            # Точная стрелка из БД: знаем, есть ли у ребёнка свои подпапки
            child._has_children = f["path"] in with_subdirs
            child.arrow_known = True
            parent_item.children.append(child)
        self.endInsertRows()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_tree_model_db.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add ui_tree_model.py tests/test_tree_model_db.py
git commit -m "feat(tree-model): populate_children_from_db with exact DB arrows"
```

---

### Task 4: Модель — `merge_children_from_api()`

**Files:**
- Modify: `ui_tree_model.py` (после `populate_children_from_db`)
- Test: `tests/test_tree_model_db.py` (добавить)

- [ ] **Step 1: Write the failing test**

Дописать в `tests/test_tree_model_db.py`:

```python
def test_merge_adds_and_removes(model):
    folders, subs = model._db.get_dir_tree_levels("/")
    model.populate_children_from_db("/", folders, subs)

    # Облако: Музыка удалена, появилась Новая
    api_items = [
        {"path": "/Документы", "name": "Документы", "type": "dir"},
        {"path": "/Фото", "name": "Фото", "type": "dir"},
        {"path": "/Новая", "name": "Новая", "type": "dir"},
    ]
    model.merge_children_from_api("/", api_items)

    item = model._find_item("/")
    assert item.loaded is True           # API-сверка проведена
    names = {c.name for c in item.children}
    assert names == {"Документы", "Фото", "Новая"}
    assert model._find_item("/Музыка") is None
    assert model._find_item("/Новая") is not None


def test_merge_skips_non_db_nodes(model):
    """Для узла без db_loaded merge — no-op (обслуживает populate_children)."""
    model.merge_children_from_api("/", [
        {"path": "/X", "name": "X", "type": "dir"},
    ])
    assert model._find_item("/X") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_tree_model_db.py -v`
Expected: FAIL — `AttributeError: ... no attribute 'merge_children_from_api'`

- [ ] **Step 3: Write minimal implementation**

После `populate_children_from_db`:

```python
    def merge_children_from_api(self, cloud_path: str, items: list[dict]) -> None:
        """Досинхронизировать db_loaded-узел списком из API (главный поток).

        Добавляет новые папки, удаляет исчезнувшие, ставит loaded=True.
        Для узлов без db_loaded — no-op (их обслуживает populate_children).
        """
        parent_item = self._find_item(cloud_path)
        if not parent_item or not parent_item.db_loaded or parent_item.loaded:
            return
        folders = [it for it in items if it.get("type") == "dir"]
        parent_item.loaded = True
        parent_item._has_children = len(folders) > 0
        api_by_path = {f["path"]: f for f in folders}
        # Удаления — с конца, чтобы row-индексы не плыли
        for row in range(len(parent_item.children) - 1, -1, -1):
            child = parent_item.children[row]
            if child.cloud_path not in api_by_path:
                self.beginRemoveRows(self._index_of(parent_item), row, row)
                parent_item.children.pop(row)
                self.endRemoveRows()
        # Вставки новых
        existing = {c.cloud_path for c in parent_item.children}
        new_items = [f for p, f in api_by_path.items() if p not in existing]
        if new_items:
            base = len(parent_item.children)
            self.beginInsertRows(self._index_of(parent_item),
                                 base, base + len(new_items) - 1)
            for f in new_items:
                parent_item.children.append(FolderTreeItem(
                    name=f["name"], cloud_path=f["path"], parent=parent_item))
            self.endInsertRows()
        # Статусы одним batch-запросом
        if self._db is not None and parent_item.children:
            paths = [c.cloud_path for c in parent_item.children]
            batch = self._db.get_folder_batch_aggregate_status(cloud_path, paths)
            for c in parent_item.children:
                c.status = batch.get(c.cloud_path, "cloud_only")
```

Затем защитить от дублей старый путь вставки: в начало `populate_children` (строка 74) после поиска `parent_item` добавить делегирование:

```python
    def populate_children(self, cloud_path: str, items: list[dict]) -> None:
        """Асинхронно добавить children узлу (из главного потока)."""
        parent_item = self._find_item(cloud_path)
        if not parent_item or parent_item.loaded:
            return
        # Узел уже наполнен из БД → API-результат мержим, а не дублируем
        if parent_item.db_loaded:
            self.merge_children_from_api(cloud_path, items)
            return
```

(остальное тело `populate_children` без изменений)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_tree_model_db.py tests/test_dir_tree_levels.py -v`
Expected: все passed

- [ ] **Step 5: Commit**

```bash
git add ui_tree_model.py tests/test_tree_model_db.py
git commit -m "feat(tree-model): merge_children_from_api reconciles DB-populated nodes"
```

---

### Task 5: `normalize_cloud_path()` в ui_shared

**Files:**
- Modify: `ui_shared.py` (рядом с `_local_path`)
- Test: `tests/test_breadcrumbs.py` (создать — файл будет дополнен в Task 6)

- [ ] **Step 1: Write the failing test**

```python
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
    assert normalize_cloud_path("//a///b//") == "/a/b"
    assert normalize_cloud_path("") == "/"
    assert normalize_cloud_path("   ") == "/"
    assert normalize_cloud_path("relative/path") == "/relative/path"
    assert normalize_cloud_path("/") == "/"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_breadcrumbs.py -v`
Expected: FAIL — `ImportError: cannot import name 'normalize_cloud_path'`

- [ ] **Step 3: Write minimal implementation**

В `ui_shared.py` рядом с `_local_path`:

```python
def normalize_cloud_path(path: str) -> str:
    """Нормализовать облачный путь: ведущий '/', без хвостовых и двойных слэшей."""
    if not path:
        return "/"
    path = path.strip()
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    while "//" in path:
        path = path.replace("//", "/")
    return path.rstrip("/") or "/"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_breadcrumbs.py -v`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add ui_shared.py tests/test_breadcrumbs.py
git commit -m "feat(shared): normalize_cloud_path helper"
```

---

### Task 6: Виджет `BreadcrumbBar` (новый файл)

**Files:**
- Create: `ui_breadcrumbs.py`
- Test: `tests/test_breadcrumbs.py` (добавить)

- [ ] **Step 1: Write the failing test**

Дописать в `tests/test_breadcrumbs.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python -m pytest tests/test_breadcrumbs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ui_breadcrumbs'`

- [ ] **Step 3: Write the widget**

Создать `ui_breadcrumbs.py` целиком:

```python
"""
BreadcrumbBar — адресная строка с хлебными крошками для YaDisk Manager.

Крошки пути (кликабельные QToolButton) + режим ручного ввода (QLineEdit).
Переполнение при узком окне: средние сегменты сворачиваются в кнопку «…»
с выпадающим меню скрытых предков (как в Проводнике Windows).
"""

import logging

from PySide6.QtCore import Qt, QEvent, Signal
from PySide6.QtCore import QStringListModel
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCompleter, QHBoxLayout, QLabel, QLineEdit, QMenu, QSizePolicy,
    QToolButton, QWidget,
)

from ui_shared import normalize_cloud_path

logger = logging.getLogger("ui.breadcrumbs")


def compute_overflow(total_width: int, available: int,
                     widths: list[int]) -> int:
    """Сколько средних сегментов скрыть, чтобы суммарная ширина влезла.

    Первый и последний сегменты всегда видимы. Возвращает число скрытых
    сегментов подряд начиная с индекса 1.
    """
    if total_width <= available or len(widths) <= 2:
        return 0
    hidden = 0
    w = total_width
    for i in range(1, len(widths) - 1):
        if w <= available:
            break
        w -= widths[i]
        hidden += 1
    return hidden


class _CrumbsArea(QWidget):
    """Контейнер крошек: клик по пустому месту → запрос режима ввода."""
    empty_clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.empty_clicked.emit()
        super().mousePressEvent(event)


class BreadcrumbBar(QWidget):
    """Адресная строка: крошки пути + ручной ввод облачного пути."""

    navigate = Signal(str)       # выбран путь (клик по крошке или Enter)
    editor_shown = Signal()      # вошли в режим ввода (для обновления completer)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._path = "/"
        self._segments: list[tuple[str, str]] = []

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)

        self._crumbs = _CrumbsArea()
        self._crumbs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._crumbs_layout = QHBoxLayout(self._crumbs)
        self._crumbs_layout.setContentsMargins(0, 0, 0, 0)
        self._crumbs_layout.setSpacing(2)
        self._crumbs.empty_clicked.connect(self.show_editor)
        lay.addWidget(self._crumbs)

        self._editor = QLineEdit()
        self._editor.hide()
        self._editor.setPlaceholderText("Путь, например: /Загрузки/Отчёты")
        self._editor.returnPressed.connect(self._on_edit_done)
        self._editor.installEventFilter(self)
        lay.addWidget(self._editor)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._rebuild()

    # ── публичный API ──────────────────────────────────────

    def set_path(self, path: str) -> None:
        """Отобразить путь крошками (вызывать при каждой навигации)."""
        self._path = normalize_cloud_path(path)
        self._segments = self._split_segments(self._path)
        if not self._editor.isVisible():
            self._rebuild()

    def show_editor(self) -> None:
        """Режим ввода: скрыть крошки, показать QLineEdit с полным путём."""
        self._crumbs.hide()
        self._editor.show()
        self._editor.setText(self._path)
        self._editor.selectAll()
        self._editor.setFocus()
        self.editor_shown.emit()

    def show_editor_with(self, text: str) -> None:
        """Режим ввода с подставленным текстом (повтор ввода неверного пути)."""
        self.show_editor()
        self._editor.setText(text)
        self._editor.selectAll()

    def set_completer_model(self, model: QStringListModel) -> None:
        """Подменить модель автодополнения (список папок из БД)."""
        completer = QCompleter(model, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setCompletionMode(
            QCompleter.PopupCompletion | QCompleter.InlineCompletion)
        self._editor.setCompleter(completer)

    # ── внутреннее ─────────────────────────────────────────

    @staticmethod
    def _split_segments(path: str) -> list[tuple[str, str]]:
        """Путь → [(подпись, cloud_path)], первый сегмент — корень диска."""
        parts = [p for p in path.split("/") if p]
        segs = [("Яндекс Диск", "/")]
        cur = ""
        for p in parts:
            cur += "/" + p
            segs.append((p, cur))
        return segs

    def _make_button(self, label: str, path: str, current: bool) -> QToolButton:
        b = QToolButton(label)
        b.setAutoRaise(True)
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(path)
        if current:
            f = QFont(b.font())
            f.setBold(True)
            b.setFont(f)
        else:
            b.clicked.connect(lambda checked=False, p=path: self.navigate.emit(p))
        return b

    @staticmethod
    def _make_sep() -> QLabel:
        s = QLabel("▸")
        s.setStyleSheet("color: #909090;")
        return s

    def _rebuild(self) -> None:
        """Перестроить крошки с учётом доступной ширины (переполнение → «…»)."""
        while self._crumbs_layout.count():
            it = self._crumbs_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

        n = len(self._segments)
        buttons = [self._make_button(lbl, p, i == n - 1)
                   for i, (lbl, p) in enumerate(self._segments)]
        seps = [self._make_sep() for _ in range(max(0, n - 1))]

        widths = [b.sizeHint().width() for b in buttons]
        seps_w = sum(s.sizeHint().width() + 2 for s in seps)
        total = sum(widths) + seps_w
        avail = max(80, self._crumbs.width() - 8)
        hidden = compute_overflow(total, avail, widths)

        if hidden > 0 and n > 2:
            dots = QToolButton("…")
            dots.setAutoRaise(True)
            dots.setToolTip("Промежуточные папки")
            menu = QMenu(dots)
            for lbl, p in self._segments[1:1 + hidden]:
                menu.addAction(lbl, lambda checked=False, pp=p: self.navigate.emit(pp))
            dots.setMenu(menu)
            dots.setPopupMode(QToolButton.InstantPopup)
            self._crumbs_layout.addWidget(buttons[0])
            self._crumbs_layout.addWidget(self._make_sep())
            self._crumbs_layout.addWidget(dots)
            self._crumbs_layout.addWidget(self._make_sep())
            self._crumbs_layout.addWidget(buttons[-1])
        else:
            for i, b in enumerate(buttons):
                if i:
                    self._crumbs_layout.addWidget(seps[i - 1])
                self._crumbs_layout.addWidget(b)
        self._crumbs_layout.addStretch(1)

    def _on_edit_done(self) -> None:
        """Enter в редакторе: нормализовать путь и сообщить о навигации."""
        raw = self._editor.text()
        self._cancel_edit()
        self.navigate.emit(normalize_cloud_path(raw))

    def _cancel_edit(self) -> None:
        """Esc: вернуть крошки без навигации."""
        self._editor.hide()
        self._crumbs.show()
        self._rebuild()

    def eventFilter(self, obj, event):
        if obj is self._editor and event.type() == QEvent.KeyPress \
                and event.key() == Qt.Key_Escape:
            self._cancel_edit()
            return True
        return super().eventFilter(obj, event)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if not self._editor.isVisible():
            self._rebuild()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python -m pytest tests/test_breadcrumbs.py -v`
Expected: все passed (нормализация + 5 новых)

- [ ] **Step 5: Commit**

```bash
git add ui_breadcrumbs.py tests/test_breadcrumbs.py
git commit -m "feat(ui): BreadcrumbBar widget with editable path and overflow menu"
```

---

### Task 7: Интеграция адресной строки в MainWindow

**Files:**
- Modify: `ui.py` (импорт, `_create_toolbar` ~1360, блок шорткатов ~834–856, `_navigate_to_folder` ~3636, `_on_folder_loaded` ~423)

Ручная проверка (GUI): после шагов запустить `.venv\Scripts\python main.py`, проверить крошки, Ctrl+L, ввод пути, Esc.

- [ ] **Step 1: Импорт**

В `ui.py` к импортам локальных модулей добавить:

```python
from ui_breadcrumbs import BreadcrumbBar
```

- [ ] **Step 2: Второй тулбар с адресной строкой**

В конце `_create_toolbar()` (после `right_margin`, до `self._update_toolbar_buttons()`):

```python
        # Адресная строка (breadcrumbs) — отдельная полоса под основным тулбаром
        self.addToolBarBreak(Qt.TopToolBarArea)
        addr_tb = QToolBar("Адрес", self)
        addr_tb.setMovable(False)
        self._breadcrumb_bar = BreadcrumbBar()
        addr_tb.addWidget(self._breadcrumb_bar)
        self.addToolBar(addr_tb)
        self._breadcrumb_bar.navigate.connect(self._on_breadcrumb_navigate)
        self._breadcrumb_bar.editor_shown.connect(self._refresh_path_completer)
```

- [ ] **Step 2b: Обёртка навигации с проверкой пути**

Крошки и редактор шлют сигнал `navigate` — MainWindow сначала проверяет путь по локальной БД (мгновенно, офлайн-совместимо): несуществующий путь не запускает навигацию, а возвращает пользователя в редактор с выделенным текстом (поведение из спеки). Метод добавить рядом с `_focus_address_bar`:

```python
    def _on_breadcrumb_navigate(self, path: str):
        """Переход из адресной строки с мгновенной проверкой существования.

        Ограничение: папки, созданные в облаке в обход Менеджера и ещё не
        попавшие в локальную БД, потребуют обычной навигации (дерево/поиск) —
        они отклоняются здесь как «не найденные» до ближайшей синхронизации.
        """
        if path != "/" and not self._db.get_file(path):
            self.statusBar().showMessage(f"Папка не найдена: {path}", 5000)
            self._breadcrumb_bar.show_editor_with(path)
            return
        self._navigate_to_folder(path)
```

В `_navigate_to_folder()` (строка 3636) после `self._current_path = path` добавить:

```python
        self._breadcrumb_bar.set_path(path)
```

- [ ] **Step 3: Шорткаты Ctrl+L / Alt+D**

В блоке клавиатурных сокращений (после `_shortcut_search`, ~строка 856):

```python
        # Ctrl+L / Alt+D — фокус в адресную строку (как в Проводнике)
        for _seq in ("Ctrl+L", "Alt+D"):
            _act_addr = QAction("Адресная строка", self)
            _act_addr.setShortcut(QKeySequence(_seq))
            _act_addr.triggered.connect(self._focus_address_bar)
            self.addAction(_act_addr)
```

И метод рядом с `_focus_search` (~строка 4789):

```python
    def _focus_address_bar(self):
        """Ctrl+L / Alt+D: войти в режим ввода пути."""
        self._breadcrumb_bar.show_editor()
```

- [ ] **Step 4: Обновление completer и защита от неверного пути**

Новые методы рядом с `_focus_address_bar`:

```python
    def _refresh_path_completer(self):
        """Перечитать список папок из БД для автодополнения адресной строки."""
        from ui_threads import DirPathsThread
        from PySide6.QtCore import QStringListModel
        t = DirPathsThread(self._db, self)

        def _done(paths, err):
            if err:
                logger.warning("Path completer refresh failed: %s", err)
                return
            self._breadcrumb_bar.set_completer_model(
                QStringListModel(paths, self))

        t.finished.connect(_done)
        t.finished.connect(t.deleteLater)
        t.start()
```

В `_on_folder_loaded()` (строка 423):
- в ветке ошибки (после `self._hide_table_loading()`, до `return`) добавить возврат крошек к последнему хорошему пути:

```python
            self._breadcrumb_bar.set_path(
                getattr(self, "_last_good_path", "/"))
```

- в успешной ветке (после защиты от race, строка ~440) добавить:

```python
        self._last_good_path = path
```

- [ ] **Step 5: Ручная проверка**

Run: `.venv\Scripts\python main.py`
Проверить: полоса с «Яндекс Диск ▸ …» под тулбаром; переход двойным кликом обновляет крошки; Ctrl+L → ввод `/Загрузки` → Enter → переход; Esc → возврат; несуществующий путь → статус-бар с ошибкой, крошки откатываются.

- [ ] **Step 6: Commit**

```bash
git add ui.py
git commit -m "feat(ui): integrate BreadcrumbBar as address toolbar (Ctrl+L/Alt+D)"
```

---

### Task 8: Синк-ходок `_sync_tree_to_path()`

**Files:**
- Modify: `ui.py` (новая секция рядом с «Navigation history» ~3605; правка `_on_tree_children_loaded` ~531)

- [ ] **Step 1: Ходок**

Добавить секцию после `_update_nav_buttons`/`_nav_*` методов (после строки 3634):

```python
    # ── Tree sync (дерево следует за навигацией) ────────────

    @staticmethod
    def _ancestor_paths(path: str) -> list[str]:
        """['/A/B/C'] → ['/', '/A', '/A/B', '/A/B/C']"""
        parts = [p for p in path.split("/") if p]
        out = ["/"]
        cur = ""
        for p in parts:
            cur += "/" + p
            out.append(cur)
        return out

    def _sync_tree_to_path(self, path: str):
        """Раскрыть дерево до path и выделить целевую папку.

        Недостающих детей берём из БД (DbChildrenThread) — мгновенно;
        API-сверка каждого раскрытого узла запускается в фоне и не
        блокирует ходок.
        """
        self._tree_sync_queue = self._ancestor_paths(path)
        self._tree_sync_target = path
        self._tree_sync_step()

    def _tree_sync_step(self):
        while self._tree_sync_queue:
            p = self._tree_sync_queue.pop(0)
            item = self.tree_model._find_item(p)
            if item is None:
                # Узла нет — нужны дети родителя из БД
                parent_p = ("/".join(p.rstrip("/").split("/")[:-1])) or "/"
                parent_item = self.tree_model._find_item(parent_p)
                if parent_item is None or parent_item.db_loaded or parent_item.loaded:
                    logger.debug("Tree sync: %s unavailable, stopping", p)
                    break
                t = DbChildrenThread(self._db, parent_p, self)
                t.finished.connect(
                    lambda folders, subs, err, pp=parent_p:
                        self._on_db_children_for_sync(pp, folders, subs, err))
                t.finished.connect(t.deleteLater)
                self._active_threads.append(t)
                t.start()
                return  # продолжение в колбэке
            idx = self.tree_model._index_of(item)
            self.tree_view.expand(idx)
            if not item.loaded:
                # Фоновая API-сверка: результат придёт в merge (не ждём)
                self._fetch_folder_list(
                    p, lambda items, err, cp=p:
                        self._on_tree_children_loaded(items, err, cp))
        self._tree_sync_finish()

    def _on_db_children_for_sync(self, parent_path, folders, has_subdirs, error):
        """Дети из БД получены — вставить и продолжить ходок."""
        if error:
            logger.warning("Tree sync DB fetch failed for %s: %s",
                           parent_path, error)
            self._tree_sync_finish()
            return
        self.tree_model.populate_children_from_db(
            parent_path, folders, has_subdirs)
        self._tree_sync_step()

    def _tree_sync_finish(self):
        """Выделить целевой узел дерева (без очистки таблицы)."""
        item = self.tree_model._find_item(self._tree_sync_target)
        if item is not None:
            idx = self.tree_model._index_of(item)
            sel = self.tree_view.selectionModel()
            # _selection_updating: не давать tree-selection обнулить таблицу
            self._selection_updating = True
            sel.setCurrentIndex(
                idx, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
            self._selection_updating = False
            self.tree_view.scrollTo(idx)
```

- [ ] **Step 2: Импорт потока**

В импортах `ui.py` рядом с `ApiListThread as _ApiListThread` добавить:

```python
from ui_threads import DbChildrenThread
```

- [ ] **Step 3: Вызов из навигации + маршрутизация API-ответов**

3a. В `_navigate_to_folder()` в самый конец метода добавить:

```python
        self._sync_tree_to_path(path)
```

3b. `_on_tree_children_loaded` (строка 531) — маршрутизация merge/populate (защита от дублей для db_loaded-узлов):

```python
    def _on_tree_children_loaded(self, items, error, cloud_path: str):
        """Подпапки загружены — добавляем в модель дерева."""
        if error:
            return
        node = self.tree_model._find_item(cloud_path)
        if node is not None and node.db_loaded and not node.loaded:
            self.tree_model.merge_children_from_api(cloud_path, items)
        else:
            self.tree_model.populate_children(cloud_path, items)
        # Если после загрузки нет подпапок → скрываем стрелку раскрытия
        item = self.tree_model._find_item(cloud_path)
```

(хвост метода после строки `item = ...` сохранить без изменений)

3c. Удалить баг 1.2: в `_on_file_double_clicked` (строка 4550–4552) убрать `self.tree_view.clearSelection()`:

```python
        if item.get("is_parent_nav"):
            self._navigate_to_folder(item["cloud_path"])
            return
```

и в обработчике Backspace (строка 4582–4587):

```python
        if key == Qt.Key_Backspace:
            if self._current_path != "/":
                parent = "/".join(self._current_path.rstrip("/").split("/")[:-1]) or "/"
                self._navigate_to_folder(parent)
            return True
```

- [ ] **Step 4: Ручная проверка**

Run: `.venv\Scripts\python main.py`
Проверить: двойной клик по папке в таблице → дерево раскрывается и подсвечивает её; ввод глубокого пути в адресной строке → всё раскрывается; повторные переходы туда-обратно не дублируют узлы; выделение в таблице не сбрасывается при переходе.

- [ ] **Step 5: Commit**

```bash
git add ui.py
git commit -m "feat(ui): DB-first tree sync walker follows navigation (fix 1.2)"
```

---

### Task 9: Выделение при возврате вверх (`select_after`)

**Files:**
- Modify: `ui.py` (`_navigate_to_folder` сигнатура ~3636; три точки подъёма: `_on_file_double_clicked` ~4550, Backspace ~4582, `_nav_up` ~3630)

- [ ] **Step 1: Сигнатура и подмена снимка**

В `_navigate_to_folder` изменить сигнатуру и блок сохранения выделения:

```python
    def _navigate_to_folder(self, path: str, select_after: str = None):
```

и в теле заменить:

```python
        # Сохраняем выделение перед навигацией
        saved_selection = self._get_selected_cloud_paths()
```

на:

```python
        # Сохраняем выделение перед навигацией; select_after (подъём вверх)
        # подменяет его: после загрузки выделяем папку, из которой вышли
        saved_selection = [select_after] if select_after \
            else self._get_selected_cloud_paths()
```

- [ ] **Step 2: Три точки подъёма**

2a. Строка «..» в таблице (`_on_file_double_clicked`, ветка `is_parent_nav`):

```python
        if item.get("is_parent_nav"):
            self._navigate_to_folder(item["cloud_path"],
                                     select_after=self._current_path)
            return
```

2b. Backspace в таблице:

```python
        if key == Qt.Key_Backspace:
            if self._current_path != "/":
                parent = "/".join(self._current_path.rstrip("/").split("/")[:-1]) or "/"
                self._navigate_to_folder(parent,
                                         select_after=self._current_path)
            return True
```

2c. `_nav_up` (строка 3630):

```python
    def _nav_up(self):
        """Перейти на уровень вверх."""
        if self._current_path != "/":
            parent = "/".join(self._current_path.rstrip("/").split("/")[:-1]) or "/"
            self._navigate_to_folder(parent, select_after=self._current_path)
```

- [ ] **Step 3: Ручная проверка**

Run: `.venv\Scripts\python main.py`
Проверить: зайти в папку → Backspace → в таблице выделена папка, из которой вышли; то же для «..», Alt+Up и кнопки «Вверх»; дерево при этом подсвечивает родителя.

- [ ] **Step 4: Прогнать все тесты**

Run: `.venv\Scripts\python -m pytest tests/test_dir_tree_levels.py tests/test_tree_model_db.py tests/test_breadcrumbs.py -v`
Expected: все passed

- [ ] **Step 5: Commit**

```bash
git add ui.py
git commit -m "feat(ui): select origin folder when navigating up (Plan 1.10)"
```

---

### Task 10: Финальная верификация

**Files:** без изменений кода

- [ ] **Step 1: Полный прогон юнит-тестов**

Run: `.venv\Scripts\python -m pytest tests/ -v --ignore=tests/bookmarks_tests`
Expected: все passed, регрессий нет (test_db, test_sync, test_folder_status и др.)

- [ ] **Step 2: Полный ручной чек-лист из спеки**

Запустить `.venv\Scripts\python main.py` и пройти:
1. Двойной клик по папке в таблице → дерево раскрылось и подсветило её
2. Backspace / «..» / Alt+Up → таблица выделила бывшую папку, дерево синхронизировалось
3. Ctrl+L → ввод пути → Enter → переход; Esc → возврат к крошкам
4. Узкое окно → средние сегменты сворачиваются в «…», меню открывается
5. Офлайн (отключить сеть) → раскрытие глубокого пути из БД работает
6. Путь с 5+ уровнями, никогда не открывавшийся → всё раскрывается, стрелки корректны
7. Регресс: поиск (Esc сбрасывает), история Назад/Вперёд, DnD не сломаны

- [ ] **Step 3: Отчёт**

Зафиксировать результаты чек-листа в ответе пользователю. Обновление PLANS.md/CHANGELOG/версии — отдельный релизный цикл (см. AGENTS.md), в этот план НЕ входит.
