@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
REM ============================================================
REM  video-ai-cut 一键安装脚本 (Windows)
REM  WorkBuddy + Codex 双平台自动安装
REM  用法: 双击运行, 或在命令行执行 install.bat
REM ============================================================

set "REPO=https://github.com/wufrancis16-sketch/video-ai-cut.git"
set "WB_DIR=%USERPROFILE%\.workbuddy\skills\video-ai-cut"
set "CODEX_DIR=%USERPROFILE%\.codex\skills\video-ai-cut"

echo ============================================================
echo   video-ai-cut 一键安装 (WorkBuddy + Codex)
echo ============================================================
echo.

REM ---------- 0. 检查 git ----------
where git >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 git。请先安装: https://git-scm.com/download/win
    echo        安装后重新运行本脚本。
    pause
    exit /b 1
)

REM ---------- 1. 克隆到 WorkBuddy skills 目录 ----------
echo [1/4] 安装到 WorkBuddy: %WB_DIR%
if exist "%WB_DIR%" (
    echo      已存在，执行 git pull 更新...
    cd /d "%WB_DIR%"
    git pull --ff-only origin main >nul 2>nul
    if errorlevel 1 echo      (pull 失败，将重新克隆)
    cd /d "%TEMP%"
)
if not exist "%WB_DIR%\.git" (
    echo      克隆仓库...
    git clone --depth 1 "%REPO%" "%WB_DIR%" >nul 2>nul
    if errorlevel 1 (
        echo [错误] 克隆失败。检查网络，或尝试镜像:
        echo        git clone --depth 1 https://ghproxy.com/%REPO% "%WB_DIR%"
        pause
        exit /b 1
    )
)
echo      完成.

REM ---------- 2. 克隆到 Codex skills 目录 ----------
echo [2/4] 安装到 Codex: %CODEX_DIR%
if exist "%CODEX_DIR%" (
    echo      已存在，执行 git pull 更新...
    cd /d "%CODEX_DIR%"
    git pull --ff-only origin main >nul 2>nul
    cd /d "%TEMP%"
)
if not exist "%CODEX_DIR%\.git" (
    echo      克隆仓库...
    git clone --depth 1 "%REPO%" "%CODEX_DIR%" >nul 2>nul
    if errorlevel 1 (
        echo [警告] Codex 目录克隆失败（不影响 WorkBuddy 使用）.
        echo        手动执行: git clone "%REPO%" "%CODEX_DIR%"
    )
)
echo      完成.

REM ---------- 3. 安装 Python 依赖 ----------
echo [3/4] 安装 Python 依赖...
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python。请安装 Python 3.10+: https://www.python.org/downloads/
    echo        安装时勾选 "Add python.exe to PATH"
    pause
    exit /b 1
)
cd /d "%WB_DIR%"
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [警告] 依赖安装未完全成功，可稍后手动执行:
    echo        cd "%WB_DIR%" ^&^& python -m pip install -r requirements.txt
)

REM ---------- 4. 检查 FFmpeg + 自检 ----------
echo [4/4] 检查 FFmpeg...
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [注意] 未检测到 FFmpeg（必装，用于视频处理）。
    echo        自动安装尝试: winget install --id Gyan.FFmpeg
    winget install --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements >nul 2>nul
    where ffmpeg >nul 2>nul
    if errorlevel 1 (
        echo        自动安装失败。请手动下载并加入 PATH:
        echo        https://www.gyan.dev/ffmpeg/builds/  (ffmpeg-release-full.7z)
        echo        或: https://github.com/BtbN/FFmpeg-Builds/releases
    ) else (
        echo      已安装 FFmpeg.
    )
)

echo.
echo ============================================================
echo   安装完成! 运行自检确认环境...
echo ============================================================
cd /d "%WB_DIR%"
python verify_skill.py

echo.
echo 使用方式:
echo   WorkBuddy: 打开 WorkBuddy, 新对话中直接说 "帮我剪辑视频 xxx.mp4"
echo   Codex:     新会话输入 $video-ai-cut (或直接描述剪辑需求)
echo   命令行:    python "%WB_DIR%\main.py" "视频路径.mp4"
echo.
pause
