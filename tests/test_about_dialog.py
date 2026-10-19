"""
Тесты окна «О программе» и иконки пункта «Справка → О программе» (v0.12.5).

Дефекты (репорт пользователя):
1. В окне «О программе» не было иконки программы — статический хелпер
   QMessageBox.about() не принимает иконку, а иконка приложения
   (app.setWindowIcon) в main.py не задаётся вовсе.
2. В окне «О программе» не было ссылки на репозиторий GitHub — строка
   никогда не попадала в ui.py (только в CHANGELOG ошибочно значилась).
3. Иконка «Справка → О программе» (help-svgrepo-com.svg, fill=#0F0F0F)
   не имела светлого варианта: _svg_icon() берёт «base-light.svg» только
   если файл существует, поэтому в тёмной теме иконка оставалась чёрной.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import db
import disk_api
import ui
import ui_shared
from _version import VERSION
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel


def _make_app():
    """Вернуть существующий QApplication или создать новый."""
    return QApplication.instance() or QApplication([])


def _make_main_window():
    """Создать MainWindow в offscreen-режиме (без сетевых вызовов)."""
    database = db.Database(":memory:")
    api = disk_api.YaDiskAPI.__new__(disk_api.YaDiskAPI)
    api._session = mock.MagicMock()
    return ui.MainWindow(api, database)


class TestAboutDialog(unittest.TestCase):
    """Окно «О программе»: иконка программы + кликабельная ссылка."""

    def setUp(self):
        self.app = _make_app()
        self.win = _make_main_window()

    def tearDown(self):
        try:
            self.win.deleteLater()
        except Exception:
            pass

    def test_about_html_contains_repo_link(self):
        """В тексте окна есть ссылка на репозиторий GitHub."""
        html = self.win._about_html()
        self.assertIn(ui._repo_url(), html)
        self.assertIn("github.com/KiM38RuS/yadisk-manager", html)

    def test_about_html_contains_version(self):
        """В тексте окна есть текущая версия."""
        self.assertIn(VERSION, self.win._about_html())

    def test_about_box_has_program_icon(self):
        """Иконка программы есть и в теле окна, и в заголовке."""
        box = self.win._build_about_box()
        self.assertFalse(box.iconPixmap().isNull(),
                         "в теле окна «О программе» нет иконки программы")
        self.assertFalse(box.windowIcon().isNull(),
                         "в заголовке окна «О программе» нет иконки")

    def test_about_box_text_has_link_and_version(self):
        """Текст окна — rich text с ссылкой и версией."""
        box = self.win._build_about_box()
        self.assertEqual(box.textFormat(), Qt.RichText)
        self.assertIn(ui._repo_url(), box.text())
        self.assertIn(VERSION, box.text())

    def test_about_link_opens_in_browser(self):
        """Клик по ссылке уходит во внешний браузер (QLabel.openExternalLinks)."""
        box = self.win._build_about_box()
        labels = box.findChildren(QLabel)
        self.assertTrue(labels, "в окне «О программе» нет QLabel с текстом")
        self.assertTrue(any(lbl.openExternalLinks() for lbl in labels),
                        "ссылка в окне не открывается во внешнем браузере")

    def test_about_box_is_window_modal(self):
        """WindowModal, а не ApplicationModal: окно лога не блокируется."""
        box = self.win._build_about_box()
        self.assertEqual(box.windowModality(), Qt.WindowModal)

    def test_show_about_execs_the_box(self):
        """_show_about() показывает собранное окно (exec вызывается).

        Подменяем метод на экземпляре (Python-атрибут), а НЕ патчим
        QMessageBox.exec через mock.patch.object: патч C++-метода
        shiboken-класса рушит процесс fail-fast 0xC0000409.
        """
        box_mock = mock.MagicMock()
        self.win._build_about_box = lambda: box_mock
        self.win._show_about()
        self.assertTrue(box_mock.exec.called)

    def test_repo_url_falls_back_when_repo_empty(self):
        """Пустой UPDATE_REPO не даёт битую ссылку «https://github.com/»."""
        with mock.patch.object(ui.updater, "UPDATE_REPO", ""):
            self.assertNotEqual(ui._repo_url(), "https://github.com/")
            self.assertIn("github.com/", ui._repo_url())


