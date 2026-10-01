@echo off
title Aspel SAE - Instalacion de dependencias
cd /d "%~dp0"

echo.
echo  =============================================
echo   Aspel SAE - Instalacion de dependencias
echo  =============================================
echo.

:: ── Verificar Python ─────────────────────────────────────────────
echo  [1/5] Verificando Python...
py --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ERROR: Python no esta instalado.
    echo  Descargalo en: https://www.python.org/downloads/
    echo  Asegurate de marcar "Add Python to PATH" durante la instalacion.
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('py --version 2^>^&1') do echo  OK: %%v

:: ── Instalar paquetes Python ──────────────────────────────────────
echo.
echo  [2/5] Instalando paquetes Python...
echo  (flask, fdb, requests, openpyxl, fpdf2, qrcode, pillow, waitress)
echo.
py -m pip install --upgrade pip >nul 2>&1
py -m pip install flask fdb requests openpyxl fpdf2 qrcode pillow waitress
if errorlevel 1 (
    echo.
    echo  ERROR: Fallo la instalacion de paquetes Python.
    pause
    exit /b 1
)
echo  OK: Paquetes Python instalados.

:: ── Verificar Node.js ─────────────────────────────────────────────
echo.
echo  [3/5] Verificando Node.js...
where node >nul 2>&1
if errorlevel 1 (
    if exist "C:\Program Files\nodejs\node.exe" (
        set "PATH=C:\Program Files\nodejs;%PATH%"
    ) else (
        echo.
        echo  ERROR: Node.js no esta instalado.
        echo  Descargalo en: https://nodejs.org  (version LTS)
        echo.
        pause
        exit /b 1
    )
)
for /f "tokens=*" %%v in ('node --version 2^>^&1') do echo  OK: Node.js %%v

:: ── Instalar dependencias Node.js ────────────────────────────────
echo.
echo  [4/5] Instalando dependencias Node.js...
echo  (whatsapp-web.js, puppeteer, express, axios, qrcode)
echo  Nota: Puppeteer descargara Chromium (~300 MB). Puede tardar varios minutos.
echo.
if exist "C:\Program Files\nodejs\npm.cmd" (
    set "NPM=C:\Program Files\nodejs\npm.cmd"
) else (
    set "NPM=npm"
)
cd wa_service
call "%NPM%" install
if errorlevel 1 (
    echo.
    echo  ERROR: Fallo la instalacion de dependencias Node.js.
    cd ..
    pause
    exit /b 1
)
echo  OK: Dependencias Node.js instaladas.

echo.
echo  Descargando Chromium para Puppeteer (~300 MB)...
echo  Esto puede tardar varios minutos segun la velocidad de internet.
echo.
call "%NPM%" exec -- puppeteer browsers install chrome
if errorlevel 1 (
    echo.
    echo  ADVERTENCIA: No se pudo descargar Chromium automaticamente.
    echo  Ejecuta manualmente en la carpeta wa_service:
    echo    npx puppeteer browsers install chrome
    echo.
) else (
    echo  OK: Chromium descargado correctamente.
)
cd ..

:: ── Cliente de Firebird de 64 bits ───────────────────────────────────
:: Python de 64 bits (lo normal hoy en dia) no puede cargar un fbclient.dll
:: de 32 bits -- truena con "WinError 193: %1 no es una aplicacion Win32
:: valida". Aspel SAE 9 instalaba el cliente de 32 bits (coincidia con su
:: propio ejecutable de 32 bits); al pasar a SAE 10 en una maquina nueva, el
:: cliente que ya esta en el equipo puede seguir siendo de 32 bits aunque
:: Python sea de 64 -- por eso se trae aparte un cliente de 64 bits propio,
:: sin tocar el que usa Aspel para si mismo.
echo.
echo  [5/5] Verificando cliente de Firebird de 64 bits (para Python)...
set "FBCLIENT64=C:\Firebird64Client\bin\fbclient.dll"
if exist "%FBCLIENT64%" (
    echo  OK: Ya existe %FBCLIENT64%
) else (
    echo  No se encontro -- descargando Firebird 2.5.9 de 64 bits...
    echo  (uso unico: solo se toma fbclient.dll de ahi, no se instala como servicio)
    echo.
    powershell -Command "try { Invoke-WebRequest -Uri 'https://github.com/FirebirdSQL/firebird/releases/download/R2_5_9/Firebird-2.5.9.27139-0_x64.zip' -OutFile '%TEMP%\firebird64.zip' } catch { exit 1 }"
    if errorlevel 1 (
        echo.
        echo  ADVERTENCIA: No se pudo descargar el cliente de Firebird de 64 bits.
        echo  Si mas adelante "py app.py" falla con WinError 193, descarga a mano:
        echo  https://github.com/FirebirdSQL/firebird/releases/download/R2_5_9/Firebird-2.5.9.27139-0_x64.zip
        echo  extrae el ZIP en C:\Firebird64Client\ y pon esta linea en db_config.ini:
        echo  fb_lib = %FBCLIENT64%
        echo.
    ) else (
        echo  Extrayendo...
        powershell -Command "Expand-Archive -Path '%TEMP%\firebird64.zip' -DestinationPath 'C:\Firebird64Client' -Force"
        del "%TEMP%\firebird64.zip" >nul 2>&1
        if exist "%FBCLIENT64%" (
            echo  OK: Cliente de Firebird de 64 bits listo en C:\Firebird64Client\bin\
        ) else (
            echo  ADVERTENCIA: La descarga no genero el archivo esperado en %FBCLIENT64%
        )
    )
)

:: Solo en una instalacion nueva (sin db_config.ini todavia) se deja listo
:: apuntando al cliente de 64 bits -- si ya existe el archivo, no se toca
:: para no pisar una configuracion que el usuario ya hizo a mano.
if exist "%FBCLIENT64%" if not exist "db_config.ini" (
    echo  Creando db_config.ini inicial con el cliente de 64 bits...
    (
        echo [settings]
        echo fb_lib = %FBCLIENT64%
        echo default = empresa_1
        echo.
        echo [empresa_1]
        echo nombre = Empresa 1
        echo db_path =
    ) > db_config.ini
    echo  OK: db_config.ini creado -- falta indicar db_path desde /admin/database.
)

:: ── Fin ───────────────────────────────────────────────────────────
echo.
echo  =============================================
echo   Instalacion completada exitosamente
echo  =============================================
echo.
echo  Siguientes pasos:
echo.
echo  1. Ejecuta "iniciar_servicios.vbs" para arrancar.
echo  2. Abre http://localhost:5000/admin/database
echo     y actualiza la ruta de la base de datos.
echo  3. Escanea el QR de WhatsApp en
echo     http://localhost:5000/admin/whatsapp
echo.
pause
