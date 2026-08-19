#!/usr/bin/env python3
"""Минимальный тест — только папка ЗАПРАВКА."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import logging
logging.basicConfig(level=logging.WARNING)
import db, disk_api

token = db.get_token()
api = disk_api.YaDiskAPI(token)

api_calls = [0]
def ls(path):
    api_calls[0] += 1
    return api.list_folder(path)

def scan(path, depth=0):
    items = ls(path)
    files = [i for i in items if i["type"] == "file"]
    dirs = [i for i in items if i["type"] == "dir"]
    
    sz = sum(i.get("size", 0) for i in files)
    sz_str = f", {sz/1024/1024:.1f} MB" if sz > 1024*1024 else f", {sz/1024:.0f} KB"
    prefix = "  " * depth + "└─ "
    print(f"{prefix}{os.path.basename(path.rstrip('/'))}/ ({len(files)} ф, {len(dirs)} п{sz_str})")
    
    total_f = len(files)
    total_d = 1
    max_d = depth
    
    for sd in dirs:
        sub_f, sub_d, sub_md = scan(sd["path"], depth + 1)
        total_f += sub_f
        total_d += sub_d
        max_d = max(max_d, sub_md)
    
    return total_f, total_d, max_d

print("=" * 60)
print("📂 ЗАПРАВКА")
print("=" * 60)

tf, td, md = scan("/ЗАПРАВКА")
print(f"\nИтого: {tf} файлов, {td} папок, глубина {md}, API запросов {api_calls[0]}")
