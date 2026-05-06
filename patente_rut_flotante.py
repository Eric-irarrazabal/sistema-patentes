import ctypes
import json
import random
import re
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from tkinter import messagebox
import tkinter as tk


APP_NAME = "Patente RUT Flotante"
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DB_PATH = APP_DIR / "patentes_rut.sqlite3"
CONFIG_PATH = APP_DIR / "config.json"
ERROR_ALREADY_EXISTS = 183
_SINGLE_INSTANCE_MUTEX = None

# Mantener vacio para no publicar ni sembrar datos reales.
# Las asociaciones se cargan desde la app con el boton "+".
SEED_ROWS = ()


def normalize_patente(value):
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def normalize_rut(value):
    return re.sub(r"[^0-9K]", "", (value or "").upper())


def is_plate_like(value):
    value = normalize_patente(value)
    return 5 <= len(value) <= 8 and any(c.isalpha() for c in value) and any(c.isdigit() for c in value)


def patente_candidates(raw_text):
    raw_text = raw_text or ""
    candidates = []

    for token in re.split(r"[\s,;:/|]+", raw_text.strip()):
        token = normalize_patente(token)
        if is_plate_like(token):
            candidates.append(token)

    compact = normalize_patente(raw_text)
    if is_plate_like(compact):
        candidates.append(compact)

    # Patentes chilenas mas frecuentes: 4 letras + 2 numeros o 2 letras + 4 numeros.
    for match in re.finditer(r"[A-Z]{4}\d{2}|[A-Z]{2}\d{4}", raw_text.upper()):
        candidates.append(normalize_patente(match.group(0)))

    seen = set()
    unique = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


