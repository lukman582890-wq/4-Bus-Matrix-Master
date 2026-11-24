$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
$BuildOut = Join-Path $Root "build_x64"
$ObjEndpoints = Join-Path $BuildOut "obj_endpoints"
$ObjDriver = Join-Path $BuildOut "obj_driver"
$PkgDir = Join-Path $BuildOut "package"

New-Item -ItemType Directory -Force -Path $ObjEndpoints, $ObjDriver, $PkgDir | Out-Null

$KitVer = "10.0.26100.0"
$KitRoot = "C:\Program Files (x86)\Windows Kits\10"
$KmInc = "$KitRoot\Include\$KitVer\km"
$KmCrt = "$KitRoot\Include\$KitVer\km\crt"
$SharedInc = "$KitRoot\Include\$KitVer\shared"
$UmInc = "$KitRoot\Include\$KitVer\um"
$KmLib = "$KitRoot\Lib\$KitVer\km\x64"
$WdfInc = "$KitRoot\Include\wdf\kmdf\1.33"
$WdfLib = "$KitRoot\Lib\wdf\kmdf\x64\1.33"
$KitBin = "$KitRoot\bin\$KitVer\x64"

$VsWhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$VsPath = & $VsWhere -latest -products * -property installationPath
$VcVars = Join-Path $VsPath "VC\Auxiliary\Build\vcvars64.bat"

