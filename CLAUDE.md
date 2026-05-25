# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bat
# Install dependencies
setup.bat

# Run from source
python tracker.py

# Build standalone .exe (outputs dist\CourseTracker.exe)
build.bat
```

`build.bat` uses `python -m PyInstaller` (not bare `pyinstaller`) to avoid PATH issues with `Scripts\`.

## Architecture

Everything lives in a single file: `tracker.py` (~1600 lines). Two main classes:

### `Tracker`
- Loads/saves `courses.json` and `settings.json` from `DATA_DIR`
  - **Frozen (exe):** `%APPDATA%\UMD Course Tracker\` — nothing is written next to the executable
  - **From source:** same directory as `tracker.py`
- Runs a background polling thread that hits `https://app.testudo.umd.edu/soc/{termId}/sections?courseIds={courseId}` every N seconds
- Parses HTML with BeautifulSoup: looks for `div.section`, reads `.open-seats-count`, checks for `open` CSS class
- State diff: remembers prior open/closed state per section; closed→open fires a Windows toast via `plyer`
- Manages Windows startup registry key under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
- Communicates with the GUI via `queue.Queue` (results posted, GUI polls with `win.after`)

### `Popup`
- Borderless `Toplevel` with `overrideredirect(True)`, `attributes("-toolwindow", True)`
- DPI awareness: `SetProcessDpiAwareness(2)` + `tk.call("tk", "scaling", dpi/72)` — must run before any tkinter import
- Win11 rounded corners: `DwmSetWindowAttribute(hwnd, 33, DWMWCP_ROUND=2, 4)`
- Layout: header → scrollable canvas (course cards) → optional Advanced panel → footer
- Uses **pack** throughout; course cards live inside a `tk.Canvas` + `tk.Frame` for virtual scrolling
- Fixed width (`PANEL_W = 480`), auto height only — no user resize

### Key constants (top of tracker.py)
| Constant | Value | Purpose |
|---|---|---|
| `PANEL_W` | 480 | Fixed window width |
| `CARD_H` | 80 | Height of one course card |
| `MAX_VIS` | 3 | Max visible courses before scrollbar appears |
| `RIGHT_W` | 185 | Width of status column in each card |
| `HDR_H` | 44 | Header height |

### Height management (important — fragile area)
The window height is **fixed programmatically** — there is no user resize. Key rules:
- `_auto_size()` fires only when `min(n, MAX_VIS)` changes (tracked via `self._vis`); it is a no-op on status-only updates
- `_natural_height()` returns `self._outer.winfo_reqheight() + 2`
- `ThinScrollbar` canvas must be created with `height=1` — without it, the platform default height inflates the grid row when the scrollbar first appears, causing the window to visually expand after the first status poll
- `_rebuild_cards()` always re-enforces `geometry(WxH+x+y)` after `_auto_size()` to prevent scrollbar show/hide from transiently expanding the window
- When Advanced panel is open, `_auto_size()` is a no-op; `_fit_to_content()` handles geometry on close and resets `self._vis = 0` so `_auto_size()` recalculates correctly

### Tray icon
`TrayIcon(pystray.Icon)` subclass overrides `__call__` for reliable left-click behavior on Windows.

## Data files

All user data lives in `DATA_DIR` (see Architecture above). When the exe is
double-clicked, that is `%APPDATA%\UMD Course Tracker\`; nothing is created
next to the executable.

| File | Created by | Purpose |
|---|---|---|
| `courses.json` | App on first run | List of courses to track |
| `settings.json` | App on first settings change | Poll interval, notification prefs, window position, theme |
| `icon.ico` | App on every launch (`save_ico()`) | Notification icon; auto-generated at startup |

### courses.json schema
```json
[
  { "courseId": "CMSC351", "termId": "202608", "sectionId": "", "label": "Algorithms" }
]
```
- `sectionId`: `""` tracks all sections; `"0101"` tracks only that section
- `termId`: `YYYYSS` where SS = `01` Spring, `05` Summer, `08` Fall, `12` Winter (Winter year = display_year − 1)

## PyInstaller notes
- Entry point: `tracker.py`
- Bundle: `--onefile --windowed --icon=icon.ico` (no `--add-data` needed; icon is generated at runtime into `%APPDATA%`)
- Output: `dist\CourseTracker.exe`
- On first run the exe creates `%APPDATA%\UMD Course Tracker\courses.json`
