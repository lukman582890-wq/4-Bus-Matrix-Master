$ErrorActionPreference = "Stop"

$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsAdmin) {
    Write-Error "Jalankan script ini di PowerShell (Run as Administrator)!"
    exit 1
}

$PkgDir = Join-Path $PSScriptRoot "build_x64\package"
$InfPath = Join-Path $PkgDir "ComponentizedAudioSample.inf"
$CerPath = Join-Path $PkgDir "MatrixAudioTestCert.cer"
$DevCon = "C:\Program Files (x86)\Windows Kits\10\Tools\10.0.26100.0\x64\devcon.exe"

if (-not (Test-Path $InfPath)) {
    Write-Error "Paket driver belum dibuild. Jalankan build_driver.ps1 terlebih dahulu."
    exit 1
}

Write-Host "==> 1. Memasang Test Certificate ke Trusted Root & Trusted Publishers..." -ForegroundColor Cyan
Import-Certificate -FilePath $CerPath -CertStoreLocation "Cert:\LocalMachine\Root" | Out-Null
Import-Certificate -FilePath $CerPath -CertStoreLocation "Cert:\LocalMachine\TrustedPublisher" | Out-Null

Write-Host "==> 2. Menghapus instance & driver lama dari DriverStore (jika ada)..." -ForegroundColor Cyan
& $DevCon remove "Root\sysvad_ComponentizedAudioSample" | Out-Null
$oldInfs = pnputil /enum-drivers | Select-String -Pattern "Published Name|Original Name" -Context 0,0
$lines = (pnputil /enum-drivers) -split "`r?`n"
for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match "Published Name\s*:\s*(oem\d+\.inf)") {
        $pub = $matches[1]
        if ($i + 1 -lt $lines.Count -and $lines[$i + 1] -match "componentizedaudiosample\.inf") {
            pnputil /delete-driver $pub /uninstall /force | Out-Null
        }
    }
}

Write-Host "==> 3. Menginstall 4-Bus Matrix Virtual Audio Driver..." -ForegroundColor Cyan
& $DevCon install $InfPath "Root\sysvad_ComponentizedAudioSample"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Gagal menginstall driver (exit code $LASTEXITCODE). Pastikan PC sudah di-restart setelah 'bcdedit /set testsigning on'."
    exit $LASTEXITCODE
}

Write-Host "`nSELESAI! Cek Windows Sound Output (Win + Ctrl + V) -> '4-Bus Matrix Out 1/2' & '4-Bus Matrix Out 3/4'." -ForegroundColor Green
