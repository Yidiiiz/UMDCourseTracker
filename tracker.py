"""
UMD Testudo Course Seat Tracker
Windows system-tray app that watches UMD course seat availability
and fires a Windows toast the moment a seat opens.
"""

# ── DPI awareness — must run before any GUI import ──────────────────────────
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ── Standard library ─────────────────────────────────────────────────────────
import datetime
import json
import logging
import os
import queue
import sys
import threading
import time
import webbrowser
import winreg
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

# ── GUI ───────────────────────────────────────────────────────────────────────
import tkinter as tk
from tkinter import messagebox, ttk

# ── Third-party ───────────────────────────────────────────────────────────────
import pystray
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw
from plyer import notification


# =============================================================================
# Paths & logging
# =============================================================================
# When frozen (exe): keep user data in %APPDATA%\UMD Course Tracker so
# nothing is written next to the executable.  When running from source:
# use the script directory as before.
if getattr(sys, "frozen", False):
    DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "UMD Course Tracker"
else:
    DATA_DIR = Path(__file__).parent

DATA_DIR.mkdir(parents=True, exist_ok=True)

COURSES_FILE  = DATA_DIR / "courses.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
ICON_FILE     = DATA_DIR / "icon.ico"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================
SEARCH_URL_BASE  = "https://app.testudo.umd.edu/soc/search"
USER_AGENT       = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_SETTINGS = {"interval": 60, "notify_on_close": False, "theme": "system"}
DEFAULT_COURSES: list = []

PANEL_W   = 480   # default panel width (px)
CARD_H    = 80    # height of one course card (px)
MAX_VIS   = 3     # max courses shown without scroll during auto-resize
RIGHT_W   = 185   # fixed width of the right status column in each card (px)
HDR_H     = 44    # fixed header height (px) — keeps buttons properly centred
MIN_W     = 320   # minimum resizable width
MIN_H     = 220   # minimum resizable height


# =============================================================================
# Theme system
# =============================================================================

def _blend(fg: str, bg: str, alpha: float) -> str:
    """Alpha-composite fg over bg at the given opacity."""
    r,  g,  b   = int(fg[1:3], 16), int(fg[3:5], 16), int(fg[5:7], 16)
    br, bg_, bb = int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16)
    return "#{:02x}{:02x}{:02x}".format(
        int(r  * alpha + br  * (1 - alpha)),
        int(g  * alpha + bg_ * (1 - alpha)),
        int(b  * alpha + bb  * (1 - alpha)),
    )


_DARK_PALETTE: dict[str, str] = {
    "BG":          "#111111",
    "SURFACE":     "#1c1c1c",
    "INPUT":       "#212121",
    "BORDER":      "#333333",
    "ACCENT":      "#2e2e2e",
    "ACCENT_H":    "#383838",
    "TEXT":        "#ececec",
    "SUB":         "#6a6a6a",
    "PLACEHOLDER": "#383838",
    "DIVIDER":     "#1f1f1f",
    "SUCCESS":     "#22c55e",
    "DANGER":      "#ef4444",
    "WARN":        "#f59e0b",
    "DANGER_SUB":  "#686868",
    "CARD_BG":     "#1c1c1c",
    "CARD_HOV":    "#212121",
    "DOT_BACK":    "#141414",
    "SCROLLTHUMB": "#505050",
}

_LIGHT_PALETTE: dict[str, str] = {
    "BG":          "#f0f0f0",
    "SURFACE":     "#ffffff",
    "INPUT":       "#e8e8e8",
    "BORDER":      "#c0c0c0",
    "ACCENT":      "#e0e0e0",
    "ACCENT_H":    "#d0d0d0",
    "TEXT":        "#1a1a1a",
    "SUB":         "#888888",
    "PLACEHOLDER": "#c0c0c0",
    "DIVIDER":     "#dcdcdc",
    "SUCCESS":     "#16a34a",
    "DANGER":      "#dc2626",
    "WARN":        "#d97706",
    "DANGER_SUB":  "#aaaaaa",
    "CARD_BG":     "#ffffff",
    "CARD_HOV":    "#f5f5f5",
    "DOT_BACK":    "#e4e4e4",
    "SCROLLTHUMB": "#aaaaaa",
}


