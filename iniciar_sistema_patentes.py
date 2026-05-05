import subprocess
import sys
import time
from pathlib import Path
from tkinter import messagebox


APP_NAME = "Iniciar Sistema Patentes"
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent / "dist"
CREATE_NO_WINDOW = 0x08000000


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
        return False
    return exe_name.lower() in output.lower()


def start_exe(path):
    if not path.exists():
        raise FileNotFoundError(f"No se encontro: {path}")
    if is_running(path.name):
        return
    subprocess.Popen([str(path)], cwd=str(path.parent), close_fds=True, creationflags=CREATE_NO_WINDOW)


def main():
    try:
        start_exe(APP_DIR / "PatenteRUTFlotante.exe")
        time.sleep(0.5)
        start_exe(APP_DIR / "LectorPatentesRTSP.exe")
        time.sleep(0.5)
        start_exe(APP_DIR / "VerPatentesManual.exe")
    except Exception as exc:
        messagebox.showerror(APP_NAME, str(exc))


if __name__ == "__main__":
    main()