# Import vcvars64 environment into current PowerShell session
$envDump = cmd /c "`"$VcVars`" >nul && set"
foreach ($line in $envDump) {
    if ($line -match "^([^=]+)=(.*)$") {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
    }
}

$Defines = @(
    "/D_WIN64", "/D_AMD64_", "/DAMD64",
    "/D_WIN32", "/DUNICODE", "/D_UNICODE",
    "/DPC_IMPLEMENTATION", "/DDEBUG_LEVEL=DEBUGLVL_TERSE",
    "/D_USE_WAVERT_", "/DSYSVAD_BTH_BYPASS", "/DSYSVAD_USB_SIDEBAND",
    "/D_USE_IPortClsRuntimePower", "/D_NEW_DELETE_OPERATORS_",
    "/DNTDDI_VERSION=0x0A00000C", "/D_WIN32_WINNT=0x0A00", "/DWINVER=0x0A00",
    "/DKMDF_VERSION_MAJOR=1", "/DKMDF_VERSION_MINOR=33"
)

$IncFlags = @(
    "/I$KmCrt", "/I$KmInc", "/I$SharedInc", "/I$WdfInc",
    "/I$Root", "/I$Root\EndpointsCommon", "/I$Root\TabletAudioSample"
)

$ClFlags = @(
    "/nologo", "/c", "/kernel", "/std:c++17", "/O2", "/Oi", "/W3",
    "/GS", "/guard:cf-", "/Zc:wchar_t", "/Zc:inline", "/GF", "/Gy", "/EHs-c-"
) + $Defines + $IncFlags

Write-Host "==> Compiling EndpointsCommon..." -ForegroundColor Cyan
$EndpointFiles = @(
    "a2dphpminwavert.cpp", "a2dphpspeakertopo.cpp", "a2dphptopo.cpp",
    "AudioModuleHelper.cpp", "bthhfpmictopo.cpp", "bthhfpminwavert.cpp",
    "bthhfpspeakertopo.cpp", "bthhfptopo.cpp", "micarraytopo.cpp",
    "MiniportAudioEngineNode.cpp", "MiniportStreamAudioEngineNode.cpp",
    "mintopo.cpp", "minwavert.cpp", "minwavertstream.cpp",
    "NewDelete.cpp", "speakerhptopo.cpp", "speakertopo.cpp",
    "usbhsminwavert.cpp", "usbhsmictopo.cpp", "usbhsspeakertopo.cpp", "usbhstopo.cpp"
)

$EndpointObjs = @()
foreach ($f in $EndpointFiles) {
    $src = Join-Path "$Root\EndpointsCommon" $f
    $obj = Join-Path $ObjEndpoints ($f -replace "\.cpp$", ".obj")
    $EndpointObjs += $obj
    & cl.exe @ClFlags "/Fo$obj" $src
    if ($LASTEXITCODE -ne 0) { throw "Failed compiling $f" }
}

$EndpointsLib = Join-Path $BuildOut "EndpointsCommon.lib"
& lib.exe /nologo "/OUT:$EndpointsLib" @EndpointObjs
if ($LASTEXITCODE -ne 0) { throw "Failed creating EndpointsCommon.lib" }

Write-Host "==> Compiling TabletAudioSample..." -ForegroundColor Cyan
$DriverFiles = @(
    "$Root\A2dpHpDevice.cpp", "$Root\adapter.cpp", "$Root\basetopo.cpp",
    "$Root\BthhfpDevice.cpp", "$Root\common.cpp", "$Root\hw.cpp",
    "$Root\kshelper.cpp", "$Root\savedata.cpp", "$Root\ToneGenerator.cpp",
    "$Root\UsbHsDevice.cpp",
    "$Root\TabletAudioSample\hdmitopo.cpp",
    "$Root\TabletAudioSample\micintopo.cpp",
    "$Root\TabletAudioSample\spdiftopo.cpp"
)

$DriverObjs = @()
foreach ($src in $DriverFiles) {
    $name = [IO.Path]::GetFileNameWithoutExtension($src)
    $obj = Join-Path $ObjDriver "$name.obj"
    $DriverObjs += $obj
    & cl.exe @ClFlags "/Fo$obj" $src
    if ($LASTEXITCODE -ne 0) { throw "Failed compiling $src" }
}

Write-Host "==> Compiling Resources..." -ForegroundColor Cyan
$ResObj = Join-Path $ObjDriver "TabletAudioSample.res"
& "$KitBin\rc.exe" /nologo /D_WIN32 /DUNICODE /D_UNICODE /DPC_IMPLEMENTATION /D_USE_WAVERT_ /DSYSVAD_BTH_BYPASS /DSYSVAD_USB_SIDEBAND /D_USE_IPortClsRuntimePower /i "$KmInc" /i "$SharedInc" /i "$Root" /i "$Root\EndpointsCommon" /i "$Root\TabletAudioSample" /fo "$ResObj" "$Root\TabletAudioSample\TabletAudioSample.rc"
if ($LASTEXITCODE -ne 0) { throw "Failed compiling TabletAudioSample.rc" }

Write-Host "==> Linking tabletaudiosample.sys..." -ForegroundColor Cyan
$SysPath = Join-Path $PkgDir "tabletaudiosample.sys"
$Libs = @(
    "$KmLib\portcls.lib",
    "$KmLib\stdunk.lib",
    "$KmLib\libcntpr.lib",
    "$KmLib\ntoskrnl.lib",
    "$KmLib\hal.lib",
    "$KmLib\wmilib.lib",
    "$KmLib\BufferOverflowFastFailK.lib",
    "$WdfLib\WdfDriverEntry.lib",
    "$WdfLib\WdfLdr.lib",
    $EndpointsLib
)

& link.exe /nologo /DRIVER /SUBSYSTEM:NATIVE /ENTRY:FxDriverEntry /NODEFAULTLIB /MERGE:_TEXT=.text /MERGE:_PAGE=PAGE /OPT:REF /OPT:ICF /INCREMENTAL:NO /MANIFEST:NO /INTEGRITYCHECK "/OUT:$SysPath" @DriverObjs $ResObj @Libs
if ($LASTEXITCODE -ne 0) { throw "Failed linking tabletaudiosample.sys" }

Write-Host "==> Preparing INF & CAT package..." -ForegroundColor Cyan
$InfPath = Join-Path $PkgDir "ComponentizedAudioSample.inf"
Copy-Item "$Root\TabletAudioSample\ComponentizedAudioSample.inx" $InfPath -Force

# Remove optional KeywordDetectorContosoAdapter DLL references so package is self-contained
$infText = Get-Content $InfPath -Raw
$infText = $infText -replace "(?m)^keywordDetectorContosoAdapter\.dll=.*\r?\n", ""
$infText = $infText -replace "(?m)^\[KEYWORDDETECTORCONTOSOADAPTER\.CopyList\]\r?\nkeyworddetectorcontosoadapter\.dll\r?\n", "[KEYWORDDETECTORCONTOSOADAPTER.CopyList]`r`n"
Set-Content -Path $InfPath -Value $infText -Encoding ASCII

& "$KitBin\stampinf.exe" -f $InfPath -a amd64 -d * -v 1.0.0.2 -k 1.33 -u 1.33.0
if ($LASTEXITCODE -ne 0) { throw "stampinf failed" }

$Inf2Cat = "$KitRoot\bin\$KitVer\x86\Inf2Cat.exe"
& $Inf2Cat "/driver:$PkgDir" "/os:10_VB_X64,ServerFE_X64"
if ($LASTEXITCODE -ne 0) { throw "inf2cat failed" }

Write-Host "==> Creating Test Certificate & Signing..." -ForegroundColor Cyan
$Cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Subject -eq "CN=MatrixAudioTestCert" } | Select-Object -First 1
if (-not $Cert) {
    $Cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=MatrixAudioTestCert" -CertStoreLocation "Cert:\CurrentUser\My" -NotAfter (Get-Date).AddYears(5)
}
$CerPath = Join-Path $PkgDir "MatrixAudioTestCert.cer"
Export-Certificate -Cert $Cert -FilePath $CerPath -Force | Out-Null

$CatPath = Join-Path $PkgDir "sysvad.cat"
& "$KitBin\signtool.exe" sign /v /s My /n "MatrixAudioTestCert" /fd sha256 /t http://timestamp.digicert.com $SysPath
if ($LASTEXITCODE -ne 0) {
    # Offline fallback without timestamp if network blocked
    & "$KitBin\signtool.exe" sign /v /s My /n "MatrixAudioTestCert" /fd sha256 $SysPath
    if ($LASTEXITCODE -ne 0) { throw "signtool failed on .sys" }
}
& "$KitBin\signtool.exe" sign /v /s My /n "MatrixAudioTestCert" /fd sha256 $CatPath
if ($LASTEXITCODE -ne 0) { throw "signtool failed on .cat" }

Write-Host "`nBUILD & SIGN SUCCESSFUL!" -ForegroundColor Green
Write-Host "Package ready at: $PkgDir" -ForegroundColor Green
