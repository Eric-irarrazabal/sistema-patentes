import json
import ctypes
import queue
import re
import socket
import sqlite3
import sys
import threading
import time
import winsound
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from urllib.parse import urlparse

import cv2
import numpy as np
from PIL import Image, ImageTk
from rapidocr_onnxruntime import RapidOCR


APP_NAME = "Lector de Patentes RTSP"
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "camera_config.json"
DB_PATH = APP_DIR / "patentes_rut.sqlite3"
ERROR_ALREADY_EXISTS = 183
_SINGLE_INSTANCE_MUTEX = None


DEFAULT_CONFIG = {
    "rtsp_url": "",
    "roi": [0.0, 0.0, 1.0, 1.0],
    "plate_polygon": [],
    "vehicle_roi": [0.0, 0.0, 1.0, 1.0],
    "ocr_interval_seconds": 0.12,
    "always_scan": False,
    "auto_read_on_vehicle": True,
    "motion_enabled": True,
    "motion_threshold": 0.025,
    "read_after_motion_delay_seconds": 0.0,
    "reading_timeout_seconds": 5.0,
    "confirmed_cooldown_seconds": 1.0,
    "restart_read_on_motion_after_confirm_seconds": 0.8,
    "recent_read_seconds": 2.0,
    "min_confirm_votes": 2,
    "min_vote_margin": 1,
    "min_ocr_score": 0.80,
    "fast_single_read_score": 0.93,
    "known_plate_single_read_score": 0.80,
    "max_candidates_per_frame": 2,
    "ocr_preprocess_variants": 1,
    "ocr_target_width": 760,
    "known_plate_refresh_seconds": 2.0,
    "require_plate_in_database": False,
    "copy_confirmed_plate_to_clipboard": True,
    "copy_provisional_plate_to_clipboard": True,
    "clear_clipboard_on_vehicle_start": True,
    "access_overlay_seconds": 2.5,
    "denied_message": "PATENTE EN LISTA DENEGADA",
    "max_frame_width": 1280,
    "open_timeout_ms": 5000,
    "read_timeout_ms": 5000,
}


PLATE_PATTERNS = (
    re.compile(r"^[A-Z]{4}[0-9]{2}$"),
    re.compile(r"^[A-Z]{2}[0-9]{4}$"),
)

DIGIT_FIX = str.maketrans(
    {
        "O": "0",
        "Q": "0",
        "D": "0",
        "I": "1",
        "L": "1",
        "Z": "2",
        "S": "5",
        "B": "8",
        "G": "6",
    }
)

LETTER_FIX = str.maketrans(
    {
        "0": "O",
        "1": "I",
        "2": "Z",
        "5": "S",
        "8": "B",
        "6": "G",
    }
)


CONFUSION_GROUPS = (
    set("0OQD"),
    set("1IL"),
    set("2Z"),
    set("5S"),
    set("8B"),
    set("6G"),
    set("7T"),
)


@dataclass
class OcrCandidate:
    text: str
    score: float
    box: list


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return dict(DEFAULT_CONFIG)

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}

    config = dict(DEFAULT_CONFIG)
    config.update(data)
    return config


def normalize_plate_text(value):
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def looks_like_plate(value):
    value = normalize_plate_text(value)
    return any(pattern.match(value) for pattern in PLATE_PATTERNS)


def chars_are_confusable(left, right):
    if left == right:
        return True
    return any(left in group and right in group for group in CONFUSION_GROUPS)


def known_plate_distance(candidate, known_plate):
    candidate = normalize_plate_text(candidate)
    known_plate = normalize_plate_text(known_plate)
    if len(candidate) != len(known_plate):
        return float("inf")

    distance = 0.0
    for left, right in zip(candidate, known_plate):
        if left == right:
            continue
        if chars_are_confusable(left, right):
            distance += 0.4
        else:
            distance += 1.0
    return distance


def correct_to_known_plate(candidate, score, known_plates):
    if not known_plates:
        return candidate, score
    if candidate in known_plates:
        return candidate, min(0.99, score + 0.06)

    ranked = sorted((known_plate_distance(candidate, plate), plate) for plate in known_plates if len(plate) == len(candidate))
    if not ranked:
        return candidate, score

    best_distance, best_plate = ranked[0]
    second_distance = ranked[1][0] if len(ranked) > 1 else float("inf")
    if best_distance <= 0.85 and second_distance - best_distance >= 0.25:
        return best_plate, min(0.99, score + 0.04)
    return candidate, score


def positional_plate_variants(value):
    value = normalize_plate_text(value)
    if len(value) != 6:
        return []

    variants = []

    # Chile: cuatro letras + dos numeros, por ejemplo ABCD12.
    first = value[:4].translate(LETTER_FIX)
    last = value[4:].translate(DIGIT_FIX)
    variants.append(first + last)

    # Chile: dos letras + cuatro numeros, por ejemplo ZY1234.
    first = value[:2].translate(LETTER_FIX)
    last = value[2:].translate(DIGIT_FIX)
    variants.append(first + last)

    unique = []
    for variant in variants:
        if variant not in unique and looks_like_plate(variant):
            unique.append(variant)
    return unique


def extract_plate_candidates(text, score, box, known_plates=None):
    raw = normalize_plate_text(text)
    if not raw:
        return []

    candidates = []
    known_plates = known_plates or set()
    pieces = [raw] if len(raw) == 6 else []

    if len(raw) > 6:
        pieces.extend(raw[index : index + 6] for index in range(0, len(raw) - 5))

    for piece in pieces:
        for index, variant in enumerate([piece, *positional_plate_variants(piece)]):
            if looks_like_plate(variant):
                adjusted_score = float(score or 0.0)
                if index > 0:
                    adjusted_score *= 0.94
                if piece != raw:
                    adjusted_score *= 0.98
                variant, adjusted_score = correct_to_known_plate(variant, adjusted_score, known_plates)
                candidates.append(OcrCandidate(variant, adjusted_score, box))

    unique = {}
    for candidate in candidates:
        current = unique.get(candidate.text)
        if current is None or candidate.score > current.score:
            unique[candidate.text] = candidate
    return sorted(unique.values(), key=lambda item: item.score, reverse=True)


