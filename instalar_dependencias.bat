@echo off
title Aspel SAE - Instalacion de dependencias
cd /d "%~dp0"

echo.
echo  =============================================
echo   Aspel SAE - Instalacion de dependencias
echo  =============================================
echo.

:: ── Verificar Python ─────────────────────────────────────────────
echo  [1/4] Verificando Python...
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
echo  [2/4] Instalando paquetes Python...
echo  (flask, fdb, requests, openpyxl, fpdf2, qrcode, pillow)
echo.
py -m pip install --upgrade pip >nul 2>&1
py -m pip install flask fdb requests openpyxl fpdf2 qrcode pillow
if errorlevel 1 (
    echo.
    echo  ERROR: Fallo la instalacion de paquetes Python.
    pause
    exit /b 1
)
echo  OK: Paquetes Python instalados.

:: ── Verificar Node.js ─────────────────────────────────────────────
echo.
echo  [3/4] Verificando Node.js...
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
echo  [4/4] Instalando dependencias Node.js...
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
