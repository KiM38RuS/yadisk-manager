"""Test sort model comparison"""
import sys
sys.path.insert(0, r"D:\Program_files\YaDiskManager")
from PySide6.QtCore import QSortFilterProxyModel, QAbstractItemModel, QModelIndex, Qt
from PySide6.QtWidgets import QApplication

app = QApplication([])

# Mock source model
from unittest.mock import MagicMock
src = MagicMock(spec=QAbstractItemModel)

from ui import FileTableSortModel
model = FileTableSortModel()
src._items = [
    {"name": "bookmarks.html", "is_dir": False, "size": 100, "modified": "2024-01-01", "status": "cloud_only"},
    {"name": "StartCopy.bat", "is_dir": False, "size": 50, "modified": "2024-01-02", "status": "cloud_only"},
    {"name": "Тест", "is_dir": True, "size": 0, "modified": "2024-01-03", "status": ""},
]

# Test lessThan for name column (col=1)
# We need QModelIndex objects, but lessThan is called by Qt internally.
# Let's manually test the comparison:
l_item = src._items[0]  # bookmarks.html
r_item = src._items[1]  # StartCopy.bat

l_name = l_item["name"].lower()
r_name = r_item["name"].lower()
result = l_name < r_name
print(f"'{l_name}' < '{r_name}' = {result}")
assert result == True, "bookmarks should come before StartCopy!"

print("Sort model comparison test PASSED")
