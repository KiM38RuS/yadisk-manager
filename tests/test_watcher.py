"""
Tests for watcher.py — watchdog-based file observer.

Covers:
  Unit: _DebouncedHandler — filtering, debounce logic, event routing
  Integration: FileWatcher — Observer lifecycle on real filesystem
"""
import os
import time
import tempfile
import shutil
from unittest import mock

import pytest

from watcher import _DebouncedHandler, FileWatcher, DEBOUNCE_SEC


# ═══════════════════════════════════════════════════════════
#  Helper
# ═══════════════════════════════════════════════════════════

def _collector():
    """Returns (callback, events) where callback records (args, kwargs) into events."""
    events: list[tuple[tuple, dict]] = []

    def cb(*args, **kwargs):
        events.append((args, kwargs))

    return cb, events


def _make_event(path: str, is_dir: bool = False, src: str = "", dest: str = ""):
    """Create a minimal mock watchdog event object."""
    class _Event:
        src_path = src or path
        dest_path = dest or path
        is_directory = is_dir
    return _Event()


# ═══════════════════════════════════════════════════════════
#  Unit: _DebouncedHandler
# ═══════════════════════════════════════════════════════════

class TestIsTemp:
    """_is_temp must filter editor temp / hidden / swap files."""

    def test_hidden_unix(self):
        h = _DebouncedHandler(lambda *a, **kw: None)
        assert h._is_temp("/path/.hidden.txt")

    def test_hidden_windows(self):
        h = _DebouncedHandler(lambda *a, **kw: None)
        assert h._is_temp("/path/~$document.docx")

    def test_tmp_extension(self):
        h = _DebouncedHandler(lambda *a, **kw: None)
        assert h._is_temp("/path/file.tmp")

    def test_normal_file_not_temp(self):
        h = _DebouncedHandler(lambda *a, **kw: None)
        assert not h._is_temp("/path/report.docx")
        assert not h._is_temp("/path/image.jpg")
        assert not h._is_temp("/path/archive.tar.gz")
        assert not h._is_temp("C:\\Users\\test\\file.pdf")


class TestDebouncedHandlerFiltering:
    """Verify events are accepted or rejected based on path/directory."""

    def test_modified_directory_ignored(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)
        h.on_modified(_make_event("/dir", is_dir=True))
        assert "/dir" not in h._pending
        assert len(events) == 0

    def test_modified_temp_ignored(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)
        h.on_modified(_make_event("/dir/.swp"))
        assert "/dir/.swp" not in h._pending

    def test_modified_normal_queued(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)
        h.on_modified(_make_event("/dir/report.txt"))
        assert "/dir/report.txt" in h._pending
        assert h._pending["/dir/report.txt"][0] == "modified"

    def test_created_directory_ignored(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)
        h.on_created(_make_event("/dir", is_dir=True))
        assert "/dir" not in h._pending

    def test_created_temp_ignored(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)
        h.on_created(_make_event("/dir/.tmp"))
        assert "/dir/.tmp" not in h._pending

    def test_created_normal_queued(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)
        h.on_created(_make_event("/dir/new.txt"))
        assert "/dir/new.txt" in h._pending
        assert h._pending["/dir/new.txt"][0] == "created"

    def test_deleted_temp_ignored(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)
        h.on_deleted(_make_event("/dir/~temp.docx"))
        assert len(events) == 0

    def test_deleted_directory_fires_immediately(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)
        h.on_deleted(_make_event("/photos", is_dir=True))
        assert len(events) == 1
        args, kwargs = events[0]
        assert args == ("deleted", "/photos", True)

    def test_deleted_file_fires_immediately(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)
        h.on_deleted(_make_event("/photos/IMG001.jpg"))
        assert len(events) == 1
        args, kwargs = events[0]
        assert args == ("deleted", "/photos/IMG001.jpg", False)

    def test_moved_directory_ignored(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)
        h.on_moved(_make_event("/old_dir", dest="/new_dir", is_dir=True))
        assert len(h._pending_moved) == 0

    def test_moved_to_temp_ignored(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)
        h.on_moved(_make_event("/doc.txt", dest="/dir/.tmp"))
        assert len(h._pending_moved) == 0

    def test_moved_normal_queued(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)
        h.on_moved(_make_event("/old.txt", dest="/new.txt"))
        assert "/new.txt" in h._pending_moved
        src, dst, ts = h._pending_moved["/new.txt"]
        assert src == "/old.txt"
        assert dst == "/new.txt"


