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
    """find_update с мокнутым requests.get."""

    def _release(self, tag, assets):
        return mock.Mock(
            status_code=200,
            json=lambda: {"tag_name": tag, "body": "notes", "assets": assets},
        )

    def test_no_repo(self):
        self.assertIsNone(updater.find_update("", "0.11.6"))

    def test_network_error_graceful(self):
        with mock.patch("updater.requests.get",
                        side_effect=requests.RequestException("boom")):
            self.assertIsNone(updater.find_update("u/r", "0.11.6"))

    def test_404_graceful(self):
        with mock.patch("updater.requests.get",
                        return_value=mock.Mock(status_code=404)):
            self.assertIsNone(updater.find_update("u/r", "0.11.6"))

    def test_no_releases_yet_same_version(self):
        with mock.patch("updater.requests.get",
                        return_value=self._release("v0.11.6", [])):
            self.assertIsNone(updater.find_update("u/r", "0.11.6"))

    def test_finds_exe_asset(self):
        assets = [
            {"name": "Setup_YaDiskManager_v0.12.0.exe",
             "browser_download_url": "https://x/Setup.exe", "size": 5},
            {"name": "YaDiskManager.exe",
             "browser_download_url": "https://x/YaDiskManager.exe", "size": 55},
        ]
        with mock.patch("updater.requests.get",
                        return_value=self._release("v0.12.0", assets)):
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
        with mock.patch("updater.requests.get",
                        return_value=self._release("v0.12.0", assets)):
            self.assertIsNone(updater.find_update("u/r", "0.11.6"))

    def test_no_assets_no_update(self):
        with mock.patch("updater.requests.get",
                        return_value=self._release("v0.12.0", [])):
            self.assertIsNone(updater.find_update("u/r", "0.11.6"))


if __name__ == "__main__":
    unittest.main()
