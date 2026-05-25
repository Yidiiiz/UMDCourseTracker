# UMD Course Tracker

A lightweight Windows tray app that watches UMD Testudo for open seats and fires a toast notification the moment one appears.

<p align="center">
  <a href="https://github.com/Yidiiiz/Course-Tracker/releases/latest/download/CourseTracker.exe">
    <img src="https://img.shields.io/badge/Download-CourseTracker.exe-brightgreen?style=for-the-badge&logo=windows" alt="Download"/>
  </a>
</p>

---

## Installation

1. Download **CourseTracker.exe** above
2. Run it — no installation needed
3. A tray icon appears in the bottom-right corner of your taskbar

Data files (`courses.json`, `settings.json`) are stored in `%APPDATA%\UMD Course Tracker\` and never written next to the exe.

---

## Usage

**Left-click** the tray icon to open the panel.

- **Add a course** — enter the course ID (e.g. `CMSC351`), an optional section number, pick the semester, and click **+ Add Course**
- **Remove a course** — hover a card and click the **×** that appears
- **Open Testudo** — click anywhere on a course card to open the search page in your browser
- **Tray icon colour** — 🟢 green = seats open · 🔴 red = all full · 🟡 yellow = checking / error

### Advanced settings

Expand the **Advanced** section at the bottom of the panel:

| Setting | Default |
|---|---|
| Poll interval | 60 s (min 30 s) |
| Notify when a section closes | Off |
| Open on Windows startup | On |
| Theme | System (follows Windows dark/light mode) |

---

## Requirements

- Windows 10 or 11
- A UMD student account is **not** required — Testudo's seat data is public

---

## Term codes

| Code | Semester |
|---|---|
| `202501` | Spring 2025 |
| `202508` | Summer 2025 |
| `202512` | Winter 2026 |
| `202601` | Spring 2026 |
| `202608` | Fall 2026 |

The app picks the most likely upcoming semester by default.

---

## Building from source

```bat
git clone https://github.com/Yidiiiz/Course-Tracker.git
cd "Course Tracker"
setup.bat      # install dependencies
build.bat      # produces dist\CourseTracker.exe
```

**Dependencies:** `requests`, `beautifulsoup4`, `pystray`, `Pillow`, `plyer`, `pyinstaller`
