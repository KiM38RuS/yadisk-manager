"""
l10n — локализация лога и интерфейса YaDisk Manager.

## Как это работает

- В коде логи пишутся на английском (шаблоны с %s, %d, %r и т.д.).
- LOG_RU — словарь «английский шаблон → русский перевод».
- LogTranslatingFormatter — Formatter, который перед подстановкой аргументов
  заменяет record.msg на перевод (плейсхолдеры %s/%d/%r сохраняются).
- make_formatter() создаёт такой Formatter с тем же форматом, что и в main.py.
- install() настраивает язык и перехватывает лог (вызывается из main.py).

## Как добавить другой язык

1. Скопируйте LOG_RU, переведите значения на нужный язык.
2. Сохраните плейсхолдеры (%s, %d, %(name)s, %r и т.п.) в переводе.
3. Создайте свою константу, например LOG_DE = {...}.
4. Зарегистрируйте в TRANSLATIONS: "de": LOG_DE.
5. Вызовите l10n.set_language("de") в main.py.

## Использование

    from l10n import make_formatter
    handler.setFormatter(make_formatter())
"""

import logging
import re

# ── Русский словарь ──────────────────────────────────────────
# Ключ — точный английский шаблон из кода (repr), значение — перевод.
# Плейсхолдеры (%s, %d, %r, %(name)s, %.1f и т.п.) сохраняются.
# ↑ — символ стрелки (U+2191) — не переводить, это часть обозначения "→".

