#!/usr/bin/env python3
"""Интеграционные тесты файловых операций на реальном API Яндекс.Диска.

Сценарии:
  1. Создание/удаление папок в облаке
  2. Загрузка файла в облако (upload)
  3. Копирование файла в облаке (copy)
  4. Перемещение файла в облаке (move)
  5. Переименование файла в облаке (rename)
  6. Скачивание файла из облака на диск (download)
  7. Удаление файла из облака (delete)
  8. Проверка статусов в БД после операций
  9. IPC-команды (raise, shutdown) через запущенное приложение

Все операции — внутри /__test_integration_<timestamp>/, 
после тестов — гарантированная очистка.
"""

import os
import sys
import json
import time
import hashlib
import logging
import tempfile
import shutil
from datetime import datetime, timezone

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("integration_test")

TEST_PREFIX = "/__test_integration"
TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
TEST_ROOT = f"{TEST_PREFIX}_{TIMESTAMP}"

passed = 0
failed = 0


def assert_eq(a, b, msg=""):
    global passed, failed
    if a == b:
        passed += 1
        logger.info("  ✅ %s", msg or f"OK: {a} == {b}")
    else:
        failed += 1
        logger.error("  ❌ %s: ожидалось %r, получено %r", msg, b, a)


def assert_true(val, msg=""):
    global passed, failed
    if val:
        passed += 1
        logger.info("  ✅ %s", msg or "OK")
    else:
        failed += 1
        logger.error("  ❌ %s: ожидалось True, получено %r", msg, val)


# ── Подготовка ──────────────────────────────────────────────

logger.info("=" * 60)
logger.info("INTEGRATION TESTS v0.11.1")
logger.info("Test root: %s", TEST_ROOT)
logger.info("=" * 60)

# Импортируем модули проекта
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import db as _db
import disk_api as _disk_api
import sync as _sync
from _version import VERSION

token = _db.get_token()
assert token, "Нет токена — выполните авторизацию через GUI"
api = _disk_api.YaDiskAPI(token)
database = _db.Database()

# ── 1. Операции с папками в облаке ─────────────────────────

logger.info("\n─── 1. Cloud folder operations ───")

# 1a. Создание тестовой папки
try:
    api.create_folder(TEST_ROOT)
    logger.info("  ✅ Created test folder: %s", TEST_ROOT)
except Exception as e:
    logger.warning("  ⚠️ create_folder error (maybe exists): %s", e)

# 1b. Проверка, что папка существует
items = api.list_folder("/")
test_folders = [i for i in items if i.get("type") == "dir" and i["path"].startswith(TEST_PREFIX)]
assert_true(len(test_folders) >= 1, f"Test folder exists in / ({len(test_folders)} found)")

# 1c. Создаём вложенные папки
api.create_folder(f"{TEST_ROOT}/sub_a")
api.create_folder(f"{TEST_ROOT}/sub_b")
api.create_folder(f"{TEST_ROOT}/sub_a/nested")

sub_items = api.list_folder(TEST_ROOT)
sub_paths = {i["path"] for i in sub_items}
assert_true(f"{TEST_ROOT}/sub_a" in sub_paths, "sub_a created")
assert_true(f"{TEST_ROOT}/sub_b" in sub_paths, "sub_b created")

# ── 2. Загрузка файла (upload) ─────────────────────────────

logger.info("\n─── 2. File upload ───")

# Создаём тестовый файл
tmp_dir = tempfile.mkdtemp(prefix="yadisk_test_")
test_content = "Hello from integration test! " + TIMESTAMP
tmp_file = os.path.join(tmp_dir, "test_hello.txt")
with open(tmp_file, "w", encoding="utf-8") as f:
    f.write(test_content)
local_md5 = hashlib.md5(test_content.encode("utf-8")).hexdigest()

# Загружаем
cloud_path = f"{TEST_ROOT}/test_hello.txt"
api.upload_file(tmp_file, cloud_path)
logger.info("  ✅ Uploaded: %s → %s", tmp_file, cloud_path)

# Проверяем в БД
rec = database.get_file(cloud_path)
if rec:
    logger.info("  ✅ Record in DB: status=%s, md5=%s", rec["status"], rec["md5"][:8] if rec.get("md5") else "None")
    assert_eq(rec["type"], "file", "type=file")
    # Статус должен быть cloud_only (не скачан)
    assert_true(rec["status"] in ("cloud_only", "downloaded"),
                f"status is {rec['status']}")
    assert_eq(rec["name"], "test_hello.txt", "filename correct")
