$ErrorActionPreference = "Continue"

$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsAdmin) {
    Write-Error "Jalankan script ini di PowerShell (Run as Administrator)!"
    exit 1
}

$DevCon = "C:\Program Files (x86)\Windows Kits\10\Tools\10.0.26100.0\x64\devcon.exe"

Write-Host "==> 1. Menghapus device node Root\sysvad_ComponentizedAudioSample..." -ForegroundColor Cyan
if (Test-Path $DevCon) {
    & $DevCon remove "Root\sysvad_ComponentizedAudioSample"
}
pnputil /remove-device "ROOT\MEDIA\0003" 2>$null

Write-Host "==> 2. Menghapus paket driver dari Windows DriverStore..." -ForegroundColor Cyan
$lines = (pnputil /enum-drivers) -split "`r?`n"
for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match "Published Name\s*:\s*(oem\d+\.inf)") {
        $pub = $matches[1]
        if ($i + 1 -lt $lines.Count -and $lines[$i + 1] -match "componentizedaudiosample\.inf") {
            Write-Host "Deleting $pub..."
            pnputil /delete-driver $pub /uninstall /force
        }
    }
}

Write-Host "==> 3. Menghapus service sysvad_componentizedaudiosample..." -ForegroundColor Cyan
sc.exe delete sysvad_componentizedaudiosample 2>$null

Write-Host "`nSELESAI DIHAPUS BERSIH! Sekarang kamu bisa restart ke Windows Normal Mode dengan aman." -ForegroundColor Green
