import ctypes
import os
import queue
import sys
import threading
import tkinter as tk

from PIL import Image, ImageDraw
import pystray


# Palette matching web/index.html Apple dark style
BG_MAIN = "#121214"
BG_CARD = "#1c1c20"
BG_BAR = "#2c2c32"
BG_BTN = "#2a2a30"
BG_BTN_PRIMARY = "#0a84ff"
BG_BTN_DANGER = "#3a1d1f"

FG_PRIMARY = "#f5f5f7"
FG_SECONDARY = "#98989f"
FG_MUTED = "#636368"
FG_GREEN = "#32d74b"
FG_YELLOW = "#ffd60a"
FG_RED = "#ff453a"
FG_BLUE = "#0a84ff"


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


def _enable_dark_titlebar(window: tk.Tk):
    if sys.platform != "win32":
        return
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        if not hwnd:
            hwnd = window.winfo_id()
        value = ctypes.c_int(1)
        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20 (19 on older Win10 builds)
        for attr in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)
            )
    except Exception:
        pass


def _battery_color(pct):
    if pct is None:
        return FG_MUTED
    if pct <= 15:
        return FG_RED
    if pct <= 30:
        return FG_YELLOW
    return FG_GREEN


def create_tray_icon_image(connected: bool = False, low_battery: bool = False):
    """Draw a crisp 64x64 tray icon with Pillow."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded dark badge background
    draw.rounded_rectangle((2, 2, 61, 61), radius=15, fill=(28, 28, 34, 255))

    accent = (255, 69, 58, 255) if low_battery else (
        (50, 215, 75, 255) if connected else (10, 132, 255, 255)
    )

    # Headband arc
    draw.arc((14, 12, 50, 46), start=180, end=0, fill=(245, 245, 247, 255), width=5)

    # Left & right ear cups
    draw.rounded_rectangle((12, 27, 22, 45), radius=5, fill=accent)
    draw.rounded_rectangle((42, 27, 52, 45), radius=5, fill=accent)

    # Status indicator bar at bottom
    draw.rounded_rectangle((18, 51, 46, 56), radius=2, fill=accent)
    return img


class BatteryBarRow(tk.Frame):
    """Single row displaying label on left, pill progress bar in middle, and bold percentage on right."""

    def __init__(self, parent, label_text: str, dpi_scale: float = 1.0):
        super().__init__(parent, bg=BG_CARD)
        self._dpi = dpi_scale
        self._bar_thick = max(8, int(round(8 * dpi_scale)))
        canvas_h = max(16, int(round(18 * dpi_scale)))

        # 1. Left label
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

        # 2. Right percentage label MUST be packed before expanding canvas so Tk never clips it
        self.lbl_val = tk.Label(
            self,
            text="--%",
            bg=BG_CARD,
            fg=FG_PRIMARY,
            font=("Segoe UI Semibold", 10),
            width=10,
            anchor="e",
        )
        self.lbl_val.pack(side="right")

        # 3. Middle pill progress bar fills remaining space
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
        self._color = FG_MUTED
        self.canvas.bind("<Configure>", lambda _e: self._redraw())

    def set_value(self, label_text: str, pct, value_text: str, color: str = None):
        self.lbl_name.config(text=label_text)
        self.lbl_val.config(text=value_text, fg=color or _battery_color(pct))
        self._pct = pct
        self._color = color or _battery_color(pct)
        self._redraw()

    def _redraw(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 12 or h < 4:
            return

        thick = self._bar_thick
        pad = thick // 2 + 1
        cy = h // 2
        x0 = pad
        x1 = max(x0 + 2, w - pad)

        # Pill track background
        c.create_line(x0, cy, x1, cy, fill=BG_BAR, width=thick, capstyle="round")

        if self._pct is not None and self._pct > 0:
            clamped = max(0, min(100, self._pct))
            span = x1 - x0
            fill_end = x0 + max(2, int(round(span * clamped / 100.0)))
            c.create_line(
                x0, cy, fill_end, cy, fill=self._color, width=thick, capstyle="round"
            )


class DesktopPanel:
    """
    Lightweight Windows desktop floating panel + system tray icon for iPhone Audio Bridge.
    Runs tkinter on the calling thread and communicates with background server/BLE via thread-safe callbacks.
    """

    def __init__(
        self,
        get_state_fn,
        on_refresh_iphone_fn,
        on_quit_fn,
        on_pair_iphone_fn=None,
        on_restart_fn=None,
    ):
        self._get_state = get_state_fn
        self._on_refresh_iphone = on_refresh_iphone_fn
        self._on_quit = on_quit_fn
        self._on_pair_iphone = on_pair_iphone_fn
        self._on_restart = on_restart_fn

        self._cmd_queue = queue.Queue()
        self._always_on_top = False
        self._quitting = False
        self._last_tooltip = ""
        self._last_icon_state = None
        self._copy_reset_after = None

        self.root = None
        self.tray_icon = None

    def request_quit(self):
        self._cmd_queue.put("quit")

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

    def _toggle_topmost_from_tray(self, _icon=None, _item=None):
        self._cmd_queue.put("toggle_top")

    def _refresh_from_tray(self, _icon=None, _item=None):
        if self._on_refresh_iphone:
            self._on_refresh_iphone()

    def _bt_settings_from_tray(self, _icon=None, _item=None):
        self._open_bluetooth_settings()

    def _quit_from_tray(self, _icon=None, _item=None):
        self._cmd_queue.put("quit")

    def _start_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("显示 / 隐藏面板", self._toggle_window_from_tray, default=True),
            pystray.MenuItem(
                "窗口置顶",
                self._toggle_topmost_from_tray,
                checked=lambda _item: self._always_on_top,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("立即刷新手机电量", self._refresh_from_tray),
            pystray.MenuItem("打开 Windows 蓝牙设置", self._bt_settings_from_tray),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出 Audio Bridge", self._quit_from_tray),
        )
        self.tray_icon = pystray.Icon(
            "iphone_audio_bridge",
            create_tray_icon_image(False, False),
            "iPhone Audio Bridge",
            menu,
        )
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _fit_window_height(self, initial: bool = False):
        if not self.root:
            return
        self.root.update_idletasks()
        req_h = max(int(420 * self._dpi), self.root.winfo_reqheight())
        if not initial and abs(req_h - self._last_height) < 3:
            return
        self._last_height = req_h
        if initial:
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            pos_x = max(20, screen_w - self._win_width - int(36 * self._dpi))
            pos_y = max(20, screen_h - req_h - int(84 * self._dpi))
            self.root.geometry(f"{self._win_width}x{req_h}+{pos_x}+{pos_y}")
        else:
            self.root.geometry(f"{self._win_width}x{req_h}")

    def _build_ui(self):
        _enable_high_dpi()

        root = tk.Tk()
        self.root = root
        root.title("iPhone Audio Bridge")
        root.configure(bg=BG_MAIN)
        root.resizable(False, False)

        self._dpi = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self._win_width = int(round(385 * self._dpi))
        self._last_height = 0
        s = lambda px: int(round(px * self._dpi))

        _enable_dark_titlebar(root)
        root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

        outer = tk.Frame(root, bg=BG_MAIN, padx=s(14), pady=s(12))
        outer.pack(fill="both", expand=True)

        # ---- Header ----
        header = tk.Frame(outer, bg=BG_MAIN)
        header.pack(fill="x", pady=(0, s(10)))

        tk.Label(
            header,
            text="🎧 Audio Bridge",
            bg=BG_MAIN,
            fg=FG_PRIMARY,
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(side="left")

        self.btn_pin = tk.Button(
            header,
            text="📌 置顶",
            command=self._toggle_topmost,
            bg=BG_BTN,
            fg=FG_SECONDARY,
            activebackground="#383840",
            activeforeground=FG_PRIMARY,
            relief="flat",
            bd=0,
            padx=s(10),
            pady=s(3),
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        self.btn_pin.pack(side="right")

        # ---- Card 1: AirPods / Headphones Battery ----
        card_ap = tk.Frame(outer, bg=BG_CARD, padx=s(12), pady=s(10))
        card_ap.pack(fill="x", pady=(0, s(10)))

        ap_head = tk.Frame(card_ap, bg=BG_CARD)
        ap_head.pack(fill="x", pady=(0, s(6)))

        self.lbl_ap_title = tk.Label(
            ap_head,
            text="🎧 耳机电量",
            bg=BG_CARD,
            fg=FG_PRIMARY,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.lbl_ap_title.pack(side="left")

        self.lbl_ap_model = tk.Label(
            card_ap,
            text="",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
            wraplength=s(320),
        )
        self.lbl_ap_model.pack(fill="x", pady=(0, s(4)))

        self.lbl_ap_badge = tk.Label(
            ap_head,
            text="扫描中...",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Microsoft YaHei UI", 8),
        )
        self.lbl_ap_badge.pack(side="right")

        self.row_ap_left = BatteryBarRow(card_ap, "左耳 (L)", self._dpi)
        self.row_ap_left.pack(fill="x", pady=s(3))

        self.row_ap_right = BatteryBarRow(card_ap, "右耳 (R)", self._dpi)
        self.row_ap_right.pack(fill="x", pady=s(3))

        self.row_ap_case = BatteryBarRow(card_ap, "充电盒", self._dpi)
        self.row_ap_case.pack(fill="x", pady=s(3))

        # ---- Card 2: iPhone Battery ----
        card_ph = tk.Frame(outer, bg=BG_CARD, padx=s(12), pady=s(10))
        card_ph.pack(fill="x", pady=(0, s(10)))

        ph_head = tk.Frame(card_ph, bg=BG_CARD)
        ph_head.pack(fill="x", pady=(0, s(6)))

        self.lbl_ph_title = tk.Label(
            ph_head,
            text="📱 iPhone 电量",
            bg=BG_CARD,
            fg=FG_PRIMARY,
            font=("Microsoft YaHei UI", 10, "bold"),
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
        self.row_phone.pack(fill="x", pady=s(3))

        self.ph_action_frame = tk.Frame(card_ph, bg=BG_CARD)
        self.ph_action_frame.pack(fill="x", pady=(s(6), 0))

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
            activebackground="#3a3a44",
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
            fg="#ffffff",
            activebackground="#0070e0",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=s(8),
            pady=s(2),
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        self.btn_ph_pair.pack(side="right")

        # ---- Card 3: Audio Bridge Server Info ----
        card_srv = tk.Frame(outer, bg=BG_CARD, padx=s(12), pady=s(10))
        card_srv.pack(fill="x", pady=(0, s(10)))

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
            activebackground="#3a3a44",
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
            text="输出声卡: 初始化中...",
            bg=BG_CARD,
            fg=FG_SECONDARY,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
            wraplength=s(320),
        )
        self.lbl_device.pack(fill="x", pady=(s(2), s(2)))

        self.lbl_pc_audio = tk.Label(
            card_srv,
            text="电脑音频采集: --",
            bg=BG_CARD,
            fg=FG_SECONDARY,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
            wraplength=s(320),
        )
        self.lbl_pc_audio.pack(fill="x", pady=(s(2), 0))

        self.lbl_iphone_audio = tk.Label(
            card_srv,
            text="iPhone 播放采样率: 等待连接",
            bg=BG_CARD,
            fg=FG_SECONDARY,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
            wraplength=s(320),
        )
        self.lbl_iphone_audio.pack(fill="x", pady=(s(2), 0))

        self.lbl_mic_audio = tk.Label(
            card_srv,
            text="iPhone 麦克风: 未开启",
            bg=BG_CARD,
            fg=FG_SECONDARY,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
            wraplength=s(320),
        )
        self.lbl_mic_audio.pack(fill="x", pady=(s(2), 0))

        self.lbl_mic_route = tk.Label(
            card_srv,
            text="电脑麦克风回传: --",
            bg=BG_CARD,
            fg=FG_SECONDARY,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
            wraplength=s(320),
        )
        self.lbl_mic_route.pack(fill="x", pady=(s(2), 0))

        self.lbl_conn = tk.Label(
            card_srv,
            text="音频流: 等待连接   |   麦克风: 未开启",
            bg=BG_CARD,
            fg=FG_SECONDARY,
            font=("Microsoft YaHei UI", 8),
            anchor="w",
        )
        self.lbl_conn.pack(fill="x")

        # ---- Footer ----
        footer = tk.Frame(outer, bg=BG_MAIN)
        footer.pack(fill="x")

        tk.Label(
            footer,
            text="点右上角 × 可最小化到系统托盘",
            bg=BG_MAIN,
            fg=FG_MUTED,
            font=("Microsoft YaHei UI", 8),
        ).pack(side="left")

        tk.Button(
            footer,
            text="退出程序",
            command=self._confirm_quit,
            bg=BG_BTN_DANGER,
            fg=FG_RED,
            activebackground="#522326",
            activeforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=s(10),
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
            activebackground="#3a3a44",
            activeforeground=FG_PRIMARY,
            relief="flat",
            bd=0,
            padx=s(10),
            pady=s(2),
            cursor="hand2",
            font=("Microsoft YaHei UI", 8),
        )
        self.btn_restart.pack(side="right", padx=(0, s(5)))

        self._fit_window_height(initial=True)

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

    def _handle_restart_click(self):
        if not self._on_restart:
            return
        self.btn_restart.config(text="正在重启...", state="disabled")
        self._on_restart()

    def _toggle_topmost(self):
        self._always_on_top = not self._always_on_top
        self.root.attributes("-topmost", self._always_on_top)
        if self._always_on_top:
            self.btn_pin.config(bg=BG_BTN_PRIMARY, fg="#ffffff", text="📌 已置顶")
        else:
            self.btn_pin.config(bg=BG_BTN, fg=FG_SECONDARY, text="📌 置顶")
        if self.tray_icon:
            try:
                self.tray_icon.update_menu()
            except Exception:
                pass

    def _hide_to_tray(self):
        if self.root:
            self.root.withdraw()

    def _show_window(self):
        if not self.root:
            return
        self.root.deiconify()
        self.root.lift()
        if self._always_on_top:
            self.root.attributes("-topmost", True)

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
                if self.root.state() == "withdrawn":
                    self._show_window()
                else:
                    self._hide_to_tray()
            elif cmd == "toggle_top":
                self._toggle_topmost()
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
        color = _battery_color(pct) if online else FG_SECONDARY
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
            state = self._get_state()
            bat = state.get("battery", {})
            ap = bat.get("airpods", {})
            ph = bat.get("iphone", {})
            row_pad = int(round(3 * self._dpi))
            wrap_wide = int(round(285 * self._dpi))
            wrap_narrow = int(round(215 * self._dpi))

            # 1. Update AirPods card
            ap_online = ap.get("online", False)
            ap_model = ap.get("model") or "耳机电量 (AirPods / Beats)"
            ap_name = ap.get("name") or ap_model
            self.lbl_ap_title.config(text=f"🎧 {ap_name}")
            self.lbl_ap_model.config(
                text=f"型号: {ap_model}" if ap_name != ap_model else ""
            )

            if ap_online:
                wearing = ap.get("leftInEar") or ap.get("rightInEar")
                badge_txt = "佩戴中" if wearing else "信号在线"
                self.lbl_ap_badge.config(text=badge_txt, fg=FG_GREEN)
            elif ap.get("updatedAt"):
                self.lbl_ap_badge.config(text="休眠 / 已入盒", fg=FG_MUTED)
            else:
                self.lbl_ap_badge.config(text="开盖或佩戴后自动显示", fg=FG_MUTED)

            if ap.get("isSingle"):
                val_txt, col = self._format_pod_value(
                    ap.get("single"), ap.get("singleCharging", False), ap_online
                )
                self.row_ap_left.set_value("耳机电量", ap.get("single"), val_txt, col)
                self.row_ap_right.pack_forget()
                self.row_ap_case.pack_forget()
            else:
                if not self.row_ap_right.winfo_ismapped():
                    self.row_ap_right.pack(fill="x", pady=row_pad)
                if not self.row_ap_case.winfo_ismapped():
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
                        "充电盒", case_pct, f"{case_pct}%{c_suf}", _battery_color(case_pct)
                    )
                elif last_case is not None:
                    self.row_ap_case.set_value("充电盒", last_case, f"{last_case}% (合盖)", FG_MUTED)
                else:
                    self.row_ap_case.set_value("充电盒", None, "盒盖关闭", FG_MUTED)

            # 2. Update iPhone card
            ph_name = ph.get("name") or "iPhone"
            ph_level = ph.get("level")
            ph_state = ph.get("state", "unpaired")
            self.lbl_ph_title.config(text=f"📱 {ph_name}")

            if ph_state in ("connected", "shortcut") and ph_level is not None:
                chg = " ⚡" if ph.get("charging") else ""
                src = ph.get("source") or "已同步"
                self.lbl_ph_badge.config(text=src, fg=FG_GREEN)
                self.row_phone.set_value("手机电量", ph_level, f"{ph_level}%{chg}", _battery_color(ph_level))
                self.lbl_ph_hint.config(
                    text=ph.get("hint") or "自动静默同步中",
                    fg=FG_MUTED,
                    wraplength=wrap_wide,
                )
                if self.btn_ph_pair.winfo_ismapped():
                    self.btn_ph_pair.pack_forget()
            elif ph_state == "pairing":
                self.lbl_ph_badge.config(text="等待手机确认...", fg=FG_BLUE)
                self.row_phone.set_value("手机电量", ph_level, "配对中...", FG_BLUE)
                self.lbl_ph_hint.config(text=ph.get("hint", ""), fg=FG_YELLOW, wraplength=wrap_narrow)
                self.btn_ph_pair.config(text="授权中...", state="disabled")
                if not self.btn_ph_pair.winfo_ismapped():
                    self.btn_ph_pair.pack(side="right")
            elif ph_state == "disconnected":
                self.lbl_ph_badge.config(text="待唤醒", fg=FG_YELLOW)
                val_str = f"上次 {ph_level}%" if ph_level is not None else "等待连接"
                self.row_phone.set_value("手机电量", ph_level, val_str, FG_SECONDARY)
                self.lbl_ph_hint.config(text=ph.get("hint", ""), fg=FG_MUTED, wraplength=wrap_wide)
                if self.btn_ph_pair.winfo_ismapped():
                    self.btn_ph_pair.pack_forget()
            elif ph_state == "needs_ble_pair":
                self.lbl_ph_badge.config(text="待授权", fg=FG_YELLOW)
                self.row_phone.set_value("手机电量", None, "待授权", FG_YELLOW)
                self.lbl_ph_hint.config(text=ph.get("hint", ""), fg=FG_SECONDARY, wraplength=wrap_narrow)
                self.btn_ph_pair.config(text="一键授权", state="normal")
                if not self.btn_ph_pair.winfo_ismapped():
                    self.btn_ph_pair.pack(side="right")
            else:
                self.lbl_ph_badge.config(text="未连接", fg=FG_MUTED)
                self.row_phone.set_value("手机电量", None, "未连接", FG_MUTED)
                self.lbl_ph_hint.config(text=ph.get("hint", ""), fg=FG_MUTED, wraplength=wrap_narrow)
                self.btn_ph_pair.config(text="一键授权", state="normal")
                if not self.btn_ph_pair.winfo_ismapped():
                    self.btn_ph_pair.pack(side="right")

            self._fit_window_height(initial=False)

            # 3. Update Server Info card
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
            conn_str = f"音频流: {'✓ 已连接 (' + str(clients_cnt) + ')' if clients_cnt else '等待手机连接'}"
            mic_str = f"麦克风: {'🎤 开启中' if mic_active else '未开启'}"
            self.lbl_conn.config(
                text=f"{conn_str}   |   {mic_str}",
                fg=FG_GREEN if clients_cnt > 0 else FG_SECONDARY,
            )

            # 4. Update System Tray Tooltip & Icon
            ap_tip = "未检测"
            if ap.get("isSingle") and ap.get("single") is not None:
                ap_tip = f"{ap['single']}%"
            elif ap.get("left") is not None or ap.get("right") is not None:
                l_s = f"{ap['left']}%" if ap.get("left") is not None else "--"
                r_s = f"{ap['right']}%" if ap.get("right") is not None else "--"
                ap_tip = f"L {l_s} R {r_s}"

            ph_tip = f"{ph_level}%" if ph_level is not None else "未配对"
            tooltip = f"Audio Bridge\n耳机: {ap_tip}\niPhone: {ph_tip}"[:120]

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
        self.root.after(100, self._poll_commands)
        self.root.after(200, self._refresh_ui)
        self.root.mainloop()
