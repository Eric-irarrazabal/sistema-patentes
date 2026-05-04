import json
import ctypes
import queue
import re
import socket
import sqlite3
import sys
import threading
import time
from collections import Counter, deque
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
    "ocr_interval_seconds": 0.75,
    "always_scan": False,
    "auto_read_on_vehicle": True,
    "motion_enabled": True,
    "motion_threshold": 0.025,
    "read_after_motion_delay_seconds": 0.35,
    "reading_timeout_seconds": 10.0,
    "confirmed_cooldown_seconds": 6.0,
    "recent_read_seconds": 8.0,
    "min_confirm_votes": 4,
    "min_vote_margin": 2,
    "min_ocr_score": 0.82,
    "require_plate_in_database": True,
    "copy_confirmed_plate_to_clipboard": True,
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


def extract_plate_candidates(text, score, box):
    raw = normalize_plate_text(text)
    if not raw:
        return []

    candidates = []
    pieces = [raw]

    for match in re.finditer(r"[A-Z0-9]{6}", raw):
        pieces.append(match.group(0))

    for piece in pieces:
        for variant in [piece, *positional_plate_variants(piece)]:
            if looks_like_plate(variant):
                candidates.append(OcrCandidate(variant, float(score or 0.0), box))

    unique = {}
    for candidate in candidates:
        unique.setdefault(candidate.text, candidate)
    return list(unique.values())


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
        self.ocr_queue = queue.Queue(maxsize=4)
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
        self.selecting_polygon = False
        self.pending_polygon = []
        self.display_info = None

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("1180x760")
        self.root.configure(bg="#0f172a")
        self.root.protocol("WM_DELETE_WINDOW", self.close)

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

        help_text = (
            "Modo estricto: define una zona poligonal para leer solo donde esta la patente. "
            "Al confirmar, copia la patente al portapapeles."
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
            if self.config["auto_read_on_vehicle"] and now >= self.cooldown_until and not self.vehicle_active:
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
        self.confirmed_at = 0
        self.plate_value.configure(text="Leyendo")
        self.rut_value.configure(text="Vehiculo detectado")
        self.status_value.configure(text="Vehiculo detectado. Leyendo patente automaticamente...")

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

    def _queue_ocr(self, frame):
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

    def _read_frame(self, crop, offset):
        try:
            results, _elapsed = self.ocr(crop)
        except Exception as exc:
            self.frame_queue.put(("status", f"Error OCR: {exc}"))
            return []

        if not results:
            return []

        candidates = []
        ox, oy = offset
        for result in results:
            box, text, score = result
            if float(score or 0.0) < float(self.config["min_ocr_score"]):
                continue
            fixed_box = [[int(x + ox), int(y + oy)] for x, y in box]
            candidates.extend(extract_plate_candidates(text, score, fixed_box))

        candidates.sort(key=lambda item: item.score, reverse=True)
        return candidates[:5]

    def _handle_candidates(self, candidates):
        now = time.time()
        self.current_candidates = candidates
        for candidate in candidates:
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
        counts = Counter(text for _t, text, _score in self.recent_reads)
        ranked = counts.most_common(2)
        best, votes = ranked[0]
        second_votes = ranked[1][1] if len(ranked) > 1 else 0
        vote_margin = votes - second_votes
        if votes >= int(self.config["min_confirm_votes"]):
            if vote_margin < int(self.config["min_vote_margin"]):
                self.status_value.configure(text=f"Lectura ambigua: {best} compite con otra patente. No se copia.")
                return

            now = time.time()
            rut = get_rut_for_plate(best)
            if self.config["require_plate_in_database"] and not rut:
                self.status_value.configure(text=f"{best} leida, pero no esta en la tabla. No se copia.")
                self.plate_value.configure(text=best)
                self.rut_value.configure(text="No esta en BD")
                return

            self.confirmed_plate = best
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
        elif not self.confirmed_plate:
            self.plate_value.configure(text=best)
            self.rut_value.configure(text=f"Leyendo... {votes}/{self.config['min_confirm_votes']}")

    def _copy_to_clipboard(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()

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