def get_rut_for_plate(plate):
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT rut FROM patente_rut WHERE patente = ?", (plate,)).fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def table_exists(conn, table_name):
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)).fetchone()
    return bool(row)


def list_known_plates():
    ensure_access_table()
    ensure_denied_table()
    conn = sqlite3.connect(DB_PATH)
    plates = set()
    try:
        if table_exists(conn, "patente_rut"):
            plates.update(row[0] for row in conn.execute("SELECT patente FROM patente_rut"))
        if table_exists(conn, "access_patentes"):
            plates.update(row[0] for row in conn.execute("SELECT patente FROM access_patentes"))
        if table_exists(conn, "denied_patentes"):
            plates.update(row[0] for row in conn.execute("SELECT patente FROM denied_patentes"))
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return {normalize_db_plate(plate) for plate in plates if looks_like_plate(plate)}


def ensure_access_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS access_patentes (
            patente TEXT PRIMARY KEY,
            mensaje TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def ensure_denied_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS denied_patentes (
            patente TEXT PRIMARY KEY,
            mensaje TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def normalize_db_plate(plate):
    return normalize_plate_text(plate)


def upsert_access_plate(plate, message=""):
    plate = normalize_db_plate(plate)
    message = (message or "").strip()
    if not looks_like_plate(plate):
        raise ValueError("La patente debe tener formato ABCD12 o AB1234.")
    ensure_access_table()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO access_patentes (patente, mensaje)
        VALUES (?, ?)
        ON CONFLICT(patente) DO UPDATE SET
            mensaje = excluded.mensaje,
            updated_at = CURRENT_TIMESTAMP
        """,
        (plate, message),
    )
    conn.commit()
    conn.close()
    delete_denied_plate(plate)
    return plate


def delete_access_plate(plate):
    ensure_access_table()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM access_patentes WHERE patente = ?", (normalize_db_plate(plate),))
    conn.commit()
    conn.close()


def list_access_plates():
    ensure_access_table()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT patente, mensaje FROM access_patentes ORDER BY patente").fetchall()
    conn.close()
    return rows


def upsert_denied_plate(plate, message=""):
    plate = normalize_db_plate(plate)
    message = (message or "").strip()
    if not looks_like_plate(plate):
        raise ValueError("La patente debe tener formato ABCD12 o AB1234.")
    ensure_denied_table()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO denied_patentes (patente, mensaje)
        VALUES (?, ?)
        ON CONFLICT(patente) DO UPDATE SET
            mensaje = excluded.mensaje,
            updated_at = CURRENT_TIMESTAMP
        """,
        (plate, message),
    )
    conn.commit()
    conn.close()
    delete_access_plate(plate)
    return plate


def delete_denied_plate(plate):
    ensure_denied_table()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM denied_patentes WHERE patente = ?", (normalize_db_plate(plate),))
    conn.commit()
    conn.close()


def list_denied_plates():
    ensure_denied_table()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT patente, mensaje FROM denied_patentes ORDER BY patente").fetchall()
    conn.close()
    return rows


def get_denied_record(plate):
    ensure_denied_table()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT patente, mensaje FROM denied_patentes WHERE patente = ?",
        (normalize_db_plate(plate),),
    ).fetchone()
    conn.close()
    return row


def get_access_record(plate):
    ensure_access_table()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT patente, mensaje FROM access_patentes WHERE patente = ?",
        (normalize_db_plate(plate),),
    ).fetchone()
    conn.close()
    return row


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


