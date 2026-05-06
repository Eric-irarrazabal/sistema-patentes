import ctypes
import json
import os
import queue
import sys
import time
import threading
from pathlib import Path

import cv2
from screeninfo import get_monitors


# ============================================================
# CONFIGURACION
# ============================================================
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "camera_config.json"
WINDOW_NAME = "Camara Patentes"
CTRL_HOLD_SECONDS = 0.05
RECONNECT_DELAY_SECONDS = 2.0
READ_SLEEP_SECONDS = 0.0
OPEN_TIMEOUT_MS = 1500
READ_TIMEOUT_MS = 1200
STREAM_STALL_SECONDS = 1.5
SHOW_LOGS = True
ERROR_ALREADY_EXISTS = 183
_SINGLE_INSTANCE_MUTEX = None

# RTSP por TCP en baja latencia para evitar leer segundos atrasados.
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
    f"rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|stimeout;{READ_TIMEOUT_MS * 1000}|timeout;{READ_TIMEOUT_MS * 1000}"
)


# ============================================================
# ESTADO GLOBAL
# ============================================================
frame_lock = threading.Lock()
last_frame = None
last_frame_ts = 0.0
stop_event = threading.Event()


# ============================================================
# UTILIDADES
# ============================================================
def log(msg: str) -> None:
    if SHOW_LOGS:
        print(msg, flush=True)


def load_rtsp_url() -> str:
    if not CONFIG_PATH.exists():
        return ""
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return ""
    return str(config.get("rtsp_url") or "")


def enforce_single_instance(name: str) -> None:
    global _SINGLE_INSTANCE_MUTEX
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = ctypes.c_ulong
    _SINGLE_INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, name)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        sys.exit(0)


def is_ctrl_pressed() -> bool:
    VK_CONTROL = 0x11
    return bool(ctypes.windll.user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)


def get_primary_monitor_size() -> tuple[int, int]:
    try:
        monitors = get_monitors()
        primary = monitors[0]
        for monitor in monitors:
            if getattr(monitor, "is_primary", False):
                primary = monitor
                break
        return primary.width, primary.height
    except Exception:
        return 1920, 1080


def fit_keep_aspect(frame, screen_w: int, screen_h: int):
    h, w = frame.shape[:2]
    scale = min(screen_w / w, screen_h / h)
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))

    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    canvas = cv2.UMat(screen_h, screen_w, cv2.CV_8UC3).get()
    canvas[:] = 0
    x = (screen_w - new_w) // 2
    y = (screen_h - new_h) // 2
    canvas[y : y + new_h, x : x + new_w] = resized
    return canvas


def is_usable_frame(frame) -> bool:
    if frame is None:
        return False
    if not hasattr(frame, "shape"):
        return False
    if len(frame.shape) < 2:
        return False
    h, w = frame.shape[:2]
    if h < 100 or w < 100:
        return False
    return True


def open_capture(rtsp_url: str):
    cap = cv2.VideoCapture()
    try:
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
    cap.open(rtsp_url, cv2.CAP_FFMPEG)
    return cap


def put_latest(target_queue, item) -> None:
    try:
        target_queue.put_nowait(item)
    except queue.Full:
        try:
            target_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            target_queue.put_nowait(item)
        except queue.Full:
            pass


def capture_loop(cap, read_queue, session_stop) -> None:
    while not stop_event.is_set() and not session_stop.is_set():
        ok, frame = cap.read()
        if session_stop.is_set():
            break
        if not ok or frame is None:
            put_latest(read_queue, ("error", None))
            break
        put_latest(read_queue, ("frame", frame))


