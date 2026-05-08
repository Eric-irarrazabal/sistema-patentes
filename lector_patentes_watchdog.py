import ctypes
import logging
import subprocess
import sys
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import messagebox


APP_NAME = "Watchdog Lector Patentes"
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent / "dist"
LECTOR_EXE = APP_DIR / "LectorPatentesRTSP.exe"
LOG_PATH = APP_DIR / "lector_watchdog.log"
CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183
_SINGLE_INSTANCE_MUTEX = None


def setup_logging():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logging.info("%s iniciado. app_dir=%s", APP_NAME, APP_DIR)


def enforce_single_instance():
    global _SINGLE_INSTANCE_MUTEX
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = ctypes.c_ulong
    _SINGLE_INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, "Local\\LectorPatentesWatchdog")
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        logging.info("Watchdog ya estaba corriendo. Cerrando segunda instancia.")
        sys.exit(0)


def is_running(exe_name):
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
            creationflags=CREATE_NO_WINDOW,
            text=True,
            encoding="latin-1",
            errors="ignore",
        )
    except Exception:
        logging.exception("No se pudo consultar tasklist")
        return False
    return exe_name.lower() in output.lower()


def show_error(message):
    logging.error(message)
    try:
        messagebox.showerror(APP_NAME, message)
    except Exception:
        pass


def main():
    setup_logging()
    if "--self-test" in sys.argv:
        if not LECTOR_EXE.exists():
            raise FileNotFoundError(f"No se encontro {LECTOR_EXE}")
        print("OK")
        return

    enforce_single_instance()
    if not LECTOR_EXE.exists():
        show_error(f"No se encontro: {LECTOR_EXE}")
        return

    if is_running(LECTOR_EXE.name):
        logging.info("%s ya esta abierto. No se abre otro lector.", LECTOR_EXE.name)
        return

    restart_times = deque()
    while True:
        logging.info("Abriendo lector: %s", LECTOR_EXE)
        started_at = time.time()
        process = subprocess.Popen(
            [str(LECTOR_EXE)],
            cwd=str(APP_DIR),
            close_fds=True,
            creationflags=CREATE_NO_WINDOW,
        )
        logging.info("Lector abierto pid=%s", process.pid)
        exit_code = process.wait()
        ran_seconds = time.time() - started_at
        logging.warning("Lector finalizo. exit_code=%s duracion=%.1fs", exit_code, ran_seconds)

        if exit_code == 0:
            logging.info("Salida normal del lector. Watchdog detenido.")
            return

        now = time.time()
        restart_times.append(now)
        while restart_times and now - restart_times[0] > 600:
            restart_times.popleft()

        delay = 3.0
        if len(restart_times) >= 5:
            delay = 30.0
            logging.error("Muchas caidas en 10 minutos. Esperando %.0fs antes de reiniciar.", delay)
        else:
            logging.info("Reiniciando lector en %.0fs por salida con error.", delay)
        time.sleep(delay)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        show_error(str(exc))
        raise
