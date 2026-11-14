import hashlib
import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self):
        self.body = b""

    def put(self, href, data=None, timeout=0):
        for chunk in data:
            self.body += chunk
        return _Resp()


class _Api:
    def __init__(self):
        self._session = _Session()

    def get_upload_url(self, path):
        return "https://upload.test/" + path.lstrip("/")


def test_upload_worker_emits_md5_of_sent_bytes(qapp, tmp_path):
    from ui_workers import UploadWorker

    payload = b"version A" * 1000
    f = tmp_path / "a.txt"
    f.write_bytes(payload)
    api = _Api()
    worker = UploadWorker(api, str(f), "/a.txt")
    got = []
    worker.finished.connect(lambda cp, md5: got.append((cp, md5)))

    worker.run()

    assert got == [("/a.txt", hashlib.md5(payload).hexdigest())]
    assert api._session.body == payload
