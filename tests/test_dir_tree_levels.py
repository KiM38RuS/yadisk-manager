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


def test_levels_underscore_name_not_wildcard(populated_db):
    """Имя с '_' не трактуется как LIKE-шаблон: чужие папки не подтягиваются."""
    populated_db.upsert_files_batch([
        {"path": "/my_folder", "name": "my_folder", "type": "dir",
         "size": 0, "modified": "2025-01-01T00:00:00Z", "md5": ""},
        {"path": "/my_folder/sub", "name": "sub", "type": "dir",
         "size": 0, "modified": "2025-01-01T00:00:00Z", "md5": ""},
        {"path": "/myXfolder", "name": "myXfolder", "type": "dir",
         "size": 0, "modified": "2025-01-01T00:00:00Z", "md5": ""},
        {"path": "/myXfolder/trap", "name": "trap", "type": "dir",
         "size": 0, "modified": "2025-01-01T00:00:00Z", "md5": ""},
    ])
    folders, subs = populated_db.get_dir_tree_levels("/my_folder")
    # Легитимный ребёнок на месте, чужой /myXfolder/trap не подтянулся
    assert [f["path"] for f in folders] == ["/my_folder/sub"]
    assert subs == set()


def test_levels_percent_name_not_wildcard(populated_db):
    """Имя с '%' не трактуется как LIKE-шаблон."""
    populated_db.upsert_files_batch([
        {"path": "/100%", "name": "100%", "type": "dir",
         "size": 0, "modified": "2025-01-01T00:00:00Z", "md5": ""},
        {"path": "/100%/подпапка", "name": "подпапка", "type": "dir",
         "size": 0, "modified": "2025-01-01T00:00:00Z", "md5": ""},
        # Ловушка: без экранирования '%' совпал бы с 'X' и вернул /100Xdecoy/trap
        {"path": "/100Xdecoy", "name": "100Xdecoy", "type": "dir",
         "size": 0, "modified": "2025-01-01T00:00:00Z", "md5": ""},
        {"path": "/100Xdecoy/trap", "name": "trap", "type": "dir",
         "size": 0, "modified": "2025-01-01T00:00:00Z", "md5": ""},
    ])
    folders, subs = populated_db.get_dir_tree_levels("/100%")
    assert [f["path"] for f in folders] == ["/100%/подпапка"]
    assert subs == set()


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


def test_db_children_thread_error(populated_db):
    """Ошибка БД → пустой результат + непустой err (контракт except-ветки)."""
    from ui_threads import DbChildrenThread

    class Boom:
        def get_dir_tree_levels(self, path):
            raise RuntimeError("db is gone")

    result = {}
    t = DbChildrenThread(Boom(), "/")
    t.finished.connect(lambda f, s, e: result.update(folders=f, subs=s, err=e))
    t.run()
    assert result["folders"] == []
    assert result["subs"] == set()
    assert "db is gone" in result["err"]


def test_dir_paths_thread_error():
    from ui_threads import DirPathsThread

    class Boom:
        def get_all_dir_paths(self):
            raise RuntimeError("boom")

    result = {}
    t = DirPathsThread(Boom())
    t.finished.connect(lambda p, e: result.update(paths=p, err=e))
    t.run()
    assert result["paths"] == []
    assert "boom" in result["err"]
