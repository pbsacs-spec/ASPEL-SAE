@echo off
cd /d "%~dp0"

echo.
echo  =============================================
echo   Aspel SAE - Iniciando servicios
echo  =============================================
echo.

echo  Iniciando Flask...
start "Flask - Aspel SAE" /d "%~dp0" py app.py

echo  Esperando Flask...
ping -n 4 127.0.0.1 >nul

echo  Iniciando WhatsApp...
start "WhatsApp - Aspel SAE" /d "%~dp0wa_service" "C:\Program Files\nodejs\node.exe" wa_service.js

echo.
echo  Servicios iniciados.
echo  Flask:    http://localhost:5000
echo  WhatsApp: http://localhost:5000/admin/whatsapp
echo.
pause
