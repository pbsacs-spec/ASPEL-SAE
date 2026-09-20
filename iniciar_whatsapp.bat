@echo off
title Servicio WhatsApp - Aspel SAE
cd /d "%~dp0wa_service"

echo.
echo  =============================================
echo   Servicio WhatsApp - Aspel SAE
echo  =============================================
echo.

node --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Node.js no esta instalado.
    echo.
    echo  Descargalo en: https://nodejs.org
    echo  Instala la version LTS y vuelve a ejecutar este archivo.
    echo.
    pause
    exit /b 1
)

if not exist node_modules (
    echo  Instalando dependencias (solo la primera vez)...
    echo  Esto puede tardar varios minutos...
    call npm install
    if errorlevel 1 (
        echo.
        echo  Error al instalar dependencias.
        pause
        exit /b 1
    )
)

echo  Iniciando servicio WhatsApp...
echo  Abre el panel de administracion en: http://localhost:5000/admin/whatsapp
echo.
node wa_service.js
pause
