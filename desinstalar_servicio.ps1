# desinstalar_servicio.ps1
# Detiene y elimina las tareas programadas de Aspel Inventario.

Write-Host ""
Write-Host "  Desinstalando servicios Aspel Inventario..." -ForegroundColor Yellow
Write-Host ""

Stop-ScheduledTask  -TaskName "AspelInventario-Flask"    -ErrorAction SilentlyContinue
Stop-ScheduledTask  -TaskName "AspelInventario-WhatsApp" -ErrorAction SilentlyContinue

Unregister-ScheduledTask -TaskName "AspelInventario-Flask"    -Confirm:$false -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "AspelInventario-WhatsApp" -Confirm:$false -ErrorAction SilentlyContinue

Write-Host "  [OK] Tareas eliminadas." -ForegroundColor Green
Write-Host ""
