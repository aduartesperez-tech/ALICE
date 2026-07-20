# Resumen del equipo. Solo lectura: no cambia nada.
# Alice lo ejecuta con: "ejecuta el script de estado del sistema".

$os = Get-CimInstance Win32_OperatingSystem
$libreMB = [math]::Round($os.FreePhysicalMemory / 1KB)
$totalMB = [math]::Round($os.TotalVisibleMemorySize / 1KB)
$encendido = (Get-Date) - $os.LastBootUpTime

Write-Output "Memoria libre: $libreMB MB de $totalMB MB"
Write-Output ("Encendido desde hace: {0} dias, {1} horas" -f $encendido.Days, $encendido.Hours)

foreach ($d in Get-PSDrive -PSProvider FileSystem) {
    if ($null -ne $d.Used -and $null -ne $d.Free) {
        $libreGB = [math]::Round($d.Free / 1GB, 1)
        $totalGB = [math]::Round(($d.Used + $d.Free) / 1GB, 1)
        Write-Output "Disco $($d.Name): $libreGB GB libres de $totalGB GB"
    }
}
