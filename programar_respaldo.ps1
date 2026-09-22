# programar_respaldo.ps1
# Registra la tarea programada diaria que corre respaldar_datos.ps1.
# Corre como SYSTEM (no necesita la contraseña de Windows, a diferencia de
# instalar_como_servicio.ps1) porque solo copia archivos, no depende del
# Python instalado en el perfil del usuario.
# Ejecutar como Administrador.

$base   = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $base "respaldar_datos.ps1"

$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At "2:00AM"
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName "AspelInventario-Respaldo" `
    -Description "Respaldo diario de embarques.db, fotos de entrega y config local (fuera del repo)" `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Tarea 'AspelInventario-Respaldo' registrada: corre todos los dias a las 2:00 AM."
Write-Host "Para correrla ahora mismo de prueba: Start-ScheduledTask -TaskName 'AspelInventario-Respaldo'"
