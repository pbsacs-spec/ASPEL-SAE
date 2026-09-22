# respaldar_datos.ps1
# Respalda a una carpeta FUERA del repo los datos que genera la app y que
# nunca van a git (embarques.db, fotos de entrega, config local). Se instala
# como tarea programada diaria via programar_respaldo.ps1, pero tambien se
# puede correr a mano en cualquier momento.

$ErrorActionPreference = "Stop"

$base    = Split-Path -Parent $MyInvocation.MyCommand.Path
$destino = "C:\Users\iscbruno\Documents\Aspel Inventario - Respaldos"
$fecha   = Get-Date -Format "yyyy-MM-dd_HHmmss"
$carpetaRespaldo = Join-Path $destino $fecha

New-Item -ItemType Directory -Path $carpetaRespaldo -Force | Out-Null

# Archivos sueltos: datos de embarques, credenciales/config local, clave de sesion.
$archivos = @(
    "embarques.db",
    "auth_config.ini",
    "db_config.ini",
    "wa_config.ini",
    "embarque_config.ini",
    ".secret_key"
)

$copiados = @()
foreach ($archivo in $archivos) {
    $ruta = Join-Path $base $archivo
    if (Test-Path $ruta) {
        Copy-Item $ruta -Destination $carpetaRespaldo -Force
        $copiados += $archivo
    }
}

# Fotos de evidencia de entrega (carpeta completa).
$fotos = Join-Path $base "embarque_fotos"
if (Test-Path $fotos) {
    Copy-Item $fotos -Destination (Join-Path $carpetaRespaldo "embarque_fotos") -Recurse -Force
    $copiados += "embarque_fotos\"
}

# Conserva solo los ultimos 30 respaldos (~1 mes si corre a diario) para no
# llenar el disco con el tiempo.
$todos = Get-ChildItem $destino -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending
if ($todos.Count -gt 30) {
    $todos | Select-Object -Skip 30 | ForEach-Object { Remove-Item $_.FullName -Recurse -Force }
}

$resumen = "$fecha - Respaldo en '$carpetaRespaldo' -- $($copiados -join ', ')"
Add-Content -Path (Join-Path $destino "respaldo.log") -Value $resumen -Encoding utf8
Write-Host $resumen
