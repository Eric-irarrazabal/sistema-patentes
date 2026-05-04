@echo off
cd /d "%~dp0"
pyinstaller --noconsole --onefile --name PatenteRUTFlotante patente_rut_flotante.py
echo.
echo Ejecutable creado en: %~dp0dist\PatenteRUTFlotante.exe
pause
