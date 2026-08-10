# YaDisk Manager — Bootstrap (L0)

> Identity: Python developer, PySide6 GUI, REST API integrations.
> Language: user speaks Russian, code/replies in Russian or English.
> Knowledge base root: `D:\\YandexDisk\\Sync\\Coding\\AIWikiVault`
> Current version: **0.11.9** (see `Backup/` for history)
>
> ## 📦 Версии и бэкапы
>
> **Правила бэкапа:**
> - После значительных изменений кода → обновить версию
> - Сохранить: `.py` (все корневые, включая `ui_*.py`), `.txt` в `Backup/{version}/`
> - Не копировать: `.md`, `.ico`, `LICENSE`, SVG-иконки (`Assets/`), `tests/`, `wiki/`, `bookmarks_tests/`, `Test/`, `__pycache__`
> - Перед обновлением версии убедиться, что бэкап предыдущей версии сохранён
> - Записать информацию в AGENTS.md
>
> ## 📦 Проект

**YaDisk Manager** — бесплатный on-demand клиент Яндекс.Диска для Windows (альтернатива десктопному приложению Яндекса после введения платной подписки).

### Архитектура

```
main.py              → точка входа, QApplication, инициализация
├── ui.py            → MainWindow (окно, трей, навигация, очереди, ~3.4 K)
│   ├── ui_shared.py          → константы, _svg_icon, _cache_dir, хелперы
│   ├── ui_braille_spinner.py → анимированный спиннер (Braille)
│   ├── ui_log.py             → LogSignal, LogHandler, LogWindow
│   ├── ui_workers.py         → DownloadWorker, UploadWorker, ZipDownloadWorker
│   ├── ui_tree_model.py      → FolderTreeItem, FolderTreeModel
│   ├── ui_table_model.py     → RowHoverDelegate, FileTableModel, FileTableSortModel
│   ├── ui_dialogs.py         → AuthDialog, SettingsDialog
│   ├── ui_threads.py         → 10 классов QThread (Sync, ApiList, Search, …)
├── ui_search_edit.py     → SearchEdit (строка поиска с историей)
├── updater.py            → система обновлений (GitHub Releases, прогресс, apply+relaunch)
├── sync.py          → двусторонняя MD5-синхронизация (full_sync, migrate_cache)
├── db.py            → SQLite (статусы файлов) + JSON-конфиг (токен)
├── disk_api.py      → REST API Яндекс.Диска (навигация, CRUD, публичные ссылки)
├── watcher.py       → watchdog (следит за изменениями локального кеша → авто-загрузка)
├── research.md      → исследование вариантов обратной синхронизации
└── requirements.txt → PySide6, requests, watchdog
```

### Хранение данных

| Что | Путь |
|---|---|
| Кеш скачанных файлов | `%USERPROFILE%\.yadisk-cache\` (пользователь может указать другой путь) |
| База данных (SQLite) | `%APPDATA%\yadisk-client\yadisk.db` |
| Конфиг (токен) | `%APPDATA%\yadisk-client\config.json` |

### Запуск (без установки зависимостей в систему)

| Способ | Когда | Команда |
|---|---|---|
| **`YaDiskManager.exe`** | повседневный запуск (двойной клик) | — (собран PyInstaller'ом, Python не нужен) |
| **`run.bat`** | запуск из исходников | двойной клик → `.venv\Scripts\pythonw.exe main.py` |
| **dev-режим** | разработка/тесты | `.venv\Scripts\python main.py` (логи в консоль) |

Подготовка окружения (один раз, вместо `pip install` в систему):

```cmd
cd D:\Program_files\YaDiskManager
uv venv .venv --python "C:\Program Files\Python312\python.exe"
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

Логи (в т.ч. из exe без консоли): `%APPDATA%\yadisk-client\yadisk.log` (ротация: 1 МБ × 3 бэкапа, всего ≤4 МБ)

### 🔧 Релиз

**IPC (управление из терминала)** — `ipc.py` слушает TCP 127.0.0.1:43210.

