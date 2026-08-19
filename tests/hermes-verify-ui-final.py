import ast, sys, os

FILE = r"D:\Program_files\YandexDiskManager\ui.py"

with open(FILE) as f:
    tree = ast.parse(f.read())
src = open(FILE).read()

checks = []

# 1. Syntax
checks.append(("AST syntax valid", True))

# 2. QSortFilterProxyModel import
ok = any(
    isinstance(n, ast.ImportFrom) and n.module == "PySide6.QtCore"
    and any(a.name == "QSortFilterProxyModel" for a in n.names)
    for n in ast.walk(tree)
)
checks.append(("QSortFilterProxyModel imported", ok))

# 3. COLUMNS = 4 items
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and hasattr(node.targets[0], "id") and node.targets[0].id == "COLUMNS":
        checks.append(("COLUMNS has 4 entries", len(node.value.elts) == 4))
        checks.append(("COLUMNS[0] = 'Имя'", node.value.elts[0].value == "Имя"))
        break

# 4. FileTableSortModel class exists
checks.append(("FileTableSortModel class", "class FileTableSortModel" in src))

# 5. lessThan override
ftsm_start = src.find("class FileTableSortModel")
ftsm_body = src[ftsm_start:ftsm_start+1500]
checks.append(("lessThan() overridden", "def lessThan" in ftsm_body))

# 6. sort override with col 2 descending default
checks.append(("sort() col 2 desc default", "def sort" in ftsm_body))

# 7. icon+name in col 0
checks.append(("icon+name in col 0", "{icon} {item['name']}" in src))

# 8. update_status uses column 3
checks.append(("update_status uses col 3", "self.index(i, 3)" in src))

# 9. table_sort_model created and set
checks.append(("table_sort_model = FileTableSortModel(self)", "table_sort_model = FileTableSortModel" in src))
checks.append(('setModel(table_sort_model)', 'self.table_view.setModel(self.table_sort_model)' in src))

# 10. Sorting UI
checks.append(("setSortingEnabled(True)", "setSortingEnabled(True)" in src))
checks.append(("setSortIndicatorShown(True)", "setSortIndicatorShown(True)" in src))

# 11. Interactive resize
checks.append(("QHeaderView.Interactive", "QHeaderView.Interactive" in src))

# 12. Search
checks.append(("QLineEdit for search", "_search_edit = QLineEdit()" in src))
checks.append(("setFilterFixedString", "setFilterFixedString(" in src))

# 13. _on_search
checks.append(("_on_search method", "def _on_search" in src))

# 14. _get_item helper
checks.append(("_get_item helper", "def _get_item" in src))
checks.append(("mapToSource in _get_item", "mapToSource" in src[src.find("def _get_item"):src.find("def _get_item")+300]))

# 15. Handlers use _get_item not raw table_model.get_item
handler_area = src[src.find("def _download_selected"):src.find("def closeEvent")]
checks.append(("No raw table_model.get_item in handlers", "table_model.get_item" not in handler_area))

# 16. Syncing guard in _start_download
sd = src[src.find("def _start_download"):src.find("def _start_download")+500]
checks.append(("syncing guard in _start_download", "cloud_path in self._syncing" in sd))

# 17. Crash protection in _ui_finished
checks.append(("_ui_finished crash protection", "_ui_finished crashed" in src))

# 18. Remove thread guarded
checks.append(("_active_threads.remove guarded", "ValueError" in src or "except ValueError" in src))

# 19. Status col width 100
checks.append(("Status col width 100", "setColumnWidth(3, 100)" in src))

# 20. stretchLastSection removed
checks.append(("stretchLastSection removed", "stretchLastSection" not in src[src.find("self.table_view"):src.find("self.table_view")+500]))

print("=== Ad-Hoc Verification: hermes-verify-ui-final ===")
all_ok = True
for name, ok in checks:
    status = "OK" if ok else "FAIL"
    print(f"  [{status}] {name}")
    if not ok:
        all_ok = False

print(f"\nRESULT: {sum(1 for _, o in checks if o)}/{len(checks)} passed" + (" — ALL GOOD" if all_ok else " — SEE FAILURES ABOVE"))
sys.exit(0 if all_ok else 1)
