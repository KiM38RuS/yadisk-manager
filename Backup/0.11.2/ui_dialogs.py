"""
AuthDialog, SettingsDialog — диалоги авторизации и настроек.
"""

import logging
import os

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTextBrowser, QMessageBox, QCheckBox,
    QGroupBox, QFormLayout, QDialogButtonBox, QComboBox,
    QFileDialog,
)
from PySide6.QtCore import Qt

import db
import disk_api
from ui_shared import _cache_dir, _autorun_is_enabled, _autorun_set, _svg_icon

logger = logging.getLogger("ui.dialogs")


class AuthDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Авторизация Яндекс.Диска")
        self.setMinimumSize(520, 420)
        self.token = None

        layout = QVBoxLayout(self)

        instructions = QTextBrowser(self)
        instructions.setOpenExternalLinks(True)
        instructions.setHtml("""
        <h2>Первый запуск</h2>
        <p>Создайте OAuth-токен для доступа к вашему Яндекс.Диску:</p>
        <ol>
          <li>Откройте <a href='https://oauth.yandex.ru/'>oauth.yandex.ru</a></li>
          <li>Нажмите <b>«Создать»</b> → выберите <b>«Для доступа к API или отладки»</b></li>
          <li>Заполните название сервиса (любое, например "MyDisk")</li>
          <li>В поле <b>«Название доступа»</b> выберите права:<br>
              <code>cloud_api:disk.info</code><br>
              <code>cloud_api:disk.read</code><br>
              <code>cloud_api:disk.write</code></li>
          <li>Нажмите <b>«Создать приложение»</b></li>
          <li>Скопируйте <b>ClientID</b> из карточки приложения</li>
          <li>Откройте в браузере:<br>
              <code>https://oauth.yandex.ru/authorize?response_type=token&amp;client_id=ВАШ_CLIENT_ID</code></li>
          <li>Нажмите <b>«Войти как ...»</b> — произойдёт редирект</li>
          <li>На открывшейся странице скопируйте токен и вставьте ниже</li>
        </ol>
        """)
        layout.addWidget(instructions)

        self.token_input = QLineEdit(self)
        self.token_input.setPlaceholderText("Вставьте OAuth-токен сюда")
        layout.addWidget(QLabel("OAuth-токен:"))
        layout.addWidget(self.token_input)

        btn_layout = QHBoxLayout()

        check_btn = QPushButton("Проверить и сохранить", self)
        check_btn.clicked.connect(self._verify)
        btn_layout.addWidget(check_btn)

        btn_layout.addStretch()

        cancel_btn = QPushButton("Отмена", self)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

    def _verify(self):
        token = self.token_input.text().strip()
        if not token:
            QMessageBox.warning(self, "Ошибка", "Введите токен")
            return
        try:
            api = disk_api.YaDiskAPI(token)
            info = api.get_disk_info()
            total = info.get("total_space", 0) / (1024**3)
            used = info.get("used_space", 0) / (1024**3)
            QMessageBox.information(
                self, "Успех!",
                f"✅ Токен работает!\n\n"
                f"Всего: {total:.1f} GB\nЗанято: {used:.1f} GB\n"
                f"Свободно: {total - used:.1f} GB",
            )
            self.token = token
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка",
                                 f"Не удалось подключиться:\n{e}")