class AccessListDialog:
    MODES = {
        "access": {
            "title": "PATENTES CON ACCESO",
            "save": "Guardar acceso",
            "saved": "guardada para dar acceso",
            "deleted": "borrada de acceso",
            "confirm": "Borrar {plate} de la lista de acceso?",
            "empty": "patente(s) autorizada(s).",
            "color": "#16a34a",
            "active": "#15803d",
        },
        "denied": {
            "title": "PATENTES DENEGADAS",
            "save": "Guardar denegado",
            "saved": "guardada para acceso denegado",
            "deleted": "borrada de denegados",
            "confirm": "Borrar {plate} de la lista de denegados?",
            "empty": "patente(s) denegada(s).",
            "color": "#b91c1c",
            "active": "#991b1b",
        },
    }

    def __init__(self, parent):
        self.parent = parent
        self.rows = []
        self.window = tk.Toplevel(parent)
        self.window.title("Listas de patentes")
        self.window.geometry("600x550")
        self.window.configure(bg="#0f172a")
        self.window.attributes("-topmost", True)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)

        self.mode_var = tk.StringVar(value="access")
        self.title_var = tk.StringVar()
        self.plate_var = tk.StringVar()
        self.message_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Guarda patentes autorizadas o denegadas.")

        self._build()
        self.reload()
        self.plate_entry.focus_set()

    def _build(self):
        tk.Label(
            self.window,
            textvariable=self.title_var,
            bg="#0f172a",
            fg="#f8fafc",
            font=("Segoe UI", 18, "bold"),
        ).pack(anchor="w", padx=18, pady=(16, 10))

        mode_row = tk.Frame(self.window, bg="#0f172a")
        mode_row.pack(fill="x", padx=18, pady=(0, 12))
        tk.Radiobutton(
            mode_row,
            text="DAR ACCESO",
            variable=self.mode_var,
            value="access",
            command=self.reload,
            indicatoron=False,
            bg="#16a34a",
            fg="white",
            selectcolor="#15803d",
            activebackground="#15803d",
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=8,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left")
        tk.Radiobutton(
            mode_row,
            text="DENEGAR",
            variable=self.mode_var,
            value="denied",
            command=self.reload,
            indicatoron=False,
            bg="#b91c1c",
            fg="white",
            selectcolor="#991b1b",
            activebackground="#991b1b",
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=8,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="left", padx=(8, 0))

        form = tk.Frame(self.window, bg="#0f172a")
        form.pack(fill="x", padx=18)

        tk.Label(form, text="PATENTE", bg="#0f172a", fg="#93c5fd", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        tk.Label(form, text="MENSAJE OPCIONAL", bg="#0f172a", fg="#93c5fd", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=1, sticky="w", padx=(12, 0)
        )

        self.plate_entry = tk.Entry(
            form,
            textvariable=self.plate_var,
            bg="#020617",
            fg="#f8fafc",
            insertbackground="#f8fafc",
            relief="flat",
            font=("Segoe UI", 16, "bold"),
        )
        self.plate_entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.plate_entry.bind("<Return>", lambda _event: self.save())

        self.message_entry = tk.Entry(
            form,
            textvariable=self.message_var,
            bg="#020617",
            fg="#f8fafc",
            insertbackground="#f8fafc",
            relief="flat",
            font=("Segoe UI", 12),
        )
        self.message_entry.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=(4, 0))
        self.message_entry.bind("<Return>", lambda _event: self.save())
        form.grid_columnconfigure(0, weight=1)
        form.grid_columnconfigure(1, weight=2)

        buttons = tk.Frame(self.window, bg="#0f172a")
        buttons.pack(fill="x", padx=18, pady=14)
        self.save_button = tk.Button(
            buttons,
            text="Guardar",
            command=self.save,
            bg="#16a34a",
            fg="white",
            activebackground="#15803d",
            activeforeground="white",
            relief="flat",
            padx=12,
            pady=7,
        )
        self.save_button.pack(side="left")
        tk.Button(
            buttons,
            text="Borrar",
            command=self.delete_selected,
            bg="#64748b",
            fg="white",
            activebackground="#475569",
            activeforeground="white",
            relief="flat",
            padx=12,
            pady=7,
        ).pack(side="left", padx=(8, 0))
        tk.Button(
            buttons,
            text="Recargar",
            command=self.reload,
            bg="#334155",
            fg="white",
            activebackground="#475569",
            activeforeground="white",
            relief="flat",
            padx=12,
            pady=7,
        ).pack(side="left", padx=(8, 0))
        tk.Button(
            buttons,
            text="Cerrar",
            command=self.window.destroy,
            bg="#1f2937",
            fg="white",
            activebackground="#374151",
            activeforeground="white",
            relief="flat",
            padx=12,
            pady=7,
        ).pack(side="right")

        list_frame = tk.Frame(self.window, bg="#020617")
        list_frame.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        self.listbox = tk.Listbox(
            list_frame,
            bg="#020617",
            fg="#e5e7eb",
            selectbackground="#16a34a",
            borderwidth=0,
            highlightthickness=0,
            font=("Consolas", 13),
        )
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar = tk.Scrollbar(list_frame, command=self.listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        tk.Label(
            self.window,
            textvariable=self.status_var,
            bg="#0f172a",
            fg="#cbd5e1",
            font=("Segoe UI", 10),
            wraplength=550,
            justify="left",
        ).pack(anchor="w", padx=18, pady=(0, 16))

    def _mode(self):
        return self.MODES[self.mode_var.get()]

    def reload(self):
        mode = self._mode()
        self.rows = list_access_plates() if self.mode_var.get() == "access" else list_denied_plates()
        self.title_var.set(mode["title"])
        self.save_button.configure(text=mode["save"], bg=mode["color"], activebackground=mode["active"])
        self.listbox.configure(selectbackground=mode["color"])
        self.listbox.delete(0, tk.END)
        for plate, message in self.rows:
            suffix = f" | {message.upper()}" if message else ""
            self.listbox.insert(tk.END, f"{plate:<8}{suffix}")
        self.status_var.set(f"{len(self.rows)} {mode['empty']}")

    def save(self):
        mode = self._mode()
        try:
            if self.mode_var.get() == "access":
                plate = upsert_access_plate(self.plate_var.get(), self.message_var.get())
            else:
                plate = upsert_denied_plate(self.plate_var.get(), self.message_var.get())
        except ValueError as exc:
            messagebox.showwarning(APP_NAME, str(exc), parent=self.window)
            return
        self.reload()
        self._select_plate(plate)
        self.status_var.set(f"{plate} {mode['saved']}.")

    def delete_selected(self):
        mode = self._mode()
        plate = self._current_plate() or normalize_db_plate(self.plate_var.get())
        if not plate:
            messagebox.showwarning(APP_NAME, "Selecciona o escribe una patente para borrar.", parent=self.window)
            return
        if not messagebox.askyesno(APP_NAME, mode["confirm"].format(plate=plate), parent=self.window):
            return
        if self.mode_var.get() == "access":
            delete_access_plate(plate)
        else:
            delete_denied_plate(plate)
        self.plate_var.set("")
        self.message_var.set("")
        self.reload()
        self.status_var.set(f"{plate} {mode['deleted']}.")

    def _on_select(self, _event=None):
        index = self._selected_index()
        if index is None:
            return
        plate, message = self.rows[index]
        self.plate_var.set(plate)
        self.message_var.set(message)

    def _selected_index(self):
        selection = self.listbox.curselection()
        if not selection:
            return None
        index = int(selection[0])
        return index if 0 <= index < len(self.rows) else None

    def _current_plate(self):
        index = self._selected_index()
        if index is None:
            return ""
        return self.rows[index][0]

    def _select_plate(self, plate):
        for index, row in enumerate(self.rows):
            if row[0] == plate:
                self.listbox.selection_clear(0, tk.END)
                self.listbox.selection_set(index)
                self.listbox.see(index)
                return


class CameraReader(threading.Thread):
    def __init__(self, rtsp_url, output_queue, stop_event, max_width, open_timeout_ms, read_timeout_ms):
        super().__init__(daemon=True)
        self.rtsp_url = rtsp_url
        self.output_queue = output_queue
        self.stop_event = stop_event
        self.max_width = max_width
        self.open_timeout_ms = open_timeout_ms
        self.read_timeout_ms = read_timeout_ms

    def run(self):
        if not self.rtsp_url:
            self.output_queue.put(("status", "Falta configurar rtsp_url"))
            return

        # TCP suele ser mas estable con camaras IP que UDP.
        import os

        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000|timeout;5000000"

        while not self.stop_event.is_set():
            self.output_queue.put(("status", "Conectando camara..."))
            if not self._port_is_reachable():
                self.output_queue.put(("status", "Camara no alcanzable en red/puerto RTSP. Reintentando..."))
                time.sleep(2)
                continue

            cap = cv2.VideoCapture()
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.open_timeout_ms)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.read_timeout_ms)
            cap.open(self.rtsp_url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            if not cap.isOpened():
                self.output_queue.put(("status", "No se pudo conectar. Reintentando..."))
                cap.release()
                time.sleep(2)
                continue

            self.output_queue.put(("status", "Camara conectada"))
            while not self.stop_event.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    self.output_queue.put(("status", "Se perdio video. Reintentando..."))
                    break

                frame = self._resize(frame)
                self._put_latest(("frame", frame))

            cap.release()
            time.sleep(0.5)

    def _port_is_reachable(self):
        parsed = urlparse(self.rtsp_url)
        host = parsed.hostname
        port = parsed.port or 554
        if not host:
            return False
        try:
            with socket.create_connection((host, port), timeout=max(1, self.open_timeout_ms / 1000)):
                return True
        except OSError:
            return False

    def _resize(self, frame):
        height, width = frame.shape[:2]
        if width <= self.max_width:
            return frame
        scale = self.max_width / width
        return cv2.resize(frame, (self.max_width, int(height * scale)), interpolation=cv2.INTER_AREA)

    def _put_latest(self, item):
        try:
            self.output_queue.put_nowait(item)
        except queue.Full:
            try:
                self.output_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.output_queue.put_nowait(item)
            except queue.Full:
                pass


class PlateReaderApp:
    def __init__(self):
        self.config = load_config()
        self.stop_event = threading.Event()
        self.frame_queue = queue.Queue(maxsize=4)
        self.ocr_queue = queue.Queue(maxsize=1)
        self.ocr = RapidOCR()

        self.latest_frame = None
        self.latest_display = None
        self.last_gray = None
        self.last_motion_ratio = 0.0
        self.last_motion_time = 0
        self.last_ocr_time = 0
        self.vehicle_active = False
        self.vehicle_started_at = 0
        self.vehicle_read_until = 0
        self.cooldown_until = 0
        self.confirmed_at = 0
        self.recent_reads = deque()
        self.current_candidates = []
        self.confirmed_plate = ""
        self.confirmed_rut = ""
        self.provisional_plate = ""
        self.access_overlay = None
        self.known_plates = set()
        self.known_plates_loaded_at = 0
        self.selecting_polygon = False
        self.pending_polygon = []
        self.display_info = None

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("1180x760")
        self.root.configure(bg="#0f172a")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        ensure_access_table()
        ensure_denied_table()

        self._build_ui()
        self._start_workers()
        self._tick()

    def _build_ui(self):
        self.video_canvas = tk.Canvas(self.root, bg="#020617", highlightthickness=0)
        self.video_canvas.pack(side="left", fill="both", expand=True, padx=(12, 8), pady=12)
        self.video_canvas.bind("<Button-1>", self._on_video_click)

        panel = tk.Frame(self.root, bg="#111827", width=330, padx=14, pady=14)
        panel.pack(side="right", fill="y", padx=(0, 12), pady=12)
        panel.pack_propagate(False)

        tk.Label(panel, text="Patente detectada", bg="#111827", fg="#93c5fd", font=("Segoe UI", 11, "bold")).pack(
            anchor="w"
        )
        self.plate_value = tk.Label(panel, text="---", bg="#111827", fg="#f8fafc", font=("Segoe UI", 34, "bold"))
        self.plate_value.pack(anchor="w", pady=(4, 12))

        self.rut_value = tk.Label(panel, text="", bg="#111827", fg="#a7f3d0", font=("Segoe UI", 16, "bold"))
        self.rut_value.pack(anchor="w", pady=(0, 20))

        tk.Label(panel, text="Estado", bg="#111827", fg="#cbd5e1", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.status_value = tk.Label(
            panel, text="Iniciando...", bg="#111827", fg="#e2e8f0", font=("Segoe UI", 10), wraplength=290, justify="left"
        )
        self.status_value.pack(anchor="w", pady=(4, 18))

        tk.Label(panel, text="Lecturas recientes", bg="#111827", fg="#cbd5e1", font=("Segoe UI", 10, "bold")).pack(
            anchor="w"
        )
        self.reads_list = tk.Listbox(
            panel,
            height=8,
            bg="#020617",
            fg="#e5e7eb",
            selectbackground="#2563eb",
            borderwidth=0,
            highlightthickness=0,
            font=("Consolas", 12),
        )
        self.reads_list.pack(fill="x", pady=(6, 18))

        button_row = tk.Frame(panel, bg="#111827")
        button_row.pack(fill="x", pady=(0, 12))
        tk.Button(
            button_row,
            text="Definir zona",
            command=self.start_polygon_selection,
            bg="#7c3aed",
            fg="white",
            activebackground="#6d28d9",
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=6,
        ).pack(side="left")
        tk.Button(
            button_row,
            text="Guardar zona",
            command=self.save_polygon_selection,
            bg="#059669",
            fg="white",
            activebackground="#047857",
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=6,
        ).pack(side="left", padx=(8, 0))

        button_row2 = tk.Frame(panel, bg="#111827")
        button_row2.pack(fill="x", pady=(0, 12))
        tk.Button(
            button_row2,
            text="Leer ahora",
            command=self.force_ocr,
            bg="#2563eb",
            fg="white",
            activebackground="#1d4ed8",
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=6,
        ).pack(side="left")
        tk.Button(
            button_row2,
            text="Limpiar",
            command=self.clear_reads,
            bg="#374151",
            fg="white",
            activebackground="#4b5563",
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=6,
        ).pack(side="left", padx=(8, 0))
        tk.Button(
            button_row2,
            text="Borrar zona",
            command=self.clear_plate_polygon,
            bg="#991b1b",
            fg="white",
            activebackground="#7f1d1d",
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=6,
        ).pack(side="left", padx=(8, 0))

        button_row3 = tk.Frame(panel, bg="#111827")
        button_row3.pack(fill="x", pady=(0, 12))
        tk.Button(
            button_row3,
            text="Listas",
            command=self.open_access_dialog,
            bg="#16a34a",
            fg="white",
            activebackground="#15803d",
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=6,
        ).pack(side="left")

        help_text = (
            "Modo estricto: define una zona poligonal para leer solo donde esta la patente. "
            "Al confirmar, copia la patente y muestra acceso autorizado o denegado."
        )
        tk.Label(panel, text=help_text, bg="#111827", fg="#94a3b8", font=("Segoe UI", 9), wraplength=290, justify="left").pack(
            anchor="w", side="bottom"
        )

    def _start_workers(self):
        CameraReader(
            self.config["rtsp_url"],
            self.frame_queue,
            self.stop_event,
            int(self.config["max_frame_width"]),
            int(self.config["open_timeout_ms"]),
            int(self.config["read_timeout_ms"]),
        ).start()
        threading.Thread(target=self._ocr_worker, daemon=True).start()

    def force_ocr(self):
        if self.latest_frame is not None:
            self._queue_ocr(self.latest_frame.copy())

    def open_access_dialog(self):
        AccessListDialog(self.root)

    def start_polygon_selection(self):
        self.selecting_polygon = True
        self.pending_polygon = []
        self.status_value.configure(text="Marca la zona de patente con clics sobre el video. Luego presiona Guardar zona.")

    def save_polygon_selection(self):
        if len(self.pending_polygon) < 3:
            messagebox.showwarning(APP_NAME, "Marca al menos 3 puntos para guardar la zona.", parent=self.root)
            return
        self.config["plate_polygon"] = self.pending_polygon[:]
        self._save_config()
        self.selecting_polygon = False
        self.pending_polygon = []
        self.status_value.configure(text="Zona de patente guardada. El OCR solo leera dentro de ese poligono.")

    def clear_plate_polygon(self):
        self.config["plate_polygon"] = []
        self.pending_polygon = []
        self.selecting_polygon = False
        self._save_config()
        self.status_value.configure(text="Zona de patente borrada. El OCR volvera a usar el rectangulo ROI completo.")

    def _save_config(self):
        CONFIG_PATH.write_text(json.dumps(self.config, indent=2), encoding="utf-8")

    def _on_video_click(self, event):
        if not self.selecting_polygon or self.latest_frame is None or not self.display_info:
            return

        info = self.display_info
        x = event.x - info["offset_x"]
        y = event.y - info["offset_y"]
        if x < 0 or y < 0 or x > info["display_w"] or y > info["display_h"]:
            return

        frame_x = x / info["scale"]
        frame_y = y / info["scale"]
        normalized = [
            max(0.0, min(1.0, frame_x / info["frame_w"])),
            max(0.0, min(1.0, frame_y / info["frame_h"])),
        ]
        self.pending_polygon.append(normalized)
        self.status_value.configure(text=f"Puntos zona patente: {len(self.pending_polygon)}. Presiona Guardar zona al terminar.")

    def clear_reads(self):
        self.recent_reads.clear()
        self.current_candidates = []
        self.confirmed_plate = ""
        self.confirmed_rut = ""
        self.provisional_plate = ""
        self.vehicle_active = False
        self.cooldown_until = 0
        self.confirmed_at = 0
        self.plate_value.configure(text="---")
        self.rut_value.configure(text="")
        self.reads_list.delete(0, tk.END)

    def _tick(self):
        self._drain_events()
        self._maybe_queue_ocr()
        self._drain_events()
        self._render_frame()
        self.root.after(33, self._tick)

    def _drain_events(self):
        while True:
            try:
                kind, payload = self.frame_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.status_value.configure(text=payload)
            elif kind == "frame":
                self.latest_frame = payload
                self._update_motion(payload)
            elif kind == "ocr_result":
                self._handle_candidates(payload)

    def _update_motion(self, frame):
        if not self.config["motion_enabled"]:
            self.last_motion_time = time.time()
            return

        vehicle_area, _offset = self._crop_configured_roi(frame, self.config["vehicle_roi"])
        small = cv2.resize(vehicle_area, (320, 180), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (9, 9), 0)

        if self.last_gray is None:
            self.last_gray = gray
            return

        diff = cv2.absdiff(self.last_gray, gray)
        changed = float(np.count_nonzero(diff > 28)) / diff.size
        self.last_motion_ratio = changed
        self.last_gray = cv2.addWeighted(self.last_gray, 0.75, gray, 0.25, 0)

        if changed >= float(self.config["motion_threshold"]):
            now = time.time()
            self.last_motion_time = now
            if self.config["auto_read_on_vehicle"] and not self.vehicle_active:
                cooldown_ready = now >= self.cooldown_until
                new_motion_after_confirm = (
                    bool(self.confirmed_plate)
                    and now - self.confirmed_at >= float(self.config["restart_read_on_motion_after_confirm_seconds"])
                )
                if cooldown_ready or new_motion_after_confirm:
                    self._start_vehicle_read(now)

    def _start_vehicle_read(self, now):
        self.vehicle_active = True
        self.vehicle_started_at = now
        self.vehicle_read_until = now + float(self.config["reading_timeout_seconds"])
        self.last_ocr_time = 0
        self.recent_reads.clear()
        self.current_candidates = []
        self.confirmed_plate = ""
        self.confirmed_rut = ""
        self.provisional_plate = ""
        self.confirmed_at = 0
        if self.config["clear_clipboard_on_vehicle_start"]:
            self._copy_to_clipboard("")
        self.plate_value.configure(text="Leyendo")
        self.rut_value.configure(text="Vehiculo detectado")
        self.status_value.configure(text="Vehiculo detectado. Leyendo patente automaticamente...")
        if self.latest_frame is not None:
            self.last_ocr_time = time.time()
            self._queue_ocr(self.latest_frame.copy())

    def _maybe_queue_ocr(self):
        if self.latest_frame is None:
            return
        now = time.time()
        interval = float(self.config["ocr_interval_seconds"])
        if now - self.last_ocr_time < interval:
            return

        automatic_window = (
            self.config["auto_read_on_vehicle"]
            and self.vehicle_active
            and now >= self.vehicle_started_at + float(self.config["read_after_motion_delay_seconds"])
            and now <= self.vehicle_read_until
        )

        if self.config["always_scan"] or automatic_window:
            self.last_ocr_time = now
            self._queue_ocr(self.latest_frame.copy())
        elif self.vehicle_active and now > self.vehicle_read_until:
            self.vehicle_active = False
            self.cooldown_until = now + 1.5
            if not self.confirmed_plate:
                self.status_value.configure(text="No se confirmo patente. Esperando proximo vehiculo...")
                self.plate_value.configure(text="---")
                self.rut_value.configure(text="")
                if self.config["clear_clipboard_on_vehicle_start"]:
                    self._copy_to_clipboard("")

    def _queue_ocr(self, frame):
        while True:
            try:
                self.ocr_queue.get_nowait()
            except queue.Empty:
                break
        try:
            self.ocr_queue.put_nowait(("scan", frame))
        except queue.Full:
            pass

    def _ocr_worker(self):
        while not self.stop_event.is_set():
            try:
                _kind, frame = self.ocr_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            while True:
                try:
                    _kind, frame = self.ocr_queue.get_nowait()
                except queue.Empty:
                    break

            crop, offset = self._crop_roi(frame)
            candidates = self._read_frame(crop, offset)
            if candidates:
                self.frame_queue.put(("status", f"OCR: {', '.join(c.text for c in candidates[:3])}"))
            self.frame_queue.put(("ocr_result", candidates))

    def _crop_roi(self, frame):
        polygon = self._get_plate_polygon()
        if polygon:
            return self._crop_polygon(frame, polygon)
        return self._crop_configured_roi(frame, self.config["roi"])

    def _get_plate_polygon(self):
        polygon = self.config.get("plate_polygon") or []
        return polygon if len(polygon) >= 3 else []

    def _crop_polygon(self, frame, normalized_polygon):
        height, width = frame.shape[:2]
        pts = np.array(
            [
                [
                    max(0, min(width - 1, int(float(x) * width))),
                    max(0, min(height - 1, int(float(y) * height))),
                ]
                for x, y in normalized_polygon
            ],
            dtype=np.int32,
        )
        x, y, w, h = cv2.boundingRect(pts)
        mask = np.zeros((height, width), dtype=np.uint8)
        cv2.fillPoly(mask, [pts], 255)
        masked = cv2.bitwise_and(frame, frame, mask=mask)
        return masked[y : y + h, x : x + w], (x, y)

    def _crop_configured_roi(self, frame, roi):
        height, width = frame.shape[:2]
        left, top, right, bottom = [float(v) for v in roi]
        x1 = max(0, min(width - 1, int(left * width)))
        y1 = max(0, min(height - 1, int(top * height)))
        x2 = max(x1 + 1, min(width, int(right * width)))
        y2 = max(y1 + 1, min(height, int(bottom * height)))
        return frame[y1:y2, x1:x2], (x1, y1)

    def _get_known_plates(self):
        now = time.time()
        refresh_seconds = float(self.config.get("known_plate_refresh_seconds", 2.0))
        if now - self.known_plates_loaded_at >= refresh_seconds:
            self.known_plates = list_known_plates()
            self.known_plates_loaded_at = now
        return self.known_plates

    def _is_known_plate(self, plate):
        return normalize_db_plate(plate) in self._get_known_plates()

    def _prepare_ocr_images(self, crop):
        if crop is None or crop.size == 0:
            return []

        height, width = crop.shape[:2]
        target_width = max(320, int(self.config.get("ocr_target_width", 760)))
        scale = 1.0
        if width < target_width:
            scale = min(3.0, target_width / max(1, width))
        elif width > target_width * 1.4:
            scale = target_width / max(1, width)

        resized = crop
        if abs(scale - 1.0) > 0.05:
            interpolation = cv2.INTER_CUBIC if scale > 1.0 else cv2.INTER_AREA
            resized = cv2.resize(crop, (int(width * scale), int(height * scale)), interpolation=interpolation)

        variants = [("base", resized, scale)]
        extra_variants = max(0, int(self.config.get("ocr_preprocess_variants", 2)))
        if extra_variants <= 0:
            return variants

        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8)).apply(gray)
        sharpened = cv2.filter2D(
            clahe,
            -1,
            np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32),
        )
        variants.append(("sharp", cv2.cvtColor(sharpened, cv2.COLOR_GRAY2BGR), scale))

        if extra_variants >= 2:
            binary = cv2.adaptiveThreshold(
                sharpened,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                31,
                7,
            )
            variants.append(("binary", cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR), scale))

        return variants

    def _read_frame(self, crop, offset):
        known_plates = self._get_known_plates()
        candidate_map = {}
        ox, oy = offset
        for _name, image, scale in self._prepare_ocr_images(crop):
            try:
                results, _elapsed = self.ocr(image)
            except Exception as exc:
                self.frame_queue.put(("status", f"Error OCR: {exc}"))
                return []

            for result in results or []:
                box, text, score = result
                if float(score or 0.0) < float(self.config["min_ocr_score"]):
                    continue
                fixed_box = [[int(x / scale + ox), int(y / scale + oy)] for x, y in box]
                for candidate in extract_plate_candidates(text, score, fixed_box, known_plates):
                    current = candidate_map.get(candidate.text)
                    if current is None:
                        candidate_map[candidate.text] = candidate
                    else:
                        current.score = min(0.99, max(current.score, candidate.score) + 0.03)

            candidates = sorted(candidate_map.values(), key=lambda item: item.score, reverse=True)
            if candidates and self._candidate_can_confirm_fast(candidates[0], known_plates):
                return candidates[:5]

        candidates = sorted(candidate_map.values(), key=lambda item: item.score, reverse=True)
        return candidates[:5]

    def _candidate_can_confirm_fast(self, candidate, known_plates):
        threshold = (
            float(self.config.get("known_plate_single_read_score", 0.82))
            if candidate.text in known_plates
            else float(self.config.get("fast_single_read_score", 0.95))
        )
        return float(candidate.score or 0.0) >= threshold

    def _handle_candidates(self, candidates):
        now = time.time()
        self.current_candidates = candidates
        max_candidates = max(1, int(self.config.get("max_candidates_per_frame", 2)))
        for candidate in candidates[:max_candidates]:
            self.recent_reads.append((now, candidate.text, candidate.score))

        while self.recent_reads and now - self.recent_reads[0][0] > float(self.config["recent_read_seconds"]):
            self.recent_reads.popleft()

        self._update_confirmation()
        self._update_reads_list()
        if self.vehicle_active and not candidates and not self.confirmed_plate:
            self.status_value.configure(text="Vehiculo detectado. Buscando texto de patente...")

    def _update_confirmation(self):
        if not self.recent_reads:
            return
        stats = {}
        for _t, text, score in self.recent_reads:
            row = stats.setdefault(text, {"votes": 0, "score_sum": 0.0, "max_score": 0.0})
            row["votes"] += 1
            row["score_sum"] += float(score or 0.0)
            row["max_score"] = max(row["max_score"], float(score or 0.0))

        ranked = sorted(
            stats.items(),
            key=lambda item: (item[1]["votes"], item[1]["score_sum"], item[1]["max_score"]),
            reverse=True,
        )
        best, best_stats = ranked[0]
        second_stats = ranked[1][1] if len(ranked) > 1 else {"votes": 0, "score_sum": 0.0, "max_score": 0.0}
        votes = int(best_stats["votes"])
        second_votes = int(second_stats["votes"])
        vote_margin = votes - second_votes
        best_score_margin = float(best_stats["score_sum"]) - float(second_stats["score_sum"])
        best_is_known = self._is_known_plate(best)
        fast_threshold = (
            float(self.config.get("known_plate_single_read_score", 0.80))
            if best_is_known
            else float(self.config.get("fast_single_read_score", 0.93))
        )
        fast_single_ok = (
            float(best_stats["max_score"]) >= fast_threshold
            and (second_votes == 0 or best_score_margin >= 0.08)
        )
        votes_ok = votes >= int(self.config["min_confirm_votes"]) and vote_margin >= int(self.config["min_vote_margin"])
        if self.confirmed_plate == best:
            return
        if fast_single_ok or votes_ok:
            now = time.time()
            rut = get_rut_for_plate(best)
            if self.config["require_plate_in_database"] and not rut:
                self.status_value.configure(text=f"{best} leida, pero no esta en la tabla. No se copia.")
                self.plate_value.configure(text=best)
                self.rut_value.configure(text="No esta en BD")
                return

            self.confirmed_plate = best
            self.provisional_plate = best
            self.confirmed_at = now
            self.vehicle_active = False
            self.cooldown_until = now + float(self.config["confirmed_cooldown_seconds"])
            self.confirmed_rut = f"RUT: {rut}" if rut else "Sin RUT asociado"
            self.plate_value.configure(text=best)
            self.rut_value.configure(text=self.confirmed_rut)
            if self.config["copy_confirmed_plate_to_clipboard"]:
                self._copy_to_clipboard(best)
                self.status_value.configure(text=f"Patente confirmada y copiada al portapapeles: {best}")
            else:
                self.status_value.configure(text=f"Patente confirmada automaticamente: {best}")
            access_record = get_access_record(best)
            if access_record:
                _stored_plate, access_message = access_record
                self._show_access_overlay(best, True, access_message)
            else:
                denied_record = get_denied_record(best)
                if denied_record:
                    _stored_plate, denied_message = denied_record
                    self._show_access_overlay(
                        best,
                        False,
                        denied_message or self.config.get("denied_message", ""),
                    )
                else:
                    self.status_value.configure(text=f"Patente copiada sin regla de acceso/denegado: {best}")
        elif votes >= int(self.config["min_confirm_votes"]) and vote_margin < int(self.config["min_vote_margin"]):
            self.status_value.configure(text=f"Lectura ambigua: {best} compite con otra patente. No se copia.")
        elif not self.confirmed_plate:
            self.plate_value.configure(text=best)
            score = float(best_stats["max_score"])
            if self.config.get("copy_provisional_plate_to_clipboard", True) and best != self.provisional_plate:
                self.provisional_plate = best
                self._copy_to_clipboard(best)
                self.status_value.configure(text=f"Patente provisional copiada al portapapeles: {best}")
            self.rut_value.configure(text=f"Leyendo... {votes}/{self.config['min_confirm_votes']} ({score:0.2f})")

    def _copy_to_clipboard(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()

    def _play_access_sound(self, allowed):
        def play():
            try:
                if allowed:
                    winsound.Beep(1200, 180)
                    time.sleep(0.05)
                    winsound.Beep(1500, 180)
                else:
                    winsound.Beep(520, 260)
                    time.sleep(0.05)
                    winsound.Beep(420, 260)
            except Exception:
                try:
                    winsound.MessageBeep(winsound.MB_ICONASTERISK if allowed else winsound.MB_ICONHAND)
                except Exception:
                    pass

        threading.Thread(target=play, daemon=True).start()

    def _show_access_overlay(self, plate, allowed, message=""):
        if self.access_overlay is not None:
            try:
                self.access_overlay.destroy()
            except tk.TclError:
                pass
            self.access_overlay = None

        bg = "#16a34a" if allowed else "#b91c1c"
        title = "LA PATENTE REGISTRADA\nY DAR ACCESO" if allowed else "ACCESO DENEGADO"
        detail = f"PATENTE: {normalize_db_plate(plate)}"
        message = (message or "").strip().upper()
        seconds = max(2.5, float(self.config.get("access_overlay_seconds", 2.5)))

        overlay = tk.Toplevel(self.root)
        self.access_overlay = overlay
        overlay.configure(bg=bg)
        overlay.overrideredirect(True)
        overlay.attributes("-fullscreen", True)
        overlay.attributes("-topmost", True)
        overlay.lift()
        overlay.focus_force()
        overlay.bind("<Escape>", lambda _event: close_overlay())

        content = tk.Frame(overlay, bg=bg)
        content.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(
            content,
            text=title,
            bg=bg,
            fg="white",
            font=("Segoe UI", 68, "bold"),
            justify="center",
        ).pack(padx=36, pady=(0, 24))
        tk.Label(
            content,
            text=detail,
            bg=bg,
            fg="white",
            font=("Segoe UI", 46, "bold"),
            justify="center",
        ).pack(padx=36, pady=(0, 18))
        if message:
            tk.Label(
                content,
                text=message,
                bg=bg,
                fg="white",
                font=("Segoe UI", 30, "bold"),
                justify="center",
                wraplength=1100,
            ).pack(padx=36)

        def keep_on_top():
            if self.access_overlay is overlay:
                try:
                    overlay.attributes("-topmost", True)
                    overlay.lift()
                except tk.TclError:
                    return
                overlay.after(250, keep_on_top)

        def close_overlay():
            if self.access_overlay is overlay:
                self.access_overlay = None
            try:
                overlay.destroy()
            except tk.TclError:
                pass

        self._play_access_sound(allowed)
        overlay.after(50, keep_on_top)
        overlay.after(int(seconds * 1000), close_overlay)

    def _update_reads_list(self):
        self.reads_list.delete(0, tk.END)
        rows = list(self.recent_reads)[-12:]
        for _t, text, score in reversed(rows):
            self.reads_list.insert(tk.END, f"{text:<8} {score:0.2f}")

    def _render_frame(self):
        if self.latest_frame is None:
            return

        frame = self.latest_frame.copy()
        self._draw_roi(frame)
        self._draw_plate_polygon(frame)
        self._draw_vehicle_roi(frame)
        self._draw_candidates(frame)
        self._draw_banner(frame)

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)

        canvas_w = max(400, self.video_canvas.winfo_width())
        canvas_h = max(300, self.video_canvas.winfo_height())
        frame_w, frame_h = image.size
        scale = min(canvas_w / frame_w, canvas_h / frame_h)
        display_w = max(1, int(frame_w * scale))
        display_h = max(1, int(frame_h * scale))
        offset_x = (canvas_w - display_w) // 2
        offset_y = (canvas_h - display_h) // 2
        self.display_info = {
            "scale": scale,
            "offset_x": offset_x,
            "offset_y": offset_y,
            "display_w": display_w,
            "display_h": display_h,
            "frame_w": frame_w,
            "frame_h": frame_h,
        }
        image = image.resize((display_w, display_h), Image.Resampling.LANCZOS)
        self.latest_display = ImageTk.PhotoImage(image)
        self.video_canvas.delete("all")
        self.video_canvas.create_image(offset_x, offset_y, image=self.latest_display, anchor="nw")

    def _draw_roi(self, frame):
        height, width = frame.shape[:2]
        left, top, right, bottom = [float(v) for v in self.config["roi"]]
        x1, y1 = int(left * width), int(top * height)
        x2, y2 = int(right * width), int(bottom * height)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (37, 99, 235), 2)

    def _draw_plate_polygon(self, frame):
        self._draw_normalized_polygon(frame, self._get_plate_polygon(), (34, 197, 94), "ZONA PATENTE")
        if self.selecting_polygon:
            self._draw_normalized_polygon(frame, self.pending_polygon, (250, 204, 21), "NUEVA ZONA")

    def _draw_normalized_polygon(self, frame, polygon, color, label):
        if not polygon:
            return
        height, width = frame.shape[:2]
        pts = np.array(
            [[int(float(x) * width), int(float(y) * height)] for x, y in polygon],
            dtype=np.int32,
        )
        if len(pts) >= 2:
            cv2.polylines(frame, [pts], len(pts) >= 3, color, 3)
        for x, y in pts:
            cv2.circle(frame, (int(x), int(y)), 5, color, -1)
        x0, y0 = pts[0]
        cv2.putText(
            frame,
            label,
            (int(x0) + 8, max(24, int(y0) - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            color,
            2,
            cv2.LINE_AA,
        )

    def _draw_vehicle_roi(self, frame):
        height, width = frame.shape[:2]
        left, top, right, bottom = [float(v) for v in self.config["vehicle_roi"]]
        x1, y1 = int(left * width), int(top * height)
        x2, y2 = int(right * width), int(bottom * height)
        color = (0, 200, 255) if self.vehicle_active else (71, 85, 105)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        cv2.putText(
            frame,
            f"mov {self.last_motion_ratio:.3f}",
            (x1 + 8, max(24, y1 + 24)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            color,
            2,
            cv2.LINE_AA,
        )

    def _draw_candidates(self, frame):
        for candidate in self.current_candidates:
            pts = np.array(candidate.box, dtype=np.int32)
            cv2.polylines(frame, [pts], True, (16, 185, 129), 3)
            x = int(min(point[0] for point in candidate.box))
            y = int(min(point[1] for point in candidate.box))
            cv2.putText(
                frame,
                candidate.text,
                (x, max(30, y - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.9,
                (16, 185, 129),
                2,
                cv2.LINE_AA,
            )

    def _draw_banner(self, frame):
        if not self.confirmed_plate:
            return
        cv2.rectangle(frame, (18, 18), (430, 86), (15, 23, 42), -1)
        cv2.putText(
            frame,
            f"PATENTE: {self.confirmed_plate}",
            (32, 64),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.1,
            (248, 250, 252),
            3,
            cv2.LINE_AA,
        )

    def close(self):
        self.stop_event.set()
        self.root.destroy()

    def run(self):
        if not self.config["rtsp_url"]:
            messagebox.showwarning(APP_NAME, "Falta rtsp_url en camera_config.json")
        self.root.mainloop()


def main():
    if "--self-test" in sys.argv:
        run_self_test()
        return
    enforce_single_instance("Local\\LectorPatentesRTSP")
    app = PlateReaderApp()
    app.run()


def run_self_test():
    assert looks_like_plate("ABCD12")
    assert looks_like_plate("ZY1234")
    assert positional_plate_variants("ABCD1Z")[0] == "ABCD12"
    assert extract_plate_candidates("ABCD12", 0.99, [])[0].text == "ABCD12"
    assert any(candidate.text == "ABCD12" for candidate in extract_plate_candidates("CHILEABCD12", 0.99, []))
    corrected, corrected_score = correct_to_known_plate("ABCD1Z", 0.80, {"ABCD12"})
    assert corrected == "ABCD12"
    assert corrected_score > 0.80

    image = np.full((140, 420, 3), 255, dtype=np.uint8)
    cv2.rectangle(image, (15, 20), (405, 120), (20, 20, 20), 3)
    cv2.putText(image, "ABCD12", (42, 92), cv2.FONT_HERSHEY_SIMPLEX, 2.1, (0, 0, 0), 5, cv2.LINE_AA)
    ocr = RapidOCR()
    results, _elapsed = ocr(image)
    candidates = []
    for result in results or []:
        box, text, score = result
        candidates.extend(extract_plate_candidates(text, score, box))
    if not any(candidate.text == "ABCD12" for candidate in candidates):
        raise AssertionError(f"OCR no detecto ABCD12. Resultado: {results}")
    print("OK")


if __name__ == "__main__":
    main()
