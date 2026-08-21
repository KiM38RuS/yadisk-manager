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