def _system_prefers_dark() -> bool:
    """Return True when Windows is set to dark mode (or detection fails)."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as k:
            val, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return val == 0   # 0 → dark,  1 → light
    except Exception:
        return True


def _effective_theme(setting: str) -> str:
    """Resolve 'system' / 'dark' / 'light' → 'dark' or 'light'."""
    if setting == "light":
        return "light"
    if setting == "dark":
        return "dark"
    return "dark" if _system_prefers_dark() else "light"


def apply_theme(name: str) -> None:
    """Apply a colour palette globally.  name: 'dark' or 'light'."""
    palette = _DARK_PALETTE if name == "dark" else _LIGHT_PALETTE
    g = globals()
    g.update(palette)
    # Recompute blended derived colours
    g["SUCCESS_G"] = _blend(g["SUCCESS"], g["SURFACE"],  0.68)
    g["DANGER_G"]  = _blend(g["DANGER"],  g["SURFACE"],  0.68)
    g["WARN_G"]    = _blend(g["WARN"],    g["SURFACE"],  0.68)
    g["SUB_G"]     = _blend(g["SUB"],     g["SURFACE"],  0.68)
    g["SUCCESS_D"] = _blend(g["SUCCESS"], g["DOT_BACK"], 0.42)
    g["DANGER_D"]  = _blend(g["DANGER"],  g["DOT_BACK"], 0.42)
    g["WARN_D"]    = _blend(g["WARN"],    g["DOT_BACK"], 0.42)


# Initialise with dark theme; Tracker.run() overrides based on user settings.
apply_theme("dark")

FONT_SM    = ("Segoe UI", 9)
FONT_BOLD  = ("Segoe UI Semibold", 10)
FONT_TITLE = ("Segoe UI Semibold", 10)
FONT_MONO  = ("Consolas", 10)
FONT_MICRO = ("Segoe UI", 8)


# =============================================================================
# Term helpers
# =============================================================================
# (display-name, term-code)  —  Winter's term year is one LESS than display year
TERM_SEASONS = [("Spring", "01"), ("Summer", "05"), ("Fall", "08"), ("Winter", "12")]
SEASON_NAMES = [name for name, _ in TERM_SEASONS]


def term_id(season: str, year: str) -> str:
    """Convert (season, display-year) → UMD termId string.

    Winter 2026 maps to 202512 (December of the preceding calendar year).
    """
    y = int(year)
    if season.lower() == "winter":
        return f"{y - 1}12"
    for name, code in TERM_SEASONS:
        if name.lower() == season.lower():
            return f"{year}{code}"
    return f"{year}01"


def term_display(tid: str) -> str:
    """'202608' → 'Fall 2026',  '202512' → 'Winter 2026'"""
    if len(tid) != 6:
        return tid
    year, code = tid[:4], tid[4:]
    if code == "12":
        return f"Winter {int(year) + 1}"
    for name, c in TERM_SEASONS:
        if c == code:
            return f"{name} {year}"
    return tid


def default_term() -> tuple[str, str]:
    """Return (season, year) for the next major semester.

    Jan–Jun  → Fall of the current year.
    Jul–Dec  → Spring of the next year.
    """
    m = datetime.date.today().month
    y = datetime.date.today().year
    return ("Fall", str(y)) if m <= 6 else ("Spring", str(y + 1))


# =============================================================================
# Data classes
# =============================================================================
@dataclass
class CourseConfig:
    courseId:  str
    termId:    str
    sectionId: str = ""

    def key(self) -> str:
        return f"{self.courseId}_{self.termId}_{self.sectionId}"

    def display_name(self) -> str:
        return f"{self.courseId} §{self.sectionId}" if self.sectionId else self.courseId

    def search_params(self) -> dict:
        flag = lambda b: "on" if b else ""
        return {
            "courseId": self.courseId, "sectionId": self.sectionId,
            "termId": self.termId, "_openSectionsOnly": "",
            "creditCompare": "", "credits": "", "courseLevelFilter": "ALL",
            "instructor": "", "_facetoface": flag(True), "_blended": flag(True),
            "_online": flag(True), "courseStartCompare": "", "courseStartHour": "",
            "courseStartMin": "", "courseStartAM": "", "courseEndHour": "",
            "courseEndMin": "", "courseEndAM": "", "teachingCenter": "ALL",
            "_classDay1": flag(True), "_classDay2": flag(True), "_classDay3": flag(True),
            "_classDay4": flag(True), "_classDay5": flag(True),
        }

    def url(self) -> str:
        return f"{SEARCH_URL_BASE}?{urlencode(self.search_params())}"


@dataclass
class SectionStatus:
    section_id: str
    open_seats: int
    is_open:    bool


@dataclass
class CourseStatus:
    config:       CourseConfig
    sections:     list[SectionStatus] = field(default_factory=list)
    error:        Optional[str]       = None
    last_checked: Optional[float]     = None

    def any_open(self) -> bool:
        return any(s.is_open and s.open_seats > 0 for s in self.sections)

    def total_open(self) -> int:
        return sum(s.open_seats for s in self.sections if s.is_open)

    def tag(self) -> str:
        """Return one of: checking | open | closed | error"""
        if self.last_checked is None:
            return "checking"
        if self.error or not self.sections:
            return "error"
        if self.any_open():
            return "open"
        return "closed"

    def status_text(self) -> str:
        t = self.tag()
        if t == "open":
            n = self.total_open()
            return f"{n} seat{'s' if n != 1 else ''} open"
        if t == "closed":
            return "Full"
        if t == "error":
            return self.error or "No data"
        return "Checking…"

    def dot_color(self) -> str:
        return {"open": SUCCESS_D, "closed": DANGER_D,
                "error": WARN_D, "checking": WARN_D}.get(self.tag(), WARN_D)

    def text_color(self) -> str:
        return {"open": SUCCESS_G, "closed": DANGER_G,
                "error": WARN_G, "checking": SUB_G}.get(self.tag(), SUB_G)


# =============================================================================
# Tray icon image
# =============================================================================
_ICON_RGB = {"green": (34, 197, 94), "red": (239, 68, 68), "yellow": (234, 179, 8)}


def make_icon_image(color: str, size: int = 64) -> Image.Image:
    """Create a tray icon that mirrors the in-app status dot.

    Draws a backing disk and a smaller coloured inner dot — identical
    in concept to the dot_canvas widgets used on each course card.
    The backing colour is taken from the current theme's DOT_BACK.
    """
    rgb  = _ICON_RGB.get(color, _ICON_RGB["yellow"])
    db   = DOT_BACK.lstrip("#")
    back = (int(db[0:2], 16), int(db[2:4], 16), int(db[4:6], 16))

    # Blend the dot colour at 0.55 opacity over the backing
    # (slightly higher than the 0.42 used in-app so it reads on the taskbar)
    dot_r = int(rgb[0] * 0.55 + back[0] * 0.45)
    dot_g = int(rgb[1] * 0.55 + back[1] * 0.45)
    dot_b = int(rgb[2] * 0.55 + back[2] * 0.45)

    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Outer dark backing circle
    pad = max(2, size // 16)
    draw.ellipse([pad, pad, size - pad, size - pad],
                 fill=back + (210,))

    # Inner coloured dot (same proportions as dot_canvas: 3/16 … 12/16 of size)
    inner = size // 4
    draw.ellipse([inner, inner, size - inner, size - inner],
                 fill=(dot_r, dot_g, dot_b, 255))

    return img


def save_ico() -> None:
    """Always regenerate the .ico so it stays in sync with the current style."""
    make_icon_image("green", 64).save(
        str(ICON_FILE), format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64)],
    )


# =============================================================================
# HTTP & scraping
# =============================================================================
_http = requests.Session()
_http.headers.update({"User-Agent": USER_AGENT})


def fetch_sections(course: CourseConfig) -> list[SectionStatus]:
    log.info("Fetching %s", course.url())
    resp = _http.get(course.url(), timeout=15)
    resp.raise_for_status()
    return parse_sections(resp.text, course.sectionId)


def parse_sections(html: str, filter_sid: str = "") -> list[SectionStatus]:
    soup    = BeautifulSoup(html, "html.parser")
    results = []
    for div in soup.select("div.section"):
        sid_el = div.select_one("span.section-id")
        sid    = sid_el.get_text(strip=True) if sid_el else ""
        if not sid:
            inp = div.find("input", {"name": "sectionId"})
            sid = inp["value"].strip() if inp else ""
        if filter_sid and sid.upper() != filter_sid.upper():
            continue
        cnt = div.select_one("span.open-seats-count")
        try:
            seats = int(cnt.get_text(strip=True)) if cnt else 0
        except ValueError:
            seats = 0
        span     = div.select_one("span.open-seats")
        has_open = span is not None and "has-open-seats" in (span.get("class") or [])
        results.append(SectionStatus(sid, seats, seats > 0 and has_open))
    return results


# =============================================================================
# Settings & courses I/O
# =============================================================================
def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with SETTINGS_FILE.open(encoding="utf-8") as f:
                return {**DEFAULT_SETTINGS, **json.load(f)}
        except Exception as exc:
            log.warning("Could not load settings: %s", exc)
    return dict(DEFAULT_SETTINGS)


def save_settings(data: dict) -> None:
    with SETTINGS_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_courses() -> list[CourseConfig]:
    if not COURSES_FILE.exists():
        with COURSES_FILE.open("w", encoding="utf-8") as f:
            json.dump(DEFAULT_COURSES, f, indent=2)
    try:
        with COURSES_FILE.open(encoding="utf-8") as f:
            raw = json.load(f)
        return [
            CourseConfig(
                courseId  = r.get("courseId",  "").strip().upper(),
                termId    = r.get("termId",    "").strip(),
                sectionId = r.get("sectionId", "").strip(),
            )
            for r in raw
            if r.get("courseId") and r.get("termId")
        ]
    except Exception as exc:
        log.error("Could not load courses: %s", exc)
        return []


def save_courses(courses: list[CourseConfig]) -> None:
    with COURSES_FILE.open("w", encoding="utf-8") as f:
        json.dump(
            [{"courseId": c.courseId, "termId": c.termId, "sectionId": c.sectionId}
             for c in courses],
            f, indent=2,
        )


# =============================================================================
# Toast notifications
# =============================================================================
def toast(title: str, msg: str, url: str = "") -> None:
    try:
        notification.notify(
            title=title, message=msg, app_name="UMD Course Tracker",
            app_icon=str(ICON_FILE) if ICON_FILE.exists() else None,
            timeout=8,
        )
    except Exception as exc:
        log.warning("Toast error: %s", exc)
    if url:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()


# =============================================================================
# Windows startup registry helpers
# =============================================================================
_STARTUP_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_REG_NAME = "UMD Course Tracker"


def _startup_cmd() -> str:
    """Command string to store in the registry."""
    if getattr(sys, "frozen", False):        # PyInstaller / cx_Freeze bundle
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{Path(__file__).resolve()}"'


def get_startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_PATH) as k:
            winreg.QueryValueEx(k, _STARTUP_REG_NAME)
            return True
    except (FileNotFoundError, OSError):
        return False


def set_startup_enabled(enabled: bool) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_PATH,
                            0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, _STARTUP_REG_NAME, 0,
                                  winreg.REG_SZ, _startup_cmd())
            else:
                try:
                    winreg.DeleteValue(k, _STARTUP_REG_NAME)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        log.warning("Could not update startup registry: %s", exc)


# =============================================================================
# Small UI helpers
# =============================================================================
def set_bg_recursive(widget: tk.Widget, color: str) -> None:
    """Set background colour on widget and all its descendants."""
    try:
        widget.config(bg=color)
    except tk.TclError:
        pass
    for child in widget.winfo_children():
        set_bg_recursive(child, color)


def bind_tree(widget: tk.Widget, event: str, handler) -> None:
    """Bind an event handler to widget and every descendant."""
    widget.bind(event, handler)
    for child in widget.winfo_children():
        bind_tree(child, event, handler)


def styled_entry(parent: tk.Widget, width: int = 10, font=FONT_MONO) -> tk.Entry:
    return tk.Entry(
        parent, width=width, font=font,
        bg=INPUT, fg=TEXT, insertbackground=TEXT,
        relief="flat", bd=0,
        highlightthickness=1, highlightbackground="#292929", highlightcolor=BORDER,
    )


def add_placeholder(entry: tk.Entry, text: str) -> None:
    """Show very subtle hint text that vanishes on focus and returns when empty."""
    entry._placeholder = text  # type: ignore[attr-defined]
    entry._has_placeholder = True  # type: ignore[attr-defined]
    entry.insert(0, text)
    entry.config(fg=PLACEHOLDER)

    def on_focus_in(_):
        if entry._has_placeholder:  # type: ignore[attr-defined]
            entry.delete(0, "end")
            entry.config(fg=TEXT)
            entry._has_placeholder = False  # type: ignore[attr-defined]

    def on_focus_out(_):
        if not entry.get():
            entry.insert(0, entry._placeholder)  # type: ignore[attr-defined]
            entry.config(fg=PLACEHOLDER)
            entry._has_placeholder = True  # type: ignore[attr-defined]

    entry.bind("<FocusIn>",  on_focus_in)
    entry.bind("<FocusOut>", on_focus_out)


def form_label(parent: tk.Widget, text: str) -> tk.Label:
    return tk.Label(parent, text=text, font=FONT_SM, bg=BG, fg=SUB)


def section_divider(parent: tk.Widget, text: str) -> None:
    """Small-caps section title with a horizontal rule."""
    row = tk.Frame(parent, bg=BG)
    row.pack(fill="x", pady=(10, 4))
    tk.Label(row, text=text.upper(), font=("Segoe UI", 7, "bold"),
             bg=BG, fg=SUB).pack(side="left")
    tk.Frame(row, bg=DIVIDER, height=1).pack(
        side="left", fill="x", expand=True, padx=(8, 0), pady=4)


def dot_canvas(parent: tk.Widget, bg_color: str) -> tuple[tk.Canvas, int]:
    """16×16 canvas with a dark backing ring and a coloured status dot."""
    c   = tk.Canvas(parent, width=16, height=16, bg=bg_color,
                    highlightthickness=0, bd=0)
    c.create_oval(0, 0, 15, 15, fill=DOT_BACK, outline="")
    oid = c.create_oval(3, 3, 12, 12, fill=WARN_D, outline="")
    return c, oid


def full_width_button(parent: tk.Widget, text: str, command=None) -> tk.Frame:
    """Full-width subtle button — quiet at rest, lights up on hover."""
    outer = tk.Frame(parent, bg=ACCENT, cursor="hand2")
    outer.pack(fill="x")
    inner = tk.Frame(outer, bg=SURFACE, cursor="hand2")
    inner.pack(fill="x", padx=1, pady=1)
    lbl   = tk.Label(inner, text=text, bg=SURFACE, fg=SUB,
                     font=FONT_SM, pady=5, cursor="hand2")
    lbl.pack(fill="x")

    def on_enter(_): inner.config(bg=ACCENT_H); lbl.config(bg=ACCENT_H, fg=TEXT)
    def on_leave(_): inner.config(bg=SURFACE);  lbl.config(bg=SURFACE,  fg=SUB)
    def on_click(_):
        if command:
            command()

    for w in (outer, inner, lbl):
        w.bind("<Enter>",    on_enter)
        w.bind("<Leave>",    on_leave)
        w.bind("<Button-1>", on_click)
    return outer


# =============================================================================
# Slim Canvas scrollbar (theme-matched)
# =============================================================================
class ThinScrollbar:
    """Minimal dark scrollbar drawn on a Canvas."""

    def __init__(self, parent: tk.Widget, command) -> None:
        self._command  = command
        self._lo       = 0.0
        self._hi       = 1.0
        self._dragging = False
        self._drag_y   = 0
        self._drag_lo  = 0.0

        self.canvas = tk.Canvas(parent, width=6, height=1, bg=BG,
                                highlightthickness=0, bd=0)
        self._thumb = self.canvas.create_rectangle(1, 0, 5, 0,
                                                   fill=SCROLLTHUMB, outline="")
        self.canvas.bind("<ButtonPress-1>",   self._on_press)
        self.canvas.bind("<B1-Motion>",       self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Configure>",       lambda _: self._redraw())

    def set(self, lo: str, hi: str) -> None:
        self._lo, self._hi = float(lo), float(hi)
        self._redraw()

    def _redraw(self) -> None:
        H = self.canvas.winfo_height()
        if H < 4:
            # Canvas not sized yet — retry after a tick
            self.canvas.after(50, self._redraw)
            return
        y0 = max(2,     int(self._lo * H))
        y1 = min(H - 2, max(y0 + 12, int(self._hi * H)))
        self.canvas.coords(self._thumb, 1, y0, 5, y1)

    def _on_press(self, e: tk.Event) -> None:
        H    = self.canvas.winfo_height()
        frac = e.y / H if H else 0.0
        if not (self._lo <= frac <= self._hi):
            offset = (self._hi - self._lo) / 2
            self._command("moveto", str(max(0.0, frac - offset)))
        self._dragging = True
        self._drag_y   = e.y
        self._drag_lo  = self._lo

    def _on_drag(self, e: tk.Event) -> None:
        if not self._dragging or not self.canvas.winfo_height():
            return
        delta = (e.y - self._drag_y) / self.canvas.winfo_height()
        self._command("moveto", str(max(0.0, self._drag_lo + delta)))

    def _on_release(self, _: tk.Event) -> None:
        self._dragging = False

    def grid(self, **kw) -> None:
        self.canvas.grid(**kw)

    def grid_remove(self) -> None:
        self.canvas.grid_remove()


# =============================================================================
# pystray subclass — reliable left-click
# =============================================================================
class TrayIcon(pystray.Icon):
    def __init__(self, *args, on_left_click=None, **kwargs):
        self._left_click = on_left_click
        super().__init__(*args, **kwargs)

    def __call__(self) -> None:
        log.info("Tray left-click")
        if self._left_click:
            self._left_click()
        else:
            super().__call__()


# =============================================================================
# Popup panel
# =============================================================================
class Popup:
    """Borderless floating panel shown when the tray icon is left-clicked."""

    _instance: Optional["Popup"] = None

    @classmethod
    def toggle(cls, tracker: "Tracker", root: tk.Tk) -> None:
        if cls._instance and cls._instance._alive:
            cls._instance.close()
        else:
            cls._instance = cls(tracker, root)

    # -------------------------------------------------------------------------
    def __init__(self, tracker: "Tracker", root: tk.Tk) -> None:
        self.tracker = tracker

        self._alive       = True
        self._job: Optional[str] = None

        # Course card widgets & in-place update references
        self._card_map:  dict[str, tk.Frame] = {}
        self._card_refs: dict[str, dict]     = {}

        # Structural change tracking (avoids full rebuilds on every poll tick)
        self._last_keys:      list[str]      = []
        self._last_open_keys: frozenset[str] = frozenset()

        # Move flag
        self._user_moved = False
        self._vis: int = 0   # last visible-course count used by _auto_size

        # Misc UI state
        self._adv_open = False
        self._quitting = False
        self._placed   = False   # True after _place_window; gates _resize_height

        self._build(root)

    # =========================================================================
    # Layout construction
    # =========================================================================
    def _build(self, root: tk.Tk) -> None:
        self._root = root          # kept so _reopen_with_new_theme can pass it back
        shell = tk.Toplevel(root)
        self.win = shell
        shell.withdraw()                        # hidden until _place_window positions it
        shell.overrideredirect(True)
        shell.attributes("-topmost", True)
        shell.attributes("-toolwindow", True)
        shell.configure(bg=BORDER)
        shell.minsize(MIN_W, MIN_H)

        # 1-px border provided by BORDER background + 1-px padx/pady
        outer = tk.Frame(shell, bg=BG)
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        self._outer = outer

        self._build_header(outer, shell)
        tk.Frame(outer, bg=DIVIDER, height=1).pack(fill="x")

        body = tk.Frame(outer, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=(4, 0))

        self._build_course_list(body)
        self._build_add_form(body)
        self._build_advanced(body)

        tk.Frame(outer, bg=DIVIDER, height=1).pack(fill="x")
        self._build_footer(outer)

        shell.bind("<Escape>", lambda _: self.close())
        shell.after(80, shell.focus_force)

        self._rebuild_cards()
        self._place_window(shell)
        self._refresh()

    # ── Header ────────────────────────────────────────────────────────────────
    def _build_header(self, outer: tk.Frame, shell: tk.Toplevel) -> None:
        hdr = tk.Frame(outer, bg=SURFACE, height=HDR_H)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)   # fixed height so place() children centre correctly

        # Status dot — vertically centred via place
        self._hdr_dot, self._hdr_dot_id = dot_canvas(hdr, SURFACE)
        self._hdr_dot.place(relx=0, rely=0.5, x=14, anchor="w")

        # Title — vertically centred
        title_lbl = tk.Label(hdr, text="UMD Course Tracker",
                             font=FONT_TITLE, bg=SURFACE, fg=TEXT)
        title_lbl.place(relx=0, rely=0.5, x=38, anchor="w")

        # × Quit — right edge, vertically centred
        x_btn = tk.Label(hdr, text="×", font=("Segoe UI", 15),
                         bg=SURFACE, fg=SUB, cursor="hand2", width=2)
        x_btn.place(relx=1, rely=0.5, x=-6, anchor="e")
        x_btn.bind("<Enter>",    lambda _: x_btn.config(fg=DANGER))
        x_btn.bind("<Leave>",    lambda _: x_btn.config(fg=SUB))
        x_btn.bind("<Button-1>", lambda _: self.quit())

        # — Minimize — spaced away from ×, vertically centred
        min_btn = tk.Label(hdr, text="—", font=("Segoe UI Semibold", 10),
                           bg=SURFACE, fg=SUB, cursor="hand2")
        min_btn.place(relx=1, rely=0.5, x=-52, anchor="e")
        min_btn.bind("<Enter>",    lambda _: min_btn.config(fg=TEXT))
        min_btn.bind("<Leave>",    lambda _: min_btn.config(fg=SUB))
        min_btn.bind("<Button-1>", lambda _: self.close())

        # Drag-to-move — bind to the header frame AND every child label so the
        # title text doesn't block dragging
        shell._dx = shell._dy = 0

        def drag_start(e):
            shell._dx = e.x_root - shell.winfo_x()
            shell._dy = e.y_root - shell.winfo_y()

        def drag_move(e):
            shell.geometry(f"+{e.x_root - shell._dx}+{e.y_root - shell._dy}")
            self._user_moved = True

        for w in (hdr, title_lbl, self._hdr_dot):
            w.bind("<Button-1>",  drag_start)
            w.bind("<B1-Motion>", drag_move)

    # ── Course list ───────────────────────────────────────────────────────────
    def _build_course_list(self, body: tk.Frame) -> None:
        section_divider(body, "Courses")

        wrap = tk.Frame(body, bg=BG)
        wrap.pack(fill="x")
        wrap.columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(wrap, bg=BG, height=CARD_H,
                                  highlightthickness=0, bd=0)
        self._sb     = ThinScrollbar(wrap, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._sb.set)
        self._canvas.grid(row=0, column=0, sticky="ew")

        self._cards_frame = tk.Frame(self._canvas, bg=BG)
        self._cfw = self._canvas.create_window((0, 0), window=self._cards_frame,
                                                anchor="nw")

        # Keep scrollregion in sync with content
        self._cards_frame.bind(
            "<Configure>",
            lambda _: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")),
        )

        self._canvas.bind("<Configure>",
                          lambda e: self._canvas.itemconfig(self._cfw, width=e.width))
        self._canvas.bind(
            "<MouseWheel>",
            lambda e: self._canvas.yview_scroll(-1 * (e.delta // 120), "units"),
        )

    # ── Add-course form ───────────────────────────────────────────────────────
    def _build_add_form(self, body: tk.Frame) -> None:
        section_divider(body, "Add Course")

        # Row 1 — Course ID + Section
        r1 = tk.Frame(body, bg=BG)
        r1.pack(fill="x", pady=(0, 5))
        form_label(r1, "Course").pack(side="left")
        self._e_course = styled_entry(r1, width=9)
        self._e_course.pack(side="left", padx=(4, 12))
        add_placeholder(self._e_course, "CMSC351")
        form_label(r1, "Section").pack(side="left")
        self._e_section = styled_entry(r1, width=8)
        self._e_section.pack(side="left", padx=(4, 0))
        add_placeholder(self._e_section, "0101")

        # Row 2 — Semester + Year
        r2 = tk.Frame(body, bg=BG)
        r2.pack(fill="x", pady=(0, 5))
        form_label(r2, "Semester").pack(side="left")
        season, year = default_term()
        self._season_var = tk.StringVar(value=season)
        ttk.Combobox(
            r2, textvariable=self._season_var, values=SEASON_NAMES,
            state="readonly", width=8, style="Popup.TCombobox", font=FONT_SM,
        ).pack(side="left", padx=(5, 10))
        form_label(r2, "Year").pack(side="left", padx=(0, 4))
        self._e_year = styled_entry(r2, width=5, font=FONT_SM)
        self._e_year.config(justify="center")
        self._e_year.insert(0, year)
        self._e_year.pack(side="left")

        # Row 3 — full-width Add button
        r3 = tk.Frame(body, bg=BG)
        r3.pack(fill="x", pady=(0, 6))
        full_width_button(r3, "+ Add Course", command=self._add_course)

        for entry in (self._e_course, self._e_section, self._e_year):
            entry.bind("<Return>", lambda _: self._add_course())

    # ── Advanced settings (collapsible) ───────────────────────────────────────
    def _build_advanced(self, body: tk.Frame) -> None:
        tk.Frame(body, bg=DIVIDER, height=1).pack(fill="x", pady=(6, 0))

        toggle_row = tk.Frame(body, bg=BG, cursor="hand2", pady=4)
        toggle_row.pack(fill="x")
        self._adv_arrow = tk.Label(toggle_row, text="▸",
                                    font=("Segoe UI", 8), bg=BG, fg=SUB,
                                    cursor="hand2")
        self._adv_arrow.pack(side="left")
        tk.Label(toggle_row, text="  ADVANCED", font=("Segoe UI", 7, "bold"),
                 bg=BG, fg=SUB, cursor="hand2").pack(side="left")
        for w in (toggle_row, *toggle_row.winfo_children()):
            w.bind("<Button-1>", lambda _: self._toggle_advanced())

        # Body is packed below when the section expands
        self._adv_body = tk.Frame(body, bg=BG)

        # Poll interval row
        row = tk.Frame(self._adv_body, bg=BG)
        row.pack(fill="x", pady=(4, 4))
        form_label(row, "Poll every").pack(side="left")
        self._iv = tk.StringVar(value=str(self.tracker.settings.get("interval", 60)))
        iv_entry = styled_entry(row, width=5, font=FONT_SM)
        iv_entry.config(textvariable=self._iv)
        iv_entry.pack(side="left", padx=(5, 5))
        form_label(row, "seconds  (min 30)").pack(side="left")
        iv_entry.bind("<FocusOut>", self._save_interval)
        iv_entry.bind("<Return>",   self._save_interval)

        # Notify-on-close toggle
        self._notif_var = tk.BooleanVar(
            value=self.tracker.settings.get("notify_on_close", False))
        tk.Checkbutton(
            self._adv_body, text="Notify when a section closes",
            variable=self._notif_var, command=self._save_notify,
            bg=BG, fg=TEXT, activebackground=BG, activeforeground=TEXT,
            selectcolor=INPUT, font=FONT_SM, bd=0, highlightthickness=0,
        ).pack(anchor="w", pady=(0, 2))

        # Open on Windows startup toggle
        self._startup_var = tk.BooleanVar(value=get_startup_enabled())
        tk.Checkbutton(
            self._adv_body, text="Open on Windows startup",
            variable=self._startup_var, command=self._save_startup,
            bg=BG, fg=TEXT, activebackground=BG, activeforeground=TEXT,
            selectcolor=INPUT, font=FONT_SM, bd=0, highlightthickness=0,
        ).pack(anchor="w", pady=(0, 5))

        # Theme selector
        row_t = tk.Frame(self._adv_body, bg=BG)
        row_t.pack(fill="x", pady=(2, 6))
        form_label(row_t, "Theme").pack(side="left")
        self._theme_var = tk.StringVar(
            value=self.tracker.settings.get("theme", "system").capitalize())
        theme_cb = ttk.Combobox(
            row_t, textvariable=self._theme_var,
            values=["System", "Dark", "Light"],
            state="readonly", width=8, style="Popup.TCombobox", font=FONT_SM,
        )
        theme_cb.pack(side="left", padx=(5, 0))
        theme_cb.bind("<<ComboboxSelected>>", self._save_theme)

        # Subtle "snap to corner" link
        snap_lbl = tk.Label(self._adv_body, text="move to bottom-right",
                            font=("Segoe UI", 8), bg=BG, fg=PLACEHOLDER,
                            cursor="hand2")
        snap_lbl.pack(anchor="w", pady=(2, 10))
        snap_lbl.bind("<Enter>",    lambda _: snap_lbl.config(fg=SUB))
        snap_lbl.bind("<Leave>",    lambda _: snap_lbl.config(fg=PLACEHOLDER))
        snap_lbl.bind("<Button-1>", lambda _: self._reset_position())

    # ── Footer ────────────────────────────────────────────────────────────────
    def _build_footer(self, outer: tk.Frame) -> None:
        foot = tk.Frame(outer, bg=SURFACE, pady=6)
        foot.pack(fill="x")

        tk.Label(foot, text="© Yidi Zhao", font=("Segoe UI", 8),
                 bg=SURFACE, fg="#3a3a3a").pack(side="left", padx=14)

    # =========================================================================
    # Window placement & sizing
    # =========================================================================
    @staticmethod
    def _work_area() -> tuple[int, int, int, int]:
        """Return (left, top, right, bottom) of the usable desktop (excludes taskbar)."""
        try:
            import ctypes.wintypes as wt
            rect = wt.RECT()
            ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0)
            return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            return 0, 0, 0, 0

    def _place_window(self, shell: tk.Toplevel) -> None:
        """Position and size the window.

        On first open: bottom-right corner of the work area (above the taskbar).
        When saved state exists (popup_moved flag): restore width and position.
        Height is always auto-computed from course count (no vertical resize).
        """
        shell.update_idletasks()

        W        = PANEL_W
        n        = len(self.tracker.courses)
        per_card = CARD_H + 4           # card height + gap from pady=(0,4)
        vis      = min(max(n, 1), MAX_VIS)

        self._canvas.config(height=vis * per_card)
        shell.update_idletasks()
        H = self._natural_height()

        sw = shell.winfo_screenwidth()
        sh = shell.winfo_screenheight()

        MARGIN = 12
        _, _, wa_r, wa_b = self._work_area()
        if wa_r <= 0:                   # fallback if API call failed
            wa_r, wa_b = sw, sh

        if self.tracker._session_x is not None:
            x = max(0, min(self.tracker._session_x, sw - W))
            y = max(0, min(self.tracker._session_bottom - H, sh - H))
        else:
            x = wa_r - W - MARGIN
            y = wa_b - H - MARGIN

        shell.geometry(f"{W}x{H}+{x}+{y}")
        shell.update()          # fully paint all widgets before revealing the window
        shell.deiconify()
        shell.lift()
        self._placed = True
        self._try_round_corners(shell)
        log.info("Popup placed: %dx%d+%d+%d", W, H, x, y)

    def _natural_height(self) -> int:
        """Preferred window height based on current content."""
        self.win.update_idletasks()
        return self._outer.winfo_reqheight() + 2

    def _resize_height(self, new_h: int) -> None:
        """Change window height while keeping the bottom edge pinned in place.

        No-op before _place_window has run (window not yet on screen).
        """
        if not self._placed:
            return
        W     = self.win.winfo_width()
        x     = self.win.winfo_x()
        old_h = self.win.winfo_height()
        y     = self.win.winfo_y() + (old_h - new_h)
        self.win.geometry(f"{W}x{new_h}+{x}+{y}")

    def _fit_to_content(self) -> None:
        """Resize window height to current content without touching canvas height.

        Used by the Advanced section toggle so only the bottom of the window
        grows/shrinks — the course canvas area is left completely untouched.
        """
        self.win.update_idletasks()
        self._resize_height(self._natural_height())
        if not self._adv_open:
            self._vis = 0   # reset so _auto_size corrects canvas height if n changed while open
            self._auto_size()

    def _auto_size(self) -> None:
        """Resize window height when the visible course count changes.

        Only fires when min(n, MAX_VIS) differs from the last known value so
        status updates and sort reorders are no-ops. Skipped while the Advanced
        panel is open; _fit_to_content calls back into _auto_size on close.
        """
        if self._adv_open:
            return
        n        = len(self.tracker.courses)
        per_card = CARD_H + 4
        vis      = min(max(n, 1), MAX_VIS)
        if vis == self._vis:
            return
        self._vis = vis
        self._canvas.config(height=vis * per_card)
        self.win.update_idletasks()
        self._resize_height(self._natural_height())

    @staticmethod
    def _try_round_corners(shell: tk.Toplevel) -> None:
        try:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                shell.winfo_id(), 33, ctypes.byref(ctypes.c_int(2)), 4)
        except Exception:
            pass

    # =========================================================================
    # Advanced section
    # =========================================================================
    def _toggle_advanced(self) -> None:
        self._adv_open = not self._adv_open
        self._adv_arrow.config(text="▾" if self._adv_open else "▸")
        if self._adv_open:
            self._adv_body.pack(fill="x")
        else:
            self._adv_body.pack_forget()
        # Extend/shrink only the bottom of the window; leave canvas height alone
        self.win.after(10, self._fit_to_content)

    # =========================================================================
    # Course cards
    # =========================================================================
    def _sorted_courses(self) -> list[CourseConfig]:
        """Open courses first, then the rest in their original order."""
        open_keys = {
            c.key() for c in self.tracker.courses
            if (st := self.tracker.statuses.get(c.key())) and st.any_open()
        }
        return (
            [c for c in self.tracker.courses if c.key() in open_keys] +
            [c for c in self.tracker.courses if c.key() not in open_keys]
        )

    def _rebuild_cards(self) -> None:
        """Destroy all cards and create them fresh (called on structural changes)."""
        for w in self._cards_frame.winfo_children():
            w.destroy()
        self._card_map.clear()
        self._card_refs.clear()

        courses = self._sorted_courses()
        if not courses:
            tk.Label(self._cards_frame, text="No courses yet — add one below.",
                     font=FONT_SM, bg=BG, fg=SUB, pady=20).pack(fill="x")
        else:
            for course in courses:
                self._make_card(course, self.tracker.statuses.get(course.key()))

        self._last_keys = [c.key() for c in self.tracker.courses]
        self._last_open_keys = frozenset(
            c.key() for c in self.tracker.courses
            if (st := self.tracker.statuses.get(c.key())) and st.any_open()
        )
        self._update_scrollbar()
        self._auto_size()
        # Re-enforce window height — scrollbar show/hide can transiently inflate
        # wrap's grid-row height, which lets tkinter expand the window.
        if not self._adv_open:
            self.win.update_idletasks()
            self._resize_height(self._natural_height())

    def _update_card_statuses(self) -> None:
        """Update status labels/dots in-place (no widget destruction, no flicker).

        Guards against redundant config() calls so widgets only redraw when
        their displayed value actually changes.
        """
        for course in self._sorted_courses():
            refs = self._card_refs.get(course.key())
            st   = self.tracker.statuses.get(course.key())
            if not (refs and st):
                continue
            new_dot  = st.dot_color()
            new_text = st.status_text()
            new_fg   = st.text_color()
            if refs["dc"].itemcget(refs["oid"], "fill") != new_dot:
                refs["dc"].itemconfig(refs["oid"], fill=new_dot)
            if refs["sl"].cget("text") != new_text or refs["sl"].cget("fg") != new_fg:
                refs["sl"].config(text=new_text, fg=new_fg)

    def _update_scrollbar(self) -> None:
        """Show/hide the scrollbar and refresh its thumb position."""
        bbox      = self._canvas.bbox("all")
        content_h = bbox[3] if bbox else 0
        canvas_h  = self._canvas.winfo_height()
        if canvas_h < 4:
            canvas_h = min(len(self.tracker.courses), MAX_VIS) * CARD_H or CARD_H
        if content_h > canvas_h + 2:   # +2 tolerance for 1-px card gaps
            self._sb.grid(row=0, column=1, sticky="ns")
            # Push current scroll fraction to the thumb immediately
            lo, hi = self._canvas.yview()
            self._sb.set(str(lo), str(hi))
        else:
            self._sb.grid_remove()

    def _make_card(self, course: CourseConfig, st: Optional[CourseStatus]) -> None:
        key = course.key()

        # ── Card frame ────────────────────────────────────────────────────────
        card = tk.Frame(self._cards_frame, bg=CARD_BG, height=CARD_H, cursor="hand2")
        card.pack_propagate(False)   # lock height to prevent text-reflow glitches
        card.pack(fill="x", pady=(0, 4))
        self._card_map[key] = card

        # ── Left: course name / section / term ────────────────────────────────
        info = tk.Frame(card, bg=CARD_BG)
        info.pack(side="left", fill="both", expand=True, padx=12, pady=0)

        # Content block placed at the vertical midpoint of the info frame so
        # the text is precisely centred regardless of card height or DPI.
        content = tk.Frame(info, bg=CARD_BG)
        content.place(relx=0, rely=0.5, anchor="w")

        name_row = tk.Frame(content, bg=CARD_BG)
        name_row.pack(anchor="w")
        tk.Label(name_row, text=course.courseId, font=FONT_BOLD,
                 bg=CARD_BG, fg=TEXT).pack(side="left")
        if course.sectionId:
            tk.Label(name_row, text=f"  ·  {course.sectionId}",
                     font=FONT_SM, bg=CARD_BG, fg=SUB).pack(side="left")
        tk.Label(content, text=term_display(course.termId),
                 font=FONT_MICRO, bg=CARD_BG, fg=SUB).pack(anchor="w", pady=(2, 0))

        # ── Right: indicator row aligned with name row; × centered in card ─────
        # Fixed-width container so the left side never shifts.
        right = tk.Frame(card, bg=CARD_BG, width=RIGHT_W)
        right.pack_propagate(False)
        right.pack(side="right", fill="y", padx=(0, 8))

        # rc mirrors the left "content" block: centred at rely=0.5 with the same
        # two-row structure (indicator row + invisible spacer matching the term
        # label) so the indicator row aligns exactly with the course-name row.
        rc = tk.Frame(right, bg=CARD_BG)
        rc.place(relx=0, rely=0.5, relwidth=1, anchor="w")

        # Indicator sub-row: dot · seats — leave room on the right for the × label
        top_row = tk.Frame(rc, bg=CARD_BG)
        top_row.pack(anchor="e", padx=(0, 22))

        # Status dot
        dc, oid = dot_canvas(top_row, CARD_BG)
        dc.itemconfig(oid, fill=st.dot_color() if st else WARN_D)
        dc.pack(side="right", padx=(0, 6))

        # Seats / status text
        seats_lbl = tk.Label(
            top_row,
            text=st.status_text()  if st else "Checking…",
            font=FONT_MICRO, bg=CARD_BG,
            fg=st.text_color() if st else SUB_G,
            anchor="e",
        )
        seats_lbl.pack(side="right", padx=(4, 4))

        # Invisible spacer — same font/padding as the term label on the left so
        # rc has the same natural height as the left content block, keeping the
        # two centred blocks' first rows vertically aligned.
        tk.Label(rc, text="", font=FONT_MICRO, bg=CARD_BG).pack(anchor="e", pady=(2, 0))

        # × — placed directly on right, vertically centred in the full card height
        x_lbl = tk.Label(right, text="×", font=("Segoe UI", 12, "bold"),
                          bg=CARD_BG, fg=CARD_BG, cursor="hand2")
        x_lbl.place(relx=1, rely=0.5, x=-4, anchor="e")

        # Store refs so _update_card_statuses can patch them without a rebuild
        self._card_refs[key] = {"dc": dc, "oid": oid, "sl": seats_lbl}

        # ── Hover enter / leave logic ─────────────────────────────────────────
        # 20 ms debounce prevents spurious × flashes when moving between cards.
        leave_job = [None]

        def enter_card(_):
            if leave_job[0]:
                try:
                    card.after_cancel(leave_job[0])
                except Exception:
                    pass
                leave_job[0] = None
            set_bg_recursive(card, CARD_HOV)
            dc.config(bg=CARD_HOV)
            x_lbl.config(fg=DANGER_SUB, bg=CARD_HOV)

        def leave_card(_):
            if leave_job[0]:
                try:
                    card.after_cancel(leave_job[0])
                except Exception:
                    pass
            leave_job[0] = card.after(20, check_leave)

        def check_leave():
            leave_job[0] = None
            try:
                px = card.winfo_pointerx()
                py = card.winfo_pointery()
                rx = card.winfo_rootx()
                ry = card.winfo_rooty()
                still_inside = (rx <= px < rx + card.winfo_width() and
                                ry <= py < ry + card.winfo_height())
            except Exception:
                still_inside = False
            if not still_inside:
                set_bg_recursive(card, CARD_BG)
                dc.config(bg=CARD_BG)
                x_lbl.config(fg=CARD_BG, bg=CARD_BG)

        bind_tree(card, "<Enter>", enter_card)
        bind_tree(card, "<Leave>", leave_card)

        # ── Click handlers ────────────────────────────────────────────────────
        def on_card_click(e):
            if e.widget is x_lbl:
                # × clicked — remove this course
                self._remove_course(key)
            else:
                # Anywhere else on the card — open Testudo in browser
                webbrowser.open(course.url())

        bind_tree(card, "<Button-1>", on_card_click)

        # Scroll wheel pass-through from card children to the canvas
        bind_tree(card, "<MouseWheel>",
                  lambda e: self._canvas.yview_scroll(-1 * (e.delta // 120), "units"))

    # =========================================================================
    # Refresh loop
    # =========================================================================
    def _refresh(self) -> None:
        if not self._alive:
            return

        cur_keys = [c.key() for c in self.tracker.courses]
        cur_open = frozenset(
            c.key() for c in self.tracker.courses
            if (st := self.tracker.statuses.get(c.key())) and st.any_open()
        )

        if cur_keys != self._last_keys or cur_open != self._last_open_keys:
            self._rebuild_cards()
        else:
            self._update_card_statuses()

        self._update_header_dot()
        self._job = self.win.after(2000, self._refresh)

    def _update_header_dot(self) -> None:
        """Update the status dot in the panel header."""
        def set_dot(color: str):
            self._hdr_dot.itemconfig(self._hdr_dot_id, fill=color)

        sts = list(self.tracker.statuses.values())
        if self.tracker._paused:
            set_dot(WARN_D)
        elif not sts or all(s.last_checked is None for s in sts):
            set_dot(WARN_D)
        elif any(s.any_open() for s in sts):
            set_dot(SUCCESS_D)
        elif any(s.error for s in sts):
            set_dot(WARN_D)
        else:
            set_dot(DANGER_D)

    # =========================================================================
    # Add course
    # =========================================================================
    def _add_course(self) -> None:
        cid    = "" if getattr(self._e_course,  "_has_placeholder", False) else self._e_course.get().strip().upper()
        sid    = "" if getattr(self._e_section, "_has_placeholder", False) else self._e_section.get().strip()
        season = self._season_var.get()
        year   = self._e_year.get().strip()

        if not cid:
            self._flash_error(self._e_course)
            return
        if not year.isdigit() or len(year) != 4:
            self._flash_error(self._e_year)
            return

        tid = term_id(season, year)
        for c in self.tracker.courses:
            if c.courseId == cid and c.termId == tid and c.sectionId == sid:
                messagebox.showinfo("Already tracked",
                                    f"{cid} is already in your list.",
                                    parent=self.win)
                return

        self.tracker.courses.append(CourseConfig(cid, tid, sid))
        save_courses(self.tracker.courses)
        self.tracker.rebuild_menu()
        self.win.focus_set()                         # release entry focus first
        for entry in (self._e_course, self._e_section):
            entry.delete(0, "end")
            entry.event_generate("<FocusOut>")   # restore placeholder
        self._rebuild_cards()                    # show card immediately
        self.tracker._set_icon_color("yellow")
        log.info("Added %s §%s term=%s", cid, sid, tid)
        threading.Thread(target=self.tracker._poll_once, daemon=True).start()

    def _flash_error(self, entry: tk.Entry) -> None:
        orig = entry.cget("highlightbackground")
        entry.config(highlightbackground=DANGER)
        self.win.after(600, lambda: entry.config(highlightbackground=orig))

    # =========================================================================
    # Remove course
    # =========================================================================
    def _remove_course(self, key: str) -> None:
        self.tracker.courses = [c for c in self.tracker.courses if c.key() != key]
        self.tracker.statuses.pop(key, None)
        cid = key.split("_")[0]
        self.tracker.previous_open = {
            k: v for k, v in self.tracker.previous_open.items()
            if not k.startswith(cid + "_")
        }
        save_courses(self.tracker.courses)
        self.tracker.update_tray_icon()
        self.tracker.rebuild_menu()
        self.win.after(0, self._rebuild_cards)   # defer past the click event, then rebuild
        log.info("Removed course %s", key)

    # =========================================================================
    # Settings persistence
    # =========================================================================
    def _save_interval(self, _=None) -> None:
        try:
            val = max(30, int(self._iv.get()))
        except ValueError:
            val = 60
        self._iv.set(str(val))
        self.tracker.settings["interval"] = val
        save_settings(self.tracker.settings)

    def _save_notify(self) -> None:
        self.tracker.settings["notify_on_close"] = self._notif_var.get()
        save_settings(self.tracker.settings)

    def _save_startup(self) -> None:
        set_startup_enabled(self._startup_var.get())

    def _save_theme(self, _=None) -> None:
        """Persist the chosen theme, re-apply colours, and reopen the popup."""
        choice = self._theme_var.get().lower()   # "system", "dark", or "light"
        self.tracker.settings["theme"] = choice
        save_settings(self.tracker.settings)
        apply_theme(_effective_theme(choice))
        self.tracker._configure_ttk_style()
        save_ico()                          # regenerate icon file with new theme colours
        self.tracker.update_tray_icon()     # push the new icon to the system tray live
        self.win.after(50, self._reopen_with_new_theme)

    def _reopen_with_new_theme(self) -> None:
        tracker = self.tracker
        root    = self._root
        self.close()                      # _save_geometry already saved adv state
        Popup.toggle(tracker, root)
        if tracker._session_adv_open and Popup._instance:
            Popup._instance._toggle_advanced()

    def _reset_position(self) -> None:
        """Snap the window to the bottom-right of the work area."""
        MARGIN = 12
        _, _, wa_r, wa_b = Popup._work_area()
        if wa_r <= 0:
            wa_r = self.win.winfo_screenwidth()
            wa_b = self.win.winfo_screenheight()
        W = self.win.winfo_width()
        H = self.win.winfo_height()
        x = wa_r - W - MARGIN
        y = wa_b - H - MARGIN
        self.win.geometry(f"+{x}+{y}")
        self.tracker._session_x      = x
        self.tracker._session_bottom = wa_b - MARGIN

    # =========================================================================
    # Geometry save / restore
    # =========================================================================
    def _save_geometry(self) -> None:
        """Remember window state for this session (not written to disk)."""
        try:
            self.tracker._session_x      = self.win.winfo_x()
            self.tracker._session_bottom = self.win.winfo_y() + self.win.winfo_height()
            self.tracker._session_adv_open = self._adv_open
        except Exception:
            pass

    # =========================================================================
    # Lifecycle
    # =========================================================================
    def close(self) -> None:
        """Hide the popup; the app remains in the system tray."""
        self._save_geometry()
        self._alive = False
        if self._job:
            try:
                self.win.after_cancel(self._job)
            except Exception:
                pass
        try:
            self.win.destroy()
        except Exception:
            pass
        Popup._instance = None

    def quit(self) -> None:
        """Quit the entire application."""
        if self._quitting:
            return
        self._quitting = True
        self._save_geometry()
        self._alive = False
        if self._job:
            try:
                self.win.after_cancel(self._job)
            except Exception:
                pass
        try:
            self.win.destroy()
        except Exception:
            pass
        Popup._instance = None
        self.tracker.cmd_q.put("quit")


# =============================================================================
# Tracker — business logic
# =============================================================================
class Tracker:
    def __init__(self) -> None:
        self.settings:      dict                    = load_settings()
        self.courses:       list[CourseConfig]      = load_courses()
        self.statuses:      dict[str, CourseStatus] = {}
        self.previous_open: dict[str, bool]         = {}
        self._paused    = False
        self._stopping  = threading.Event()
        self._quitting  = False
        self.cmd_q: queue.Queue = queue.Queue()
        self._icon: Optional[TrayIcon] = None
        self._root: Optional[tk.Tk]    = None
        # Session-only window state (not persisted; resets to bottom-right on launch)
        self._session_x:       Optional[int] = None
        self._session_bottom:  Optional[int] = None   # bottom edge — survives height changes
        self._session_adv_open: bool         = False
        # Remove any legacy persisted position keys from settings
        _changed = False
        for _k in ("popup_moved", "popup_x", "popup_y"):
            if _k in self.settings:
                del self.settings[_k]
                _changed = True
        if _changed:
            save_settings(self.settings)
        save_ico()

    # ── Icon colour ───────────────────────────────────────────────────────────
    def _set_icon_color(self, color: str) -> None:
        if self._icon:
            self._icon.icon = make_icon_image(color, 64)

    def update_tray_icon(self) -> None:
        if not self.courses or not self.statuses:
            self._set_icon_color("yellow")
            return
        if any(s.any_open() for s in self.statuses.values()):
            self._set_icon_color("green")
        elif any(s.error for s in self.statuses.values()):
            self._set_icon_color("yellow")
        else:
            self._set_icon_color("red")

    # ── Polling ───────────────────────────────────────────────────────────────
    def _poll_once(self) -> None:
        if not self.courses:
            self._set_icon_color("yellow")
            return
        self._set_icon_color("yellow")
        for course in list(self.courses):   # snapshot — list may change during poll
            key = course.key()
            try:
                sections = fetch_sections(course)
                self.statuses[key] = CourseStatus(
                    config=course, sections=sections, last_checked=time.time())
                if sections:
                    self._check_transitions(course, sections)
            except requests.RequestException as exc:
                log.error("Network error %s: %s", course.courseId, exc)
                prev = self.statuses.get(key)
                self.statuses[key] = CourseStatus(
                    config=course,
                    sections=prev.sections if prev else [],
                    error=str(exc),
                    last_checked=time.time(),
                )
            except Exception:
                log.exception("Unexpected error for %s", course.courseId)
                self.statuses[key] = CourseStatus(
                    config=course, error="Unexpected error",
                    last_checked=time.time())
        self.update_tray_icon()

    def _check_transitions(self, course: CourseConfig,
                            sections: list[SectionStatus]) -> None:
        for sec in sections:
            k        = f"{course.courseId}_{sec.section_id}"
            was_open = self.previous_open.get(k)
            is_open  = sec.is_open and sec.open_seats > 0
            if was_open is not None:
                if not was_open and is_open:
                    msg = (f"{course.display_name()} §{sec.section_id}: "
                           f"{sec.open_seats} seat(s) open!")
                    log.info("OPEN: %s", msg)
                    toast("Seat Available!", msg, url=course.url())
                elif was_open and not is_open and self.settings.get("notify_on_close"):
                    msg = f"{course.display_name()} §{sec.section_id} is now full."
                    log.info("CLOSED: %s", msg)
                    toast("Course Full", msg)
            self.previous_open[k] = is_open

    def _poll_loop(self) -> None:
        log.info("Poll thread started — first poll in 2 s")
        self._stopping.wait(2)
        while not self._stopping.is_set():
            if not self._paused:
                try:
                    self._poll_once()
                except Exception:
                    log.exception("Poll loop error")
            interval = max(30, int(self.settings.get("interval", 60)))
            log.info("Next poll in %d s", interval)
            self._stopping.wait(interval)
        log.info("Poll thread stopped")

    # ── Tray menu ─────────────────────────────────────────────────────────────
    def rebuild_menu(self) -> None:
        if self._icon:
            self._icon.update_menu()

    def _build_menu(self) -> pystray.Menu:
        def pause_label(_item):
            return "Resume" if self._paused else "Pause"

        return pystray.Menu(
            pystray.MenuItem("Check Now",   self._on_check_now),
            pystray.MenuItem(pause_label,   self._on_toggle_pause),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit",        self._on_quit),
        )

    def _on_check_now(self, *_):
        threading.Thread(target=self._poll_once, daemon=True).start()

    def _on_toggle_pause(self, *_):
        self._paused = not self._paused
        if self._icon:
            self._icon.update_menu()
        if not self._paused:
            threading.Thread(target=self._poll_once, daemon=True).start()
        else:
            self._set_icon_color("yellow")

    def _on_open_ui(self):
        self.cmd_q.put("open")

    def _on_quit(self, *_):
        if not self._quitting:
            self.cmd_q.put("quit")

    # ── Tkinter command pump ──────────────────────────────────────────────────
    def _pump(self) -> None:
        try:
            while True:
                cmd = self.cmd_q.get_nowait()
                log.info("Command: %s", cmd)
                if cmd == "open":
                    Popup.toggle(self, self._root)
                elif cmd == "quit" and not self._quitting:
                    self._quitting = True
                    self._stopping.set()
                    if self._icon:
                        threading.Thread(target=self._icon.stop,
                                         daemon=True).start()
                    self._root.after(250, self._root.quit)
                    return   # stop rescheduling
        except queue.Empty:
            pass
        except Exception:
            log.exception("Pump error")
        if self._root and not self._quitting:
            self._root.after(100, self._pump)

    # ── TTK style ─────────────────────────────────────────────────────────────
    def _configure_ttk_style(self) -> None:
        """Configure the ttk Combobox style to match the current colour theme.

        Called once at startup and again whenever the user changes the theme
        so the dropdown reflects the new palette immediately.
        """
        if not self._root:
            return
        style = ttk.Style(self._root)
        style.theme_use("clam")
        style.configure("Popup.TCombobox",
                         fieldbackground=INPUT, background=ACCENT,
                         foreground=TEXT, arrowcolor=SUB,
                         selectbackground=INPUT, selectforeground=TEXT,
                         padding=(5, 3),
                         focuscolor=INPUT,
                         bordercolor=ACCENT,
                         lightcolor=ACCENT, darkcolor=ACCENT)
        style.map("Popup.TCombobox",
                  fieldbackground=[("readonly", INPUT), ("focus", INPUT)],
                  selectbackground=[("readonly", INPUT)],
                  selectforeground=[("readonly", TEXT)],
                  background=[("active", ACCENT_H), ("!active", ACCENT)],
                  focuscolor=[("focus", INPUT),  ("!focus", INPUT)],
                  bordercolor=[("focus", BORDER), ("!focus", ACCENT)],
                  lightcolor=[("focus", ACCENT),  ("!focus", ACCENT)],
                  darkcolor=[("focus",  ACCENT),  ("!focus", ACCENT)])
        self._root.option_add("*TCombobox*Listbox.background",       ACCENT)
        self._root.option_add("*TCombobox*Listbox.foreground",       TEXT)
        self._root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_H)
        self._root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        self._root.option_add("*TCombobox*Listbox.font",             "Segoe\\ UI 9")
        self._root.option_add("*TCombobox*Listbox.relief",           "flat")

    # ── Run ───────────────────────────────────────────────────────────────────
    def run(self) -> None:
        # Apply the colour theme before any widgets are created
        effective = _effective_theme(self.settings.get("theme", "system"))
        apply_theme(effective)

        root = tk.Tk()
        self._root = root
        root.withdraw()
        root.protocol("WM_DELETE_WINDOW", lambda: None)

        self._configure_ttk_style()

        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
            root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass

        # Enable startup by default on the very first launch
        if not self.settings.get("startup_defaulted"):
            self.settings["startup_defaulted"] = True
            if not get_startup_enabled():
                set_startup_enabled(True)
            save_settings(self.settings)

        root.after(100, self._pump)
        threading.Thread(target=self._poll_loop, daemon=True,
                         name="PollThread").start()

        self._icon = TrayIcon(
            "UMD Course Tracker",
            icon=make_icon_image("yellow", 64),
            title="UMD Course Tracker",
            menu=self._build_menu(),
            on_left_click=self._on_open_ui,
        )
        self._icon.run_detached()

        # Auto-open panel when no courses are configured
        if not self.courses:
            root.after(400, lambda: Popup.toggle(self, root))

        log.info("Entering mainloop")
        root.mainloop()
        log.info("Shutdown complete")
        self._stopping.set()


# =============================================================================
# Entry point
# =============================================================================
if __name__ == "__main__":
    Tracker().run()
