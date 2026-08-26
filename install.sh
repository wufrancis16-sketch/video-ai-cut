#!/usr/bin/env bash
# ============================================================
#  video-ai-cut 一键安装脚本 (macOS / Linux)
#  WorkBuddy + Codex 双平台自动安装
#  用法: bash install.sh
# ============================================================
set -e

REPO="https://github.com/wufrancis16-sketch/video-ai-cut.git"
WB_DIR="$HOME/.workbuddy/skills/video-ai-cut"
CODEX_DIR="$HOME/.codex/skills/video-ai-cut"

echo "============================================================"
echo "  video-ai-cut 一键安装 (WorkBuddy + Codex)"
echo "============================================================"
echo

# ---------- 0. 检查 git ----------
if ! command -v git >/dev/null 2>&1; then
  echo "[错误] 未检测到 git，请先安装: brew install git (macOS) / apt install git (Linux)"
  exit 1
fi

# ---------- 1. WorkBuddy ----------
echo "[1/4] 安装到 WorkBuddy: $WB_DIR"
mkdir -p "$HOME/.workbuddy/skills"
if [ -d "$WB_DIR/.git" ]; then
  echo "      已存在，git pull 更新..."
  git -C "$WB_DIR" pull --ff-only origin main || true
else
  git clone --depth 1 "$REPO" "$WB_DIR" || {
    echo "[错误] 克隆失败。可用镜像: git clone --depth 1 https://ghproxy.com/$REPO $WB_DIR"
    exit 1
  }
fi
echo "      完成."

# ---------- 2. Codex ----------
echo "[2/4] 安装到 Codex: $CODEX_DIR"
mkdir -p "$HOME/.codex/skills"
if [ -d "$CODEX_DIR/.git" ]; then
  echo "      已存在，git pull 更新..."
  git -C "$CODEX_DIR" pull --ff-only origin main || true
else
  git clone --depth 1 "$REPO" "$CODEX_DIR" || {
    echo "[警告] Codex 目录克隆失败（不影响 WorkBuddy 使用）。手动执行:"
    echo "        git clone $REPO $CODEX_DIR"
  }
fi
echo "      完成."

# ---------- 3. Python 依赖 ----------
echo "[3/4] 安装 Python 依赖..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未检测到 python3，请先安装 Python 3.10+"
  exit 1
fi
python3 -m pip install -r "$WB_DIR/requirements.txt" || {
  echo "[警告] 依赖安装未完全成功，可稍后手动执行:"
  echo "        python3 -m pip install -r $WB_DIR/requirements.txt"
}

# ---------- 4. FFmpeg ----------
echo "[4/4] 检查 FFmpeg..."
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[注意] 未检测到 FFmpeg（必装）。安装方法:"
  echo "  macOS: brew install ffmpeg"
  echo "  Debian/Ubuntu: sudo apt install ffmpeg"
  echo "  安装后重新运行 verify_skill.py 确认。"
else
  echo "      FFmpeg 已就绪."
fi

# ---------- 5. 配置 LLM（可选；经智能体使用时无需配置，本步仅供纯命令行运行 main.py）----------
echo "[5/5] 配置 LLM（可选；经智能体使用时封面标题由智能体自动生成，无需配置；本步仅用于纯命令行运行 main.py）..."
CONFIGURED=0
[ -s "$WB_DIR/.env" ] && [ -s "$CODEX_DIR/.env" ] && CONFIGURED=1
if [ "$CONFIGURED" -eq 1 ]; then
  echo "      已检测到 .env 配置，跳过（如需重配可删除对应 .env 后重跑脚本）。"
else
  read -r -p "请输入 LLM API Key（DeepSeek/通义/智谱，留空跳过）: " LLMKEY || true
  if [ -n "$LLMKEY" ]; then
    LLMURL="https://api.deepseek.com/v1"
    LLMMODEL="deepseek-chat"
    read -r -p "Base URL（默认 https://api.deepseek.com/v1，回车用默认）: " LLMURL_IN || true
    [ -n "$LLMURL_IN" ] && LLMURL="$LLMURL_IN"
    read -r -p "模型名（默认 deepseek-chat，回车用默认）: " LLMMODEL_IN || true
    [ -n "$LLMMODEL_IN" ] && LLMMODEL="$LLMMODEL_IN"
    for D in "$WB_DIR" "$CODEX_DIR"; do
      if [ -d "$D/.git" ]; then
        cat > "$D/.env" <<EOF
AVEditor_LLM_API_KEY=$LLMKEY
AVEditor_LLM_BASE_URL=$LLMURL
AVEditor_LLM_MODEL=$LLMMODEL
EOF
        echo "      已写入 $D/.env"
      fi
    done
    echo "      完成。重启 WorkBuddy/Codex 会话后即可自动生成高质量封面标题。"
  else
    echo "      跳过 LLM 配置。经智能体使用时封面标题仍由智能体自动生成；仅纯命令行运行 main.py 时才需手动配（可随时重跑脚本）。"
  fi
fi

echo
echo "============================================================"
echo "  安装完成! 运行自检..."
echo "============================================================"
cd "$WB_DIR"
python3 verify_skill.py

echo
echo "使用方式:"
echo "  WorkBuddy: 新对话直接说 \"帮我剪辑视频 xxx.mp4\""
echo "  Codex:     新会话输入 \$video-ai-cut 或描述剪辑需求"
echo "  命令行:    python3 $WB_DIR/main.py \"视频路径.mp4\""