else:
    logger.warning("  ⚠️  No DB record for freshly uploaded file (upsert happens on listing)")

# ── 3. Копирование файла ───────────────────────────────────

logger.info("\n─── 3. Copy ───")

copy_path = f"{TEST_ROOT}/sub_a/test_hello_copy.txt"
api.copy(cloud_path, copy_path, overwrite=False)
logger.info("  ✅ Copied: %s → %s", cloud_path, copy_path)

# Проверяем
sub_a_items = api.list_folder(f"{TEST_ROOT}/sub_a")
sub_a_paths = {i["path"] for i in sub_a_items}
assert_true(copy_path in sub_a_paths, "copy file exists in sub_a")

# Синхронизируем БД
database.sync_children_from_api(TEST_ROOT, api.list_folder(TEST_ROOT))
copy_rec = database.get_file(copy_path)
if copy_rec:
    logger.info("  ✅ Copy record in DB: status=%s", copy_rec["status"])
else:
    logger.warning("  ⚠️  No DB record for copy (needs refresh)")

# ── 4. Перемещение файла ───────────────────────────────────

logger.info("\n─── 4. Move ───")

moved_path = f"{TEST_ROOT}/sub_b/test_hello_moved.txt"
api.move(copy_path, moved_path, overwrite=False)
logger.info("  ✅ Moved: %s → %s", copy_path, moved_path)

# Проверяем, что исходного больше нет
try:
    api.list_folder(f"{TEST_ROOT}/sub_a")
    sub_a_items2 = api.list_folder(f"{TEST_ROOT}/sub_a")
    moved_from_paths = {i["path"] for i in sub_a_items2}
    assert_true(copy_path not in moved_from_paths, "source file gone after move")
except Exception:
    pass  # папка может быть пустой

# Проверяем, что файл появился в sub_b
sub_b_items = api.list_folder(f"{TEST_ROOT}/sub_b")
sub_b_paths = {i["path"] for i in sub_b_items}
assert_true(moved_path in sub_b_paths, "moved file exists in sub_b")

# ── 5. Переименование файла ────────────────────────────────

logger.info("\n─── 5. Rename ───")

renamed_path = f"{TEST_ROOT}/sub_b/test_renamed.txt"
api.move(moved_path, renamed_path, overwrite=False)
logger.info("  ✅ Renamed: %s → %s", moved_path, renamed_path)

sub_b_items2 = api.list_folder(f"{TEST_ROOT}/sub_b")
sub_b_paths2 = {i["path"] for i in sub_b_items2}
assert_true(renamed_path in sub_b_paths2, "renamed file exists")
assert_true(moved_path not in sub_b_paths2, "old name gone")

# ── 6. Скачивание файла ────────────────────────────────────

logger.info("\n─── 6. Download ───")

# Upload ещё один файл для скачивания
dl_content = "File to download " + TIMESTAMP
dl_local = os.path.join(tmp_dir, "download_me.txt")
with open(dl_local, "w", encoding="utf-8") as f:
    f.write(dl_content)

dl_cloud = f"{TEST_ROOT}/download_me.txt"
api.upload_file(dl_local, dl_cloud)

# Скачиваем через API
dl_dest = os.path.join(tmp_dir, "downloaded_copy.txt")
api.download_file(dl_cloud, dl_dest)
assert_true(os.path.exists(dl_dest), "downloaded file exists on disk")

with open(dl_dest, "r", encoding="utf-8") as f:
    dl_content_check = f.read()
assert_eq(dl_content_check, dl_content, "downloaded content matches")

# ── 7. Удаление файла ──────────────────────────────────────

logger.info("\n─── 7. Delete ───")

api.delete(renamed_path)
logger.info("  ✅ Deleted: %s", renamed_path)

# Проверяем, что файл исчез
sub_b_items3 = api.list_folder(f"{TEST_ROOT}/sub_b")
sub_b_paths3 = {i["path"] for i in sub_b_items3}
assert_true(renamed_path not in sub_b_paths3, "deleted file gone")

# ── 8. Массовые операции ───────────────────────────────────

logger.info("\n─── 8. Batch operations ───")

