
import os, sys
desktop = os.path.join(os.path.expanduser("~"), "Desktop")

variants = {
    "YDM_test_A_sort_fix.lnk": (r"D:\Program_files\YaDiskManager\bookmarks_tests\A_sort_fix_only\Запуск.bat", "Гипотеза A: только deferred sort fix"),
    "YDM_test_B_search_clear.lnk": (r"D:\Program_files\YaDiskManager\bookmarks_tests\B_search_clear_all\Запуск.bat", "Гипотеза B: очистка поиска во всех путях"),
    "YDM_test_C_logging.lnk": (r"D:\Program_files\YaDiskManager\bookmarks_tests\C_logging\Запуск.bat", "Гипотеза C: логирование F2 и загрузки"),
}

import win32com.client  # comes with PySide6/pywin32
shell = win32com.client.Dispatch("WScript.Shell")

for fname, (target, desc) in variants.items():
    lnk_path = os.path.join(desktop, fname)
    shortcut = shell.CreateShortCut(lnk_path)
    shortcut.TargetPath = target
    shortcut.Description = desc
    shortcut.WorkingDirectory = os.path.dirname(target)
    shortcut.Save()
    print(f"Created: {lnk_path} -> {target}")
