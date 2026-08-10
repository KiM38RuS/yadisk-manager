"""
Shared fixtures for YaDisk Manager tests.

Provides:
  - in_memory_db()      — SQLite in-memory Database
  - temp_cache()        — temporary cache directory + helper files
  - mock_api()          — MockYaDiskAPI that tracks calls
  - sample_cloud_files() — typical cloud file listing
"""

import os
import sys
import json
import shutil
import tempfile
import hashlib
from pathlib import Path
from collections import deque
from typing import Optional, Callable

import pytest

# Ensure project root is on sys.path so imports work
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Script-style test files: run manually (live app + real API), not via pytest.
# test_integration_real_api.py  — sys.exit() at module level breaks collection
# test_qt_init.py              — creates QApplication at module level, no tests
collect_ignore = ["test_integration_real_api.py", "test_qt_init.py"]

# ---- disable real API imports during tests -----------------
# We patch disk_api before any real import happens
import disk_api  # noqa: E402


# ── Mock API ───────────────────────────────────────────────

class MockYaDiskAPI:
    """
    A fake YaDiskAPI that doesn't make HTTP requests.

    Stores a dict-based filesystem so tests can control exactly
    what the "cloud" contains.  Call `set_cloud_files(items)` to
    configure the mock state.
    """

    def __init__(self, token: str = "fake-token"):
        self.token = token
        self._cloud: dict[str, dict] = {}  # cloud_path → item dict
        self._publish_urls: dict[str, str] = {}
        self._call_log: list[tuple[str, tuple, dict]] = []  # for test assertions
        # Error simulation flags
        self._fail_next = 0  # count of next N calls to fail
        self._auth_error = False

    def _log(self, method: str, *args, **kwargs):
        self._call_log.append((method, args, kwargs))

    # ── configuration helpers (for tests) ─────────

    def set_cloud_files(self, items: list[dict]):
        """Replace the entire cloud file tree."""
        self._cloud = {}
        for it in items:
            path = it["path"]
            # ensure leading /
            if not path.startswith("/"):
                path = "/" + path
            it = dict(it)
            it["path"] = path
            # auto-fill md5 if missing
            if "md5" not in it:
                it["md5"] = hashlib.md5(path.encode()).hexdigest()
            self._cloud[path] = it

    def add_cloud_file(self, item: dict):
        """Add a single file to the cloud."""
        path = item.get("path", "")
        if not path.startswith("/"):
            path = "/" + path
        item = dict(item)
        item["path"] = path
        if "md5" not in item:
            item["md5"] = hashlib.md5(path.encode()).hexdigest()
        self._cloud[path] = item

    def remove_cloud_file(self, path: str):
        """Remove a file from the cloud."""
        if not path.startswith("/"):
            path = "/" + path
        self._cloud.pop(path, None)

    def fail_next(self, n: int = 1):
        """Make the next N API calls raise YaDiskError."""
        self._fail_next = n

    def set_auth_error(self, val: bool = True):
        self._auth_error = val

    def clear_call_log(self):
        self._call_log.clear()

    # ── API methods (mocked) ──────────────────────

    def get_disk_info(self) -> dict:
        self._log("get_disk_info")
        if self._auth_error:
            raise disk_api.AuthError("HTTP 401: token invalid")
        total = 10 * 1024**3  # 10 GB
        used = sum(f.get("size", 0) for f in self._cloud.values())
        return {"total_space": total, "used_space": used}

    def list_folder(self, path: str = "/", limit: int = 200,
                    offset: int = 0) -> list[dict]:
        self._log("list_folder", path)
        if self._auth_error:
            raise disk_api.AuthError("HTTP 401: token invalid")
        if self._fail_next > 0:
            self._fail_next -= 1
            raise disk_api.YaDiskError("Simulated API failure")

        # Return direct children of the given path
        prefix = path.rstrip("/")
        if prefix:
            prefix_slash = prefix + "/"
        else:
            prefix_slash = "/"
        result = []
        for cp, item in self._cloud.items():
            if cp == path:
                continue
            if cp.startswith(prefix_slash):
                remainder = cp[len(prefix_slash):]
                # Only direct children
                if "/" not in remainder:
                    result.append(dict(item))
        return result

    def get_meta(self, path: str) -> dict:
        self._log("get_meta", path)
        if self._auth_error:
            raise disk_api.AuthError("HTTP 401: token invalid")
        item = self._cloud.get(path)
        if item:
            return dict(item)
        raise disk_api.YaDiskError(f"Resource not found: {path}")

    def get_all_files_page(self, limit: int = 200,
                           offset: int = 0) -> list[dict]:
        """Mock одной страницы /resources/files с пагинацией."""
        self._log("get_all_files_page")
        if self._auth_error:
            raise disk_api.AuthError("HTTP 401: token invalid")
        if self._fail_next > 0:
            self._fail_next -= 1
            raise disk_api.YaDiskError("Simulated API failure")
        files = [dict(v) for v in self._cloud.values()
                 if v.get("type") != "dir"]
        return files[offset:offset + limit]

    def get_all_files(self, limit: int = 200,
                      offset: int = 0) -> list[dict]:
        self._log("get_all_files")
        if self._auth_error:
            raise disk_api.AuthError("HTTP 401: token invalid")
        if self._fail_next > 0:
            self._fail_next -= 1
            raise disk_api.YaDiskError("Simulated API failure")
        files = [dict(v) for v in self._cloud.values()
                 if v.get("type") != "dir"]
        return files

    def get_download_url(self, path: str) -> str:
        self._log("get_download_url", path)
        if self._auth_error:
            raise disk_api.AuthError("HTTP 401: token invalid")
        return f"https://mock-dl.yadisk.test/{path.lstrip('/')}"

    def download_file(self, path: str, local_path: str) -> None:
        """Mock download — just create an empty file."""
        self._log("download_file", path, local_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "w") as f:
            f.write(f"mock content for {path}")

    def get_upload_url(self, path: str) -> str:
        self._log("get_upload_url", path)
        return f"https://mock-ul.yadisk.test/{path.lstrip('/')}"

    def upload_file(self, local_path: str, cloud_path: str) -> int:
        self._log("upload_file", local_path, cloud_path)
        # Actually add the file to the cloud store so tests can verify
        if os.path.exists(local_path):
            import hashlib
            name = os.path.basename(local_path)
            with open(local_path, "rb") as f:
                md5 = hashlib.md5(f.read()).hexdigest()
            size = os.path.getsize(local_path)
            self._cloud[cloud_path] = {
                "path": cloud_path,
                "name": name,
                "type": "file",
                "size": size,
                "modified": "2025-07-01T00:00:00Z",
                "md5": md5,
            }
        return 201

    def delete(self, path: str, permanently: bool = False) -> None:
        self._log("delete", path, permanently)
        self._cloud.pop(path, None)

    def create_folder(self, path: str) -> None:
        self._log("create_folder", path)
        self._cloud[path] = {
            "path": path, "name": path.rstrip("/").split("/")[-1],
            "type": "dir", "size": 0,
        }

    def move(self, src: str, dst: str, overwrite: bool = False) -> None:
        self._log("move", src, dst, overwrite)
        if src in self._cloud:
            item = self._cloud.pop(src)
            item["path"] = dst
            self._cloud[dst] = item

    def copy(self, src: str, dst: str, overwrite: bool = False) -> None:
        self._log("copy", src, dst, overwrite)
        if src in self._cloud:
            item = dict(self._cloud[src])
            item["path"] = dst
            self._cloud[dst] = item

    def publish(self, path: str) -> str:
        self._log("publish", path)
        url = f"https://yadisk.test/public/{path.lstrip('/')}"
        self._publish_urls[path] = url
        return url

    def unpublish(self, path: str) -> None:
        self._log("unpublish", path)
        self._publish_urls.pop(path, None)

    def get_recent_uploaded(self, limit: int = 50) -> list[dict]:
        self._log("get_recent_uploaded")
        return [dict(v) for v in self._cloud.values()
                if v.get("type") != "dir"]