| Что | Команда |
|---|---|
| Флаг вкл/выкл | `IPC_ENABLED = True` / `False` в `ipc.py:22` |
| Перезапуск | `python -c "from ipc import send_ipc_command; send_ipc_command('restart')"` |
| Показать окно | `python -c "from ipc import send_ipc_command; send_ipc_command('raise')"` |
| Завершить (чисто) | `python -c "from ipc import send_ipc_command; send_ipc_command('shutdown')"` |

**Правило:** перед компиляцией `.exe` или перед релизом (`.py`-дистрибутив) **обязательно** выставить `IPC_ENABLED = False` в `ipc.py`. В разработке держать `True`.

**Сборка exe:** `build_exe.bat` — автоматизирует правило: копирует исходники во временную папку, выставляет `IPC_ENABLED = False` только там (исходники в корне не трогает), собирает `YaDiskManager.exe` (onefile, иконка `icon-main.ico`) и кладёт в корень проекта. Требует `uv pip install --python .venv\Scripts\python.exe pyinstaller`.

### 📦 Инсталлер (Inno Setup)

- Скрипт: `installer.iss` (корень проекта). Сборка:
  `"D:\Program_files\InnoSetup\ISCC.exe" installer.iss` → `dist\Setup_YaDiskManager_v<ver>.exe`
- Inno Setup установлен портабельно в `D:\Program_files\InnoSetup` (не в систему).
- Инсталлер ставит в `%ProgramFiles%\YaDiskManager` (или в AppData при отказе от админ-прав), ярлык в меню «Пуск», опционально на рабочий стол, деинсталлятор. При удалении чистит артефакты апдейтера (`apply_update.bat`, `*.old`, `*.new.exe`).
- Версия в инсталлере — из `_version.py` (передавать через `/DMyAppVersion=` при сборке, по умолчанию в .iss).

### 🔄 Обновления (updater.py)

- **Перед релизом** заполнить `UPDATE_REPO = "username/repo"` в `updater.py` (пока пусто — проверка отключена).
- Релиз на GitHub: тег `v0.11.x` + ассеты `YaDiskManager.exe` (обязательно) и `Setup_YaDiskManager_v*.exe` (инсталлер, опционально). Апдейтер ищет ассет с именем, оканчивающимся на `.exe` и не содержащим `setup`.
- Обновление применяется только в frozen-режиме (exe): скачивание в `YaDiskManager.new.exe` рядом с текущим, скрытый `apply_update.bat` ждёт выхода приложения, переименовывает и запускает новый exe. В dev-режиме — только скачивание в temp.
- Авто-проверка: через 20 сек после старта и каждые 4 часа; уведомление в трее; настройка в Настройках (по умолчанию вкл.).

### 🏷️ Именование сессий

При изменении версии проекта (bump в `_version.py`) агент автоматически переименовывает текущую сессию в формат:
```
YaDiskManager v0.X.Y [краткое описание изменений]
```
Команда: `hermes sessions rename $HERMES_SESSION_ID "YaDiskManager v0.X.Y ..."`
Это позволяет легко находить в списке сессий, с какой версией велась работа.

### 🧪 Тестирование файловых операций (агентное)

Агент может самостоятельно тестировать файловые операции без GUI:

1. **Запустить программу** в фоне:
   ```bash
   cd D:\Program_files\YaDiskManager && python main.py
   ```
   (требуется OAuth-токен, иначе вылетит диалог — тестирование только после первой авторизации)

2. **Дождаться старта** (IPC-сервер поднимается на 127.0.0.1:43210):
   ```python
   from ipc import send_ipc_command
   resp = send_ipc_command('raise')  # проверить, что отвечает
   ```

3. **Выполнить операции** через API напрямую (не через GUI):
   ```python
   import db, disk_api
   api = disk_api.YaDiskAPI(db.get_token())
   # например: api.copy("/path/a", "/path/b", overwrite=False)
   ```
   **Проверить побочные эффекты** в БД:
   ```python
   database = db.Database()
   rec = database.get_file("/path/b")
   assert rec is not None
   ```

4. **Перезагрузить** (если менялись файлы, которые подхватываются при старте):
   ```python
   send_ipc_command('restart')
   ```