# ============================================================
# CAMARA EN SEGUNDO PLANO
# ============================================================
def camera_worker(rtsp_url: str) -> None:
    global last_frame, last_frame_ts

    if not rtsp_url:
        log("[ERROR] Falta rtsp_url en camera_config.json")
        return

    while not stop_event.is_set():
        log("[INFO] Conectando a camara manual...")
        cap = open_capture(rtsp_url)

        if not cap.isOpened():
            log("[WARN] No se pudo abrir RTSP manual. Reintentando...")
            try:
                cap.release()
            except Exception:
                pass
            time.sleep(RECONNECT_DELAY_SECONDS)
            continue

        log("[OK] Camara manual conectada.")
        read_queue = queue.Queue(maxsize=1)
        session_stop = threading.Event()
        threading.Thread(target=capture_loop, args=(cap, read_queue, session_stop), daemon=True).start()
        last_seen_at = time.time()

        while not stop_event.is_set():
            try:
                kind, frame = read_queue.get(timeout=0.05)
            except queue.Empty:
                if time.time() - last_seen_at >= STREAM_STALL_SECONDS:
                    log("[WARN] Camara manual sin frames nuevos. Reconectando...")
                    break
                continue

            if kind != "frame" or frame is None:
                log("[WARN] Error leyendo frame manual. Reconectando...")
                break

            if not is_usable_frame(frame):
                continue

            last_seen_at = time.time()
            with frame_lock:
                last_frame = frame.copy()
                last_frame_ts = time.time()

            if READ_SLEEP_SECONDS > 0:
                time.sleep(READ_SLEEP_SECONDS)

        try:
            session_stop.set()
            cap.release()
        except Exception:
            pass

        if not stop_event.is_set():
            time.sleep(RECONNECT_DELAY_SECONDS)


# ============================================================
# VENTANA
# ============================================================
def create_window() -> None:
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    try:
        cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_TOPMOST, 1)
    except Exception:
        pass


def close_window() -> None:
    try:
        cv2.destroyWindow(WINDOW_NAME)
    except Exception:
        pass


def get_latest_frame_copy():
    with frame_lock:
        if last_frame is None:
            return None, 0.0
        return last_frame.copy(), last_frame_ts


# ============================================================
# LOOP PRINCIPAL
# ============================================================
def main() -> None:
    if "--self-test" in sys.argv:
        run_self_test()
        return

    enforce_single_instance("Local\\VerPatentesManual")
    rtsp_url = load_rtsp_url()
    screen_w, screen_h = get_primary_monitor_size()

    thread = threading.Thread(target=camera_worker, args=(rtsp_url,), daemon=True)
    thread.start()

    ctrl_start = None
    showing = False
    frozen = None
    frozen_ts = 0.0

    log("[INFO] Presiona CTRL para abrir la imagen de ese momento.")
    log("[INFO] Suelta CTRL para cerrar. ESC para salir del modo manual.")

    while True:
        ctrl = is_ctrl_pressed()

        if ctrl:
            if ctrl_start is None:
                ctrl_start = time.time()

            held = time.time() - ctrl_start
            if held >= CTRL_HOLD_SECONDS and not showing:
                frame, ts = get_latest_frame_copy()
                if frame is not None:
                    frozen = fit_keep_aspect(frame, screen_w, screen_h)
                    frozen_ts = ts
                    create_window()
                    showing = True
                    age_ms = int((time.time() - frozen_ts) * 1000) if frozen_ts else -1
                    log(f"[OK] Foto manual abierta. Antiguedad aprox: {age_ms} ms")

            if showing and frozen is not None:
                cv2.imshow(WINDOW_NAME, frozen)
        else:
            ctrl_start = None
            if showing:
                close_window()
                showing = False
                frozen = None
                frozen_ts = 0.0
                log("[INFO] Ventana manual cerrada.")

        key = cv2.waitKey(15) & 0xFF
        if key == 27:
            break

    stop_event.set()
    close_window()
    cv2.destroyAllWindows()
    log("[INFO] Modo manual finalizado.")


def run_self_test() -> None:
    class FakeFrame:
        shape = (1080, 1920, 3)

    assert is_usable_frame(FakeFrame())
    assert not is_usable_frame(None)
    screen_w, screen_h = get_primary_monitor_size()
    assert screen_w > 0 and screen_h > 0
    print("OK")


if __name__ == "__main__":
    main()
