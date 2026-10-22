"""
Тесты системы обновлений (updater.py).

Проверяет:
- parse_version: 'v0.11.6' / '0.11.6' / мусор
- is_newer: сравнение (major, minor, patch)
- find_update: парсинг ответа GitHub API (мок), поиск exe-ассета,
  отказ от Setup-инсталлера, отсутствие обновления, пустой UPDATE_REPO
- graceful fail: ошибка сети не роняет проверку
"""

import sys
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import updater


class TestParseVersion(unittest.TestCase):
    def test_v_prefix(self):
        self.assertEqual(updater.parse_version("v0.11.6"), (0, 11, 6))

    def test_no_prefix(self):
        self.assertEqual(updater.parse_version("0.11.6"), (0, 11, 6))

    def test_three_digits(self):
        self.assertEqual(updater.parse_version("1.2.3"), (1, 2, 3))

    def test_garbage(self):
        self.assertIsNone(updater.parse_version("latest"))
        self.assertIsNone(updater.parse_version(""))
        self.assertIsNone(updater.parse_version(None))

    def test_partial(self):
        # '0.11' без patch — не распознаётся (не наш формат)
        self.assertIsNone(updater.parse_version("0.11"))


class TestIsNewer(unittest.TestCase):
    def test_newer_major(self):
        self.assertTrue(updater.is_newer("v1.0.0", "0.11.6"))

    def test_newer_minor(self):
        self.assertTrue(updater.is_newer("v0.12.0", "0.11.6"))

    def test_newer_patch(self):
        self.assertTrue(updater.is_newer("v0.11.7", "0.11.6"))

    def test_same(self):
        self.assertFalse(updater.is_newer("v0.11.6", "0.11.6"))

    def test_older(self):
        self.assertFalse(updater.is_newer("v0.11.5", "0.11.6"))

    def test_garbage(self):
        self.assertFalse(updater.is_newer("garbage", "0.11.6"))
        self.assertFalse(updater.is_newer("v0.11.7", "garbage"))


class TestFindUpdate(unittest.TestCase):
    """find_update с мокнутой общей сессией (get_shared_session)."""

    def _release(self, tag, assets):
        return mock.Mock(
            status_code=200,
            json=lambda: {"tag_name": tag, "body": "notes", "assets": assets},
        )

    def _patch_session(self, response_or_side_effect):
        """Подменить get_shared_session() на фиктивный объект, чей .get(...)
        возвращает response_or_side_effect.

        Если передан requests.RequestException (или любой BaseException) —
        это side_effect. Иначе — return_value.

        После PLANS.md 2.15 fetch_latest_release ходит через
        get_shared_session().get(), а не голый requests.get — поэтому
        мокаем именно session.get().
        """
        mock_session = mock.Mock()
        if isinstance(response_or_side_effect, BaseException):
            mock_session.get.side_effect = response_or_side_effect
        else:
            mock_session.get.return_value = response_or_side_effect
        return mock.patch("updater.get_shared_session", return_value=mock_session)

    def test_no_repo(self):
        self.assertIsNone(updater.find_update("", "0.11.6"))

    def test_network_error_graceful(self):
        with self._patch_session(requests.RequestException("boom")):
            self.assertIsNone(updater.find_update("u/r", "0.11.6"))

    def test_404_graceful(self):
        with self._patch_session(mock.Mock(status_code=404)):
            self.assertIsNone(updater.find_update("u/r", "0.11.6"))

    def test_no_releases_yet_same_version(self):
        with self._patch_session(self._release("v0.11.6", [])):
            self.assertIsNone(updater.find_update("u/r", "0.11.6"))

    def test_finds_exe_asset(self):
        assets = [
            {"name": "Setup_YaDiskManager_v0.12.0.exe",
             "browser_download_url": "https://x/Setup.exe", "size": 5},
            {"name": "YaDiskManager.exe",
             "browser_download_url": "https://x/YaDiskManager.exe", "size": 55},
        ]
        with self._patch_session(self._release("v0.12.0", assets)):
            info = updater.find_update("u/r", "0.11.6")
        self.assertIsNotNone(info)
        self.assertEqual(info.version, "0.12.0")
        self.assertEqual(info.download_url, "https://x/YaDiskManager.exe")
        self.assertEqual(info.size, 55)
        # Setup-инсталлер проигнорирован, взят exe

    def test_only_setup_asset_no_update(self):
        assets = [
            {"name": "Setup_YaDiskManager_v0.12.0.exe",
             "browser_download_url": "https://x/Setup.exe", "size": 5},
        ]
        with self._patch_session(self._release("v0.12.0", assets)):
            self.assertIsNone(updater.find_update("u/r", "0.11.6"))

    def test_no_assets_no_update(self):
        with self._patch_session(self._release("v0.12.0", [])):
            self.assertIsNone(updater.find_update("u/r", "0.11.6"))


class TestApplyUpdateCreationFlags(unittest.TestCase):
    """apply_update: флаги запуска apply_update.bat.

    Регресс v0.11.6–v0.12.5: в apply_update creationflags задан как
    `0x08000000 | 0x00000004` с комментарием `CREATE_NO_WINDOW |
    DETACHED_PROCESS`, но 0x4 — это CREATE_SUSPENDED (DETACHED_PROCESS
    = 0x8). cmd.exe создавался приостановленным и bat никогда не
    выполнялся: обновление скачивалось, приложение закрывалось —
    дальше тишина, в системе оставался зомби cmd.exe (Suspended).
    """

    def _apply_in_sandbox(self):
        """apply_update в песочнице: frozen, Popen замокан."""
        sandbox = tempfile.mkdtemp(prefix="ydm-test-apply-")
        self.addCleanup(shutil.rmtree, sandbox, ignore_errors=True)
        exe = os.path.join(sandbox, "YaDiskManager.exe")
        new_exe = os.path.join(sandbox, "YaDiskManager.new.exe")
        for f in (exe, new_exe):
            with open(f, "wb"):
                pass
        with mock.patch.object(updater.sys, "frozen", True, create=True), \
                mock.patch.object(updater.sys, "executable", exe), \
                mock.patch.object(updater.subprocess, "Popen") as popen:
            ok = updater.apply_update(new_exe)
        return ok, popen, sandbox

    def test_no_create_suspended_flag(self):
        ok, popen, _ = self._apply_in_sandbox()
        self.assertTrue(ok, "apply_update должен запустить bat")
        popen.assert_called_once()
        flags = popen.call_args.kwargs["creationflags"]
        self.assertFalse(
            flags & 0x4,
            "creationflags содержит CREATE_SUSPENDED (0x4): cmd.exe "
            "стартует приостановленным и apply_update.bat не выполнится",
        )

    def test_flags_are_detached_and_no_window(self):
        ok, popen, _ = self._apply_in_sandbox()
        self.assertTrue(ok)
        flags = popen.call_args.kwargs["creationflags"]
        self.assertEqual(
            flags,
            subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )

    def test_bat_written_next_to_exe(self):
        _, popen, sandbox = self._apply_in_sandbox()
        bat = popen.call_args.args[0][2]
        self.assertEqual(os.path.dirname(bat), sandbox)
        self.assertTrue(os.path.isfile(bat))


if __name__ == "__main__":
    unittest.main()