class SettingsDialog(QDialog):
    def __init__(self, parent=None, on_cache_changed=None):
        super().__init__(parent)
        self._on_cache_changed = on_cache_changed
        self._old_cache = _cache_dir()
        self.setWindowTitle("Настройки")
        self.setMinimumSize(400, 250)

        layout = QVBoxLayout(self)

        # Автозапуск
        grp = QGroupBox("Запуск", self)
        frm = QFormLayout(grp)
        self._chk_autorun = QCheckBox("Автозапуск при старте Windows")
        self._chk_autorun.setChecked(_autorun_is_enabled())
        frm.addRow(self._chk_autorun)
        layout.addWidget(grp)

        # Внешний вид
        grp3 = QGroupBox("Внешний вид", self)
        frm3 = QFormLayout(grp3)
        self._theme_combo = QComboBox()
        self._theme_combo.addItem("Системная", "system")
        self._theme_combo.addItem("Светлая", "light")
        self._theme_combo.addItem("Тёмная", "dark")
        current = db.get_theme()
        idx = self._theme_combo.findData(current)
        if idx >= 0:
            self._theme_combo.setCurrentIndex(idx)
        frm3.addRow('Тема оформления:', self._theme_combo)
        # Live-переключение темы при выборе в комбобоксе
        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        self._chk_save_geo = QCheckBox('Сохранять положение окна')
        self._chk_save_geo.setChecked(db.get_save_window_geometry())
        frm3.addRow(self._chk_save_geo)
        layout.addWidget(grp3)

        # Расположение файлов
        grp2 = QGroupBox("Расположение файлов", self)
        frm2 = QFormLayout(grp2)

        cache_layout = QHBoxLayout()
        self._cache_edit = QLineEdit(_cache_dir())
        browse_btn = QPushButton("Обзор…")
        cache_layout.addWidget(self._cache_edit, 1)
        cache_layout.addWidget(browse_btn)
        frm2.addRow("Папка кеша:", cache_layout)

        frm2.addRow("База данных:", QLabel(db.DB_PATH))
        frm2.addRow("Конфиг:", QLabel(db.CONFIG_PATH))
        layout.addWidget(grp2)

        # Расширенное
        grp4 = QGroupBox("Расширенное", self)
        frm4 = QFormLayout(grp4)
        self._chk_zip = QCheckBox(
            "Показывать «Скачать как ZIP» в контекстном меню папок")
        self._chk_zip.setChecked(db.get_zip_download_enabled())
        self._chk_zip.setToolTip(
            "Добавляет пункт «Скачать как ZIP» в контекстное меню папок.\n"
            "Архив скачивается в выбранную вами папку на ПК,\n"
            "не затрагивая кеш и базу данных.")
        frm4.addRow(self._chk_zip)
        self._chk_log_startup = QCheckBox(
            "Показывать окно лога при запуске")
        self._chk_log_startup.setChecked(db.get_show_log_on_startup())
        frm4.addRow(self._chk_log_startup)
        layout.addWidget(grp4)

        layout.addStretch()

        btn_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        btn_box.accepted.connect(self._save)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        # Сигнал для кнопки обзора
        browse_btn.clicked.connect(self._browse_cache)

    def _browse_cache(self):
        chosen = QFileDialog.getExistingDirectory(
            self, 'Выберите папку для файлов',
            self._cache_edit.text(),
        )
        if chosen:
            self._cache_edit.setText(chosen)

    def _on_theme_changed(self):
        """Live-переключение темы — сразу при выборе в комбобоксе."""
        theme = self._theme_combo.currentData()
        db.set_theme(theme)
        if self.parent() and hasattr(self.parent(), '_apply_theme'):
            self.parent()._apply_theme()

    def _save(self):
        _autorun_set(self._chk_autorun.isChecked())
        new_cache = self._cache_edit.text().strip()
        if new_cache and new_cache != self._old_cache:
            os.makedirs(new_cache, exist_ok=True)
            db.set_cache_dir(new_cache)
            if self._on_cache_changed:
                self._on_cache_changed(self._old_cache, new_cache)
        # Сохранить тему
        db.set_theme(self._theme_combo.currentData())
        # Сохранить настройку геометрии
        db.set_save_window_geometry(self._chk_save_geo.isChecked())
        # Сохранить настройку ZIP-скачивания
        db.set_zip_download_enabled(self._chk_zip.isChecked())
        # Сохранить настройку показа лога при запуске
        db.set_show_log_on_startup(self._chk_log_startup.isChecked())
        if self.parent() and hasattr(self.parent(), "_apply_theme"):
            self.parent()._apply_theme()
        self.accept()
