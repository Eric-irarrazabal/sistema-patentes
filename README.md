# Patente RUT Flotante

Mini programa para Windows que crea una barra flotante con dos botones:

- `RUT`: toma el texto del campo activo, busca la patente en `patentes_rut.sqlite3` y reemplaza el texto por el RUT asociado.
- `+`: abre una ventana para guardar o actualizar una patente con su RUT.

## Uso

1. Ejecuta `ejecutar.bat`.
2. En Datanet, escribe la patente en el campo principal.
3. Presiona el boton flotante `RUT` o la tecla `Flecha derecha`.
4. Si existe en la base, el campo queda solamente con el RUT, sin formato.

Para agregar una asociacion:

1. Presiona `+` o usa `Ctrl+Shift+G`.
2. Ingresa patente y RUT.
3. Presiona `Guardar`.

## Datos

La base de datos se crea automaticamente como `patentes_rut.sqlite3` en esta misma carpeta. Parte vacia para no incluir datos reales; las asociaciones se agregan desde el boton `+`.

Si guardas una patente que ya existe, se actualiza el RUT.

## Posicion

La barra aparece cerca del sector marcado en la imagen. Para moverla, mantén `Alt` y arrastra con el mouse. La posicion se guarda en `config.json`.

Click derecho sobre la barra muestra opciones para convertir, agregar, volver a la posicion inicial o salir.

## Crear EXE

Ejecuta `crear_ejecutable.bat`. El archivo quedara en:

`dist\PatenteRUTFlotante.exe`

## Lector de patentes por camara

El prototipo RTSP esta en `lector_patentes_rtsp.py`.

Uso:

1. Revisa `camera_config.json`.
2. Ejecuta `ejecutar_lector_patentes.bat`.
3. La ventana queda en modo automatico: espera movimiento de vehiculo, lee patente y confirma cuando se repite.

Si la camara no conecta, verifica que este PC este en la misma red que la camara y que el puerto RTSP `554` responda.

## Abrir todo junto

El lanzador `IniciarSistemaPatentes.exe` abre juntos:

- `PatenteRUTFlotante.exe`
- `LectorPatentesRTSP.exe`
- `VerPatentesManual.exe`

Para reconstruir todo usa `crear_ejecutables_completos.bat`.

## Modo Manual

`VerPatentesManual.exe` queda corriendo en segundo plano. Manten `CTRL` presionado por 1 segundo para abrir una foto congelada de la camara en pantalla completa. Al soltar `CTRL`, la ventana se cierra. Usa `ESC` para cerrar ese modo.
