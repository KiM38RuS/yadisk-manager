"""
AuthDialog, SettingsDialog — диалоги авторизации и настроек.
"""

import logging
import os

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTextBrowser, QMessageBox, QCheckBox,
    QGroupBox, QFormLayout, QDialogButtonBox, QComboBox,
    QFileDialog, QProgressBar,
)
from PySide6.QtCore import Qt, QTimer

import db
import disk_api
import updater
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
        self._chk_updates = QCheckBox(
            "Проверять обновления автоматически")
        self._chk_updates.setChecked(db.get_check_updates_enabled())
        self._chk_updates.setToolTip(
            "При наличии новой версии на GitHub появится уведомление\n"
            "в трее — обновление займёт меньше минуты.")
        frm4.addRow(self._chk_updates)
        self._chk_beta = QCheckBox("Обновляться до бета-версий")
        self._chk_beta.setChecked(db.get_update_beta_enabled())
        self._chk_beta.setToolTip(
            "Проверять и предлагать pre-release версии с GitHub.\n"
            "По умолчанию выключено — предлагаются только\n"
            "стабильные релизы.")
        frm4.addRow(self._chk_beta)
        self._chk_auto = QCheckBox("Обновляться автоматически")
        self._chk_auto.setChecked(db.get_auto_update_enabled())
        self._chk_auto.setToolTip(
            "Обновления скачиваются и применяются в фоне, без вопросов,\n"
            "как в Яндекс Диск 3.0. Новое обновление применится\n"
            "при следующем перезапуске программы.")
        frm4.addRow(self._chk_auto)
        self._chk_net_heal = QCheckBox("Обход DNS-блокировок (DoH)")
        self._chk_net_heal.setChecked(bool(db.get_net_heal_enabled()))
        self._chk_net_heal.setToolTip(
            "Если DNS вашей сети блокирует Яндекс.Диск, имена будут\n"
            "резолвиться через DNS-over-HTTPS (Cloudflare/Google).\n"
            "Работает без админ-прав и не меняет системные настройки.")
        frm4.addRow(self._chk_net_heal)
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
        """Live-переключение темы — сразу при выборе в комбобоксе.

        Применение откладывается через singleShot(0): при клике по popup
        QComboBox сигнал currentIndexChanged эмитится ДО закрытия popup,
        а _apply_theme реполошит ВСЕ виджеты (app.allWidgets()) — включая
        открытый popup. Repolish открытого popup повреждает кучу Qt
        (STATUS_HEAP_CORRUPTION, 0xc0000374). singleShot(0) выполняется
        после обработки события клика — popup уже закрыт.
        """
        theme = self._theme_combo.currentData()
        logger.info("Theme selected in settings combo: %r (was %r)",
                    theme, db.get_theme())
        db.set_theme(theme)
        parent = self.parent()
        if parent and hasattr(parent, '_apply_theme'):
            QTimer.singleShot(0, parent._apply_theme)

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
        # Сохранить настройку авто-проверки обновлений
        db.set_check_updates_enabled(self._chk_updates.isChecked())
        # Сохранить настройку бета-версий
        db.set_update_beta_enabled(self._chk_beta.isChecked())
        # Сохранить настройку бесшумных обновлений
        db.set_auto_update_enabled(self._chk_auto.isChecked())
        # Сохранить настройку обхода DNS-блокировок
        db.set_net_heal_enabled(self._chk_net_heal.isChecked())
        # Тему НЕ переприменяем здесь: она уже применена вживую при выборе
        # в комбобоксе (_on_theme_changed). Повторный _apply_theme() при
        # закрытии диалога вызывает полный unpolish/polish всех виджетов,
        # что в PySide6 даёт STATUS_HEAP_CORRUPTION (0xc0000374).
        self.accept()