class PlateRutDatabase:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS patente_rut (
                patente TEXT PRIMARY KEY,
                rut TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_patente_rut_rut ON patente_rut(rut)")
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO patente_rut (patente, rut)
            VALUES (?, ?)
            """,
            [(normalize_patente(p), normalize_rut(r)) for p, r in SEED_ROWS],
        )
        self.conn.commit()

    def close(self):
        self.conn.close()

    def upsert(self, patente, rut):
        patente = normalize_patente(patente)
        rut = normalize_rut(rut)
        if not is_plate_like(patente):
            raise ValueError("La patente debe tener letras y numeros.")
        if not rut:
            raise ValueError("El RUT no puede quedar vacio.")

        self.conn.execute(
            """
            INSERT INTO patente_rut (patente, rut)
            VALUES (?, ?)
            ON CONFLICT(patente) DO UPDATE SET
                rut = excluded.rut,
                updated_at = CURRENT_TIMESTAMP
            """,
            (patente, rut),
        )
        self.conn.commit()
        return patente, rut

    def lookup_patente(self, patente):
        patente = normalize_patente(patente)
        row = self.conn.execute(
            "SELECT rut FROM patente_rut WHERE patente = ?",
            (patente,),
        ).fetchone()
        return row[0] if row else None

    def lookup_text(self, raw_text):
        for candidate in patente_candidates(raw_text):
            rut = self.lookup_patente(candidate)
            if rut:
                return candidate, rut
        return None, None

    def random_patente(self):
        rows = self.conn.execute(
            """
            SELECT DISTINCT patente
            FROM patente_rut
            WHERE patente IS NOT NULL AND patente <> ''
            """
        ).fetchall()
        if not rows:
            return None
        return random.choice(rows)[0]


class Win32:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    KEYEVENTF_KEYUP = 0x0002
    VK_CONTROL = 0x11
    VK_A = 0x41
    VK_C = 0x43
    VK_G = 0x47
    VK_R = 0x52
    VK_V = 0x56
    VK_RIGHT = 0x27

    GA_ROOT = 2
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    HWND_TOPMOST = -1
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    SWP_FRAMECHANGED = 0x0020

    MOD_SHIFT = 0x0004
    MOD_CONTROL = 0x0002
    MOD_NOREPEAT = 0x4000
    WM_HOTKEY = 0x0312


if ctypes.sizeof(ctypes.c_void_p) == 8:
    _get_window_long = Win32.user32.GetWindowLongPtrW
    _set_window_long = Win32.user32.SetWindowLongPtrW
else:
    _get_window_long = Win32.user32.GetWindowLongW
    _set_window_long = Win32.user32.SetWindowLongW


class MSG(ctypes.Structure):
    _fields_ = (
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_ulong),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    )


def configure_win32_api():
    _get_window_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _get_window_long.restype = ctypes.c_ssize_t
    _set_window_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    _set_window_long.restype = ctypes.c_ssize_t

    Win32.user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    Win32.user32.OpenClipboard.restype = ctypes.c_bool
    Win32.user32.CloseClipboard.argtypes = []
    Win32.user32.CloseClipboard.restype = ctypes.c_bool
    Win32.user32.EmptyClipboard.argtypes = []
    Win32.user32.EmptyClipboard.restype = ctypes.c_bool
    Win32.user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    Win32.user32.IsClipboardFormatAvailable.restype = ctypes.c_bool
    Win32.user32.GetClipboardData.argtypes = [ctypes.c_uint]
    Win32.user32.GetClipboardData.restype = ctypes.c_void_p
    Win32.user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    Win32.user32.SetClipboardData.restype = ctypes.c_void_p

    Win32.kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    Win32.kernel32.GlobalAlloc.restype = ctypes.c_void_p
    Win32.kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    Win32.kernel32.GlobalLock.restype = ctypes.c_void_p
    Win32.kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    Win32.kernel32.GlobalUnlock.restype = ctypes.c_bool
    Win32.kernel32.GetCurrentThreadId.argtypes = []
    Win32.kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
    Win32.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    Win32.user32.GetAsyncKeyState.restype = ctypes.c_short

    Win32.user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    Win32.user32.GetAncestor.restype = ctypes.c_void_p
    Win32.user32.GetForegroundWindow.argtypes = []
    Win32.user32.GetForegroundWindow.restype = ctypes.c_void_p
    Win32.user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    Win32.user32.SetForegroundWindow.restype = ctypes.c_bool
    Win32.user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    Win32.user32.BringWindowToTop.restype = ctypes.c_bool
    Win32.user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    Win32.user32.ShowWindow.restype = ctypes.c_bool
    Win32.user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    Win32.user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    Win32.user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
    Win32.user32.AttachThreadInput.restype = ctypes.c_bool
    Win32.user32.IsWindow.argtypes = [ctypes.c_void_p]
    Win32.user32.IsWindow.restype = ctypes.c_bool
    Win32.user32.SetWindowPos.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    Win32.user32.SetWindowPos.restype = ctypes.c_bool
    Win32.user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ulong, ctypes.c_ulong]
    Win32.user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    Win32.user32.RegisterHotKey.restype = ctypes.c_bool
    Win32.user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
    Win32.user32.UnregisterHotKey.restype = ctypes.c_bool
    Win32.user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
    Win32.user32.GetMessageW.restype = ctypes.c_int
    Win32.user32.PostThreadMessageW.argtypes = [ctypes.c_ulong, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    Win32.user32.PostThreadMessageW.restype = ctypes.c_bool


configure_win32_api()


def enforce_single_instance(name):
    global _SINGLE_INSTANCE_MUTEX
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = ctypes.c_ulong
    _SINGLE_INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, name)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        sys.exit(0)


class Clipboard:
    @staticmethod
    def _open():
        for _ in range(30):
            if Win32.user32.OpenClipboard(None):
                return
            time.sleep(0.01)
        raise RuntimeError("No se pudo abrir el portapapeles.")

    @staticmethod
    def get_text():
        Clipboard._open()
        try:
            if not Win32.user32.IsClipboardFormatAvailable(Win32.CF_UNICODETEXT):
                return ""
            handle = Win32.user32.GetClipboardData(Win32.CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = Win32.kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                return ctypes.wstring_at(pointer)
            finally:
                Win32.kernel32.GlobalUnlock(handle)
        finally:
            Win32.user32.CloseClipboard()

    @staticmethod
    def set_text(text):
        text = text or ""
        encoded = (text + "\0").encode("utf-16-le")
        handle = Win32.kernel32.GlobalAlloc(Win32.GMEM_MOVEABLE, len(encoded))
        if not handle:
            raise RuntimeError("No se pudo reservar memoria para el portapapeles.")
        pointer = Win32.kernel32.GlobalLock(handle)
        if not pointer:
            raise RuntimeError("No se pudo bloquear memoria del portapapeles.")
        try:
            ctypes.memmove(pointer, encoded, len(encoded))
        finally:
            Win32.kernel32.GlobalUnlock(handle)

        Clipboard._open()
        try:
            Win32.user32.EmptyClipboard()
            if not Win32.user32.SetClipboardData(Win32.CF_UNICODETEXT, handle):
                raise RuntimeError("No se pudo escribir en el portapapeles.")
            handle = None
        finally:
            Win32.user32.CloseClipboard()


def press_hotkey(*keys):
    for key in keys:
        Win32.user32.keybd_event(key, 0, 0, 0)
        time.sleep(0.008)
    for key in reversed(keys):
        Win32.user32.keybd_event(key, 0, Win32.KEYEVENTF_KEYUP, 0)
        time.sleep(0.008)


def load_config():
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config):
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


class FloatingApp:
    def __init__(self):
        self.db = PlateRutDatabase()
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#111827")
        self.root.resizable(False, False)

        self.config = load_config()
        self.last_target_hwnd = None
        self.hotkey_thread_id = None
        self.hotkeys_running = True
        self.ctrl_watcher_running = True
        self.busy = False
        self.drag_start = None
        self.flash_job = None
        self.ctrl_taps = []
        self.ignore_ctrl_taps_until = 0

        self._build_ui()
        self._place_window()
        self.root.update_idletasks()
        self.root_hwnd = self.root.winfo_id()
        self._make_no_activate(self.root_hwnd)
        self._remember_foreground()
        self._start_hotkeys()
        self._start_ctrl_tap_watcher()

        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build_ui(self):
        self.frame = tk.Frame(self.root, bg="#111827", padx=4, pady=4)
        self.frame.pack(fill="both", expand=True)

        self.convert_button = tk.Button(
            self.frame,
            text="RUT",
            command=self.convert_active_patente,
            width=5,
            height=1,
            font=("Segoe UI", 9, "bold"),
            fg="white",
            bg="#059669",
            activeforeground="white",
            activebackground="#047857",
            relief="flat",
            bd=0,
            takefocus=0,
            padx=6,
            pady=4,
            cursor="hand2",
        )
        self.convert_button.grid(row=0, column=0, padx=(0, 4), pady=0)

        self.add_button = tk.Button(
            self.frame,
            text="+",
            command=self.open_add_dialog,
            width=3,
            height=1,
            font=("Segoe UI", 10, "bold"),
            fg="white",
            bg="#2563eb",
            activeforeground="white",
            activebackground="#1d4ed8",
            relief="flat",
            bd=0,
            takefocus=0,
            padx=6,
            pady=4,
            cursor="hand2",
        )
        self.add_button.grid(row=0, column=1, padx=0, pady=0)

        for widget in (self.root, self.frame, self.convert_button, self.add_button):
            widget.bind("<Button-3>", self._show_menu)
            widget.bind("<Alt-ButtonPress-1>", self._start_drag)
            widget.bind("<Alt-B1-Motion>", self._drag)
            widget.bind("<Alt-ButtonRelease-1>", self._end_drag)

    def _place_window(self):
        self.root.update_idletasks()
        width = 98
        height = 38
        screen_w = self.root.winfo_screenwidth()
        x = int(self.config.get("x", screen_w * 0.42))
        y = int(self.config.get("y", 46))
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _make_no_activate(self, hwnd):
        style = _get_window_long(hwnd, Win32.GWL_EXSTYLE)
        style |= Win32.WS_EX_TOOLWINDOW | Win32.WS_EX_NOACTIVATE
        _set_window_long(hwnd, Win32.GWL_EXSTYLE, style)
        Win32.user32.SetWindowPos(
            hwnd,
            Win32.HWND_TOPMOST,
            0,
            0,
            0,
            0,
            Win32.SWP_NOMOVE | Win32.SWP_NOSIZE | Win32.SWP_NOACTIVATE | Win32.SWP_FRAMECHANGED,
        )

    def _remember_foreground(self):
        hwnd = Win32.user32.GetForegroundWindow()
        root_hwnd = getattr(self, "root_hwnd", None)
        hwnd_root = Win32.user32.GetAncestor(hwnd, Win32.GA_ROOT) if hwnd else None
        own_root = Win32.user32.GetAncestor(root_hwnd, Win32.GA_ROOT) if root_hwnd else None
        if hwnd and hwnd_root != own_root:
            self.last_target_hwnd = hwnd
        self.root.after(120, self._remember_foreground)

    def _activate_target(self):
        if not self.last_target_hwnd or not Win32.user32.IsWindow(self.last_target_hwnd):
            return False

        target = self.last_target_hwnd
        current_thread = Win32.kernel32.GetCurrentThreadId()
        target_thread = Win32.user32.GetWindowThreadProcessId(target, None)
        attached = False
        try:
            if target_thread and target_thread != current_thread:
                attached = bool(Win32.user32.AttachThreadInput(current_thread, target_thread, True))
            Win32.user32.ShowWindow(target, 9)
            Win32.user32.BringWindowToTop(target)
            Win32.user32.SetForegroundWindow(target)
            time.sleep(0.08)
            return True
        finally:
            if attached:
                Win32.user32.AttachThreadInput(current_thread, target_thread, False)

    def _suppress_ctrl_taps(self, seconds=0.7):
        self.ignore_ctrl_taps_until = max(self.ignore_ctrl_taps_until, time.monotonic() + seconds)

    def _copy_active_field(self):
        original_clipboard = Clipboard.get_text()
        copied = ""

        for _ in range(3):
            sentinel = f"__PATENTE_RUT_EMPTY_{time.time_ns()}__"
            self._activate_target()
            Clipboard.set_text(sentinel)
            time.sleep(0.04)
            self._suppress_ctrl_taps()
            press_hotkey(Win32.VK_CONTROL, Win32.VK_A)
            time.sleep(0.07)
            self._suppress_ctrl_taps()
            press_hotkey(Win32.VK_CONTROL, Win32.VK_C)

            for _ in range(20):
                time.sleep(0.025)
                copied = Clipboard.get_text()
                if copied != sentinel:
                    break

            copied = "" if copied == sentinel else (copied or "").strip()
            if copied and len(copied) <= 64:
                return copied, original_clipboard
            if not copied:
                return copied, original_clipboard
            time.sleep(0.08)

        # Si se copio media pantalla, no lo usamos para evitar convertir con datos del historial.
        return "", original_clipboard

    def _replace_active_field(self, text, original_clipboard):
        self._activate_target()
        Clipboard.set_text(text)
        time.sleep(0.04)
        self._suppress_ctrl_taps()
        press_hotkey(Win32.VK_CONTROL, Win32.VK_A)
        time.sleep(0.07)
        self._suppress_ctrl_taps()
        press_hotkey(Win32.VK_CONTROL, Win32.VK_V)
        time.sleep(0.30)
        Clipboard.set_text(original_clipboard)

    def convert_active_patente(self):
        if self.busy:
            return
        self.busy = True
        try:
            raw_text, original_clipboard = self._copy_active_field()
            patente, rut = self.db.lookup_text(raw_text)
            if not rut:
                Clipboard.set_text(original_clipboard)
                self._flash("NO", "#dc2626")
                Win32.user32.MessageBeep(0xFFFFFFFF)
                return

            self._replace_active_field(rut, original_clipboard)
            self._flash("OK", "#059669")
        except Exception as exc:
            self._flash("ERR", "#dc2626")
            messagebox.showerror(APP_NAME, str(exc))
        finally:
            self.busy = False

    def open_add_dialog(self):
        try:
            prefill, original_clipboard = self._copy_active_field()
            Clipboard.set_text(original_clipboard)
        except Exception:
            prefill = ""

        patente_prefill = patente_candidates(prefill)
        dialog = AddMappingDialog(
            self.root,
            self.db,
            patente_prefill[0] if patente_prefill else "",
            self._on_saved_mapping,
        )
        dialog.show()

    def _on_saved_mapping(self, patente, rut):
        self._flash("+OK", "#2563eb")

    def copy_random_patente(self):
        if self.busy:
            return
        self.busy = True
        try:
            patente = self.db.random_patente()
            if not patente:
                self._flash("SIN", "#dc2626")
                return
            Clipboard.set_text(patente)
            self._flash("PAT", "#7c3aed")
        except Exception as exc:
            self._flash("ERR", "#dc2626")
            messagebox.showerror(APP_NAME, str(exc))
        finally:
            self.busy = False

    def _flash(self, text, color):
        if self.flash_job:
            self.root.after_cancel(self.flash_job)
        original_text = "RUT"
        original_color = "#059669"
        self.convert_button.configure(text=text, bg=color, activebackground=color)

        def restore():
            self.convert_button.configure(text=original_text, bg=original_color, activebackground="#047857")
            self.flash_job = None

        self.flash_job = self.root.after(750, restore)

    def _show_menu(self, event):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Convertir patente a RUT  Flecha derecha", command=self.convert_active_patente)
        menu.add_command(label="Agregar / actualizar  Ctrl+Shift+G", command=self.open_add_dialog)
        menu.add_command(label="Copiar patente aleatoria  Ctrl x3", command=self.copy_random_patente)
        menu.add_separator()
        menu.add_command(label="Volver a posicion inicial", command=self._reset_position)
        menu.add_command(label="Salir", command=self.close)
        menu.tk_popup(event.x_root, event.y_root)

    def _reset_position(self):
        self.config = {}
        if CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
        self._place_window()

    def _start_drag(self, event):
        self.drag_start = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _drag(self, event):
        if not self.drag_start:
            return
        start_x, start_y, window_x, window_y = self.drag_start
        x = window_x + event.x_root - start_x
        y = window_y + event.y_root - start_y
        self.root.geometry(f"+{x}+{y}")

    def _end_drag(self, _event):
        self.drag_start = None
        self.config["x"] = self.root.winfo_x()
        self.config["y"] = self.root.winfo_y()
        save_config(self.config)

    def _start_hotkeys(self):
        def worker():
            self.hotkey_thread_id = Win32.kernel32.GetCurrentThreadId()
            Win32.user32.RegisterHotKey(None, 1, Win32.MOD_NOREPEAT, Win32.VK_RIGHT)
            Win32.user32.RegisterHotKey(None, 2, Win32.MOD_CONTROL | Win32.MOD_SHIFT, Win32.VK_G)
            msg = MSG()
            while self.hotkeys_running and Win32.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == Win32.WM_HOTKEY:
                    if msg.wParam == 1:
                        self.root.after(0, self.convert_active_patente)
                    elif msg.wParam == 2:
                        self.root.after(0, self.open_add_dialog)
            Win32.user32.UnregisterHotKey(None, 1)
            Win32.user32.UnregisterHotKey(None, 2)

        threading.Thread(target=worker, daemon=True).start()

    def _start_ctrl_tap_watcher(self):
        def worker():
            last_down = False
            while self.ctrl_watcher_running:
                down = bool(Win32.user32.GetAsyncKeyState(Win32.VK_CONTROL) & 0x8000)
                now = time.monotonic()
                if self.busy or now < self.ignore_ctrl_taps_until:
                    self.ctrl_taps.clear()
                    last_down = down
                    time.sleep(0.018)
                    continue
                if down and not last_down:
                    self.ctrl_taps = [tap for tap in self.ctrl_taps if now - tap <= 1.05]
                    self.ctrl_taps.append(now)
                    if len(self.ctrl_taps) >= 3:
                        self.ctrl_taps.clear()
                        self.root.after(0, self.copy_random_patente)
                        time.sleep(0.35)
                last_down = down
                time.sleep(0.018)

        threading.Thread(target=worker, daemon=True).start()

    def close(self):
        self.hotkeys_running = False
        self.ctrl_watcher_running = False
        try:
            if self.hotkey_thread_id:
                Win32.user32.PostThreadMessageW(self.hotkey_thread_id, 0x0012, 0, 0)
        except Exception:
            pass
        self.db.close()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


class AddMappingDialog:
    def __init__(self, parent, db, patente_prefill, on_saved):
        self.parent = parent
        self.db = db
        self.on_saved = on_saved
        self.window = tk.Toplevel(parent)
        self.window.title("Agregar patente y RUT")
        self.window.attributes("-topmost", True)
        self.window.resizable(False, False)
        self.window.configure(bg="#f8fafc")
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)

        self._build(patente_prefill)

    def _build(self, patente_prefill):
        outer = tk.Frame(self.window, bg="#f8fafc", padx=14, pady=12)
        outer.pack(fill="both", expand=True)

        tk.Label(outer, text="Patente", bg="#f8fafc", fg="#334155", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        self.patente_entry = tk.Entry(outer, width=20, font=("Segoe UI", 12))
        self.patente_entry.grid(row=1, column=0, sticky="ew", pady=(2, 9))
        self.patente_entry.insert(0, patente_prefill)

        tk.Label(outer, text="RUT sin formato", bg="#f8fafc", fg="#334155", font=("Segoe UI", 9, "bold")).grid(
            row=2, column=0, sticky="w"
        )
        self.rut_entry = tk.Entry(outer, width=20, font=("Segoe UI", 12))
        self.rut_entry.grid(row=3, column=0, sticky="ew", pady=(2, 12))

        buttons = tk.Frame(outer, bg="#f8fafc")
        buttons.grid(row=4, column=0, sticky="ew")

        tk.Button(
            buttons,
            text="Guardar",
            command=self.save,
            font=("Segoe UI", 9, "bold"),
            fg="white",
            bg="#2563eb",
            activeforeground="white",
            activebackground="#1d4ed8",
            relief="flat",
            padx=12,
            pady=5,
        ).pack(side="left")
        tk.Button(
            buttons,
            text="Cancelar",
            command=self.window.destroy,
            font=("Segoe UI", 9),
            fg="#0f172a",
            bg="#e2e8f0",
            activebackground="#cbd5e1",
            relief="flat",
            padx=12,
            pady=5,
        ).pack(side="left", padx=(8, 0))

        self.window.bind("<Return>", lambda _event: self.save())
        self.window.bind("<Escape>", lambda _event: self.window.destroy())

    def show(self):
        self.window.update_idletasks()
        x = self.parent.winfo_x() + 10
        y = self.parent.winfo_y() + 46
        self.window.geometry(f"+{x}+{y}")
        self.window.deiconify()
        self.window.grab_set()
        if self.patente_entry.get().strip():
            self.rut_entry.focus_set()
        else:
            self.patente_entry.focus_set()

    def save(self):
        try:
            patente, rut = self.db.upsert(self.patente_entry.get(), self.rut_entry.get())
        except ValueError as exc:
            messagebox.showwarning(APP_NAME, str(exc), parent=self.window)
            return
        self.on_saved(patente, rut)
        self.window.destroy()


def run_self_tests():
    assert normalize_patente("ABCD12") == "ABCD12"
    assert normalize_patente(" ab-cd 12 ") == "ABCD12"
    assert normalize_rut("12.345.678-K") == "12345678K"
    assert patente_candidates("ABCD12 12345678K")[0] == "ABCD12"
    assert "ZZ1234" in patente_candidates("ZZ1234")

    with tempfile.TemporaryDirectory() as temp_dir:
        db = PlateRutDatabase(Path(temp_dir) / "test.sqlite3")
        db.upsert("ABCD12", "12.345.678-K")
        assert db.lookup_patente("abcd12") == "12345678K"
        assert db.lookup_text("ABCD12")[1] == "12345678K"
        assert db.lookup_text("ABCD12 12345678K")[1] == "12345678K"
        assert db.random_patente() == "ABCD12"
        db.close()


def main():
    if "--self-test" in sys.argv:
        run_self_tests()
        print("OK")
        return

    enforce_single_instance("Local\\PatenteRUTFlotante")
    app = FloatingApp()
    app.run()


if __name__ == "__main__":
    main()
