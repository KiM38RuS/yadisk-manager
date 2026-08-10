"""
Tests for Drag-and-Drop logic (v0.4 feature).

Since drag-and-drop is Qt/event-based, these tests verify the
*processing logic* that would be called when files are dropped:
  1. File gathering from dropped paths (files + folder recursion)
  2. Cloud path computation
  3. Cache copy + upload trigger
"""

import os
import hashlib
import shutil
import tempfile
import pytest


def _collect_dropped(urls: list[str]) -> list[str]:
    """Simulate MainWindow.dropEvent's file gathering logic."""
    all_files: list[str] = []
    for local_path in urls:
        if os.path.isfile(local_path):
            all_files.append(local_path)
        elif os.path.isdir(local_path):
            for root, dirs, files in os.walk(local_path):
                for f in files:
                    all_files.append(os.path.join(root, f))
    return all_files


def _make_cloud_path(current_path: str, filename: str) -> str:
    """Simulate cloud path computation."""
    return (current_path.rstrip("/") + "/" + filename).replace("//", "/")


class TestDropFileGathering:
    """Verify that dropped files are correctly enumerated."""

    def test_single_file(self, temp_cache):
        f = os.path.join(temp_cache, "doc.txt")
        with open(f, "w") as fh:
            fh.write("content")
        files = _collect_dropped([f])
        assert len(files) == 1
        assert files[0] == f

    def test_multiple_files(self, temp_cache):
        f1 = os.path.join(temp_cache, "a.txt")
        f2 = os.path.join(temp_cache, "b.txt")
        for p in (f1, f2):
            with open(p, "w") as fh:
                fh.write("x")
        files = _collect_dropped([f1, f2])
        assert len(files) == 2
        assert f1 in files
        assert f2 in files

    def test_folder_recursion(self, temp_cache):
        """Drop a folder → all files inside (nested) are collected."""
        os.makedirs(os.path.join(temp_cache, "sub"))
        f1 = os.path.join(temp_cache, "root.txt")
        f2 = os.path.join(temp_cache, "sub", "nested.txt")
        for p in (f1, f2):
            with open(p, "w") as fh:
                fh.write("x")
        files = _collect_dropped([temp_cache])
        assert len(files) == 2
        assert f1 in files
        assert f2 in files

    def test_mixed_files_and_folders(self, temp_cache):
        """Drop files + folders → all files collected."""
        os.makedirs(os.path.join(temp_cache, "D"))
        f1 = os.path.join(temp_cache, "f1.txt")
        f2 = os.path.join(temp_cache, "D", "f2.txt")
        f3 = os.path.join(temp_cache, "f3.txt")
        for p in (f1, f2, f3):
            with open(p, "w") as fh:
                fh.write("x")
        files = _collect_dropped([f1, os.path.join(temp_cache, "D"), f3])
        assert len(files) == 3
        assert f1 in files
        assert f2 in files
        assert f3 in files

    def test_empty_folder_ignored(self, temp_cache):
        empty_dir = os.path.join(temp_cache, "empty")
        os.makedirs(empty_dir)
        files = _collect_dropped([empty_dir])
        assert files == []

    def test_nonexistent_path_handled(self, temp_cache):
        """Non-existent paths return 0 files (os.path.isfile/isdir = False)."""
        files = _collect_dropped([os.path.join(temp_cache, "no_such.txt")])
        assert files == []


class TestDropCloudPath:
    """Verify cloud path computation from drag-drop."""

    def test_root_folder(self):
        path = _make_cloud_path("/", "report.docx")
        assert path == "/report.docx"

    def test_nested_folder(self):
        path = _make_cloud_path("/Документы", "photo.jpg")
        assert path == "/Документы/photo.jpg"

    def test_deeply_nested(self):
        path = _make_cloud_path("/A/B/C", "notes.txt")
        assert path == "/A/B/C/notes.txt"

    def test_path_with_trailing_slash(self):
        path = _make_cloud_path("/Docs/", "f.txt")
        assert path == "/Docs/f.txt"

    def test_file_name_with_spaces(self):
        path = _make_cloud_path("/", "my file.pdf")
        assert path == "/my file.pdf"

    def test_cyrillic_filename(self):
        path = _make_cloud_path("/Фото", "отчёт.docx")
        assert path == "/Фото/отчёт.docx"


class TestDropUploadRoundtrip:
    """Integration test: drop event chain → DB + mock API."""

    def test_dropped_file_uploaded_to_cloud(self, in_memory_db, mock_api,
                                             temp_cache):
        """Simulate what happens when a file is dropped:
        1. File copied to cache
        2. Upload triggered
        3. DB updated
        """
        import sync

        # Create a file to "drop"
        src = os.path.join(temp_cache, "dropped.txt")
        with open(src, "w") as f:
            f.write("hello from drag-drop")

        # 1. Copy to cache (simulating dropEvent)
        cloud_path = "/dropped.txt"
        local_copy = os.path.join(temp_cache, "local", "dropped.txt")
        os.makedirs(os.path.dirname(local_copy), exist_ok=True)
        shutil.copy2(src, local_copy)

        # 2. Simulate the upload (what _start_upload does + _ui_finished)
        md5 = hashlib.md5(b"hello from drag-drop").hexdigest()
        mock_api.upload_file(local_copy, cloud_path)
        in_memory_db.upsert_file(cloud_path, "dropped.txt", "file",
                                 size=os.path.getsize(local_copy),
                                 modified="2025-07-01T00:00:00Z",
                                 md5=md5)
        in_memory_db.set_downloaded(cloud_path, local_copy,
                                    last_sync_md5=md5)

        # 3. Verify
        rec = in_memory_db.get_file(cloud_path)
        assert rec is not None
        assert rec["status"] == "downloaded"
        assert rec["local_path"] == local_copy
        # Cloud should have the file after upload
        assert mock_api._cloud.get(cloud_path) is not None

    def test_dropped_folder_uploaded(self, in_memory_db, mock_api,
                                      temp_cache):
        """Dropping a folder → each file is uploaded individually."""
        import sync

        # Create folder with nested files
        src_dir = os.path.join(temp_cache, "drop_folder")
        os.makedirs(os.path.join(src_dir, "sub"))
        files_data = {
            "a.txt": "file a",
            "b.txt": "file b",
            "sub/c.txt": "file c",
        }

        for rel, content in files_data.items():
            p = os.path.join(src_dir, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(content)

        files = _collect_dropped([src_dir])
        assert len(files) == 3

        for src in files:
            rel = os.path.relpath(src, src_dir)
            cloud_path = "/" + rel.replace("\\", "/")
            local_copy = os.path.join(temp_cache, "uploads", rel)
            os.makedirs(os.path.dirname(local_copy), exist_ok=True)
            shutil.copy2(src, local_copy)

            md5 = hashlib.md5(
                files_data[rel.replace("\\", "/")].encode()).hexdigest()
            mock_api.upload_file(local_copy, cloud_path)
            in_memory_db.upsert_file(
                cloud_path, os.path.basename(rel), "file",
                size=os.path.getsize(local_copy),
                modified="2025-07-01T00:00:00Z", md5=md5)
            in_memory_db.set_downloaded(cloud_path, local_copy,
                                        last_sync_md5=md5)

        for rel in files_data:
            cloud_path = "/" + rel.replace("\\", "/")
            rec = in_memory_db.get_file(cloud_path)
            assert rec is not None
            assert rec["status"] == "downloaded"