class UpdateDialog(QDialog):
    """Окно обновления: предложение + скачивание с полосой прогресса.

    Жизненный цикл:
      1. Показывает версию и кнопки [Обновить] [Позже]
      2. [Обновить] → поток скачивания (UpdateDownloadThread), прогресс-бар
      3. Готово → apply_update() (frozen: скрытый bat, перезапуск) или
         сообщение с путём (dev: применить вручную).
    """

    def __init__(self, info: updater.UpdateInfo, parent=None):
        super().__init__(parent)
        self._info = info
        self._thread = None
        self._download_path = None
        self.setWindowTitle("Обновление YaDisk Manager")
        # WindowModal (не ApplicationModal): блокируется только окно
        # Менеджера; окно лога остаётся интерактивным во время обновления.
        self.setWindowModality(Qt.WindowModal)
        self.setFixedWidth(440)

        layout = QVBoxLayout(self)

        self._lbl_title = QLabel(
            f"Доступна новая версия <b>{info.version}</b>")
        self._lbl_title.setWordWrap(True)
        layout.addWidget(self._lbl_title)

        notes = (info.notes or "").strip()
        if notes:
            self._lbl_notes = QLabel(notes[:500])
            self._lbl_notes.setWordWrap(True)
            self._lbl_notes.setStyleSheet("color: #666; font-size: 11px;")
            layout.addWidget(self._lbl_notes)
        else:
            self._lbl_notes = None

        size_mb = info.size / 1048576 if info.size else 0
        self._lbl_status = QLabel(
            f"Размер: {size_mb:.1f} МБ. Скачивание займёт меньше минуты.")
        self._lbl_status.setWordWrap(True)
        layout.addWidget(self._lbl_status)

        self._bar = QProgressBar(self)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setVisible(False)
        layout.addWidget(self._bar)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self._btn_update = QPushButton("Обновить")
        self._btn_update.setDefault(True)
        self._btn_update.clicked.connect(self._start_download)
        btns.addWidget(self._btn_update)
        self._btn_later = QPushButton("Позже")
        self._btn_later.clicked.connect(self.reject)
        btns.addWidget(self._btn_later)
        layout.addLayout(btns)

    # ── Скачивание ────────────────────────────────────────

    def _start_download(self):
        self._btn_update.setEnabled(False)
        self._btn_later.setEnabled(False)
        self._bar.setVisible(True)
        self._lbl_status.setText("Скачивание обновления…")
        self._thread = updater.UpdateDownloadThread(self._info, parent=self)
        self._thread.progress.connect(self._on_progress)
        self._thread.finished.connect(self._on_downloaded)
        self._thread.start()

    def _on_progress(self, done: int, total: int):
        if total > 0:
            self._bar.setValue(int(done * 100 / total))
            self._lbl_status.setText(
                f"Скачивание: {done / 1048576:.1f} / {total / 1048576:.1f} МБ")
        else:
            self._bar.setRange(0, 0)  # неизвестный размер — индетерминант
            self._lbl_status.setText(f"Скачивание: {done / 1048576:.1f} МБ")

    def _on_downloaded(self, path, error):
        self._thread = None
        if error or not path:
            self._bar.setVisible(False)
            self._lbl_status.setText(f"Не удалось скачать обновление: {error}")
            self._btn_update.setText("Повторить")
            self._btn_update.setEnabled(True)
            self._btn_later.setEnabled(True)
            self._btn_update.clicked.disconnect()
            self._btn_update.clicked.connect(self._start_download)
            return
        self._download_path = path
        self._bar.setRange(0, 0)
        self._lbl_status.setText("Установка…")
        self._btn_later.setText("Закрыть")
        self._btn_later.setEnabled(True)
        ok = updater.apply_update(path)
        if ok:
            # apply_update.bat ждёт 3 сек, потом заменяет exe и запускает
            # новый. Закрываем приложение (и это окно).
            QTimer.singleShot(500, self._finish_and_quit)
        else:
            # dev-режим: только скачали
            self._lbl_status.setText(
                f"Скачано: {path}\nПримените вручную (dev-режим).")

    def _finish_and_quit(self):
        try:
            app = self.window().windowHandle()  # noqa: F841 — keep reference
        except Exception:
            pass
        self.accept()
        # Останавливаем Qt-приложение: bat переименует exe и запустит новый
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()