LOG_RU = {
    "Account changed, token renewed":
        "Аккаунт изменён, токен обновлён",
    "AllFilesThread: %d files loaded":
        "AllFilesThread: загружено файлов: %d",
    "AllFilesThread: DB insert failed: %s":
        "AllFilesThread: ошибка записи в БД: %s",
    "AllFilesThread: error at offset=%d (retry %d/%d): %s":
        "AllFilesThread: ошибка на offset=%d (попытка %d/%d): %s",
    "AllFilesThread: loading all files (resume offset=%d)...":
        "AllFilesThread: загрузка всех файлов (продолжение с offset=%d)...",
    "AllFilesThread: max retries exceeded":
        "AllFilesThread: превышено число попыток",
    "AllFilesThread: stop requested":
        "AllFilesThread: запрошена остановка",
    "ApiListThread: %s -> %d items":
        "ApiListThread: %s -> элементов: %d",
    "ApiListThread: %s FAILED: %s":
        "ApiListThread: %s ОШИБКА: %s",
    "ApiListThread: fetching %s...":
        "ApiListThread: получение %s...",
    "Apply aborted: %s is not %s":
        "Применение прервано: %s не является %s",
    "Apply failed: %s":
        "Не удалось применить обновление: %s",
    "Apply skipped: dev mode, downloaded to %s":
        "Применение пропущено: dev-режим, скачано в %s",
    "Apply: apply_update.bat launched, app will restart":
        "Применение: запущен apply_update.bat, приложение перезапустится",
    "Applying theme...":
        "Применение темы...",
    "Applying theme: config=%r is_dark=%s (windows_dark=%s)":
        "Применение темы: config=%r тёмная=%s (windows тёмная=%s)",
    "Auto-download: %s (parent was fully synced)":
        "Авто-загрузка: %s (родитель полностью синхронизирован)",
    "Auto-update download failed: %s":
        "Ошибка скачивания авто-обновления: %s",
    "Auto-update: apply skipped":
        "Авто-обновление: применение пропущено",
    "Auto-update: apply_update launched":
        "Авто-обновление: запущен apply_update",
    "Auto-update: downloaded %s":
        "Авто-обновление: скачано %s",
    "Auto-update: downloading %s in background":
        "Авто-обновление: скачивание %s в фоне",
    "AutoDownloadThread failed: %s":
        "AutoDownloadThread: ошибка: %s",
    "AutoDownloadThread: %d file(s) to auto-download":
        "AutoDownloadThread: файлов для авто-загрузки: %d",
    "AutoDownloadThread: analyzing folders...":
        "AutoDownloadThread: анализ папок...",
    "Bulk sync: %d downloaded file(s) changed in cloud":
        "Полная синхронизация: в облаке изменилось файлов: %d",
    "Bulk sync: all %d downloaded files match cloud MD5":
        "Полная синхронизация: все %d скачанных файлов совпадают с облаком (MD5)",
    "Bulk sync: no files loaded, nothing to check":
        "Полная синхронизация: файлы не загружены, проверять нечего",
    "Bulk sync: starting full file scan...":
        "Полная синхронизация: запуск полного сканирования...",
    "Cache dir changed: %s → %s":
        "Папка кеша изменена: %s → %s",
    "Cache dir missing, user chose CANCEL":
        "Папка кеша отсутствует, пользователь выбрал ОТМЕНА",
    "Cache dir missing, user chose CLEAR (%d files reset)":
        "Папка кеша отсутствует, пользователь выбрал ОЧИСТИТЬ (%d файлов сброшено)",
    "Cache dir missing, user chose RESTORE (%d files)":
        "Папка кеша отсутствует, пользователь выбрал ВОССТАНОВИТЬ (%d файлов)",
    "Cache dir set: %s":
        "Папка кеша задана: %s",
    "Cannot remove %s: %s":
        "Не удалось удалить %s: %s",
    "Conflict auto-cancelled (offscreen): %s local=%s cloud=%s":
        "Конфликт автоматически отменён (окно вне экрана): %s локально=%s облако=%s",
    "Connected. Total: %.1f GB, Used: %.1f GB, Free: %.1f GB":
        "Подключено. Всего: %.1f ГБ, занято: %.1f ГБ, свободно: %.1f ГБ",
    "Created file outside cache: %s":
        "Создан файл вне кеша: %s",
    "DB init: reset %d stale syncing entries ":
        "Инициализация БД: сброшено устаревших записей «синхронизация»: %d",
    "DB migrate_path(%s→%s) failed: %s":
        "Ошибка migrate_path(%s→%s): %s",
    "DB migration failed: %s":
        "Ошибка миграции БД: %s",
    "DB migration: CHECK constraint dropped successfully":
        "Миграция БД: ограничение CHECK успешно удалено",
    "DB migration: dropping CHECK constraint from 'files' table":
        "Миграция БД: удаление ограничения CHECK из таблицы 'files'",
    "DB migration: stripping 'disk:' prefix from %d row(s)":
        "Миграция БД: удаление префикса 'disk:' из %d строк",
    "DB: created %d missing dir entries under %s":
        "БД: создано %d недостающих записей папок в %s",
    "DB: pruned %d stale children from %s (%s)":
        "БД: удалено %d устаревших записей из %s (%s)",
    "DLW: EXCEPTION: %s":
        "DLW: ИСКЛЮЧЕНИЕ: %s",
    "DLW: GET status=%d":
        "DLW: GET status=%d",
    "DLW: cancelled":
        "DLW: отменено",
    "DLW: dirs created":
        "DLW: папки созданы",
    "DLW: finished, wrote %d bytes to %s":
        "DLW: готово, записано %d байт в %s",
    "DLW: get_download_url %s":
        "DLW: запрос ссылки на скачивание %s",
    "DLW: href OK -> %s...":
        "DLW: ссылка получена -> %s...",
    "DLW: remove partial %s failed: %s":
        "DLW: не удалось удалить неполный файл %s: %s",
    "DLW: total=%d bytes, local_path=%s":
        "DLW: всего=%d байт, локальный путь=%s",
    "DbChildrenThread: %s FAILED: %s":
        "DbChildrenThread: %s ОШИБКА: %s",
    "Delete error: %s":
        "Ошибка удаления: %s",
    "Delete: complete_pending_op failed: %s":
        "Удаление: ошибка complete_pending_op: %s",
    "DirPathsThread FAILED: %s":
        "DirPathsThread ОШИБКА: %s",
    "Directory deleted (no tracked files): %s":
        "Папка удалена (отслеживаемых файлов нет): %s",
    "Directory deleted, %d files set to cloud_only: %s":
        "Папка удалена, %d файлов переведены в cloud_only: %s",
    "Disk API check failed: %s":
        "Ошибка проверки API диска: %s",
    "DnD error: %s":
        "Ошибка перетаскивания: %s",
    "DnD: complete_pending_op failed: %s":
        "Перетаскивание: ошибка complete_pending_op: %s",
    "DnD: migrate_path %s → %s failed: %s":
        "Перетаскивание: ошибка migrate_path %s → %s: %s",
    "DnD: remove stub %s failed: %s":
        "Перетаскивание: не удалось удалить заглушку %s: %s",
    "DnD: set_cloud_only %s failed: %s":
        "Перетаскивание: ошибка set_cloud_only %s: %s",
    "DoH %s failed: %s":
        "DoH %s: ошибка: %s",
    "DoH %s: %s -> %s (ttl=%ss)":
        "DoH %s: %s -> %s (ttl=%s с)",
    "Download crashed":
        "Сбой при скачивании",
    "Downloaded %s (%d bytes)":
        "Скачано %s (%d байт)",
    "Downloaded: %s → %s":
        "Скачано: %s → %s",
    "Downloading %s → %s":
        "Скачивание: %s → %s",
    "Drop upload ready: %d file(s) copied, starting upload":
        "Перетаскивание готово: скопировано файлов: %d, начинаем загрузку",
    "Drop: %d file(s) copied to cache":
        "Перетаскивание: скопировано файлов в кеш: %d",
    "Drop: %d file(s) into %s (async)":
        "Перетаскивание: %d файлов в %s (асинхронно)",
    "Drop: failed to register %s — %s":
        "Перетаскивание: не удалось зарегистрировать %s — %s",
    "DropUploadThread: skip %s — %s":
        "DropUploadThread: пропуск %s — %s",
    "Empty folder created locally: %s → %s":
        "Пустая папка создана локально: %s → %s",
    "Empty folder removed locally: %s":
        "Пустая папка удалена локально: %s",
    "Failed to reset stale syncing statuses: %s":
        "Не удалось сбросить устаревшие статусы синхронизации: %s",
    "Fetching %s...":
        "Получение %s...",
    "File %s (debounced): %s":
        "Файл %s (с задержкой): %s",
    "File already in sync, skipping upload: %s":
        "Файл уже синхронизирован, загрузка пропущена: %s",
    "File deleted: %s":
        "Файл удалён: %s",
    "File gone before flush: %s":
        "Файл исчез до обработки: %s",
    "File moved locally: %s → %s (cloud: %s → %s)":
        "Файл перемещён локально: %s → %s (облако: %s → %s)",
    "File moved: %s → %s":
        "Файл перемещён: %s → %s",
    "File set to cloud_only: %s":
        "Файл переведён в cloud_only: %s",
    "File watcher started on %s (recursive)":
        "Наблюдатель запущен для %s (рекурсивно)",
    "File watcher stopped":
        "Наблюдатель остановлен",
    "Folder load failed for %s: %s":
        "Не удалось загрузить папку %s: %s",
    "FolderDownloadThread failed: %s":
        "FolderDownloadThread: ошибка: %s",
    "FolderDownloadThread: %d items collected":
        "FolderDownloadThread: собрано элементов: %d",
    "FolderDownloadThread: BFS %d folder(s)...":
        "FolderDownloadThread: обход %d папок...",
    "FolderDownloadThread: failed to list %s: %s":
        "FolderDownloadThread: не удалось получить список %s: %s",
    "FolderLoadThread: %s FAILED: %s":
        "FolderLoadThread: %s ОШИБКА: %s",
    "FullSync cleanup DB: %s":
        "Полная синхронизация: очистка БД: %s",
    "FullSync delete failed %s: %s":
        "Полная синхронизация: ошибка удаления %s: %s",
    "FullSync delete: %s (removed from cloud)":
        "Полная синхронизация: удалено из облака %s",
    "FullSync done: %(matched)d matched, %(uploaded)d uploaded, ":
        "Полная синхронизация завершена: совпало %(matched)d, загружено %(uploaded)d",
    "FullSync download: %s (cloud newer)":
        "Полная синхронизация: скачивание %s (облако новее)",
    "FullSync move: %s → %s":
        "Полная синхронизация: перемещение %s → %s",
    "FullSync upload new: %s":
        "Полная синхронизация: загрузка нового %s",
    "FullSync upload: %s (local newer)":
        "Полная синхронизация: загрузка %s (локально новее)",
    "FullSync: %d files in cloud":
        "Полная синхронизация: файлов в облаке: %d",
    "FullSync: cache dir empty, nothing to sync":
        "Полная синхронизация: папка кеша пуста, синхронизировать нечего",
    "FullSync: failed to get cloud file list: %s":
        "Полная синхронизация: не удалось получить список файлов в облаке: %s",
    "IP %s unreachable for %s (%s)":
        "IP %s недоступен для %s (%s)",
    "IPC command: %s":
        "IPC-команда: %s",
    "IPC error: %s":
        "Ошибка IPC: %s",
    "IPC server listening on 127.0.0.1:%d":
        "IPC-сервер слушает 127.0.0.1:%d",
    "IPC: ping":
        "IPC: ping",
    "IPC: raise window requested":
        "IPC: запрошен вывод окна",
    "IPC: restart requested":
        "IPC: запрошен перезапуск",
    "IPC: shutdown requested":
        "IPC: запрошено завершение",
    "Ignored stale folder load for %s (current %s)":
        "Пропущена устаревшая загрузка папки %s (текущая %s)",
    "Ignored stale search results for %r (current %r)":
        "Пропущены устаревшие результаты поиска для %r (текущие %r)",
    "Initial load: starting async fetch of /":
        "Начальная загрузка: запуск асинхронного получения /",
    "Internal DnD: %s %d items to %s":
        "Внутреннее перетаскивание: %s %d элементов в %s",
    "Local cache ready: %d files":
        "Локальный кеш готов: файлов: %d",
    "Local file deleted, set cloud_only: %s":
        "Локальный файл удалён, статус cloud_only: %s",
    "Local file not found, updating DB: %s":
        "Локальный файл не найден, обновление БД: %s",
    "Local rename failed: %s":
        "Ошибка локального переименования: %s",
    "Local renamed: %s → %s":
        "Локально переименовано: %s → %s",
    "MetaFetchThread (404, expected): %s — %s":
        "MetaFetchThread (404, ожидаемо): %s — %s",
    "MetaFetchThread: %s — %s":
        "MetaFetchThread: %s — %s",
    "Migrate: failed to move %s → %s: %s":
        "Миграция: не удалось переместить %s → %s: %s",
    "Migrate: moved %d files from %s to %s":
        "Миграция: перемещено файлов: %d из %s в %s",
    "Moved from unknown path: %s → %s":
        "Перемещено из неизвестного пути: %s → %s",
    "Nav fix: %s → downloaded (found locally)":
        "Исправление навигации: %s → скачано (найдено локально)",
    "Net heal: blocks persist — suggesting VPN":
        "Восстановление сети: блокировки сохраняются — предлагаем VPN",
    "Net heal: user answer = %s":
        "Восстановление сети: ответ пользователя = %s",
    "New local file detected, uploading: %s → %s":
        "Обнаружен новый локальный файл, загрузка: %s → %s",
    "Operation %s completed":
        "Операция %s завершена",
    "Operation %s not found (likely completed)":
        "Операция %s не найдена (вероятно, уже завершена)",
    "Path completer refresh failed: %s":
        "Ошибка обновления автодополнения пути: %s",
    "Permission denied reading (maybe locked by another app): %s":
        "Отказано в доступе при чтении (возможно, файл занят другой программой): %s",
    "Poll error: %s":
        "Ошибка опроса: %s",
    "Poll: %d downloaded file(s) changed in cloud":
        "Опрос: в облаке изменилось скачанных файлов: %d",
    "Poll: %d new item(s)":
        "Опрос: новых элементов: %d",
    "Removed local copy: %s":
        "Удалена локальная копия: %s",
    "Rename: complete_pending_op failed":
        "Переименование: ошибка complete_pending_op",
    "Restarting application...":
        "Перезапуск приложения...",
    "Restore: queuing %d files for re-download":
        "Восстановление: в очередь на повторное скачивание — %d файлов",
    "Reverse sync: conflict %s (both changed)":
        "Обратная синхронизация: конфликт %s (изменены обе версии)",
    "Reverse sync: downloading %s (cloud changed)":
        "Обратная синхронизация: скачивание %s (облако изменилось)",
    "Silent mode: window hidden, tray only":
        "Тихий режим: окно скрыто, только трей",
    "Startup scan: done, refreshing UI...":
        "Стартовое сканирование: готово, обновление интерфейса...",
    "Startup scan: launching background thread...":
        "Стартовое сканирование: запуск фонового потока...",
    "Startup: %d local files changed, will upload":
        "Старт: локальных файлов изменено: %d, будут загружены",
    "Startup: %d new local files detected":
        "Старт: обнаружено новых локальных файлов: %d",
    "Startup: local file missing, set cloud_only: %s":
        "Старт: локальный файл отсутствует, статус cloud_only: %s",
    "Startup: registered %d local directories":
        "Старт: зарегистрировано локальных папок: %d",
    "Startup: registered local file as downloaded (no cloud copy): %s":
        "Старт: локальный файл зарегистрирован как скачанный (копии в облаке нет): %s",
    "Startup: restored local copy (was %s): %s":
        "Старт: восстановлена локальная копия (была %s): %s",
    "StartupScanThread error: %s":
        "StartupScanThread: ошибка: %s",
    "StartupScanThread: scanning local cache...":
        "StartupScanThread: сканирование локального кеша...",
    "StartupScanThread: stale=%d, changed=%d, new=%d, new_dirs=%d":
        "StartupScanThread: устарело=%d, изменено=%d, новых=%d, новых папок=%d",
    "Sync download failed %s: %s":
        "Ошибка синхронизации при скачивании %s: %s",
    "Sync move failed %s → %s: %s":
        "Ошибка синхронизации при перемещении %s → %s: %s",
    "Sync moved locally: %s → %s":
        "Синхронизация: перемещено локально: %s → %s",
    "Sync removed empty dir: %s":
        "Синхронизация: удалена пустая папка: %s",
    "Sync upload failed %s: %s":
        "Ошибка синхронизации при загрузке %s: %s",
    "Sync upload new failed %s: %s":
        "Ошибка синхронизации при загрузке нового %s: %s",
    "SyncThread auth error: %s":
        "SyncThread: ошибка авторизации: %s",
    "SyncThread error: %s":
        "SyncThread: ошибка: %s",
    "SyncThread: done -> %s":
        "SyncThread: готово -> %s",
    "SyncThread: starting full sync...":
        "SyncThread: запуск полной синхронизации...",
    "System path to %s failed (%s), heal=%s":
        "Системный путь к %s не получен (%s), восстановление=%s",
    "Theme applied OK":
        "Тема применена",
    "Theme selected in settings combo: %r (was %r)":
        "Тема выбрана в настройках: %r (была %r)",
    "Token renewed":
        "Токен обновлён",
    "Tree load failed: %s":
        "Не удалось загрузить дерево: %s",
    "Tree loaded: %d top-level folders":
        "Дерево загружено: папок верхнего уровня: %d",
    "Tree sync DB fetch failed for %s: %s":
        "Синхронизация дерева: ошибка чтения БД для %s: %s",
    "Tree sync: %s unavailable, stopping":
        "Синхронизация дерева: %s недоступно, остановка",
    "Tree sync: stale DB response for %s (gen %d)":
        "Синхронизация дерева: устаревший ответ БД для %s (gen %d)",
    "Unknown local file modified, treating as created: %s":
        "Неизвестный локальный файл изменён, считаем созданным: %s",
    "Update available: %s":
        "Доступно обновление: %s",
    "Update check crashed":
        "Сбой при проверке обновлений",
    "Update check error: %s":
        "Ошибка проверки обновлений: %s",
    "Update check failed (network): %s":
        "Ошибка проверки обновлений (сеть): %s",
    "Update check: %s has no exe asset":
        "Проверка обновлений: у %s нет exe-файла",
    "Update check: HTTP %s from GitHub":
        "Проверка обновлений: HTTP %s от GitHub",
    "Update check: bad JSON from GitHub":
        "Проверка обновлений: неверный JSON от GitHub",
    "Update check: latest %s — already current (%s)":
        "Проверка обновлений: последняя %s — уже установлена (%s)",
    "Update check: no releases for %s":
        "Проверка обновлений: нет релизов для %s",
    "Update check: no releases yet for %s":
        "Проверка обновлений: для %s релизов пока нет",
    "Uploaded: %s":
        "Загружено: %s",
    "Watcher handler error: %s":
        "Ошибка обработчика наблюдателя: %s",
    "Watcher moved handler error: %s":
        "Ошибка обработчика перемещений наблюдателя: %s",
    "Worker error: %s %s — reset to cloud_only":
        "Ошибка воркера: %s %s — сброс в cloud_only",
    "Worker error: download %s — %s":
        "Ошибка воркера: скачивание %s — %s",
    "Worker error: upload %s — %s":
        "Ошибка воркера: загрузка %s — %s",
    "ZIP download failed: %s — %s":
        "Ошибка скачивания ZIP: %s — %s",
    "ZIP download: %s → %s":
        "Скачивание ZIP: %s → %s",
    "ZIP saved: %s":
        "ZIP сохранён: %s",
    "ZipDownloadWorker error: %s":
        "ZipDownloadWorker: ошибка: %s",
    "ZipDownloadWorker: remove partial %s failed: %s":
        "ZipDownloadWorker: не удалось удалить неполный файл %s: %s",
    "_apply_theme: reentrant call skipped (already applying)":
        "_apply_theme: повторный вызов пропущен (тема уже применяется)",
    "_process_download_queue: skipping cancelled %s":
        "_process_download_queue: пропуск отменённого %s",
    "_process_result_queue failed: %s":
        "Ошибка _process_result_queue: %s",
    "_process_upload_queue: skipping cancelled %s":
        "_process_upload_queue: пропуск отменённого %s",
    "_start_upload: no local_md5 provided, computing in main thread — this may freeze UI":
        "_start_upload: local_md5 не указан, вычисление в главном потоке — возможна блокировка интерфейса",
    "_start_upload: queued %s (active=%d/%d)":
        "_start_upload: в очереди %s (активных=%d/%d)",
    "config.json: lock timeout (%ss) — key %r skipped":
        "config.json: таймаут блокировки (%s с) — ключ %r пропущен",
    "config.json: lock timeout in load_config (%ss) — using empty config":
        "config.json: таймаут блокировки в load_config (%s с) — используется пустой конфиг",
    "config.json: lock timeout in save_config (%ss) — write skipped":
        "config.json: таймаут блокировки в save_config (%s с) — запись пропущена",
    "config.json: replace failed (file busy by another process) — ":
        "config.json: замена не удалась (файл занят другим процессом) — ",
    "config.json: unreadable (%s) — rewriting":
        "config.json: не читается (%s) — перезапись",
    "config.json: write failed: %s":
        "config.json: ошибка записи: %s",
    "do_download: path=%s local=%s":
        "do_download: путь=%s локально=%s",
    "ensure_folder_path: %s — %s":
        "ensure_folder_path: %s — %s",
    "net heal event routing failed":
        "Ошибка маршрутизации события восстановления сети",
    "start_download: already syncing %s":
        "start_download: %s уже синхронизируется",
    "start_download: queued %s (active=%d/%d)":
        "start_download: в очереди %s (активных=%d/%d)",
    "start_upload: already syncing %s":
        "start_upload: %s уже синхронизируется",
    "sync_children_from_api failed: %s — fallback to upsert only":
        "Ошибка sync_children_from_api: %s — запасной вариант только upsert",
    "⚠️ FREEZE: UI blocked for ~%d ms%s":
        "⚠️ ЗАВИСАНИЕ: интерфейс заблокирован на ~%d мс%s",
    "⚠️ SLOW EVENT: type=%d(%s) on %s took %dms":
        "⚠️ МЕДЛЕННОЕ СОБЫТИЕ: type=%d(%s) на %s заняло %d мс",
    "⚠️ SLOW WINDOW EVENT: type=%d(%s) took %dms":
        "⚠️ МЕДЛЕННОЕ СОБЫТИЕ ОКНА: type=%d(%s) заняло %d мс",
    "📋 PasteFilesThread: copy %s → %s (overwrite=%s)":
        "📋 PasteFilesThread: копирование %s → %s (перезапись=%s)",
    "📋 PasteFilesThread: move %s → %s (overwrite=%s)":
        "📋 PasteFilesThread: перемещение %s → %s (перезапись=%s)",
    "🔄 Found %d pending operations from last session":
        "🔄 Найдено незавершённых операций с прошлого запуска: %d",
    "🔄 Retry %s failed: %s":
        "🔄 Повтор %s не удался: %s",
    "🔄 Retry %s: %s → %s":
        "🔄 Повтор %s: %s → %s",
    "🔄 Retry delete: %d paths":
        "🔄 Повтор удаления: путей: %d",
    "🔄 Retry rename: %s → %s":
        "🔄 Повтор переименования: %s → %s",
    "🗑️ DeleteFilesThread: removing %d items from DB + cloud":
        "🗑️ DeleteFilesThread: удаление %d элементов из БД и облака",
}