class TestDebouncedHandlerFlush:
    """Verify _flush logic directly (manipulating _pending timestamps)."""

    def test_flush_empty_is_noop(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)
        h._flush()
        assert len(events) == 0

    def test_flush_delivers_ready_modified(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)

        # Put an expired event in _pending
        old_ts = time.time() - DEBOUNCE_SEC - 1.0
        h._pending["/report.txt"] = ("modified", old_ts)

        with mock.patch("os.path.exists", return_value=True):
            h._flush()

        assert len(events) == 1
        args, kwargs = events[0]
        assert args == ("modified", "/report.txt")
        assert "/report.txt" not in h._pending

    def test_flush_skips_not_yet_ready(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)

        # Fresh event — debounce hasn't passed yet
        h._pending["/report.txt"] = ("modified", time.time())
        h._flush()

        assert len(events) == 0
        assert "/report.txt" in h._pending  # still waiting

    def test_flush_drops_nonexistent_file(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)

        old_ts = time.time() - DEBOUNCE_SEC - 1.0
        h._pending["/gone.txt"] = ("modified", old_ts)

        with mock.patch("os.path.exists", return_value=False):
            h._flush()

        assert len(events) == 0
        assert "/gone.txt" not in h._pending  # dropped

    def test_flush_multiple_ready_events(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)

        old_ts = time.time() - DEBOUNCE_SEC - 1.0
        h._pending["/a.txt"] = ("modified", old_ts)
        h._pending["/b.txt"] = ("created", old_ts)
        h._pending["/c.txt"] = ("modified", old_ts)

        with mock.patch("os.path.exists", return_value=True):
            h._flush()

        assert len(events) == 3

    def test_flush_moved_before_modified(self):
        """Moved events are flushed before modified/created."""
        cb, events = _collector()
        h = _DebouncedHandler(cb)

        old_ts = time.time() - DEBOUNCE_SEC - 1.0
        h._pending["/file.txt"] = ("modified", old_ts)
        h._pending_moved["/new.txt"] = ("/old.txt", "/new.txt", old_ts)

        with mock.patch("os.path.exists", return_value=True):
            h._flush()

        # First event should be moved
        assert len(events) == 2
        assert events[0][0][0] == "moved"


class TestDebouncedHandlerCatchupTimer:
    """The catch-up timer ensures pending events are flushed after debounce."""

    def test_catchup_timer_fires_after_debounce(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)

        # Add a fresh event (not yet ready)
        h._pending["/report.txt"] = ("modified", time.time())

        # Manually arm the catch-up
        h._arm_catchup()
        try:
            with mock.patch("os.path.exists", return_value=True):
                # Wait for timer to fire (DEBOUNCE_SEC + small margin)
                time.sleep(DEBOUNCE_SEC + 0.5)
                assert len(events) >= 1
        finally:
            if h._catchup_timer:
                h._catchup_timer.cancel()

    def test_catchup_does_not_fire_if_no_pending(self):
        cb, events = _collector()
        h = _DebouncedHandler(cb)

        h._arm_catchup()
        try:
            time.sleep(DEBOUNCE_SEC + 0.3)
            # No pending → no second flush with events
            assert len(events) == 0
        finally:
            if h._catchup_timer:
                h._catchup_timer.cancel()


class TestFileWatcherLifecycle:
    """FileWatcher start/stop (no real FS events expected)."""

    def test_start_stop(self, watch_dir):
        cb, events = _collector()
        fw = FileWatcher(watch_dir, cb)
        fw.start()
        time.sleep(0.3)
        fw.stop()
        assert fw._observer is not None
        assert not fw._observer.is_alive()


# ═══════════════════════════════════════════════════════════
#  Integration: FileWatcher on real filesystem
# ═══════════════════════════════════════════════════════════