class TestHelpIconLightVariant(unittest.TestCase):
    """Светлый вариант иконки «Справка → О программе» для тёмной темы."""

    def setUp(self):
        self.app = _make_app()
        ui_shared._clear_svg_icon_cache()

    def tearDown(self):
        ui_shared._clear_svg_icon_cache()

    def test_light_file_exists(self):
        """Assets/help-svgrepo-com-light.svg существует."""
        path = os.path.join(ui_shared.ASSETS_DIR,
                            "help-svgrepo-com-light.svg")
        self.assertTrue(os.path.isfile(path), f"нет файла {path}")

    def test_light_variant_is_white(self):
        """Светлый вариант действительно светлый (не #0F0F0F)."""
        path = os.path.join(ui_shared.ASSETS_DIR,
                            "help-svgrepo-com-light.svg")
        with open(path, encoding="utf-8") as fh:
            svg = fh.read().lower()
        self.assertNotIn("#0f0f0f", svg)
        self.assertIn("#ffffff", svg)

    def test_dark_theme_resolves_to_light_file(self):
        """В тёмной теме _get_svg_path отдаёт -light файл."""
        with mock.patch.object(db, "get_theme", return_value="dark"):
            path = ui_shared._get_svg_path("help-svgrepo-com.svg")
        self.assertTrue(
            path.endswith("help-svgrepo-com-light.svg"),
            f"в тёмной теме выбрана иконка {path}")

    def test_light_theme_keeps_dark_icon(self):
        """В светлой теме берётся обычный (чёрный) вариант."""
        with mock.patch.object(db, "get_theme", return_value="light"):
            path = ui_shared._get_svg_path("help-svgrepo-com.svg")
        self.assertTrue(path.endswith("help-svgrepo-com.svg"))
        self.assertNotIn("-light", os.path.basename(path))


class TestMenuIconsRefreshOnThemeChange(unittest.TestCase):
    """Смена темы «на лету» перерисовывает иконки пунктов меню."""

    def setUp(self):
        self.app = _make_app()
        ui_shared._clear_svg_icon_cache()
        self.win = _make_main_window()

    def tearDown(self):
        try:
            self.win.deleteLater()
        except Exception:
            pass
        ui_shared._clear_svg_icon_cache()

    def test_about_menu_icon_differs_between_themes(self):
        """Иконка «О программе» в тёмной теме — другой пиксмап (light).

        Регресс: меню строится один раз, а _apply_theme перезагружал только
        иконки тулбара/трея → после смены темы в тёмном меню оставалась
        чёрная иконка до перезапуска.
        """
        ui_shared._clear_svg_icon_cache()
        with mock.patch.object(db, "get_theme", return_value="light"):
            self.win._refresh_menu_icons()
            key_light = self.win._act_about.icon().cacheKey()
        ui_shared._clear_svg_icon_cache()
        with mock.patch.object(db, "get_theme", return_value="dark"):
            self.win._refresh_menu_icons()
            key_dark = self.win._act_about.icon().cacheKey()
        self.assertNotEqual(
            key_light, key_dark,
            "иконка «О программе» не меняется при смене темы")

    def test_refresh_menu_icons_keeps_all_icons(self):
        """После перезагрузки ни один пункт меню не теряет иконку."""
        self.win._refresh_menu_icons()
        for name in ("_act_save", "_act_leave", "_act_refresh",
                     "_act_settings", "_act_logout", "_act_quit",
                     "_act_copy", "_act_cut", "_act_paste", "_act_delete",
                     "_act_new_folder", "_act_get_link", "_act_restart",
                     "_act_check_updates", "_act_about"):
            act = getattr(self.win, name, None)
            self.assertIsNotNone(act, f"нет пункта меню {name}")
            self.assertFalse(act.icon().isNull(),
                             f"пункт {name} остался без иконки")


class TestHelpMenuContent(unittest.TestCase):
    """Состав меню «Справка» (документация обещала два пункта)."""

    def setUp(self):
        self.app = _make_app()
        self.win = _make_main_window()

    def tearDown(self):
        try:
            self.win.deleteLater()
        except Exception:
            pass

    def _help_menu(self):
        """Меню «Справка».

        Важно: `menuBar().actions()` отдаёт QAction-обёртки, владение
        которыми PySide6 считает своим — если список действий собрать
        мусором, вместе с обёрткой уничтожается C++-действие и его QMenu
        («Internal C++ object already deleted»). Поэтому ссылки держим
        на self до конца теста.
        """
        self._top_actions = self.win.menuBar().actions()
        for act in self._top_actions:
            if act.text() == "Справка":
                self._help_menu_ref = act.menu()
                return self._help_menu_ref
        return None

    def test_help_menu_has_both_items(self):
        menu = self._help_menu()
        self.assertIsNotNone(menu, "в менюбаре нет меню «Справка»")
        texts = [a.text() for a in menu.actions() if a.text()]
        self.assertIn("Проверить обновления…", texts)
        self.assertIn("О программе", texts)

    def test_check_updates_item_has_icon(self):
        menu = self._help_menu()
        item = next(a for a in menu.actions()
                    if a.text() == "Проверить обновления…")
        self.assertFalse(item.icon().isNull())

    def test_tray_reuses_the_same_check_updates_action(self):
        """В трее и в меню — один и тот же QAction (единый обработчик)."""
        self.assertTrue(hasattr(self.win, "_tray_menu"),
                        "меню трея не создано")
        self.assertIn(self.win._act_check_updates,
                      self.win._tray_menu.actions())


if __name__ == "__main__":
    unittest.main()