# ── Реестр языков ─────────────────────────────────────────
# Ключ — код языка (ISO 639-1), значение — словарь.
TRANSLATIONS: dict[str, dict[str, str]] = {
    "ru": LOG_RU,
}

# ── Текущий язык (по умолчанию русский — приложение для русскоязычных) ──
_current_lang = "ru"


def set_language(lang: str) -> None:
    """Установить язык лога (ISO 639-1, например 'ru', 'en')."""
    global _current_lang
    _current_lang = lang


def get_language() -> str:
    """Вернуть текущий язык лога."""
    return _current_lang


def translate(msg: str) -> str:
    """Перевести шаблон лога на текущий язык.

    Если язык 'en' или перевод не найден, возвращает msg без изменений.
    """
    if _current_lang == "en":
        return msg
    table = TRANSLATIONS.get(_current_lang)
    if table is None:
        return msg
    return table.get(msg, msg)


# ── Форматтер с переводом ─────────────────────────────────


class LogTranslatingFormatter(logging.Formatter):
    """Formatter, переводящий шаблон сообщения до подстановки аргументов.

    Используется для файлового лога, консоли и GUI-окна лога.
    """

    def format(self, record: logging.LogRecord) -> str:
        # record.msg — это шаблон с %s/%d, который ещё не отформатирован.
        # Подменяем его на перевод до вызова super().format(),
        # который вызывает record.getMessage() → msg % args.
        if isinstance(record.msg, str):
            translated = translate(record.msg)
            if translated is not record.msg:
                record.msg = translated
        return super().format(record)


