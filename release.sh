#!/usr/bin/env bash
# release.sh — QevosAgent 一键发布脚本（Linux / macOS）
# 用法：
#   ./release.sh           # 自动在当前版本基础上 patch +1
#   ./release.sh 0.4.0     # 手动指定版本号

set -e

# ── 1. 读取当前版本 ──────────────────────────────────────────
PKG_PATH="desktop/package.json"
CURRENT=$(node -p "require('./$PKG_PATH').version")

# ── 2. 计算新版本 ────────────────────────────────────────────
if [ -z "$1" ]; then
    MAJOR=$(echo "$CURRENT" | cut -d. -f1)
    MINOR=$(echo "$CURRENT" | cut -d. -f2)
    PATCH=$(echo "$CURRENT" | cut -d. -f3)
    NEW_VERSION="$MAJOR.$MINOR.$((PATCH + 1))"
else
    NEW_VERSION="$1"
fi

if ! echo "$NEW_VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "ERROR: 版本号格式不正确，应为 X.Y.Z（例如 0.3.8）"
    exit 1
fi

echo ""
echo "  当前版本：$CURRENT"
echo "  新版本：  $NEW_VERSION"
echo ""
read -rp "确认发布 v$NEW_VERSION？(Y/n) " CONFIRM
if [ "$CONFIRM" = "n" ] || [ "$CONFIRM" = "N" ]; then
    echo "已取消。"
    exit 0
fi

# ── 3. 更新 desktop/package.json + package-lock.json ────────
echo ""
echo "[1/4] 更新 desktop 版本文件..."
(cd desktop && npm version "$NEW_VERSION" --no-git-tag-version --allow-same-version > /dev/null)

# ── 4. 更新 Android build.gradle ────────────────────────────
echo "[2/4] 更新 Android 版本..."
GRADLE_PATH="mobile/android/app/build.gradle"
MAJOR=$(echo "$NEW_VERSION" | cut -d. -f1)
MINOR=$(echo "$NEW_VERSION" | cut -d. -f2)
PATCH=$(echo "$NEW_VERSION" | cut -d. -f3)
VERSION_CODE=$(( MAJOR * 10000 + MINOR * 100 + PATCH ))

sed -i "s/versionName \"[^\"]*\"/versionName \"$NEW_VERSION\"/" "$GRADLE_PATH"
sed -i "s/versionCode [0-9]*/versionCode $VERSION_CODE/" "$GRADLE_PATH"

# ── 5. Git commit ────────────────────────────────────────────
echo "[3/4] 提交代码..."
git add desktop/package.json desktop/package-lock.json mobile/android/app/build.gradle
git commit -m "chore: release v$NEW_VERSION"

# ── 6. Push + tag（tag push 触发 GitHub Actions）────────────
echo "[4/4] 推送并打 tag，触发 CI/CD 编译..."
git push origin main
git tag "v$NEW_VERSION"
git push origin "v$NEW_VERSION"

echo ""
echo "  v$NEW_VERSION 已发布，GitHub Actions 构建已触发。"
echo "  查看进度：https://github.com/HongyunQiu/QevosAgent/actions"
echo ""
