# instalar_como_servicio.ps1
# Registra Flask y WhatsApp como tareas programadas de Windows.
# Los procesos arrancan con el sistema y sobreviven el cierre de sesion.
# Ejecutar como Administrador.

$base   = Split-Path -Parent $MyInvocation.MyCommand.Path

# El "py launcher" (py.exe) y node.exe se buscan en el PATH en vez de una
# ruta fija -- la ruta exacta de Python varia de equipo a equipo (depende
# del usuario y la version con la que se instalo), asi que una ruta fija
# aqui solo funcionaria en la maquina donde se escribio el script.
$pyCmd = Get-Command py -ErrorAction SilentlyContinue
if (-not $pyCmd) {
    Write-Host "  ERROR: no se encontro 'py' en el PATH. Instala Python primero" -ForegroundColor Red
    Write-Host "  (o corre instalar_dependencias.bat)." -ForegroundColor Red
    exit 1
}
$python = $pyCmd.Source

$nodeCmd = Get-Command node -ErrorAction SilentlyContinue
if ($nodeCmd) {
    $node = $nodeCmd.Source
} elseif (Test-Path "C:\Program Files\nodejs\node.exe") {
    $node = "C:\Program Files\nodejs\node.exe"
} else {
    Write-Host "  ERROR: no se encontro node.exe. Instala Node.js primero" -ForegroundColor Red
    Write-Host "  (o corre instalar_dependencias.bat)." -ForegroundColor Red
    exit 1
}

# $env:COMPUTERNAME es siempre el nombre de ESTE equipo, aunque la cuenta
# sea de dominio -- si la cuenta es de dominio, Register-ScheduledTask no
# puede resolver "NOMBRE_EQUIPO\usuario" a un identificador de seguridad
# (falla con "No se efectuo ninguna asignacion entre los nombres de cuenta
# y los identificadores de seguridad", HRESULT 0x80070534) porque esa
# cuenta no existe localmente con ese nombre. $env:USERDOMAIN si refleja
# correctamente el dominio real (o el nombre del equipo, si la cuenta es
# local) segun corresponda en cada maquina.
$usuario = "$env:USERDOMAIN\$env:USERNAME"

Write-Host ""
Write-Host "  Instalador de servicios Aspel Inventario" -ForegroundColor Cyan
Write-Host "  Usuario: $usuario" -ForegroundColor Gray
Write-Host ""
Write-Host "  Se necesita la contrasena de Windows para registrar" -ForegroundColor Yellow
Write-Host "  las tareas como proceso de fondo (sin sesion abierta)." -ForegroundColor Yellow
Write-Host ""

$securePass = Read-Host "  Contrasena de Windows" -AsSecureString
$bstr       = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePass)
$pass       = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

Write-Host ""

# ── Flask ──────────────────────────────────────────────────────────────────────

$action_flask = New-ScheduledTaskAction `
    -Execute       "cmd.exe" `
    -Argument      "/c $python app.py >> $base\flask.log 2>&1" `
    -WorkingDirectory $base

$trigger_flask  = New-ScheduledTaskTrigger -AtStartup

$settings_flask = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit  (New-TimeSpan -Seconds 0) `
    -RestartCount        5 `
    -RestartInterval     (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -MultipleInstances   IgnoreNew

Register-ScheduledTask `
    -TaskName    "AspelInventario-Flask" `
    -Description "Aspel Inventario - Servidor web Flask (puerto 5000)" `
    -Action      $action_flask `
    -Trigger     $trigger_flask `
    -Settings    $settings_flask `
    -RunLevel    Highest `
    -User        $usuario `
    -Password    $pass `
    -Force | Out-Null

Write-Host "  [OK] Tarea Flask registrada" -ForegroundColor Green

# ── WhatsApp / Node ────────────────────────────────────────────────────────────
# ping -n 16 espera ~15 segundos para que Flask levante antes que Node

$action_wa = New-ScheduledTaskAction `
    -Execute       "cmd.exe" `
    -Argument      "/c ping -n 16 127.0.0.1 >nul && `"$node`" wa_service.js >> `"$base\wa_service\wa.log`" 2>&1" `
    -WorkingDirectory "$base\wa_service"

$trigger_wa  = New-ScheduledTaskTrigger -AtStartup

$settings_wa = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit  (New-TimeSpan -Seconds 0) `
    -RestartCount        5 `
    -RestartInterval     (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -MultipleInstances   IgnoreNew

Register-ScheduledTask `
    -TaskName    "AspelInventario-WhatsApp" `
    -Description "Aspel Inventario - Servicio WhatsApp Web (puerto 5001)" `
    -Action      $action_wa `
    -Trigger     $trigger_wa `
    -Settings    $settings_wa `
    -RunLevel    Highest `
    -User        $usuario `
    -Password    $pass `
    -Force | Out-Null

Write-Host "  [OK] Tarea WhatsApp registrada" -ForegroundColor Green

$pass = $null

# ── Iniciar ahora ──────────────────────────────────────────────────────────────

Write-Host ""
$resp = Read-Host "  Iniciar los servicios ahora? (S/N)"
if ($resp -match "^[Ss]") {
    Start-ScheduledTask -TaskName "AspelInventario-Flask"
    Start-Sleep -Seconds 5
    Start-ScheduledTask -TaskName "AspelInventario-WhatsApp"
    Write-Host "  [OK] Servicios iniciados" -ForegroundColor Green
}

Write-Host ""
Write-Host "  ================================================" -ForegroundColor Cyan
Write-Host "  Instalacion completada." -ForegroundColor Cyan
Write-Host "  Los servicios arrancan solos al reiniciar Windows." -ForegroundColor Cyan
Write-Host ""
Write-Host "  Comandos utiles:" -ForegroundColor Yellow
Write-Host "    Ver estado:   Get-ScheduledTask -TaskName 'AspelInventario-*'"
Write-Host "    Iniciar:      Start-ScheduledTask -TaskName 'AspelInventario-Flask'"
Write-Host "    Detener:      Stop-ScheduledTask  -TaskName 'AspelInventario-Flask'"
Write-Host "    Desinstalar:  .\desinstalar_servicio.ps1"
Write-Host "  ================================================" -ForegroundColor Cyan
Write-Host ""