# ── Fixtures ───────────────────────────────────────────────

@pytest.fixture
def in_memory_db():
    """Provide a Database connected to :memory: SQLite."""
    import db
    database = db.Database(":memory:")
    # Ensure in-memory schema is created
    database._init_schema()
    yield database
    database.close()


@pytest.fixture
def temp_cache():
    """Provide a temporary cache directory with cleanup."""
    tmpdir = tempfile.mkdtemp(prefix="ydm_test_")
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def mock_api():
    """Provide a fresh MockYaDiskAPI."""
    return MockYaDiskAPI()


@pytest.fixture
def sample_cloud_files():
    """
    Return a standard set of cloud items for most tests:

    /Документы/ (dir)
    /Документы/отчёт.docx
    /Фото/ (dir)
    /Фото/2024/ (dir)
    /Фото/2024/отпуск.jpg
    /Фото/2024/пляж.jpg
    /Музыка/ (dir)
    /Музыка/трек.mp3
    /readme.txt
    """
    return [
        {"path": "/Документы", "name": "Документы", "type": "dir",
         "size": 0, "modified": "2025-01-01T00:00:00Z",
         "md5": ""},
        {"path": "/Документы/отчёт.docx", "name": "отчёт.docx",
         "type": "file", "size": 2340000,
         "modified": "2025-01-15T10:30:00Z",
         "md5": "aaa111"},
        {"path": "/Фото", "name": "Фото", "type": "dir",
         "size": 0, "modified": "2025-01-01T00:00:00Z",
         "md5": ""},
        {"path": "/Фото/2024", "name": "2024", "type": "dir",
         "size": 0, "modified": "2025-01-01T00:00:00Z",
         "md5": ""},
        {"path": "/Фото/2024/отпуск.jpg", "name": "отпуск.jpg",
         "type": "file", "size": 1100000,
         "modified": "2025-02-01T08:00:00Z",
         "md5": "bbb222"},
        {"path": "/Фото/2024/пляж.jpg", "name": "пляж.jpg",
         "type": "file", "size": 950000,
         "modified": "2025-02-01T09:00:00Z",
         "md5": "ccc333"},
        {"path": "/Музыка", "name": "Музыка", "type": "dir",
         "size": 0, "modified": "2025-01-01T00:00:00Z",
         "md5": ""},
        {"path": "/Музыка/трек.mp3", "name": "трек.mp3",
         "type": "file", "size": 8500000,
         "modified": "2025-03-01T12:00:00Z",
         "md5": "ddd444"},
        {"path": "/readme.txt", "name": "readme.txt",
         "type": "file", "size": 1200,
         "modified": "2024-12-01T00:00:00Z",
         "md5": "eee555"},
    ]


@pytest.fixture
def populated_db(in_memory_db, sample_cloud_files):
    """Provide an in-memory DB pre-loaded with sample cloud files."""
    from db import Database
    db: Database = in_memory_db
    db.upsert_files_batch(sample_cloud_files)

    # Mark some files as downloaded to simulate partial state
    # By default: /Фото/2024/отпуск.jpg is downloaded,
    # /Фото/2024/пляж.jpg is cloud_only
    db.set_downloaded(
        "/Фото/2024/отпуск.jpg",
        "/tmp/ydm-test/Фото/2024/отпуск.jpg",
        last_sync_md5="bbb222",
    )
    return db
