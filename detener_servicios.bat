@echo off
echo Deteniendo servicios Aspel SAE...

schtasks /End /TN "AspelInventario-Flask"    >nul 2>&1
schtasks /End /TN "AspelInventario-WhatsApp" >nul 2>&1

for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5000 " ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5001 " ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1

echo Servicios detenidos.
timeout /t 2 /nobreak >nul