# ── Фабрика ────────────────────────────────────────────────

_DEFAULT_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DEFAULT_DATE = "%H:%M:%S"


def make_formatter(fmt: str = None, datefmt: str = None) -> LogTranslatingFormatter:
    """Создать форматтер лога с переводом.

    По умолчанию использует тот же формат, что и main.py + ui_log.py:
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    """
    return LogTranslatingFormatter(
        fmt or _DEFAULT_FMT,
        datefmt or _DEFAULT_DATE,
    )


# ── Валидация (проверка покрытия и плейсхолдеров) ────────

_PLACEHOLDER_RE = re.compile(
    r"%(?:\([^)]*\))?[#0\- +]*(?:\d+|\*)?(?:\.\d+)?[diouxXeEfFgGcrsa%]"
)


def _extract_placeholders(template: str) -> frozenset[str]:
    """Извлечь все %-плейсхолдеры из шаблона."""
    return frozenset(_PLACEHOLDER_RE.findall(template))


def verify() -> list[str]:
    """Проверить покрытие и сохранность плейсхолдеров.

    Возвращает список ошибок (пустой — всё ок).
    """
    errors: list[str] = []
    for lang, table in TRANSLATIONS.items():
        for en_msg, ru_msg in table.items():
            en_ph = _extract_placeholders(en_msg)
            ru_ph = _extract_placeholders(ru_msg)
            if en_ph != ru_ph:
                errors.append(
                    f"[{lang}] placeholder mismatch: {en_msg!r}\n"
                    f"  source: {sorted(en_ph)}\n"
                    f"  trans:  {sorted(ru_ph)}"
                )
    return errors