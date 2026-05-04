@echo off
cd /d "%~dp0"

echo Creando PatenteRUTFlotante.exe...
pyinstaller --noconsole --onefile --name PatenteRUTFlotante patente_rut_flotante.py

echo.
echo Creando LectorPatentesRTSP.exe...
".venv-placas\Scripts\pyinstaller.exe" --noconsole --onefile --collect-data rapidocr_onnxruntime --name LectorPatentesRTSP lector_patentes_rtsp.py

echo.
echo Creando IniciarSistemaPatentes.exe...
pyinstaller --noconsole --onefile --name IniciarSistemaPatentes iniciar_sistema_patentes.py

copy /Y patentes_rut.sqlite3 dist\patentes_rut.sqlite3 >nul
copy /Y camera_config.json dist\camera_config.json >nul

echo.
echo Listo. Abre:
echo %~dp0dist\IniciarSistemaPatentes.exe
pause
