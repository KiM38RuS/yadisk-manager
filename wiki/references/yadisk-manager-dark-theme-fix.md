# YaDisk Manager — Redesigned Themes (session 2026-07-03, v0.5 → v0.6)

## Problems Found & Fixed

### 1. Startup Freeze (0xC0000409 crash + "Not Responding")

**Root cause:** `app.setStyleSheet(DARK_QSS)` was called synchronously in `_apply_theme()` during `__init__`. On old CPUs (Xeon E5440) parsing + applying QSS to the entire widget tree blocks the main thread for 3–8 seconds, triggering Windows "Not Responding".

**Fix:** Deferred QSS via `QTimer.singleShot(0, apply_qss)` in `_apply_theme()`. QPalette is applied immediately (fast), QSS is applied on the next event-loop iteration after the window is shown. User sees the window instantly.

```python
# In _apply_theme():
def apply_qss():
    if theme == "light":
        app.setStyleSheet(self._light_qss())
    elif theme == "dark" or self._is_windows_dark_mode():
        app.setStyleSheet(self._dark_qss())

# ... set palette immediately ...
QTimer.singleShot(0, apply_qss)  # defer QSS to after window show
```

### 2. Modern Dark Theme (Fluent Design / VS Code inspired)

Replaced flat `#2D2D2D`/`#383838` palette with deeper, more modern colors:

| Role | Old | New |
|------|-----|-----|
| Window bg | `#2D2D2D` | `#1E1E1E` |
| Panel/surface | `#383838` | `#252526` |
| Widget bg | `#383838` | `#2D2D30` |
| Alternate row | `#424242` | `#2A2A2E` |
| Hover | — | `#3E3E42` |
| Text | `#E0E0E0` | `#CCCCCC` |
| Accent | `#0078D7` | `#0078D4` |
| Selection bg | `#0078D7` | `#094771` |
| Status bar | `#2D2D2D`/`#B0B0B0` | `#007ACC`/`#FFFFFF` |
| Borders | `#555` | `#3E3E42` |
| Border radius | — | 4–6px |
| Scrollbars | 12px/`#555` | 10px/`#424242` rounded |

### 3. Added Light Theme QSS (`_light_qss()`)

Previously light theme used only QPalette (no QSS), causing:
- Broken status indicators (no foreground styling)
- Inconsistent widget rendering
- No visual polish

New `_light_qss()` applies a clean modern light theme:
- Background: `#F5F5F5`, surfaces: `#FFFFFF`
- Accent: `#0066FF` with `#E8F0FE` selection
- Thin `#E0E0E0` borders, subtle `#D0D0D0` hover states
- Same widget coverage as dark QSS

### 4. Theme-Aware Status Colors

Added `STATUS_COLOR_LIGHT` dict with adjusted colors for light backgrounds:

| Status | Dark theme | Light theme |
|--------|-----------|-------------|
| cloud_only | `#999` (disabled palette) | `#999` |
| downloaded | `#2a2` (bright green) | `#1B7A1B` (dark green) |
| modified | `#e80` (orange) | `#C65300` (dark orange) |

### 5. GC Crash Fix for _DiskInfoThread

- Renamed `finished = Signal(dict, str)` → `disk_info_ready = Signal(dict, str)` to avoid clashing with built-in `QThread.finished()`
- Added `thread._self_ref = thread` to prevent GC collection while QThread is still running

## Current Color Schemes

### Dark (Fluent/VS Code)
```
Main bg:    #1E1E1E
Panels:     #252526
Widgets:    #2D2D30
Hover:      #3E3E42
Text:       #CCCCCC
Accent:     #0078D4
Selection:  #094771
StatusBar:  #007ACC
```

### Light (Modern Clean)
```
Main bg:    #F5F5F5
Panels:     #FFFFFF
Widgets:    #FFFFFF
Hover:      #F0F0F0
Text:       #1A1A1A
Sec text:   #666666
Accent:     #0066FF
Selection:  #E8F0FE
StatusBar:  #0066FF
Borders:    #E0E0E0
```

## Files Changed
- `ui.py` — `_dark_qss()`, `_light_qss()`, `_apply_theme()`, `data()`, `STATUS_COLOR_LIGHT`
- `main.py` — `_DiskInfoThread` (signal rename + GC guard)
