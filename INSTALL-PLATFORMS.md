# WorkBuddy 与 Codex 用户安装说明（分平台）

> 本技能遵循通用 **SKILL.md 标准**（frontmatter 含 name + description），
> WorkBuddy 与 Codex 共用同一份技能文件，只是**安装目录不同**。
> 安装前请确保已装好 [Git](https://git-scm.com/download/win) 和 **Python 3.10+**（安装时勾选 Add to PATH）。

**仓库链接：`https://github.com/wufrancis16-sketch/video-ai-cut.git`**

---

## 一、WorkBuddy 用户安装

### 1. 安装（复制这一条命令）

**Windows**：

```bat
git clone --depth 1 https://github.com/wufrancis16-sketch/video-ai-cut.git "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && cd "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && install.bat
```

GitHub 慢/打不开（用镜像）：

```bat
git clone --depth 1 https://ghproxy.com/https://github.com/wufrancis16-sketch/video-ai-cut.git "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && cd "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && install.bat
```

**macOS / Linux**：

```bash
git clone --depth 1 https://github.com/wufrancis16-sketch/video-ai-cut.git ~/.workbuddy/skills/video-ai-cut && cd ~/.workbuddy/skills/video-ai-cut && bash install.sh
```

### 2. 安装脚本自动做了 4 件事

1. 把技能放进 WorkBuddy 技能目录（Windows: `%USERPROFILE%\.workbuddy\skills\video-ai-cut`）
2. 装 Python 依赖（`pip install -r requirements.txt`）
3. 检测 / 自动装 FFmpeg（装不上会提示手动装）
4. 运行 `verify_skill.py` 自检（9 项，全 PASS 即环境就绪）

### 3. 使用

- **在 WorkBuddy 对话里**：新对话 → 直接说「帮我剪辑这个视频」并拖入视频 → AI 自动识别技能并处理
- **命令行**：`python "%USERPROFILE%\.workbuddy\skills\video-ai-cut\main.py" 视频.mp4`

> ⚠️ 装完技能后**新开一个对话**才会加载到最新技能。

---

## 二、Codex 用户安装

### 1. 安装（复制这一条命令）

**Windows（PowerShell / CMD）**：

```bat
git clone --depth 1 https://github.com/wufrancis16-sketch/video-ai-cut.git "%USERPROFILE%\.codex\skills\video-ai-cut" && cd "%USERPROFILE%\.codex\skills\video-ai-cut" && install.bat
```

GitHub 慢/打不开（用镜像）：

```bat
git clone --depth 1 https://ghproxy.com/https://github.com/wufrancis16-sketch/video-ai-cut.git "%USERPROFILE%\.codex\skills\video-ai-cut" && cd "%USERPROFILE%\.codex\skills\video-ai-cut" && install.bat
```

**macOS / Linux**：

```bash
git clone --depth 1 https://github.com/wufrancis16-sketch/video-ai-cut.git ~/.codex/skills/video-ai-cut && cd ~/.codex/skills/video-ai-cut && bash install.sh
```

### 2. 安装脚本自动做了 4 件事

1. 把技能放进 Codex 技能目录（Windows: `%USERPROFILE%\.codex\skills\video-ai-cut`）
2. 装 Python 依赖（`pip install -r requirements.txt`）
3. 检测 / 自动装 FFmpeg
4. 运行 `verify_skill.py` 自检

### 3. 使用

- **显式调用**：新会话输入 `$video-ai-cut` 后跟需求，例如：
  ```
  $video-ai-cut 剪辑 D:\视频\xxx.mp4，加字幕、删停顿
  ```
- **自动触发**：直接自然描述需求，Codex 会根据技能 description 自动匹配，例如：
  ```
  帮我剪辑这个视频：烧录中文字幕、把敏感数字消音、删掉腾讯会议开场
  ```

> ⚠️ **必须新开一个 Codex 会话**技能才会被识别；旧会话看不到。
> Codex 运行在沙箱中，处理视频时确认允许技能执行 `main.py` / FFmpeg 命令。

---

## 三、两个平台都要装？（可选）

```bash
# WorkBuddy
git clone --depth 1 https://github.com/wufrancis16-sketch/video-ai-cut.git ~/.workbuddy/skills/video-ai-cut
# Codex
git clone --depth 1 https://github.com/wufrancis16-sketch/video-ai-cut.git ~/.codex/skills/video-ai-cut
```

两个目录各自 `pip install -r requirements.txt`（或跑一次 `install.bat` / `install.sh`）。

---

## 四、升级到最新版

```bash
git -C ~/.workbuddy/skills/video-ai-cut pull        # WorkBuddy
git -C ~/.codex/skills/video-ai-cut pull            # Codex
```

## 五、装完后的共同步骤

1. **FFmpeg**（必装）：`install.bat`/`install.sh` 会尝试自动装；失败则手动下载 https://www.gyan.dev/ffmpeg/builds/ → 解压 → `bin` 目录加入 PATH → 重开命令行验证 `ffmpeg -version`
2. **LLM 配置（可选，经智能体使用时无需）**：**经 WorkBuddy/Codex 智能体使用时，封面标题由智能体自带 LLM 自动生成并注入，同事安装即用，无需配置任何 Key**（详见 `SKILL.md`「执行方式 → 智能体生成封面标题」）。仅当**纯命令行独立运行 `main.py`** 时才需 `AVEditor_LLM_*`（手动在 `.env` 写入或设环境变量）。
3. **自检**：`python verify_skill.py` → 9 项全 PASS
4. **首次剪辑**：自动下载 whisper 模型（~460MB），需几分钟
5. **视频号草稿同步**（可选）：`pip install playwright` + 首次 `--headed` 扫码，详见 `INSTALL.md`「三.5」

## 六、macOS 用户注意事项（2026-08-27 起已全功能支持）

**安装：**
```bash
# 1) 先装好前置（Mac 自带 python3 是 3.9，偏旧，务必装新版）
brew install python@3.13 ffmpeg git

# 2) 安装技能（WorkBuddy 用户）
git clone --depth 1 https://github.com/wufrancis16-sketch/video-ai-cut.git ~/.workbuddy/skills/video-ai-cut && cd ~/.workbuddy/skills/video-ai-cut && bash install.sh
```
GitHub 慢/打不开（用镜像）：
```bash
git clone --depth 1 https://ghproxy.com/https://github.com/wufrancis16-sketch/video-ai-cut.git ~/.workbuddy/skills/video-ai-cut && cd ~/.workbuddy/skills/video-ai-cut && bash install.sh
```

**使用注意：**
1. **用 `python3` 运行**（不用管 SKILL.md「本机运行环境」里的 Windows managed Python 路径，那是 Windows 专属铁律）
2. **Whisper 模型自动缓存**在 `~/.cache/aveditor/models`，首次剪辑自动下载（~460MB），无需手动配置
3. **渲染自动用 Apple 硬件编码**（`h264_videotoolbox`，auto 模式自动探测）：Mac 长视频提速 5~30 倍；探测失败自动回退 libx264（会慢，属正常）
4. **视频号上传自动找本机浏览器**：`/Applications/Google Chrome.app`（或 Edge / Chromium、`~/Applications`、PATH），**首次加 `--headed` 扫码一次**，之后免扫码
5. `verify_skill.py` 自检的 ffmpeg 回退路径是 Windows 硬编码，Mac 上以 `brew install ffmpeg` 后的 PATH 探测为准（`shutil.which` 能找到即 PASS）

## 七、常见问题

| 问题 | 解决 |
|------|------|
| 技能不被识别 | 新开对话/会话；确认目录名是 `video-ai-cut`（含 SKILL.md）|
| `pip install` 慢 | `pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple` |
| GitHub clone 超时 | 用 `https://ghproxy.com/` 镜像前缀 |
| verify 报 ffmpeg 缺失 | 重开命令行（PATH 刷新）；或手动装 FFmpeg |
| 处理耗时长 | 正常：企微 OCR 全片扫描为主瓶颈，30 分钟视频约 1.5-2 小时（详见 INSTALL-QUICK.md「时长说明」）|
