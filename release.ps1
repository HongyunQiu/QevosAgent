# release.ps1 — QevosAgent 一键发布脚本
# 用法：
#   .\release.ps1          # 自动在当前版本基础上 patch +1
#   .\release.ps1 0.4.0    # 手动指定版本号

param([string]$Version = "")

# ── 1. 读取当前版本 ──────────────────────────────────────────
$pkgPath = "desktop/package.json"
$pkg = Get-Content $pkgPath -Raw | ConvertFrom-Json
$current = $pkg.version

# ── 2. 计算新版本 ────────────────────────────────────────────
if ($Version -eq "") {
    $parts = $current -split '\.'
    $newVersion = "$([int]$parts[0]).$([int]$parts[1]).$([int]$parts[2] + 1)"
} else {
    $newVersion = $Version
}

if ($newVersion -notmatch '^\d+\.\d+\.\d+$') {
    Write-Host "ERROR: 版本号格式不正确，应为 X.Y.Z（例如 0.3.8）" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "  当前版本：$current" -ForegroundColor DarkGray
Write-Host "  新版本：  $newVersion" -ForegroundColor Cyan
Write-Host ""

$confirm = Read-Host "确认发布 v$newVersion？(Y/n)"
if ($confirm -eq "n" -or $confirm -eq "N") {
    Write-Host "已取消。" -ForegroundColor Yellow
    exit 0
}

# ── 3. 更新 desktop/package.json + package-lock.json ────────
Write-Host "`n[1/4] 更新 desktop 版本文件..." -ForegroundColor Green
Push-Location desktop
npm version $newVersion --no-git-tag-version --allow-same-version | Out-Null
Pop-Location

# ── 4. 更新 Android build.gradle ────────────────────────────
Write-Host "[2/4] 更新 Android 版本..." -ForegroundColor Green
$gradlePath = "mobile/android/app/build.gradle"
$parts = $newVersion -split '\.'
# versionCode：主版本*10000 + 次版本*100 + 修订版本（如 0.3.8 → 308）
$versionCode = [int]$parts[0] * 10000 + [int]$parts[1] * 100 + [int]$parts[2]

$gradle = Get-Content $gradlePath -Raw
$gradle = $gradle -replace 'versionName "[^"]*"', "versionName `"$newVersion`""
$gradle = $gradle -replace 'versionCode \d+', "versionCode $versionCode"
Set-Content $gradlePath $gradle

# ── 5. Git commit ────────────────────────────────────────────
Write-Host "[3/4] 提交代码..." -ForegroundColor Green
git add desktop/package.json desktop/package-lock.json mobile/android/app/build.gradle
git commit -m "chore: release v$newVersion"

# ── 6. Push + tag（tag push 触发 GitHub Actions）────────────
Write-Host "[4/4] 推送并打 tag，触发 CI/CD 编译..." -ForegroundColor Green
git push origin main
git tag "v$newVersion"
git push origin "v$newVersion"

Write-Host ""
Write-Host "  v$newVersion 已发布，GitHub Actions 构建已触发。" -ForegroundColor Cyan
Write-Host "  查看进度：https://github.com/HongyunQiu/QevosAgent/actions" -ForegroundColor DarkGray
Write-Host ""
