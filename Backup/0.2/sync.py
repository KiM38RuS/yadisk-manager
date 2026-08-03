"""
Полная двусторонняя синхронизация локальной папки с Яндекс.Диском.
"""

import os
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ── игнорируемые файлы/папки ─────────────────────────────

# Папки, которые полностью игнорируются (созданы оф.клиентом Яндекс.Диска)
IGNORED_DIRS = {".sync"}


def _is_ignored(path: str, cache_dir: str) -> bool:
    """Проверить, находится ли файл/папка внутри .sync."""
    rel = os.path.relpath(path, cache_dir).replace("\\", "/")
    return any(p in IGNORED_DIRS for p in rel.split("/"))


# ── helpers ───────────────────────────────────────────────

def md5_file(path: str) -> str:
    """MD5 хеш файла."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_mtime(iso_str: str) -> float:
    """ISO-дата из облака → UNIX timestamp."""
    if not iso_str:
        return 0.0
    try:
        return datetime.fromisoformat(iso_str).timestamp()
    except Exception:
        return 0.0


def _upsert_local(database, cloud_path: str, name: str,
                  local_path: str, size: int, md5: str,
                  cloud_item: dict) -> None:
    """Обновить запись в БД после синхронизации."""
    ctype = cloud_item.get("type") if isinstance(cloud_item, dict) else "file"
    database.upsert_file(
        cloud_path=cloud_path,
        name=name,
        type_=ctype or "file",
        size=size,
        modified=datetime.now(timezone.utc).isoformat(),
        md5=md5,
    )


# ── move / remove helpers ────────────────────────────────

def move_local_file(cache_dir: str, old_cp: str, new_cp: str,
                    old_local: str, database) -> None:
    """Переместить локальный файл вслед за перемещением в облаке."""
    new_local = os.path.join(cache_dir, new_cp.lstrip("/"))
    try:
        os.makedirs(os.path.dirname(new_local), exist_ok=True)
        if os.path.exists(new_local):
            os.remove(new_local)
        os.rename(old_local, new_local)
        rec = database.get_file(old_cp)
        if rec:
            database.remove_file(old_cp)
        database.upsert_file(
            cloud_path=new_cp,
            name=os.path.basename(new_local),
            type_="file",
            size=os.path.getsize(new_local),
            modified=datetime.now(timezone.utc).isoformat(),
            md5=md5_file(new_local),
        )
        database.set_downloaded(new_cp, new_local,
                                last_sync_md5=md5_file(new_local))
        _remove_empty_parents(old_local, cache_dir)
        logger.info("Sync moved locally: %s → %s", old_cp, new_cp)
    except Exception as e:
        logger.error("Sync move failed %s → %s: %s", old_cp, new_cp, e)


def _remove_empty_parents(path: str, cache_dir: str) -> None:
    """Удалить пустые родительские папки (до cache_dir)."""
    parent = os.path.dirname(path)
    while parent and parent != cache_dir:
        try:
            if os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
                logger.info("Sync removed empty dir: %s", parent)
                parent = os.path.dirname(parent)
            else:
                break
        except Exception:
            break


# ── sync actions ─────────────────────────────────────────

def _do_upload(api, database, local_path: str, cloud_path: str,
               name: str, size: int, md5: str, cloud_item: dict) -> None:
    """Загрузить локальный файл в облако и обновить БД."""
    try:
        api.upload_file(local_path, cloud_path)
        database.set_downloaded(cloud_path, local_path, last_sync_md5=md5)
        _upsert_local(database, cloud_path, name, local_path,
                      size, md5, cloud_item)
    except Exception as e:
        logger.error("Sync upload failed %s: %s", cloud_path, e)


def _do_download(api, database, cloud_path: str, local_path: str,
                 cloud_item: dict) -> None:
    """Скачать файл из облака, перезаписав локальный."""
    try:
        api.download_file(cloud_path, local_path)
        new_md5 = md5_file(local_path)
        new_size = os.path.getsize(local_path)
        database.set_downloaded(cloud_path, local_path, last_sync_md5=new_md5)
        _upsert_local(database, cloud_path,
                      cloud_item.get("name", ""), local_path,
                      new_size, new_md5, cloud_item)
    except Exception as e:
        logger.error("Sync download failed %s: %s", cloud_path, e)


def _do_upload_new(api, database, local_path: str, cloud_path: str,
                   name: str, size: int, md5: str) -> None:
    """Создать новый файл в облаке из локального."""
    try:
        api.upload_file(local_path, cloud_path)
        database.upsert_file(
            cloud_path=cloud_path, name=name, type_="file",
            size=size,
            modified=datetime.now(timezone.utc).isoformat(),
            md5=md5,
        )
        database.set_downloaded(cloud_path, local_path, last_sync_md5=md5)
    except Exception as e:
        logger.error("Sync upload new failed %s: %s", cloud_path, e)


# ── main sync entry point ────────────────────────────────

def full_sync(api, database, cache_dir: str) -> dict:
    """
    Полная двусторонняя синхронизация.

    Возвращает словарь со счётчиками: matched, uploaded, downloaded, moved, deleted.
    """
    result = {"matched": 0, "uploaded": 0, "downloaded": 0,
              "moved": 0, "deleted": 0}

    if not os.path.isdir(cache_dir):
        logger.info("FullSync: cache dir empty, nothing to sync")
        return result

    # ── Phase 1: получить состояние облака ────────────────
    try:
        cloud_files = api.get_all_files()
    except Exception as e:
        logger.warning("FullSync: failed to get cloud file list: %s", e)
        return result

    cloud_by_path: dict[str, dict] = {}
    cloud_by_md5: dict[str, list[str]] = {}
    for f in cloud_files:
        cp = f["path"]
        cloud_by_path[cp] = f
        md5 = (f.get("md5") or "").strip()
        if md5:
            cloud_by_md5.setdefault(md5, []).append(cp)

    logger.info("FullSync: %d files in cloud", len(cloud_by_path))

    # ── Phase 2: обойти локальные файлы ───────────────────
    for root, dirs, files in os.walk(cache_dir):
        # Исключаем служебные папки (.sync, ~*, .*)
        dirs[:] = [d for d in dirs if not _is_ignored(
            os.path.join(root, d), cache_dir)]

        for name in files:
            local_path = os.path.join(root, name)

            if _is_ignored(local_path, cache_dir):
                continue

            rel = os.path.relpath(local_path, cache_dir).replace("\\", "/")
            cp = "/" + rel
            local_mtime = os.path.getmtime(local_path)
            local_size = os.path.getsize(local_path)

            if cp in cloud_by_path:
                ci = cloud_by_path[cp]
                cloud_md5 = (ci.get("md5") or "").strip()
                local_md5 = md5_file(local_path)

                if local_md5 == cloud_md5:
                    database.set_downloaded(cp, local_path,
                                            last_sync_md5=local_md5)
                    _upsert_local(database, cp, name, local_path,
                                  local_size, local_md5, ci)
                    result["matched"] += 1
                    continue

                cloud_mtime = _parse_mtime(ci.get("modified", ""))
                if local_mtime > cloud_mtime + 1:
                    logger.info("FullSync upload: %s (local newer)", cp)
                    _do_upload(api, database, local_path, cp,
                               name, local_size, local_md5, ci)
                    result["uploaded"] += 1
                else:
                    logger.info("FullSync download: %s (cloud newer)", cp)
                    _do_download(api, database, cp, local_path, ci)
                    result["downloaded"] += 1
            else:
                local_md5 = md5_file(local_path)

                # Проверка: может, файл перемещён в облаке?
                if local_md5 in cloud_by_md5:
                    new_cp = cloud_by_md5[local_md5][0]
                    if new_cp != cp:
                        logger.info("FullSync move: %s → %s", cp, new_cp)
                        move_local_file(cache_dir, cp, new_cp,
                                        local_path, database)
                        result["moved"] += 1
                        continue

                # Проверка: файл был скачан, но удалён из облака?
                db_rec = database.get_file(cp)
                if db_rec and db_rec.get("status") == "downloaded":
                    logger.info("FullSync delete: %s (removed from cloud)", cp)
                    try:
                        os.remove(local_path)
                        _remove_empty_parents(local_path, cache_dir)
                        database.remove_file(cp)
                        result["deleted"] += 1
                    except Exception as e:
                        logger.error("FullSync delete failed %s: %s", cp, e)
                    continue

                # ➕ Новый локальный файл → создать в облаке
                logger.info("FullSync upload new: %s", cp)
                _do_upload_new(api, database, local_path, cp,
                               name, local_size, local_md5)
                result["uploaded"] += 1

    # ── Phase 3: обработать файлы из БД,缺失 локально ────
    for rec in database.get_by_status("downloaded"):
        cp = rec["cloud_path"]
        local_path_rec = rec.get("local_path", "")
        if not local_path_rec or not os.path.exists(local_path_rec):
            if cp not in cloud_by_path:
                database.remove_file(cp)
                logger.info("FullSync cleanup DB: %s", cp)

    logger.info("FullSync done: %(matched)d matched, %(uploaded)d uploaded, "
                "%(downloaded)d downloaded, %(moved)d moved, %(deleted)d deleted",
                result)
    return result


def migrate_cache(old_dir: str, new_dir: str, database) -> int:
    """
    Переместить файлы из старой папки кеша в новую (с сохранением поддиректорий).
    Игнорирует служебные файлы/папки.
    Возвращает количество перемещённых файлов.
    """
    if not os.path.isdir(old_dir):
        return 0
    if old_dir == new_dir:
        return 0

    os.makedirs(new_dir, exist_ok=True)
    count = 0

    for root, dirs, files in os.walk(old_dir):
        # Исключаем служебные папки
        dirs[:] = [d for d in dirs if not _is_ignored(
            os.path.join(root, d), old_dir)]

        for name in files:
            src = os.path.join(root, name)
            if _is_ignored(src, old_dir):
                continue

            rel = os.path.relpath(src, old_dir)
            dst = os.path.join(new_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)

            if os.path.exists(dst):
                os.remove(dst)

            try:
                os.rename(src, dst)
                count += 1
                # Обновить local_path в БД
                cloud_path = "/" + rel.replace("\\", "/")
                rec = database.get_file(cloud_path)
                if rec and rec.get("status") == "downloaded":
                    database.set_downloaded(cloud_path, dst,
                                            last_sync_md5=rec.get("last_sync_md5", ""))
            except Exception as e:
                logger.warning("Migrate: failed to move %s → %s: %s", src, dst, e)

        # Удаляем пустые папки из старой директории
        _remove_empty_parents(root, old_dir)

    # Финальная зачистка пустых папок в old_dir
    _remove_empty_parents(old_dir, old_dir)

    logger.info("Migrate: moved %d files from %s to %s", count, old_dir, new_dir)
    return count
