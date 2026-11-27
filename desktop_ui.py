from ctypes import wintypes
import ctypes
import datetime
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk

from PIL import Image, ImageDraw, ImageFilter, ImageTk
import pystray


# ============================================================================
# "25-ji, Nightcord de." (25時、ナイトコードで。) Visual Palette
# Inspired by Empty SEKAI (誰もいないセカイ) & the Nightcord voice-chat client
# ============================================================================
BG_MAIN = "#121018"          # Deep Empty-SEKAI void charcoal-violet
BG_HEADER = "#171420"        # Nightcord top channel bar
BG_CARD = "#1B1826"          # Nightcord channel card surface
BG_CARD_BORDER = "#2E273D"   # Hairline geometric card frame
BG_OUTER_BORDER = "#4B355E"  # Outer edge drawer hairline border
BG_BAR = "#292338"           # DAW / battery empty track
BG_BTN = "#262035"           # Muted slate-violet button
BG_BTN_HOVER = "#352C49"     # Button hover surface
BG_BTN_PRIMARY = "#884499"   # Official N25 unit color (#884499)
BG_BTN_PRIMARY_HOVER = "#9D53B0"
BG_BTN_DANGER = "#2D1A24"    # Muted dark rose surface

# Official N25 Unit & 4 Member Representative Colors
N25_UNIT = "#884499"         # 25時、ナイトコードで。 (#884499)
N25_GLOW = "#B97BD1"         # Brightened N25 violet for dark-bg legibility
COL_KANADE = "#BB6688"       # 宵崎 奏 (Yoisaki Kanade - Dusty Rose)
COL_MAFUYU = "#8888CC"       # 朝比奈 まふゆ (Asahina Mafuyu - Periwinkle)
COL_ENA = "#CCAA88"          # 東雲 絵名 (Shinonome Ena - Cinnamon Sand)
COL_MIZUKI = "#DDAACC"       # 暁山 瑞希 (Akiyama Mizuki - Pastel Mauve)

# Desaturated "Dusty Tone" Typography & Status Colors
FG_PRIMARY = "#F0ECF6"       # Pale mist white
FG_SECONDARY = "#A69EB8"     # Fog lavender-gray
FG_MUTED = "#6D6480"         # Muted twilight gray
FG_GREEN = "#84C7AE"         # Desaturated mint-seafoam (online / connected)
FG_BAR_OK = "#A67FCA"        # Healthy battery bar (N25 dusty violet-lavender)
FG_YELLOW = "#CCAA88"        # Ena sand-gold (warning / manual override)
FG_RED = "#D46A88"           # Kanade dusty crimson (low battery / danger)
FG_BLUE = "#9898DC"          # Mafuyu periwinkle link / telemetry highlight

BASE_DIR = Path(__file__).resolve().parent
ICON_CANDIDATES = (
    BASE_DIR / "assets" / "nightcord_icon.png",
    BASE_DIR / "ios" / "AudioBridge" / "Assets.xcassets" / "AppIcon.appiconset" / "AppIcon.png",
)
_CACHED_ICON_ALPHA = None


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _enable_high_dpi():
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _get_top_hwnd(window: tk.Tk) -> int:
    if sys.platform != "win32":
        return 0
    try:
        window.update_idletasks()
        wid = window.winfo_id()
        # GA_ROOT = 2 reliably returns top-level HWND even with overrideredirect(True)
        hwnd = ctypes.windll.user32.GetAncestor(wid, 2)
        if not hwnd:
            hwnd = ctypes.windll.user32.GetParent(wid) or wid
        return int(hwnd)
    except Exception:
        return 0


def _configure_borderless_toolwindow(window: tk.Tk):
    """
    Hide window from Windows Taskbar & Alt+Tab (WS_EX_TOOLWINDOW) and apply
    Windows 11 DWM rounded corners + Nightcord dark border if supported.
    """
    if sys.platform != "win32":
        return
    try:
        hwnd = _get_top_hwnd(window)
        if not hwnd:
            return
        user32 = ctypes.windll.user32
        gwlp_exstyle = -20
        ws_ex_toolwindow = 0x00000080
        ws_ex_appwindow = 0x00040000

        if ctypes.sizeof(ctypes.c_void_p) == 8:
            get_long = user32.GetWindowLongPtrW
            set_long = user32.SetWindowLongPtrW
            get_long.restype = ctypes.c_ssize_t
            set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        else:
            get_long = user32.GetWindowLongW
            set_long = user32.SetWindowLongW

        ex_style = get_long(hwnd, gwlp_exstyle)
        new_style = (ex_style | ws_ex_toolwindow) & ~ws_ex_appwindow
        if new_style != ex_style:
            set_long(hwnd, gwlp_exstyle, new_style)
            # SWP_NOMOVE|SWP_NOSIZE|SWP_NOZORDER|SWP_NOACTIVATE|SWP_FRAMECHANGED
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0037)

        dwmapi = ctypes.windll.dwmapi
        dark = ctypes.c_int(1)
        for attr in (20, 19):
            dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(dark), ctypes.sizeof(dark)
            )
        # DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2
        corner_pref = ctypes.c_int(2)
        dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(corner_pref), ctypes.sizeof(corner_pref)
        )
        # DWMWA_BORDER_COLOR = 34 -> COLORREF 0x00BBGGRR for #4B355E -> 0x005E354B
        border_col = ctypes.c_uint32(0x005E354B)
        dwmapi.DwmSetWindowAttribute(
            hwnd, 34, ctypes.byref(border_col), ctypes.sizeof(border_col)
        )
    except Exception:
        pass


def _get_cursor_pos(fallback_root: tk.Tk = None):
    if sys.platform == "win32":
        pt = _POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return int(pt.x), int(pt.y)
    if fallback_root is not None:
        try:
            return fallback_root.winfo_pointerxy()
        except Exception:
            pass
    return 0, 0