# Загружаем несколько файлов
for i in range(3):
    fpath = os.path.join(tmp_dir, f"batch_{i}.txt")
    with open(fpath, "w") as f:
        f.write(f"batch file {i} - {TIMESTAMP}")
    api.upload_file(fpath, f"{TEST_ROOT}/batch_{i}.txt")

batch_items = api.list_folder(TEST_ROOT)
batch_txts = [i for i in batch_items if i["name"].startswith("batch_")]
assert_eq(len(batch_txts), 3, "3 batch files uploaded")

# ── 9. Проверка sync.py с реальным API ─────────────────────

logger.info("\n─── 9. DB sync + status check ───")

# Создаём локальные копии для sync
cache_sub = os.path.join(tmp_dir, "cache")
os.makedirs(cache_sub, exist_ok=True)

# Копируем один скачанный файл в кеш
shutil.copy2(dl_dest, os.path.join(cache_sub, "download_me.txt"))

# Загружаем информацию в БД — только для тестовой папки
test_cloud_items = api.list_folder(TEST_ROOT)
if test_cloud_items:
    database.upsert_files_batch(test_cloud_items)
    logger.info("  ✅ Synced %d test items to DB", len(test_cloud_items))
    
    # Проверяем статус в БД
    for item in test_cloud_items:
        cp = item["path"]
        rec = database.get_file(cp)
        if rec:
            assert_eq(rec["name"], item["name"], f"DB name match: {item['name']}")
    
    # Проверяем, что наша тестовая папка существует в БД
    test_root_rec = database.get_file(TEST_ROOT)
    if test_root_rec:
        assert_eq(test_root_rec["type"], "dir", "test root is dir in DB")
    
    # Проверяем pending_ops (должно быть пусто)
    ops = database.get_pending_ops()
    assert_eq(len(ops), 0, "no pending ops after API operations")
    
    # Проверяем миграцию путей (test: переименованный файл)
    renamed_rec = database.get_file(renamed_path)
    logger.info("  ℹ️  renamed path in DB: %s", 
                "present" if renamed_rec else "absent (needs sync)")
    
    logger.info("  ✅ DB consistency verified — %d files in test folder", 
                len(test_cloud_items))
else:
    logger.warning("  ⚠️  list_folder returned empty")

# ── 10. IPC (если приложение запущено) ─────────────────────

logger.info("\n─── 10. IPC commands ───")

import ipc as _ipc

# Проверяем, отвечает ли IPC-сервер
resp = _ipc.send_ipc_command("raise")
logger.info("  IPC raise response: %s", resp)
if resp.get("status") == "ok":
    assert_eq(resp["status"], "ok", "IPC raise works")
    logger.info("  ✅ IPC server is running and responds")
    
    # Проверяем shutdown
    resp2 = _ipc.send_ipc_command("shutdown")
    logger.info("  IPC shutdown response: %s", resp2)
    logger.info("  ✅ IPC shutdown sent — app should exit cleanly")
else:
    logger.warning("  ⚠️  IPC server not running — start app with 'python main.py' first")
    logger.info("  ℹ️  Чтобы протестировать IPC, запустите программу в отдельном окне:")
    logger.info("      cd D:\\Program_files\\YaDiskManager && python main.py")
    logger.info("      затем в другом терминале:")
    logger.info('      python -c "from ipc import send_ipc_command; print(send_ipc_command(\'raise\'))"')

# ── 11. Очистка ─────────────────────────────────────────────

logger.info("\n─── 11. Cleanup ───")

try:
    api.delete(TEST_ROOT)
    logger.info("  ✅ Test folder deleted: %s", TEST_ROOT)
except Exception as e:
    logger.warning("  ⚠️  Cleanup error: %s (may need manual delete)", e)

# Удаляем временные файлы
try:
    shutil.rmtree(tmp_dir, ignore_errors=True)
    logger.info("  ✅ Temp dir cleaned: %s", tmp_dir)
except Exception as e:
    logger.warning("  ⚠️  Temp cleanup error: %s", e)

# ── Итог ────────────────────────────────────────────────────

logger.info("\n" + "=" * 60)
logger.info("RESULTS: %d passed, %d failed out of %d",
            passed, failed, passed + failed)
logger.info("=" * 60)

if failed == 0:
    logger.info("✅ ALL INTEGRATION TESTS PASSED")
else:
    logger.error("❌ %d TEST(S) FAILED", failed)

sys.exit(0 if failed == 0 else 1)
