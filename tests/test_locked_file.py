"""
Тесты определения «файл заблокирован» (is_file_locked_error).

Проверяет:
- PermissionError → True
- OSError(errno=13) → True
- OSError(errno=5) → False
- OSError с текстом «being used by another process» → True
- OSError с русским «Отказано в доступе» → True
- ValueError → False
- Обычный текст без маркеров → False
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# PySide6 нужен для импорта ui_shared, но в тестах не используется
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui_shared import is_file_locked_error


class TestIsFileLockedError(unittest.TestCase):
    def test_permission_error(self):
        """PermissionError всегда считается занятостью."""
        exc = PermissionError(13, "Permission denied")
        self.assertTrue(is_file_locked_error(exc))

    def test_os_error_errno_13(self):
        """OSError с errno 13 → True."""
        exc = OSError(13, "Permission denied")
        self.assertTrue(is_file_locked_error(exc))

    def test_os_error_errno_5(self):
        """OSError с другим errno → False."""
        exc = OSError(5, "Access is denied")
        self.assertFalse(is_file_locked_error(exc))

    def test_windows_being_used(self):
        """Текст «being used by another process» — типичный Windows-маркер."""
        exc = OSError(0, "The process cannot access the file "
                       "because it is being used by another process")
        self.assertTrue(is_file_locked_error(exc))

    def test_russian_text(self):
        """Русский текст «Отказано в доступе» — маркер для русской локали."""
        exc = OSError(0, "[Errno 13] Отказано в доступе")
        self.assertTrue(is_file_locked_error(exc))

    def test_value_error(self):
        """ValueError — не имеет отношения к блокировке файла."""
        exc = ValueError("something")
        self.assertFalse(is_file_locked_error(exc))

    def test_unrelated_text(self):
        """Обычный текст без маркеров занятости → False."""
        exc = OSError(0, "File not found")
        self.assertFalse(is_file_locked_error(exc))

    def test_substring_permission_denied(self):
        """Подстрока «permission denied» в нижнем регистре."""
        exc = OSError(0, "some error: permission denied by os")
        self.assertTrue(is_file_locked_error(exc))


if __name__ == "__main__":
    unittest.main()
