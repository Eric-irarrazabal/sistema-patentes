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

Para copiar un RUT aleatorio de la lista al portapapeles, presiona `Ctrl` dos veces seguidas. Luego puedes pegarlo directo con `Ctrl+V`.

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
4. Usa `Listas` para guardar patentes autorizadas o denegadas, cada una con mensaje opcional.

Cuando una patente autorizada se confirma, aparece una pantalla completa verde durante al menos 2.5 segundos con la patente y el mensaje. Si esta en la lista de denegados, aparece una pantalla completa roja con acceso denegado. En ambos casos se emite un bip y la patente confirmada queda copiada al portapapeles. Si no esta en ninguna lista, solo queda copiada.

La lectura prioriza rapidez y precision: procesa siempre el cuadro mas reciente, prepara la imagen de la zona de patente antes del OCR, descarta candidatos debiles y puede confirmar en una sola lectura cuando la confianza es alta o cuando calza con una patente ya registrada. Mientras no hay auto escanea suave cada 0.6 segundos; si ve algo parecido a patente, lo copia como provisional. En `1/2`, copia una patente provisional al portapapeles; cuando confirma, reemplaza por la definitiva.

Si la camara no conecta, verifica que este PC este en la misma red que la camara y que el puerto RTSP `554` responda.

## Abrir todo junto

El lanzador `IniciarSistemaPatentes.exe` abre juntos:

- `PatenteRUTFlotante.exe`
- `LectorPatentesRTSP.exe`
- `VerPatentesManual.exe`

Para reconstruir todo usa `crear_ejecutables_completos.bat`.

## Modo Manual

`VerPatentesManual.exe` queda corriendo en segundo plano. Manten `CTRL` presionado por 1 segundo para abrir una foto congelada de la camara en pantalla completa. Al soltar `CTRL`, la ventana se cierra. Usa `ESC` para cerrar ese modo.