5. **Завершить чисто** (не оставляет мёртвый значок в трее):
   ```python
   send_ipc_command('shutdown')
   ```

6. **Проверить статус** `pending_ops`:
   ```python
   database = db.Database()
   ops = database.get_pending_ops()
   assert len(ops) == 0  # все операции должны быть завершены
   ```

**Ограничения:** 
- Без GUI нельзя кликнуть по кнопке «Скачать», «Удалить» и т.д. — только прямые API-вызовы.
- Drag-and-drop из Проводника не воспроизвести без GUI.
- Для тестов, где нужен UI (кнопки, двойной клик), требуется `computer_use` или ручной запуск пользователем.

## 🧠 Knowledge Layers

| Layer | Что | Когда грузить |
|-------|-----|---------------|
| **L0** | Этот файл (Bootstrap) | Всегда в контексте |
| **L1** | `wiki/_routing.md` + `wiki/_index.md` | Если тема не покрыта Quick Triggers |
| **L2** | `wiki/rules/*`, `wiki/references/*`, `wiki/architecture/*`, `wiki/workflows/*` | По триггеру из Quick Triggers |
| **L3** | `memory/inbox.md` + `memory/_protocol.md` | Проверить inbox при старте каждой сессии |

## ⚡ Quick Triggers (L2 — загружать по теме)

```yaml
triggers:
  - id: python-pyqt
    keywords: [python, pyside6, qt, qwidget, qapplication, qtreewidget, qtablewidget, qthread, qprogressbar]
    load:
      - wiki/references/uv-package-manager.md
      - wiki/rules/uv-install-best-practices.md
      # PySide6 специфика дописывается через self-learning

  - id: yandex-api
    keywords: [яндекс, yandex, disk, api, rest, oauth, token, upload, download, delete, публичная ссылка]
    load:
      - wiki/architecture/yandex-disk-api.md       # если есть
      - wiki/references/yandex-disk-rest-api.md    # если есть

  - id: sqlite
    keywords: [sqlite, database, db, таблица, запрос, sql, миграция]
    load:
      - wiki/references/python-sqlite-patterns.md  # если есть

  - id: watchdog
    keywords: [watchdog, watcher, observer, file system, файловый наблюдатель, авто-загрузка]
    load:
      - wiki/references/watchdog-usage.md          # если есть

  - id: windows-integration
    keywords: [windows, tray, трей, автозапуск, реестр, notification, уведомление, startup, shell]
    load:
      - wiki/rules/sound-notification.md

  - id: versioning
    keywords: [changelog, version, версионирование, bump, релиз]
    load:
      - wiki/rules/changelog-versioning.md

  - id: hooks
    keywords: [hook, хук, session-start, session-end, pre-compact, context injection]
    load:
      - wiki/references/claude-code-hooks.md
      - wiki/architecture/session-context-injection.md
```

Если ни один триггер не подошёл — читай `wiki/_routing.md` (полная YAML-таблица) или `wiki/_index.md` (оглавление).

## 📐 Проектные нюансы

### PySide6
- **Единственный QApplication** — создаётся один раз в `main.py`, живёт всё время программы.
- `app.setQuitOnLastWindowClosed(False)` — окно скрывается, трей остаётся.
- Воркеры через `QThread` / `QRunnable` — UI не блокировать.
- Сигналы/слоты — стандартный Qt-механизм (`Signal`, `Slot`).

### REST API Яндекс.Диска
- OAuth-токен с правами на Яндекс.Диск (все галочки).
- API-лимит: 40 запросов/сек.
- Базовый URL: `https://cloud-api.yandex.net/v1/disk/`.
- Загрузка по частям (multipart) если файл большой — не реализовано, но имей в виду.

### SQLite (db.py)
- Отслеживание статусов: `cloud` (только в облаке), `downloaded` (скачан локально), `modified` (изменён локально).
- Конфиг отдельно: `config.json` (токен, настройки).

