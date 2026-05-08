@echo off
cd /d "%~dp0"

echo Creando PatenteRUTFlotante.exe...
pyinstaller --noconsole --onefile --name PatenteRUTFlotante patente_rut_flotante.py

echo.
echo Creando LectorPatentesRTSP.exe...
".venv-placas\Scripts\pyinstaller.exe" --noconsole --onefile --collect-data rapidocr_onnxruntime --name LectorPatentesRTSP lector_patentes_rtsp.py

echo.
echo Creando LectorPatentesWatchdog.exe...
".venv-placas\Scripts\pyinstaller.exe" --noconsole --onefile --name LectorPatentesWatchdog lector_patentes_watchdog.py

echo.
echo Creando VerPatentesManual.exe...
".venv-placas\Scripts\pyinstaller.exe" --noconsole --onefile --name VerPatentesManual ver_patentes_manual.py

echo.
echo Creando IniciarSistemaPatentes.exe...
pyinstaller --noconsole --onefile --name IniciarSistemaPatentes iniciar_sistema_patentes.py

copy /Y patentes_rut.sqlite3 dist\patentes_rut.sqlite3 >nul
copy /Y camera_config.json dist\camera_config.json >nul
if exist plate_detector.onnx copy /Y plate_detector.onnx dist\plate_detector.onnx >nul
if exist "ACCESO PERMITIDO.mp3" copy /Y "ACCESO PERMITIDO.mp3" "dist\ACCESO PERMITIDO.mp3" >nul
if exist "ACCESO DENEGADO.mp3" copy /Y "ACCESO DENEGADO.mp3" "dist\ACCESO DENEGADO.mp3" >nul

echo.
echo Listo. Abre:
echo %~dp0dist\IniciarSistemaPatentes.exe
echo O solo el lector vigilado:
echo %~dp0dist\LectorPatentesWatchdog.exe
pause