def _get_monitor_geometry_at(x: int, y: int, fallback_root: tk.Tk = None):
    """
    Return (mon_left, mon_top, mon_right, mon_bottom, work_top, work_bottom, is_outer_right_edge)
    for the monitor containing (x, y).
    """
    if sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            hmon = user32.MonitorFromPoint(_POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
            if hmon:
                mi = _MONITORINFO()
                mi.cbSize = ctypes.sizeof(_MONITORINFO)
                if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    mr = mi.rcMonitor
                    wr = mi.rcWork
                    # True outer right screen edge iff there is no monitor 8px to the right
                    probe = _POINT(mr.right + 8, max(mr.top, min(mr.bottom - 1, y)))
                    neighbor = user32.MonitorFromPoint(probe, 0)  # MONITOR_DEFAULTTONULL
                    return (
                        int(mr.left),
                        int(mr.top),
                        int(mr.right),
                        int(mr.bottom),
                        int(wr.top),
                        int(wr.bottom),
                        neighbor == 0,
                    )
        except Exception:
            pass

    sw = fallback_root.winfo_screenwidth() if fallback_root else 1920
    sh = fallback_root.winfo_screenheight() if fallback_root else 1080
    return (0, 0, sw, sh, 0, sh - 48, True)


def _load_nightcord_alpha() -> Image.Image:
    """Load the clean alpha mask of the Nightcord at 25:00 SVG emblem."""
    global _CACHED_ICON_ALPHA
    if _CACHED_ICON_ALPHA is not None:
        return _CACHED_ICON_ALPHA

    for p in ICON_CANDIDATES:
        if p.exists():
            try:
                rgba = Image.open(p).convert("RGBA")
                _CACHED_ICON_ALPHA = rgba.getchannel("A")
                return _CACHED_ICON_ALPHA
            except Exception:
                pass

    # Fallback procedural diamond-heart mask if asset files were moved
    fallback = Image.new("L", (256, 256), 0)
    d = ImageDraw.Draw(fallback)
    d.polygon([(128, 44), (212, 128), (128, 216), (44, 128)], fill=255)
    d.line([(84, 96), (172, 164)], fill=0, width=16)
    _CACHED_ICON_ALPHA = fallback
    return _CACHED_ICON_ALPHA


def render_nightcord_emblem(
    size: int,
    rgb=(182, 114, 204),
    glow_rgb=(136, 68, 153),
    with_glow: bool = True,
) -> Image.Image:
    """Render crisp Nightcord emblem at `size x size` RGBA with optional twilight glow."""
    alpha_src = _load_nightcord_alpha()
    alpha = alpha_src.resize((size, size), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if with_glow:
        blur_r = max(1.2, size * 0.075)
        glow_a = alpha.filter(ImageFilter.GaussianBlur(radius=blur_r))
        glow_layer = Image.new("RGBA", (size, size), (*glow_rgb, 0))
        glow_layer.putalpha(glow_a.point(lambda a: min(255, int(a * 0.75))))
        canvas = Image.alpha_composite(canvas, glow_layer)

    fg_layer = Image.new("RGBA", (size, size), (*rgb, 0))
    fg_layer.putalpha(alpha)
    return Image.alpha_composite(canvas, fg_layer)


def create_tray_icon_image(connected: bool = False, low_battery: bool = False):
    """Draw a crisp anti-aliased 64x64 Nightcord at 25:00 tray icon with status pip."""
    hi = 256
    img = Image.new("RGBA", (hi, hi), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Empty-SEKAI dark badge + subtle violet rim (4x supersampled)
    draw.rounded_rectangle(
        (8, 8, 247, 247),
        radius=60,
        fill=(20, 17, 28, 255),
        outline=(98, 64, 124, 255),
        width=8,
    )

    alpha_full = _load_nightcord_alpha()
    bbox = alpha_full.getbbox()
    cropped_a = alpha_full.crop(bbox) if bbox else alpha_full

    mark_px = 168
    cw, ch = cropped_a.size
    scale = min(mark_px / max(1, cw), mark_px / max(1, ch))
    nw = max(1, int(round(cw * scale)))
    nh = max(1, int(round(ch * scale)))
    mark_a = cropped_a.resize((nw, nh), Image.Resampling.LANCZOS)

    emblem_rgb = (200, 130, 222) if connected else (172, 102, 196)
    mark_layer = Image.new("RGBA", (nw, nh), (*emblem_rgb, 0))
    mark_layer.putalpha(mark_a)
    ox = (hi - nw) // 2
    oy = (hi - nh) // 2
    img.alpha_composite(mark_layer, (ox, oy))

    # Status indicator dot in bottom-right corner
    if low_battery:
        pip_col = (216, 106, 138, 255)  # Kanade dusty crimson
        draw.ellipse((174, 174, 234, 234), fill=(20, 17, 28, 255))
        draw.ellipse((184, 184, 224, 224), fill=pip_col)
    elif connected:
        pip_col = (132, 199, 174, 255)  # Online mint-seafoam
        draw.ellipse((174, 174, 234, 234), fill=(20, 17, 28, 255))
        draw.ellipse((184, 184, 224, 224), fill=pip_col)

    return img.resize((64, 64), Image.Resampling.LANCZOS)


def _battery_bar_color(pct):
    if pct is None:
        return FG_MUTED
    if pct <= 15:
        return FG_RED
    if pct <= 30:
        return FG_YELLOW
    return FG_BAR_OK


def _battery_text_color(pct):
    if pct is None:
        return FG_MUTED
    if pct <= 15:
        return FG_RED
    if pct <= 30:
        return FG_YELLOW
    return FG_PRIMARY


def _nightcord_clock_str(now: datetime.datetime = None) -> str:
    """Format late-night hours in N25 style (01:00 -> 25:00)."""
    dt = now or datetime.datetime.now()
    if 0 <= dt.hour <= 4:
        return f"{dt.hour + 24:02d}:{dt.minute:02d}"
    return f"25:00 · {dt.hour:02d}:{dt.minute:02d}"


class BatteryBarRow(tk.Frame):
    """Nightcord DAW-style meter row: label on left, slim geometric bar, percentage on right."""

    def __init__(self, parent, label_text: str, dpi_scale: float = 1.0):
        super().__init__(parent, bg=BG_CARD)
        self._dpi = dpi_scale
        self._bar_h = max(6, int(round(6 * dpi_scale)))
        canvas_h = max(15, int(round(17 * dpi_scale)))

        self.lbl_name = tk.Label(
            self,
            text=label_text,
            bg=BG_CARD,
            fg=FG_SECONDARY,
            font=("Microsoft YaHei UI", 9),
            width=7,
            anchor="w",
        )
        self.lbl_name.pack(side="left")

        self.lbl_val = tk.Label(
            self,
            text="--%",
            bg=BG_CARD,
            fg=FG_PRIMARY,
            font=("Consolas", 10, "bold"),
            width=10,
            anchor="e",
        )
        self.lbl_val.pack(side="right")

        self.canvas = tk.Canvas(
            self,
            width=60,
            height=canvas_h,
            bg=BG_CARD,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(
            side="left",
            fill="x",
            expand=True,
            padx=(int(8 * dpi_scale), int(8 * dpi_scale)),
        )

        self._pct = None
        self._bar_col = FG_MUTED
        self.canvas.bind("<Configure>", lambda _e: self._redraw())

    def set_value(self, label_text: str, pct, value_text: str, color: str = None):
        self.lbl_name.config(text=label_text)
        txt_col = color or _battery_text_color(pct)
        self.lbl_val.config(text=value_text, fg=txt_col)
        self._pct = pct
        if color in (FG_MUTED, FG_SECONDARY):
            self._bar_col = color
        else:
            self._bar_col = _battery_bar_color(pct)
        self._redraw()

    def _redraw(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 12 or h < 4:
            return

        bh = self._bar_h
        cy = h // 2
        y0 = cy - bh // 2
        y1 = y0 + bh
        x0 = 2
        x1 = max(x0 + 4, w - 2)

        # Crisp geometric track with subtle border ticks
        c.create_rectangle(x0, y0, x1, y1, fill=BG_BAR, outline="#342C46", width=1)

        if self._pct is not None and self._pct > 0:
            clamped = max(0, min(100, self._pct))
            span = x1 - x0 - 2
            fill_w = max(2, int(round(span * clamped / 100.0)))
            fx1 = x0 + 1 + fill_w
            c.create_rectangle(
                x0 + 1, y0 + 1, fx1, y1 - 1, fill=self._bar_col, outline=""
            )
            # Subtle luminous playhead tick at the right tip of the meter
            if clamped < 100:
                c.create_rectangle(
                    max(x0 + 1, fx1 - 2),
                    y0,
                    fx1,
                    y1,
                    fill=COL_MIZUKI,
                    outline="",
                )


def _make_nightcord_card(parent, accent_hex: str, dpi_scale: float):
    """
    Create a Nightcord-styled card with a 1px geometric border and a 3px left
    member-colored channel bar. Returns (outer_card_frame, inner_content_frame).
    """
    s = lambda px: int(round(px * dpi_scale))
    border = tk.Frame(
        parent,
        bg=BG_CARD,
        highlightbackground=BG_CARD_BORDER,
        highlightcolor=BG_CARD_BORDER,
        highlightthickness=1,
        bd=0,
    )
    accent = tk.Frame(border, bg=accent_hex, width=max(3, s(3)))
    accent.pack(side="left", fill="y")

    body = tk.Frame(border, bg=BG_CARD, padx=s(11), pady=s(9))
    body.pack(side="left", fill="both", expand=True)
    return border, body


class DesktopPanel:
    """
    "25-ji, Nightcord de." styled right-edge auto-hide drawer + system tray icon.
    - Hidden from Windows Taskbar & Alt+Tab (overrideredirect + WS_EX_TOOLWINDOW).
    - Automatically slides out when cursor touches the right edge of the screen,
      and tucks back away when cursor leaves (unless pinned).
    """

    def __init__(
        self,
        get_state_fn,
        on_refresh_iphone_fn,
        on_quit_fn,
        on_pair_iphone_fn=None,
        on_restart_fn=None,
        on_switch_virtual_fn=None,
        on_restore_audio_fn=None,
        on_select_airpods_fn=None,
        on_reject_airpods_fn=None,
        on_unlock_airpods_fn=None,
        on_toggle_autostart_fn=None,
    ):
        self._get_state = get_state_fn
        self._on_refresh_iphone = on_refresh_iphone_fn
        self._on_quit = on_quit_fn
        self._on_pair_iphone = on_pair_iphone_fn
        self._on_restart = on_restart_fn
        self._on_switch_virtual = on_switch_virtual_fn
        self._on_restore_audio = on_restore_audio_fn
        self._on_select_airpods = on_select_airpods_fn
        self._on_reject_airpods = on_reject_airpods_fn
        self._on_unlock_airpods = on_unlock_airpods_fn
        self._on_toggle_autostart = on_toggle_autostart_fn

        self._cmd_queue = queue.Queue()
        self._pinned = False
        self._autostart_enabled = False
        self._quitting = False
        self._last_tooltip = ""
        self._last_icon_state = None
        self._copy_reset_after = None
        self._switch_reset_after = None
        self._switch_flash_text = ""

        # Edge-drawer state machine
        self._drawer_visible = False
        self._animating = False
        self._popup_menu_open = False
        self._edge_dwell_since = None
        self._outside_since = None
        self._manual_grace_until = 0.0
        self._suppress_edge_until = 0.0
        self._dock_x = 0
        self._dock_y = 0
        self._mon_right = 1920

        self.root = None
        self.tray_icon = None
        self._header_logo_photo = None
        self._win_icon_photo = None

    def request_quit(self):
        self._cmd_queue.put("quit")

    def request_show(self):
        self._cmd_queue.put("show")

    def request_restart_failed(self):
        self._cmd_queue.put("restart_failed")

    def _open_bluetooth_settings(self):
        if sys.platform == "win32":
            try:
                os.startfile("ms-settings:bluetooth")
            except Exception:
                pass

    def _toggle_window_from_tray(self, _icon=None, _item=None):
        self._cmd_queue.put("toggle")

    def _toggle_pin_from_tray(self, _icon=None, _item=None):
        self._cmd_queue.put("toggle_pin")

    def _toggle_autostart_from_tray(self, _icon=None, _item=None):
        self._cmd_queue.put("toggle_autostart")

    def _switch_virtual_from_tray(self, _icon=None, _item=None):
        if self._on_switch_virtual:
            self._on_switch_virtual()

    def _restore_audio_from_tray(self, _icon=None, _item=None):
        if self._on_restore_audio:
            self._on_restore_audio()

    def _refresh_from_tray(self, _icon=None, _item=None):
        if self._on_refresh_iphone:
            self._on_refresh_iphone()

    def _bt_settings_from_tray(self, _icon=None, _item=None):
        self._open_bluetooth_settings()

    def _quit_from_tray(self, _icon=None, _item=None):
        self._cmd_queue.put("quit")

    def _start_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem(
                "呼出 / 收起侧栏", self._toggle_window_from_tray, default=True
            ),
            pystray.MenuItem(
                "固定面板常驻 (不自动收起)",
                self._toggle_pin_from_tray,
                checked=lambda _item: self._pinned,
            ),
            pystray.MenuItem(
                "开机自动启动",
                self._toggle_autostart_from_tray,
                checked=lambda _item: self._autostart_enabled,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("一键切回虚拟声卡", self._switch_virtual_from_tray),
            pystray.MenuItem("暂时切回原声卡", self._restore_audio_from_tray),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("立即刷新手机电量", self._refresh_from_tray),
            pystray.MenuItem("打开 Windows 蓝牙设置", self._bt_settings_from_tray),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出 Nightcord Bridge", self._quit_from_tray),
        )
        self.tray_icon = pystray.Icon(
            "iphone_audio_bridge",
            create_tray_icon_image(False, False),
            "25:00 // Nightcord Audio Bridge",
            menu,
        )
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _compute_dock_position(self, ref_x: int = None, ref_y: int = None):
        if ref_x is None or ref_y is None:
            ref_x, ref_y = _get_cursor_pos(self.root)
        _ml, _mt, mr, _mb, wt, wb, _is_outer = _get_monitor_geometry_at(
            ref_x, ref_y, self.root
        )
        margin_r = int(round(8 * self._dpi))
        margin_v = int(round(14 * self._dpi))
        req_h = max(int(430 * self._dpi), self._last_height)
        work_h = max(req_h, wb - wt)

        target_x = mr - self._win_width - margin_r
        target_y = wt + (work_h - req_h) // 2
        target_y = max(wt + margin_v, min(wb - req_h - margin_v, target_y))
        return target_x, target_y, mr

    def _fit_window_height(self, initial: bool = False):
        if not self.root:
            return
        self.root.update_idletasks()
        req_h = max(int(430 * self._dpi), self.root.winfo_reqheight())
        if not initial and abs(req_h - self._last_height) < 3:
            return
        self._last_height = req_h
        if self._drawer_visible and not self._animating:
            tx, ty, mr = self._compute_dock_position(self._mon_right - 2, self._dock_y + 40)
            self._dock_x, self._dock_y, self._mon_right = tx, ty, mr
            self.root.geometry(f"{self._win_width}x{req_h}+{tx}+{ty}")
        elif initial:
            tx, ty, mr = self._compute_dock_position()
            self._dock_x, self._dock_y, self._mon_right = tx, ty, mr
            self.root.geometry(f"{self._win_width}x{req_h}+{tx}+{ty}")

    def _draw_header_decor(self, _event=None):
        c = self.header_canvas
        c.delete("decor")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 40 or h < 20:
            return
        s = lambda px: int(round(px * self._dpi))
        # Subtle Empty-SEKAI geometric truss / fractured cord lines across header
        rx = w - s(165)
        c.create_line(rx, 0, rx + s(56), h, fill="#241D34", width=1, tags="decor")
        c.create_line(rx + s(32), h, rx + s(88), 0, fill="#2A213C", width=1, tags="decor")
        c.create_line(rx - s(12), h // 2, w - s(12), h // 2, fill="#201A2E", width=1, tags="decor")
        c.create_polygon(
            rx + s(24), s(7),
            rx + s(35), s(24),
            rx + s(13), s(21),
            outline="#33284A",
            fill="",
            width=1,
            tags="decor",
        )
        c.create_polygon(
            rx + s(64), h - s(8),
            rx + s(73), h - s(21),
            rx + s(56), h - s(18),
            outline="#2C2340",
            fill="",
            width=1,
            tags="decor",
        )
        c.tag_lower("decor")

    def _build_ui(self):
        _enable_high_dpi()

        root = tk.Tk()
        self.root = root
        root.title("25:00 // Nightcord Audio Bridge")
        root.configure(bg=BG_MAIN)
        root.resizable(False, False)

        # Borderless window -> never shown on Windows Taskbar
        root.overrideredirect(True)
        root.attributes("-topmost", True)

        self._dpi = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self._win_width = int(round(396 * self._dpi))
        self._last_height = 0
        s = lambda px: int(round(px * self._dpi))

        # Set window icon photo using Nightcord emblem
        try:
            win_ico_pil = render_nightcord_emblem(64, with_glow=False)
            self._win_icon_photo = ImageTk.PhotoImage(win_ico_pil)
            root.iconphoto(True, self._win_icon_photo)
        except Exception:
            pass

        root.protocol("WM_DELETE_WINDOW", self._slide_out_drawer)

        # Outer 1px Nightcord frame
        shell = tk.Frame(
            root,
            bg=BG_MAIN,
            highlightbackground=BG_OUTER_BORDER,
            highlightcolor=BG_OUTER_BORDER,
            highlightthickness=1,
            bd=0,
        )
        shell.pack(fill="both", expand=True)

        # ---- Top 3px N25 Representative + 4 Members Chord Strip ----
        chord_bar = tk.Frame(shell, bg=BG_MAIN, height=max(3, s(3)))
        chord_bar.pack(fill="x")
        chord_bar.pack_propagate(False)
        tk.Frame(chord_bar, bg=N25_UNIT).place(relx=0.0, rely=0, relwidth=0.60, relheight=1.0)
        tk.Frame(chord_bar, bg=COL_KANADE).place(relx=0.60, rely=0, relwidth=0.10, relheight=1.0)
        tk.Frame(chord_bar, bg=COL_MAFUYU).place(relx=0.70, rely=0, relwidth=0.10, relheight=1.0)
        tk.Frame(chord_bar, bg=COL_ENA).place(relx=0.80, rely=0, relwidth=0.10, relheight=1.0)
        tk.Frame(chord_bar, bg=COL_MIZUKI).place(relx=0.90, rely=0, relwidth=0.10, relheight=1.0)

        # ---- Nightcord Header Banner ----
        header_h = s(54)
        self.header_canvas = tk.Canvas(
            shell,
            bg=BG_HEADER,
            height=header_h,
            highlightthickness=0,
            bd=0,
        )
        self.header_canvas.pack(fill="x")
        self.header_canvas.bind("<Configure>", self._draw_header_decor)

        # Nightcord SVG Logo rendered at high DPI with soft violet glow
        logo_px = s(36)
        logo_pil = render_nightcord_emblem(
            logo_px,
            rgb=(196, 126, 218),
            glow_rgb=(136, 68, 153),
            with_glow=True,
        )
        self._header_logo_photo = ImageTk.PhotoImage(logo_pil)
        self.header_canvas.create_image(
            s(12) + logo_px // 2,
            header_h // 2,
            image=self._header_logo_photo,
            anchor="center",
        )

        tx0 = s(12) + logo_px + s(9)
        self.header_canvas.create_text(
            tx0,
            s(18),
            text="NIGHTCORD",
            fill=FG_PRIMARY,
            font=("Segoe UI Semibold", 11, "bold"),
            anchor="w",
        )
        self.lbl_clock = tk.Label(
            self.header_canvas,
            text=_nightcord_clock_str(),
            bg="#251E35",
            fg=COL_MIZUKI,
            font=("Consolas", 8, "bold"),
            padx=s(5),
            pady=1,
        )
        self.header_canvas.create_window(
            tx0 + s(95),
            s(18),
            window=self.lbl_clock,
            anchor="w",
        )
        self.header_canvas.create_text(
            tx0,
            s(37),
            text="25時、ナイトコードで。 // AUDIO BRIDGE",
            fill=FG_MUTED,
            font=("Microsoft YaHei UI", 7),
            anchor="w",
        )

        # Right-side header controls: Tuck button + Pin toggle
        hdr_btns = tk.Frame(self.header_canvas, bg=BG_HEADER)
        self.header_canvas.create_window(
            self._win_width - s(12),
            header_h // 2,
            window=hdr_btns,
            anchor="e",
        )

        btn_tuck = tk.Button(
            hdr_btns,
            text="›",
            command=lambda: self._slide_out_drawer(force_unpin=True),
            bg=BG_BTN,
            fg=FG_SECONDARY,
            activebackground=BG_BTN_HOVER,
            activeforeground=FG_PRIMARY,
            relief="flat",
            bd=0,
            padx=s(7),
            pady=s(2),
            cursor="hand2",
            font=("Consolas", 10, "bold"),
        )
        btn_tuck.pack(side="right", padx=(s(5), 0))

        self.btn_pin = tk.Button(
            hdr_btns,
            text="◈ 自动收起",
            command=self._toggle_pin,
            bg=BG_BTN,
            fg=FG_SECONDARY,
            activebackground=BG_BTN_HOVER,
            activeforeground=FG_PRIMARY,
            relief="flat",
            bd=0,
            padx=s(8),
            pady=s(3),
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        self.btn_pin.pack(side="right")

        # Hairline separator under header
        tk.Frame(shell, bg=BG_CARD_BORDER, height=1).pack(fill="x")

        outer = tk.Frame(shell, bg=BG_MAIN, padx=s(12), pady=s(10))
        outer.pack(fill="both", expand=True)

        # ---- Card 1: AirPods / Headphones Battery (N25 Unit Purple #884499) ----
        card_ap_wrap, card_ap = _make_nightcord_card(outer, N25_UNIT, self._dpi)
        card_ap_wrap.pack(fill="x", pady=(0, s(8)))

        ap_head = tk.Frame(card_ap, bg=BG_CARD)
        ap_head.pack(fill="x", pady=(0, s(4)))

        self.lbl_ap_title = tk.Label(
            ap_head,
            text="◈ 耳机电量",
            bg=BG_CARD,
            fg=FG_PRIMARY,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.lbl_ap_title.pack(side="left")

        self.btn_ap_menu = tk.Button(
            ap_head,
            text="换耳机 ▾",
            command=self._show_airpods_menu,
            bg=BG_BTN,
            fg=FG_SECONDARY,
            activebackground=BG_BTN_HOVER,
            activeforeground=FG_PRIMARY,
            relief="flat",
            bd=0,
            padx=s(6),
            pady=s(1),
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        self.btn_ap_menu.pack(side="right", padx=(s(6), 0))

        self.lbl_ap_badge = tk.Label(
            ap_head,
            text="扫描中...",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Microsoft YaHei UI", 8),
        )
        self.lbl_ap_badge.pack(side="right")

        self.lbl_ap_model = tk.Label(
            card_ap,
            text="",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Microsoft YaHei UI", 8),
            justify="left",
            anchor="w",
            wraplength=s(330),
        )
        self.lbl_ap_model.pack(fill="x", pady=(0, s(3)))

        self.row_ap_left = BatteryBarRow(card_ap, "左耳 (L)", self._dpi)
        self.row_ap_left.pack(fill="x", pady=s(2))

        self.row_ap_right = BatteryBarRow(card_ap, "右耳 (R)", self._dpi)
        self.row_ap_right.pack(fill="x", pady=s(2))

        self.row_ap_case = BatteryBarRow(card_ap, "充电盒", self._dpi)
        self.row_ap_case.pack(fill="x", pady=s(2))

        # ---- Card 2: iPhone Battery (Mafuyu Periwinkle #8888CC) ----
        card_ph_wrap, card_ph = _make_nightcord_card(outer, COL_MAFUYU, self._dpi)
        card_ph_wrap.pack(fill="x", pady=(0, s(8)))

        ph_head = tk.Frame(card_ph, bg=BG_CARD)
        ph_head.pack(fill="x", pady=(0, s(4)))

        self.lbl_ph_title = tk.Label(
            ph_head,
            text="◈ iPhone 终端",
            bg=BG_CARD,
            fg=FG_PRIMARY,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.lbl_ph_title.pack(side="left")

        self.lbl_ph_badge = tk.Label(
            ph_head,
            text="检测中...",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Microsoft YaHei UI", 8),
        )
        self.lbl_ph_badge.pack(side="right")

        self.row_phone = BatteryBarRow(card_ph, "手机电量", self._dpi)
        self.row_phone.pack(fill="x", pady=s(2))

        self.ph_action_frame = tk.Frame(card_ph, bg=BG_CARD)
        self.ph_action_frame.pack(fill="x", pady=(s(5), 0))

        self.lbl_ph_hint = tk.Label(
            self.ph_action_frame,
            text="",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Microsoft YaHei UI", 8),
            justify="left",
            anchor="w",
            wraplength=s(215),
        )
        self.lbl_ph_hint.pack(side="left", fill="x", expand=True)

        self.btn_ph_refresh = tk.Button(
            self.ph_action_frame,
            text="刷新",
            command=self._handle_refresh_click,
            bg=BG_BTN,
            fg=FG_PRIMARY,
            activebackground=BG_BTN_HOVER,
            activeforeground=FG_PRIMARY,
            relief="flat",
            bd=0,
            padx=s(8),
            pady=s(2),
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        self.btn_ph_refresh.pack(side="right", padx=(s(4), 0))

        self.btn_ph_pair = tk.Button(
            self.ph_action_frame,
            text="一键授权",
            command=self._handle_pair_click,
            bg=BG_BTN_PRIMARY,
            fg=FG_PRIMARY,
            activebackground=BG_BTN_PRIMARY_HOVER,
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=s(8),
            pady=s(2),
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        self.btn_ph_pair.pack(side="right")

        # ---- Card 3: Virtual Sound Card Routing (Mizuki Mauve #DDAACC) ----
        card_vsc_wrap, card_vsc = _make_nightcord_card(outer, COL_MIZUKI, self._dpi)
        card_vsc_wrap.pack(fill="x", pady=(0, s(8)))

        vsc_head = tk.Frame(card_vsc, bg=BG_CARD)
        vsc_head.pack(fill="x", pady=(0, s(4)))

        tk.Label(
            vsc_head,
            text="◈ 虚拟声卡路由",
            bg=BG_CARD,
            fg=FG_PRIMARY,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="left")

        self.lbl_vsc_badge = tk.Label(
            vsc_head,
            text="检测中...",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Microsoft YaHei UI", 8),
        )
        self.lbl_vsc_badge.pack(side="right")

        self.lbl_vsc_detail = tk.Label(
            card_vsc,
            text="正在检测系统音频设备...",
            bg=BG_CARD,
            fg=FG_SECONDARY,
            font=("Microsoft YaHei UI", 8),
            justify="left",
            anchor="w",
            wraplength=s(336),
        )
        self.lbl_vsc_detail.pack(fill="x", pady=(0, s(2)))

        self.lbl_vsc_restore = tk.Label(
            card_vsc,
            text="退出自动切回: --",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Microsoft YaHei UI", 8),
            justify="left",
            anchor="w",
            wraplength=s(336),
        )
        self.lbl_vsc_restore.pack(fill="x", pady=(0, s(5)))

        vsc_btn_row = tk.Frame(card_vsc, bg=BG_CARD)
        vsc_btn_row.pack(fill="x")

        self.btn_switch_virtual = tk.Button(
            vsc_btn_row,
            text="一键切回虚拟声卡",
            command=self._handle_switch_virtual_click,
            bg=BG_BTN_PRIMARY,
            fg=FG_PRIMARY,
            activebackground=BG_BTN_PRIMARY_HOVER,
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=s(10),
            pady=s(3),
            cursor="hand2",
            font=("Microsoft YaHei UI", 8, "bold"),
        )
        self.btn_switch_virtual.pack(side="left")

        self.btn_restore_audio = tk.Button(
            vsc_btn_row,
            text="暂时切回原声卡",
            command=self._handle_restore_audio_click,
            bg=BG_BTN,
            fg=FG_SECONDARY,
            activebackground=BG_BTN_HOVER,
            activeforeground=FG_PRIMARY,
            relief="flat",
            bd=0,
            padx=s(8),
            pady=s(3),
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        self.btn_restore_audio.pack(side="left", padx=(s(6), 0))

        self.btn_pick_virtual = tk.Button(
            vsc_btn_row,
            text="换卡 ▾",
            command=self._show_virtual_picker_menu,
            bg=BG_BTN,
            fg=FG_SECONDARY,
            activebackground=BG_BTN_HOVER,
            activeforeground=FG_PRIMARY,
            relief="flat",
            bd=0,
            padx=s(8),
            pady=s(3),
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        self.btn_pick_virtual.pack(side="right")

        # ---- Card 4: Nightcord Voice Channel / Server Telemetry (Kanade #BB6688) ----
        card_srv_wrap, card_srv = _make_nightcord_card(outer, COL_KANADE, self._dpi)
        card_srv_wrap.pack(fill="x", pady=(0, s(9)))

        url_row = tk.Frame(card_srv, bg=BG_CARD)
        url_row.pack(fill="x", pady=(0, s(4)))

        self.lbl_url = tk.Label(
            url_row,
            text="https://...",
            bg=BG_CARD,
            fg=FG_BLUE,
            font=("Consolas", 10, "bold"),
            anchor="w",
        )
        self.lbl_url.pack(side="left")

        self.btn_copy = tk.Button(
            url_row,
            text="复制地址",
            command=self._copy_url,
            bg=BG_BTN,
            fg=FG_PRIMARY,
            activebackground=BG_BTN_HOVER,
            activeforeground=FG_PRIMARY,
            relief="flat",
            bd=0,
            padx=s(8),
            pady=s(2),
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        self.btn_copy.pack(side="right")

        self.lbl_device = tk.Label(
            card_srv,
            text="电脑播放设备: 初始化中...",
            bg=BG_CARD,
            fg=FG_SECONDARY,
            font=("Microsoft YaHei UI", 8),
            justify="left",
            anchor="w",
            wraplength=s(336),
        )
        self.lbl_device.pack(fill="x", pady=(s(1), s(1)))

        self.lbl_pc_audio = tk.Label(
            card_srv,
            text="电脑音频采集: --",
            bg=BG_CARD,
            fg=FG_SECONDARY,
            font=("Microsoft YaHei UI", 8),
            justify="left",
            anchor="w",
            wraplength=s(336),
        )
        self.lbl_pc_audio.pack(fill="x", pady=(s(1), 0))

        self.lbl_iphone_audio = tk.Label(
            card_srv,
            text="iPhone 播放采样率: 等待连接",
            bg=BG_CARD,
            fg=FG_SECONDARY,
            font=("Microsoft YaHei UI", 8),
            justify="left",
            anchor="w",
            wraplength=s(336),
        )
        self.lbl_iphone_audio.pack(fill="x", pady=(s(1), 0))

        self.lbl_mic_audio = tk.Label(
            card_srv,
            text="iPhone 麦克风: 未开启",
            bg=BG_CARD,
            fg=FG_SECONDARY,
            font=("Microsoft YaHei UI", 8),
            justify="left",
            anchor="w",
            wraplength=s(336),
        )
        self.lbl_mic_audio.pack(fill="x", pady=(s(1), 0))

        self.lbl_mic_route = tk.Label(
            card_srv,
            text="电脑麦克风回传: --",
            bg=BG_CARD,
            fg=FG_SECONDARY,
            font=("Microsoft YaHei UI", 8),
            justify="left",
            anchor="w",
            wraplength=s(336),
        )
        self.lbl_mic_route.pack(fill="x", pady=(s(1), s(3)))

        self.lbl_conn = tk.Label(
            card_srv,
            text="VOICE CH // 等待连接   |   MIC: OFF",
            bg=BG_CARD,
            fg=FG_SECONDARY,
            font=("Microsoft YaHei UI", 8, "bold"),
            anchor="w",
        )
        self.lbl_conn.pack(fill="x")

        # ---- Footer ----
        footer = tk.Frame(outer, bg=BG_MAIN)
        footer.pack(fill="x")

        self.lbl_footer_hint = tk.Label(
            footer,
            text="贴右缘呼出 · 移开收起",
            bg=BG_MAIN,
            fg=FG_MUTED,
            font=("Microsoft YaHei UI", 8),
        )
        self.lbl_footer_hint.pack(side="left")

        tk.Button(
            footer,
            text="退出程序",
            command=self._confirm_quit,
            bg=BG_BTN_DANGER,
            fg=FG_RED,
            activebackground="#452233",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=s(9),
            pady=s(2),
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        ).pack(side="right")

        self.btn_restart = tk.Button(
            footer,
            text="重启服务",
            command=self._handle_restart_click,
            bg=BG_BTN,
            fg=FG_PRIMARY,
            activebackground=BG_BTN_HOVER,
            activeforeground=FG_PRIMARY,
            relief="flat",
            bd=0,
            padx=s(9),
            pady=s(2),
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        self.btn_restart.pack(side="right", padx=(0, s(5)))

        self.btn_autostart = tk.Button(
            footer,
            text="○ 开机自启",
            command=self._handle_toggle_autostart,
            bg=BG_BTN,
            fg=FG_SECONDARY,
            activebackground=BG_BTN_HOVER,
            activeforeground=FG_PRIMARY,
            relief="flat",
            bd=0,
            padx=s(8),
            pady=s(2),
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        self.btn_autostart.pack(side="right", padx=(0, s(5)))

        self._fit_window_height(initial=True)
        _configure_borderless_toolwindow(root)

        # Start tucked away off-screen / hidden from taskbar; appears when cursor hits right edge
        root.withdraw()
        self._drawer_visible = False

    # ========================================================================
    # Right-Edge Auto-Slide Drawer Engine
    # ========================================================================
    def _slide_in_drawer(self, ref_x: int = None, ref_y: int = None, manual: bool = False):
        if not self.root or self._quitting:
            return
        if manual:
            self._manual_grace_until = time.monotonic() + 3.0
        if self._drawer_visible and not self._animating:
            self.root.lift()
            self.root.attributes("-topmost", True)
            return

        tx, ty, mr = self._compute_dock_position(ref_x, ref_y)
        self._dock_x, self._dock_y, self._mon_right = tx, ty, mr
        self._outside_since = None
        self._drawer_visible = True
        self._animating = True

        h = max(int(430 * self._dpi), self._last_height)
        start_x = mr - int(round(12 * self._dpi))
        self.root.geometry(f"{self._win_width}x{h}+{start_x}+{ty}")
        self.root.deiconify()
        _configure_borderless_toolwindow(self.root)
        self.root.lift()
        self.root.attributes("-topmost", True)

        steps = 9

        def _step(idx: int):
            if self._quitting or not self.root or not self._drawer_visible:
                self._animating = False
                return
            t = idx / steps
            ease = 1.0 - (1.0 - t) ** 3
            cur_x = int(round(start_x + (tx - start_x) * ease))
            cur_h = max(int(430 * self._dpi), self._last_height)
            self.root.geometry(f"{self._win_width}x{cur_h}+{cur_x}+{self._dock_y}")
            if idx < steps:
                self.root.after(15, lambda: _step(idx + 1))
            else:
                self._animating = False

        _step(1)

    def _slide_out_drawer(self, force_unpin: bool = False):
        if not self.root or self._quitting:
            return
        if force_unpin and self._pinned:
            self._set_pinned(False)
            # Suppress immediate re-trigger for 0.6s when manually dismissed
            self._suppress_edge_until = time.monotonic() + 0.6
        elif force_unpin:
            self._suppress_edge_until = time.monotonic() + 0.6

        if not self._drawer_visible:
            return

        self._drawer_visible = False
        self._outside_since = None
        self._edge_dwell_since = None
        self._animating = True

        start_x = self._dock_x
        end_x = self._mon_right
        steps = 7

        def _step(idx: int):
            if self._quitting or not self.root or self._drawer_visible:
                self._animating = False
                return
            t = idx / steps
            ease = t * t
            cur_x = int(round(start_x + (end_x - start_x) * ease))
            cur_h = max(int(430 * self._dpi), self._last_height)
            if idx < steps:
                self.root.geometry(f"{self._win_width}x{cur_h}+{cur_x}+{self._dock_y}")
                self.root.after(14, lambda: _step(idx + 1))
            else:
                self._animating = False
                self.root.withdraw()

        _step(1)

    def _poll_edge_hover(self):
        if self._quitting or not self.root:
            return

        try:
            now = time.monotonic()
            cx, cy = _get_cursor_pos(self.root)

            if not self._drawer_visible:
                if now >= self._suppress_edge_until:
                    _ml, _mt, mr, _mb, wt, wb, is_outer = _get_monitor_geometry_at(
                        cx, cy, self.root
                    )
                    edge_tol = max(4, int(round(4 * self._dpi)))
                    # Leave top 48px (maximized app X close button) & bottom 40px (taskbar clock) untouched
                    safe_top = wt + int(round(48 * self._dpi))
                    safe_bot = wb - int(round(40 * self._dpi))
                    at_right_edge = (
                        is_outer
                        and cx >= (mr - edge_tol)
                        and safe_top <= cy <= safe_bot
                    )
                    if at_right_edge:
                        if self._edge_dwell_since is None:
                            self._edge_dwell_since = now
                        elif (now - self._edge_dwell_since) >= 0.075:
                            self._edge_dwell_since = None
                            self._slide_in_drawer(cx, cy, manual=False)
                    else:
                        self._edge_dwell_since = None
            else:
                if (
                    not self._pinned
                    and not self._popup_menu_open
                    and not self._animating
                    and now >= self._manual_grace_until
                ):
                    pad = int(round(22 * self._dpi))
                    cur_h = max(int(430 * self._dpi), self._last_height)
                    inside_zone = (
                        (self._dock_x - pad) <= cx <= (self._mon_right + 4)
                        and (self._dock_y - pad) <= cy <= (self._dock_y + cur_h + pad)
                    )
                    if inside_zone:
                        self._outside_since = None
                    else:
                        if self._outside_since is None:
                            self._outside_since = now
                        elif (now - self._outside_since) >= 0.26:
                            self._slide_out_drawer()
        except Exception:
            pass
        finally:
            if not self._quitting and self.root:
                self.root.after(40, self._poll_edge_hover)

    def _set_pinned(self, pinned: bool):
        self._pinned = bool(pinned)
        if self._pinned:
            self.btn_pin.config(
                bg=BG_BTN_PRIMARY,
                fg="#ffffff",
                text="◈ 已常驻",
            )
            self.lbl_footer_hint.config(text="已固定面板常驻")
        else:
            self.btn_pin.config(
                bg=BG_BTN,
                fg=FG_SECONDARY,
                text="◈ 自动收起",
            )
            self.lbl_footer_hint.config(text="贴右缘呼出 · 移开收起")
        if self.tray_icon:
            try:
                self.tray_icon.update_menu()
            except Exception:
                pass

    def _toggle_pin(self):
        self._set_pinned(not self._pinned)
        if self._pinned and not self._drawer_visible:
            self._slide_in_drawer(manual=True)

    # ========================================================================
    # Interactive Controls & Menus
    # ========================================================================
    def _copy_url(self):
        state = self._get_state()
        url = state.get("url", "")
        if not url or not self.root:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self.btn_copy.config(text="✓ 已复制", fg=FG_GREEN)
        if self._copy_reset_after:
            self.root.after_cancel(self._copy_reset_after)
        self._copy_reset_after = self.root.after(
            1500, lambda: self.btn_copy.config(text="复制地址", fg=FG_PRIMARY)
        )

    def _flash_switch_badge(self, text: str):
        self._switch_flash_text = text
        if self._switch_reset_after and self.root:
            self.root.after_cancel(self._switch_reset_after)
        if self.root:
            self._switch_reset_after = self.root.after(
                1400, lambda: setattr(self, "_switch_flash_text", "")
            )

    def _handle_switch_virtual_click(self):
        if self._on_switch_virtual:
            self._on_switch_virtual()
        self._flash_switch_badge("✓ 已切回虚拟声卡")

    def _handle_restore_audio_click(self):
        if self._on_restore_audio:
            self._on_restore_audio()
        self._flash_switch_badge("✓ 已恢复原声卡")

    def _show_virtual_picker_menu(self):
        if not self.root or not self._on_switch_virtual:
            return
        state = self._get_state()
        audio_sw = state.get("audioSwitch", {})
        choices = audio_sw.get("virtualRenderChoices") or []
        if not choices:
            return

        current_vid = audio_sw.get("virtualRenderId", "")
        menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=BG_CARD,
            fg=FG_PRIMARY,
            activebackground=BG_BTN_PRIMARY,
            activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 8),
            bd=1,
            relief="solid",
        )
        for ch in choices:
            cid = ch.get("id", "")
            cname = ch.get("name", "")
            tag = " (独立不串音)" if ch.get("tier") == 1 else ""
            prefix = "✓ " if cid == current_vid else "    "
            menu.add_command(
                label=f"{prefix}{cname}{tag}",
                command=lambda target_id=cid: self._select_virtual_render(target_id),
            )
        self._popup_menu_open = True
        try:
            bx = self.btn_pick_virtual.winfo_rootx()
            by = self.btn_pick_virtual.winfo_rooty() + self.btn_pick_virtual.winfo_height()
            menu.tk_popup(bx, by)
        finally:
            menu.grab_release()
            self._popup_menu_open = False
            self._manual_grace_until = max(self._manual_grace_until, time.monotonic() + 0.5)

    def _select_virtual_render(self, render_id: str):
        if self._on_switch_virtual:
            self._on_switch_virtual(render_id)
        self._flash_switch_badge("✓ 已切换虚拟声卡")

    def _show_airpods_menu(self):
        if not self.root:
            return
        state = self._get_state()
        ap = state.get("battery", {}).get("airpods", {})
        candidates = ap.get("candidates") or []
        locked = bool(ap.get("locked"))
        has_active = bool(ap.get("activeKey") or ap.get("updatedAt") or locked)

        menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=BG_CARD,
            fg=FG_PRIMARY,
            activebackground=BG_BTN_PRIMARY,
            activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 8),
            bd=1,
            relief="solid",
        )

        if candidates:
            for cand in candidates:
                ckey = cand.get("key", "")
                title = cand.get("name") or cand.get("model") or "AirPods"
                if cand.get("isSingle"):
                    s_pct = cand.get("single")
                    bat_str = f"{s_pct}%" if s_pct is not None else "--%"
                else:
                    l_p = f"{cand['left']}%" if cand.get("left") is not None else "--"
                    r_p = f"{cand['right']}%" if cand.get("right") is not None else "--"
                    bat_str = f"L {l_p} R {r_p}"
                rssi = cand.get("rssi")
                rssi_str = f" ({rssi} dBm)" if rssi is not None else ""
                ignored_tag = " [已屏蔽]" if cand.get("ignored") else ""
                prefix = "✓ " if cand.get("isCurrent") else "    "
                menu.add_command(
                    label=f"{prefix}{title} · {bat_str}{rssi_str}{ignored_tag}",
                    command=lambda k=ckey: self._on_select_airpods and self._on_select_airpods(k),
                )
        else:
            menu.add_command(label="附近暂未收到耳机开盖广播", state="disabled")

        menu.add_separator()
        menu.add_command(
            label="🚫 这不是我的耳机 (屏蔽当前并重搜)",
            state="normal" if (has_active and self._on_reject_airpods) else "disabled",
            command=lambda: self._on_reject_airpods and self._on_reject_airpods(),
        )
        menu.add_command(
            label="🔓 解除耳机绑定 (允许识别新耳机)",
            state="normal" if (locked and self._on_unlock_airpods) else "disabled",
            command=lambda: self._on_unlock_airpods and self._on_unlock_airpods(),
        )

        self._popup_menu_open = True
        try:
            bx = self.btn_ap_menu.winfo_rootx()
            by = self.btn_ap_menu.winfo_rooty() + self.btn_ap_menu.winfo_height()
            menu.tk_popup(bx, by)
        finally:
            menu.grab_release()
            self._popup_menu_open = False
            self._manual_grace_until = max(self._manual_grace_until, time.monotonic() + 0.5)

    def _handle_refresh_click(self):
        if self._on_refresh_iphone:
            self._on_refresh_iphone()
        self.btn_ph_refresh.config(text="刷新中...")
        self.root.after(1200, lambda: self.btn_ph_refresh.config(text="刷新"))

    def _handle_pair_click(self):
        if self._on_pair_iphone:
            self._on_pair_iphone()
        else:
            self._open_bluetooth_settings()

    def _apply_autostart_style(self, enabled: bool):
        prev = self._autostart_enabled
        self._autostart_enabled = bool(enabled)
        if self._autostart_enabled:
            self.btn_autostart.config(
                text="✓ 开机自启",
                bg="#281F3A",
                fg=FG_GREEN,
                activebackground=BG_BTN_HOVER,
            )
        else:
            self.btn_autostart.config(
                text="○ 开机自启",
                bg=BG_BTN,
                fg=FG_SECONDARY,
                activebackground=BG_BTN_HOVER,
            )
        if prev != self._autostart_enabled and self.tray_icon:
            try:
                self.tray_icon.update_menu()
            except Exception:
                pass

    def _handle_toggle_autostart(self):
        if not self._on_toggle_autostart:
            return
        try:
            enabled = bool(self._on_toggle_autostart())
            self._apply_autostart_style(enabled)
        except Exception as e:
            print("Failed to toggle autostart:", e)

    def _handle_restart_click(self):
        if not self._on_restart:
            return
        self.btn_restart.config(text="正在重启...", state="disabled")
        self._on_restart()

    def _confirm_quit(self):
        if self._quitting:
            return
        self._quitting = True
        if self._on_quit:
            self._on_quit()
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        if self.root:
            try:
                self.root.quit()
                self.root.destroy()
            except Exception:
                pass

    def _process_commands(self):
        while True:
            try:
                cmd = self._cmd_queue.get_nowait()
            except queue.Empty:
                break

            if cmd == "toggle":
                if self._drawer_visible:
                    self._slide_out_drawer(force_unpin=True)
                else:
                    self._slide_in_drawer(manual=True)
            elif cmd == "show":
                self._slide_in_drawer(manual=True)
            elif cmd == "toggle_pin":
                self._toggle_pin()
            elif cmd == "toggle_autostart":
                self._handle_toggle_autostart()
            elif cmd == "quit":
                self._confirm_quit()
                return
            elif cmd == "restart_failed":
                self.btn_restart.config(text="重启失败", state="normal")
                self.root.after(
                    2500, lambda: self.btn_restart.config(text="重启服务")
                )

    def _format_pod_value(self, pct, charging: bool, online: bool):
        if pct is None:
            return "--%", FG_MUTED
        suffix = " ⚡" if charging else ""
        text = f"{pct}%{suffix}"
        color = _battery_text_color(pct) if online else FG_SECONDARY
        return text, color

    def _poll_commands(self):
        if self._quitting or not self.root:
            return
        self._process_commands()
        if not self._quitting and self.root:
            self.root.after(100, self._poll_commands)

    def _refresh_ui(self):
        if self._quitting or not self.root:
            return

        try:
            self.lbl_clock.config(text=_nightcord_clock_str())

            state = self._get_state()
            if "autostart" in state:
                self._apply_autostart_style(bool(state.get("autostart")))

            bat = state.get("battery", {})
            ap = bat.get("airpods", {})
            ph = bat.get("iphone", {})
            row_pad = int(round(2 * self._dpi))
            wrap_wide = int(round(290 * self._dpi))
            wrap_narrow = int(round(215 * self._dpi))

            # 1. Update AirPods card
            ap_online = ap.get("online", False)
            ap_locked = ap.get("locked", False)
            ap_model = ap.get("model") or "耳机电量 (AirPods / Beats)"
            ap_name = ap.get("name") or ap_model
            self.lbl_ap_title.config(text=f"◈ {ap_name}")
            self.lbl_ap_model.config(
                text=f"型号: {ap_model}" if ap_name != ap_model else ""
            )
            self.btn_ap_menu.config(text="🔒 换耳机 ▾" if ap_locked else "换耳机 ▾")

            if ap_online:
                wearing = ap.get("leftInEar") or ap.get("rightInEar")
                badge_txt = "● 佩戴中" if wearing else "● 信号在线"
                self.lbl_ap_badge.config(text=badge_txt, fg=FG_GREEN)
            elif ap.get("updatedAt"):
                self.lbl_ap_badge.config(text="休眠 / 已入盒", fg=FG_MUTED)
            elif ap_locked:
                self.lbl_ap_badge.config(text="已锁定 · 等待开盖", fg=FG_MUTED)
            else:
                self.lbl_ap_badge.config(text="开盖自动绑定", fg=FG_MUTED)

            if ap.get("isSingle"):
                val_txt, col = self._format_pod_value(
                    ap.get("single"), ap.get("singleCharging", False), ap_online
                )
                self.row_ap_left.set_value("耳机电量", ap.get("single"), val_txt, col)
                self.row_ap_right.pack_forget()
                self.row_ap_case.pack_forget()
            else:
                if not self.row_ap_right.winfo_manager():
                    self.row_ap_right.pack(fill="x", pady=row_pad)
                if not self.row_ap_case.winfo_manager():
                    self.row_ap_case.pack(fill="x", pady=row_pad)

                l_txt, l_col = self._format_pod_value(
                    ap.get("left"), ap.get("leftCharging", False), ap_online
                )
                r_txt, r_col = self._format_pod_value(
                    ap.get("right"), ap.get("rightCharging", False), ap_online
                )
                self.row_ap_left.set_value("左耳 (L)", ap.get("left"), l_txt, l_col)
                self.row_ap_right.set_value("右耳 (R)", ap.get("right"), r_txt, r_col)

                case_pct = ap.get("case")
                last_case = ap.get("lastCase")
                if case_pct is not None:
                    c_suf = " ⚡" if ap.get("caseCharging") else ""
                    self.row_ap_case.set_value(
                        "充电盒",
                        case_pct,
                        f"{case_pct}%{c_suf}",
                        _battery_text_color(case_pct),
                    )
                elif last_case is not None:
                    self.row_ap_case.set_value(
                        "充电盒", last_case, f"{last_case}% (合盖)", FG_MUTED
                    )
                else:
                    self.row_ap_case.set_value("充电盒", None, "盒盖关闭", FG_MUTED)

            # 2. Update iPhone card
            ph_name = ph.get("name") or "iPhone"
            ph_level = ph.get("level")
            ph_state = ph.get("state", "unpaired")
            self.lbl_ph_title.config(text=f"◈ {ph_name}")

            if ph_state in ("connected", "shortcut") and ph_level is not None:
                chg = " ⚡" if ph.get("charging") else ""
                src = ph.get("source") or "已同步"
                self.lbl_ph_badge.config(text=f"● {src}", fg=FG_GREEN)
                self.row_phone.set_value(
                    "手机电量", ph_level, f"{ph_level}%{chg}", _battery_text_color(ph_level)
                )
                self.lbl_ph_hint.config(
                    text=ph.get("hint") or "自动静默同步中",
                    fg=FG_MUTED,
                    wraplength=wrap_wide,
                )
                if self.btn_ph_pair.winfo_manager():
                    self.btn_ph_pair.pack_forget()
            elif ph_state == "pairing":
                self.lbl_ph_badge.config(text="等待手机确认...", fg=FG_BLUE)
                self.row_phone.set_value("手机电量", ph_level, "配对中...", FG_BLUE)
                self.lbl_ph_hint.config(
                    text=ph.get("hint", ""), fg=FG_YELLOW, wraplength=wrap_narrow
                )
                self.btn_ph_pair.config(text="授权中...", state="disabled")
                if not self.btn_ph_pair.winfo_manager():
                    self.btn_ph_pair.pack(side="right")
            elif ph_state == "disconnected":
                self.lbl_ph_badge.config(text="待唤醒", fg=FG_YELLOW)
                val_str = f"上次 {ph_level}%" if ph_level is not None else "等待连接"
                self.row_phone.set_value("手机电量", ph_level, val_str, FG_SECONDARY)
                self.lbl_ph_hint.config(
                    text=ph.get("hint", ""), fg=FG_MUTED, wraplength=wrap_wide
                )
                if self.btn_ph_pair.winfo_manager():
                    self.btn_ph_pair.pack_forget()
            elif ph_state == "needs_ble_pair":
                self.lbl_ph_badge.config(text="待授权", fg=FG_YELLOW)
                self.row_phone.set_value("手机电量", None, "待授权", FG_YELLOW)
                self.lbl_ph_hint.config(
                    text=ph.get("hint", ""), fg=FG_SECONDARY, wraplength=wrap_narrow
                )
                self.btn_ph_pair.config(text="一键授权", state="normal")
                if not self.btn_ph_pair.winfo_manager():
                    self.btn_ph_pair.pack(side="right")
            else:
                self.lbl_ph_badge.config(text="未连接", fg=FG_MUTED)
                self.row_phone.set_value("手机电量", None, "未连接", FG_MUTED)
                self.lbl_ph_hint.config(
                    text=ph.get("hint", ""), fg=FG_MUTED, wraplength=wrap_narrow
                )
                self.btn_ph_pair.config(text="一键授权", state="normal")
                if not self.btn_ph_pair.winfo_manager():
                    self.btn_ph_pair.pack(side="right")

            # 3. Update Virtual Sound Card card
            audio_sw = state.get("audioSwitch", {})
            v_avail = audio_sw.get("virtualAvailable", False)
            on_virt = audio_sw.get("isOnVirtual", False)
            choices = audio_sw.get("virtualRenderChoices") or []

            if self._switch_flash_text:
                self.lbl_vsc_badge.config(text=self._switch_flash_text, fg=FG_GREEN)
            elif not v_avail:
                self.lbl_vsc_badge.config(text="未检测到虚拟声卡", fg=FG_MUTED)
            elif on_virt:
                self.lbl_vsc_badge.config(text="● 虚拟声卡生效中", fg=FG_GREEN)
            else:
                self.lbl_vsc_badge.config(text="● 已手动切离 (可一键切回)", fg=FG_YELLOW)

            if not v_avail:
                self.lbl_vsc_detail.config(
                    text="未发现虚拟扬声器或 VB-Cable，请先安装虚拟声卡驱动",
                    fg=FG_MUTED,
                )
                self.btn_switch_virtual.config(state="disabled", bg=BG_BTN, fg=FG_MUTED)
                self.btn_restore_audio.config(state="disabled")
            else:
                self.btn_switch_virtual.config(state="normal")
                self.btn_restore_audio.config(state="normal")
                if on_virt:
                    v_r = audio_sw.get("virtualRenderName") or "未接管"
                    v_c = audio_sw.get("virtualCaptureName") or "未接管"
                    self.lbl_vsc_detail.config(
                        text=f"接管目标: {v_r}  ·  {v_c}",
                        fg=FG_SECONDARY,
                    )
                    self.btn_switch_virtual.config(
                        bg=BG_BTN,
                        fg=FG_PRIMARY,
                        activebackground=BG_BTN_HOVER,
                    )
                else:
                    cur_r = audio_sw.get("currentRenderName") or "--"
                    cur_c = audio_sw.get("currentCaptureName") or "--"
                    self.lbl_vsc_detail.config(
                        text=f"当前系统: {cur_r}  ·  {cur_c}",
                        fg=FG_YELLOW,
                    )
                    self.btn_switch_virtual.config(
                        bg=BG_BTN_PRIMARY,
                        fg="#ffffff",
                        activebackground=BG_BTN_PRIMARY_HOVER,
                    )

            orig_r = audio_sw.get("originalRenderName") or "--"
            orig_c = audio_sw.get("originalCaptureName") or "--"
            self.lbl_vsc_restore.config(text=f"退出自动切回: {orig_r}  ·  {orig_c}")

            if len(choices) > 1:
                if not self.btn_pick_virtual.winfo_manager():
                    self.btn_pick_virtual.pack(side="right")
            else:
                if self.btn_pick_virtual.winfo_manager():
                    self.btn_pick_virtual.pack_forget()

            # 4. Update Server Info card
            self.lbl_url.config(text=state.get("url", "https://127.0.0.1:8443"))
            dev_name = state.get("deviceName") or "未检测到声卡"
            rate = state.get("sampleRate") or "--"
            channels = state.get("channels") or "--"
            self.lbl_device.config(text=f"电脑播放设备: {dev_name}")
            self.lbl_pc_audio.config(
                text=f"电脑音频采集: {rate} Hz · {channels} 声道 · 16-bit PCM"
            )

            clients_cnt = state.get("clients", 0)
            mic_active = state.get("micActive", False)
            phone_playback_rate = state.get("iphonePlaybackRate")
            if phone_playback_rate:
                playback_text = f"iPhone 播放采样率: {phone_playback_rate} Hz"
            elif clients_cnt:
                playback_text = "iPhone 播放采样率: 正在读取..."
            else:
                playback_text = "iPhone 播放采样率: 未连接"
            self.lbl_iphone_audio.config(text=playback_text)

            phone_mic_input_rate = state.get("iphoneMicInputRate")
            phone_mic_context_rate = state.get("iphoneMicContextRate")
            if mic_active and phone_mic_input_rate and phone_mic_context_rate:
                phone_mic_text = (
                    f"iPhone 麦克风: {phone_mic_input_rate} Hz 采集 → "
                    f"{phone_mic_context_rate} Hz 处理"
                )
            elif mic_active and phone_mic_context_rate:
                phone_mic_text = f"iPhone 麦克风处理: {phone_mic_context_rate} Hz"
            elif mic_active:
                phone_mic_text = "iPhone 麦克风: 正在读取采样率..."
            else:
                phone_mic_text = "iPhone 麦克风: 未开启"
            self.lbl_mic_audio.config(text=phone_mic_text)

            mic_rate = state.get("micRate") or "--"
            mic_channels = state.get("micChannels") or "--"
            mic_device_name = state.get("micDeviceName") or "VB-Cable 不可用"
            self.lbl_mic_route.config(
                text=(
                    f"电脑麦克风回传: {mic_rate} Hz · {mic_channels} 声道 · "
                    f"16-bit PCM · {mic_device_name}"
                )
            )
            conn_str = (
                f"● VOICE CH ONLINE ({clients_cnt})"
                if clients_cnt
                else "○ VOICE CH STANDBY"
            )
            mic_str = "MIC: ON" if mic_active else "MIC: OFF"
            self.lbl_conn.config(
                text=f"{conn_str}   |   {mic_str}",
                fg=FG_GREEN if clients_cnt > 0 else FG_MUTED,
            )

            self._fit_window_height(initial=False)

            # 5. Update System Tray Tooltip & Icon
            ap_tip = "未检测"
            if ap.get("isSingle") and ap.get("single") is not None:
                ap_tip = f"{ap['single']}%"
            elif ap.get("left") is not None or ap.get("right") is not None:
                l_s = f"{ap['left']}%" if ap.get("left") is not None else "--"
                r_s = f"{ap['right']}%" if ap.get("right") is not None else "--"
                ap_tip = f"L {l_s} R {r_s}"

            ph_tip = f"{ph_level}%" if ph_level is not None else "未配对"
            tooltip = (
                f"25:00 // Nightcord Bridge\n耳机: {ap_tip}\niPhone: {ph_tip}"[:120]
            )

            if self.tray_icon and tooltip != self._last_tooltip:
                self._last_tooltip = tooltip
                try:
                    self.tray_icon.title = tooltip
                except Exception:
                    pass

            low_bat = any(
                v is not None and v <= 15
                for v in (ap.get("left"), ap.get("right"), ap.get("single"), ph_level)
            )
            icon_state = (clients_cnt > 0 or ap_online, low_bat)
            if self.tray_icon and icon_state != self._last_icon_state:
                self._last_icon_state = icon_state
                try:
                    self.tray_icon.icon = create_tray_icon_image(*icon_state)
                except Exception:
                    pass
        except Exception as e:
            print("UI refresh warning:", e)
        finally:
            if not self._quitting and self.root:
                self.root.after(600, self._refresh_ui)

    def run(self):
        self._build_ui()
        self._start_tray()
        self.root.after(40, self._poll_edge_hover)
        self.root.after(100, self._poll_commands)
        self.root.after(200, self._refresh_ui)
        self.root.mainloop()