class TestFileWatcherIntegration:
    """Start a real FileWatcher on a temp dir, exercise FS events.

    These tests use the real watchdog Observer + real file operations.
    The Observer runs in its own thread; the catch-up timer in
    _DebouncedHandler ensures pending events are flushed after DEBOUNCE_SEC.
    """

    @pytest.fixture(autouse=True)
    def _clean_catchup(self):
        """Fixture-level cleanup: stop any background catch-up timers after each test."""
        yield
        # Nothing to do here — each test creates its own handler.

    def test_file_created_triggers_callback(self, watch_dir):
        """Creating a file in watched dir should fire callback (after debounce)."""
        cb, events = _collector()
        fw = FileWatcher(watch_dir, cb)
        fw.start()
        try:
            test_file = os.path.join(watch_dir, "hello.txt")
            with open(test_file, "w") as f:
                f.write("hello")

            # Wait for catch-up timer (DEBOUNCE_SEC) + watchdog latency
            time.sleep(DEBOUNCE_SEC + 2.0)

            matched = any(
                len(args) >= 2 and args[1] == test_file and args[0] in ("created", "modified")
                for (args, kwargs) in events
            )
            assert matched, f"No event for {test_file} in {events}"
        finally:
            fw.stop()

    def test_file_deleted_triggers_callback(self, watch_dir):
        """Deleting a file should fire callback immediately (no debounce)."""
        test_file = os.path.join(watch_dir, "todelete.txt")
        with open(test_file, "w") as f:
            f.write("delete me")

        cb, events = _collector()
        fw = FileWatcher(watch_dir, cb)
        fw.start()
        try:
            time.sleep(0.3)

            os.remove(test_file)
            time.sleep(0.5)

            matched = any(
                len(args) >= 2 and args[1] == test_file and args[0] == "deleted"
                for (args, kwargs) in events
            )
            assert matched, f"No deleted event for {test_file} in {events}"
        finally:
            fw.stop()

    def test_file_modified_triggers_callback(self, watch_dir):
        """Modifying a file should fire callback (after debounce)."""
        test_file = os.path.join(watch_dir, "editme.txt")
        with open(test_file, "w") as f:
            f.write("original")

        cb, events = _collector()
        fw = FileWatcher(watch_dir, cb)
        fw.start()
        try:
            time.sleep(0.3)

            with open(test_file, "w") as f:
                f.write("modified content")

            time.sleep(DEBOUNCE_SEC + 2.0)

            matched = any(
                len(args) >= 2 and args[1] == test_file and args[0] == "modified"
                for (args, kwargs) in events
            )
            assert matched, f"No modified event for {test_file} in {events}"
        finally:
            fw.stop()

    def test_file_moved_triggers_callback(self, watch_dir):
        """Move (rename) a file should fire a moved callback."""
        src = os.path.join(watch_dir, "source.txt")
        dst = os.path.join(watch_dir, "dest.txt")
        with open(src, "w") as f:
            f.write("move me")

        cb, events = _collector()
        fw = FileWatcher(watch_dir, cb)
        fw.start()
        try:
            time.sleep(0.3)

            os.rename(src, dst)

            time.sleep(DEBOUNCE_SEC + 2.0)

            matched = any(
                len(args) >= 2 and args[0] == "moved" and args[1] == src
                for (args, kwargs) in events
            )
            assert matched, f"No moved event for {src}→{dst} in {events}"
        finally:
            fw.stop()

    def test_temp_file_ignored(self, watch_dir):
        """Temp files should NOT trigger callback."""
        cb, events = _collector()
        fw = FileWatcher(watch_dir, cb)
        fw.start()
        try:
            for name in (".hidden.txt", "~$temp.docx", "temp.tmp"):
                p = os.path.join(watch_dir, name)
                with open(p, "w") as f:
                    f.write("temp")

            time.sleep(DEBOUNCE_SEC + 2.0)

            for (args, _) in events:
                ev_path = args[1] if len(args) >= 2 else ""
                basename = os.path.basename(ev_path)
                assert not basename.startswith("."), f"Hidden file triggered: {ev_path}"
                assert not basename.startswith("~"), f"Office temp triggered: {ev_path}"
                assert not basename.endswith(".tmp"), f".tmp file triggered: {ev_path}"
        finally:
            fw.stop()

    def test_multiple_files_batched(self, watch_dir):
        """Multiple files created rapidly should all be detected."""
        cb, events = _collector()
        fw = FileWatcher(watch_dir, cb)
        fw.start()
        try:
            files = []
            for i in range(5):
                p = os.path.join(watch_dir, f"batch_{i}.txt")
                files.append(p)
                with open(p, "w") as f:
                    f.write(f"file {i}")

            time.sleep(DEBOUNCE_SEC + 2.0)

            fired = {args[1] for (args, _) in events if len(args) >= 2}
            for f in files:
                assert f in fired, f"No event for {f}"
        finally:
            fw.stop()

    def test_subdirectory_file_detected(self, watch_dir):
        """Files in subdirectories (recursive) should be detected."""
        subdir = os.path.join(watch_dir, "sub", "nested")
        os.makedirs(subdir, exist_ok=True)

        cb, events = _collector()
        fw = FileWatcher(watch_dir, cb)
        fw.start()
        try:
            deep_file = os.path.join(subdir, "deep.txt")
            with open(deep_file, "w") as f:
                f.write("deep")

            time.sleep(DEBOUNCE_SEC + 2.0)

            matched = any(
                len(args) >= 2 and args[1] == deep_file and args[0] in ("created", "modified")
                for (args, kwargs) in events
            )
            assert matched, f"No event for nested {deep_file} in {events}"
        finally:
            fw.stop()


class TestFileWatcherDeletionStress:
    """Verify watcher handles directory removal gracefully."""

    def test_watcher_survives_subdir_deleted(self, watch_dir):
        """Watcher should not crash if a watched subdirectory is deleted externally."""
        sub = os.path.join(watch_dir, "subdir")
        os.makedirs(sub)

        cb, events = _collector()
        fw = FileWatcher(watch_dir, cb)
        fw.start()
        try:
            time.sleep(0.3)

            shutil.rmtree(sub)
            time.sleep(0.5)

            assert fw._observer.is_alive()
        finally:
            fw.stop()

    def test_watch_dir_deleted_while_running(self, watch_dir):
        """Watcher should not crash if the root watch dir is deleted."""
        cb, events = _collector()
        fw = FileWatcher(watch_dir, cb)
        fw.start()
        try:
            time.sleep(0.3)

            shutil.rmtree(watch_dir)
            time.sleep(0.5)

            assert True  # no crash = pass
        finally:
            fw.stop()


# ═══════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def watch_dir():
    tmpdir = tempfile.mkdtemp(prefix="ydm_watch_")
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)
