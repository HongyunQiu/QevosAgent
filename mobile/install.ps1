# QevosAgent Android - 一键 debug 构建 + adb 推送 + 启动
#
# 用法（在仓库根目录或 mobile/ 下都行）:
#   pwsh mobile/install.ps1
#   pwsh mobile/install.ps1 -SkipBuild     # 跳过 gradle 构建，直接装现有 APK
#   pwsh mobile/install.ps1 -NoLaunch      # 装完不自动启动
#   pwsh mobile/install.ps1 -Serial <id>   # 指定设备（多设备时）

param(
    [switch]$SkipBuild,
    [switch]$NoLaunch,
    [string]$Serial
)

$ErrorActionPreference = 'Stop'

$pkg          = 'com.qevos.agent'
$launcherAct  = "$pkg/.MainActivity"
$scriptDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$androidDir   = Join-Path $scriptDir 'android'
$apkPath      = Join-Path $androidDir 'app\build\outputs\apk\debug\app-debug.apk'
$gradlew      = Join-Path $androidDir 'gradlew.bat'

function Step($msg) { Write-Host ">> $msg" -ForegroundColor Cyan }

# ── 0. 找 adb ────────────────────────────────────────────────────────────────
$adb = (Get-Command adb -ErrorAction SilentlyContinue)?.Source
if (-not $adb) {
    $localProps = Join-Path $androidDir 'local.properties'
    if (Test-Path $localProps) {
        $sdkLine = (Get-Content $localProps | Where-Object { $_ -match '^sdk\.dir=' }) -replace '^sdk\.dir=',''
        $sdkLine = $sdkLine -replace '\\\\','\' -replace '\\:',':'
        $candidate = Join-Path $sdkLine 'platform-tools\adb.exe'
        if (Test-Path $candidate) { $adb = $candidate }
    }
}
if (-not $adb) { throw "找不到 adb。请把 platform-tools 加入 PATH，或在 mobile/android/local.properties 设置 sdk.dir" }
Step "adb: $adb"

$adbArgs = @()
if ($Serial) { $adbArgs = @('-s', $Serial) }

# ── 1. 检查设备 ──────────────────────────────────────────────────────────────
$devices = & $adb @adbArgs devices | Select-String -Pattern '\sdevice$'
if (-not $devices) { throw "没有检测到已授权的 adb 设备。先 'adb devices' 确认并在手机上接受调试授权。" }
Step "设备已连接 ($($devices.Count))"

# ── 2. 构建 ──────────────────────────────────────────────────────────────────
if (-not $SkipBuild) {
    # AGP 8.2.2 需要 JDK 11+（推荐 17）。如果当前 JAVA_HOME 指向 Java 8，临时切到 17/11。
    function Get-JdkMajor($javaHome) {
        if (-not $javaHome -or -not (Test-Path "$javaHome\bin\java.exe")) { return $null }
        $out = & "$javaHome\bin\java.exe" -version 2>&1 | Select-Object -First 1
        if ($out -match '"(\d+)\.(\d+)') {
            $m1 = [int]$matches[1]; $m2 = [int]$matches[2]
            return $(if ($m1 -eq 1) { $m2 } else { $m1 })  # "1.8" => 8, "17.x" => 17
        }
        return $null
    }
    $currentMajor = Get-JdkMajor $env:JAVA_HOME
    if (-not $currentMajor -or $currentMajor -lt 11) {
        $candidates = @(
            "$env:USERPROFILE\scoop\apps\temurin17-jdk\current",
            "$env:USERPROFILE\scoop\apps\temurin11-jdk\current",
            "$env:USERPROFILE\scoop\apps\temurin21-jdk\current",
            "C:\Program Files\Eclipse Adoptium\jdk-17*",
            "C:\Program Files\Eclipse Adoptium\jdk-11*",
            "C:\Program Files\Java\jdk-17*",
            "C:\Program Files\Java\jdk-11*"
        ) | ForEach-Object { Get-Item $_ -ErrorAction SilentlyContinue } | Where-Object { $_ }
        $picked = $null
        foreach ($c in $candidates) {
            $m = Get-JdkMajor $c.FullName
            if ($m -ge 11) { $picked = $c.FullName; break }
        }
        if (-not $picked) { throw "需要 JDK 11+（推荐 17）。当前 JAVA_HOME 是 Java $currentMajor，且没在 scoop / Adoptium 默认路径找到合适的 JDK。" }
        Step "切换 JAVA_HOME -> $picked (临时)"
        $env:JAVA_HOME = $picked
        $env:PATH = "$picked\bin;$env:PATH"
    } else {
        Step "JDK: Java $currentMajor ($env:JAVA_HOME)"
    }

    Step "gradle assembleDebug"
    Push-Location $androidDir
    try {
        & $gradlew assembleDebug
        if ($LASTEXITCODE -ne 0) { throw "gradle 构建失败 (exit $LASTEXITCODE)" }
    } finally { Pop-Location }
}
if (-not (Test-Path $apkPath)) { throw "找不到 APK: $apkPath" }
Step "APK: $apkPath"

# ── 3. 停掉旧进程 ────────────────────────────────────────────────────────────
Step "停掉旧 App ($pkg)"
& $adb @adbArgs shell am force-stop $pkg | Out-Null
# 兜底：杀掉残留进程（如果有的话）
& $adb @adbArgs shell pm clear-incomplete-installations 2>$null | Out-Null

# ── 4. 安装 ──────────────────────────────────────────────────────────────────
Step "adb install -r"
& $adb @adbArgs install -r -d $apkPath
if ($LASTEXITCODE -ne 0) {
    Write-Host "安装失败，尝试卸载后重装..." -ForegroundColor Yellow
    & $adb @adbArgs uninstall $pkg | Out-Null
    & $adb @adbArgs install -d $apkPath
    if ($LASTEXITCODE -ne 0) { throw "adb install 失败" }
}

# ── 5. 启动 ──────────────────────────────────────────────────────────────────
if (-not $NoLaunch) {
    Step "启动 $launcherAct"
    & $adb @adbArgs shell am start -n $launcherAct | Out-Null
}

Write-Host "`n✓ 完成" -ForegroundColor Green
