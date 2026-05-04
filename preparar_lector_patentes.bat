@echo off
cd /d "%~dp0"
python -m venv .venv-placas
".venv-placas\Scripts\python.exe" -m pip install --upgrade pip
".venv-placas\Scripts\python.exe" -m pip install -r requirements-lector.txt
echo.
echo Entorno del lector listo.
pause