### Watchdog (watcher.py)
- Следит за `%USERPROFILE%\.yadisk-cache\`.
- На события `modified`, `created`, `deleted` → авто-загрузка/синхронизация.
- Debounce изменений (не загружать 10 раз за секунду).

### Обратная синхронизация (облако → локальный кеш)
Реализована 3-слойным гибридом:
1. **Poll** (`_on_poll_result`, каждые 60с) — `GET /resources/last-uploaded?limit=50`, сравнивает `md5` с `last_sync_md5` для существующих downloaded файлов
2. **Bulk** (`_AllFilesThread`, каждые 15 мин) — полный обход всех файлов с пагинацией, `get_changed_downloaded_files()` находит расхождения
3. **Lazy nav** (`_load_folder_local`) — при открытии папки проверяет каждый downloaded файл
Все три слоя используют `_queue_reverse_meta_fetch()` → `MetaFetchThread` → `_on_reverse_meta_fetched()`:
- Если изменилось только облако → авто-скачивание
- Если изменилось и локально, и в облаке → конфликт (`_handle_conflict`)

### run.bat
- Путь в `run.bat` может отличаться от актуального — поправь при необходимости.

## 📝 Self-Learning (L3)

Агент пишет в `memory/inbox.md` при 4 триггерах:
1. **Неожиданное поведение системы** — ошибка, которой не ждал
2. **Workaround** — пришлось сделать что-то неочевидное
3. **Недокументированное API** — поведение не из документации
4. **Явная поправка от пользователя** — «нет, делай вот так»

### Жизненный цикл знания

```
draft (inbox, пишет агент)
  ↓ авто-промоут при повторе (2+ раза)
validated (wiki/rules/, подтверждено практикой)
  ↓ только с разрешения пользователя
core (этот файл, Critical Rules — конституция)
```

### Формат записи в inbox
```
[{TAG}] Краткое наблюдение
Почему это важно
Когда применять
```

## 🔧 SEK-команды

| Команда | Что делает |
|---------|------------|
| `ingest <path>` | Добавить внешний источник в wiki |
| `/inbox` | Показать текущие наблюдения агента |
| `/revoke [TAG]` | Удалить правило из wiki |
| `/promote` | Предложить продвинуть inbox → rules |
| `что в wiki про [тема]?` | Найти информацию (авто-роутинг) |
| `lint wiki` | Проверить здоровье wiki |

## ⚠️ Critical Rules

> Агент никогда не редактирует эту секцию. Только пользователь промоутит правила сюда.

- **Токен** — никогда не выводить токен в лог, чат, консоль. Только `db.get_token()` / `db.set_token()`.
- **Не блокировать UI** — все сетевые запросы и файловые операции через `QThread`.

## 📁 Структура вики (AIWikiVault)

```
AIWikiVault/
├── AGENTS.md              ← L0 Bootstrap (этот файл)
├── memory/                ← L3 Самообучение
│   ├── inbox.md           ← Конвейер новых знаний (≤20 записей)
│   └── _protocol.md       ← Инструкция по самообучению
├── wiki/                  ← L1+L2 База знаний
│   ├── _routing.md        ← YAML-маршрутизация (keywords → files)
│   ├── _index.md          ← Оглавление
│   ├── rules/             ← Правила с тегами [XXX-001]
│   ├── references/        ← Справочные материалы (API, синтаксис)
│   ├── architecture/      ← Архитектурные описания
│   ├── workflows/         ← Многошаговые процедуры
│   ├── concepts/          ← Концептуальные страницы
│   ├── entities/          ← Страницы сущностей
│   └── sources/           ← Саммари источников
├── raw/                   ← Сырые источники (ручное добавление)
│   └── projects/yandex-disk-manager/ ← источники этого проекта
├── daily/                 ← Архив сессий
├── hooks/                 ← Хуки Claude Code
└── scripts/               ← Python-скрипты (flush, compile, lint, query)
```

## 🔗 Полезные ссылки

- [API Яндекс.Диска REST](https://yandex.ru/dev/disk-api/doc/ru/)
- [PySide6 Documentation](https://doc.qt.io/qtfor6/)
- [Watchdog on PyPI](https://pypi.org/project/watchdog/)

## 📋 История версий

Полный лог изменений — в [`CHANGELOG.md`](CHANGELOG.md).
