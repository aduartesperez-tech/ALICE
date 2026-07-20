# SIMULACION: cuenta lo que ocupan los temporales, pero NO borra nada.
#
# Sirve para probar el flujo de confirmacion de forma segura (esta marcado con
# confirm = true en catalog.toml, asi que Alice pregunta antes de ejecutarlo).
#
# Si algun dia quieres que borre de verdad, descomenta la linea del Remove-Item
# al final. Hazlo con cuidado: eso si es destructivo.

$carpeta = $env:TEMP
if (-not (Test-Path $carpeta)) {
    Write-Output "No encontre la carpeta de temporales."
    exit 0
}

$archivos = Get-ChildItem -Path $carpeta -Recurse -File -ErrorAction SilentlyContinue
$total = ($archivos | Measure-Object -Property Length -Sum).Sum
$mb = [math]::Round($total / 1MB, 1)

Write-Output "Carpeta de temporales: $carpeta"
Write-Output "Archivos encontrados: $($archivos.Count)"
Write-Output "Espacio que ocupan: $mb MB"
Write-Output "(Simulacion: no se ha borrado nada.)"

# Para borrar de verdad, descomenta la siguiente linea:
# $archivos | Remove-Item -Force -ErrorAction SilentlyContinue
