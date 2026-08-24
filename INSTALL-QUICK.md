# video-ai-cut 一键安装（复制即装）

> 一个 Git 链接，WorkBuddy 和 Codex 都能用。
> 本技能遵循通用 **SKILL.md 标准**，两个平台共用同一份文件，只是安装目录不同。

**仓库链接：`https://github.com/wufrancis16-sketch/video-ai-cut.git`**

---

## 🪟 Windows 用户（最简单）

**方式 A：GitHub 能访问时**，在命令行粘贴这一条：

```bat
git clone --depth 1 https://github.com/wufrancis16-sketch/video-ai-cut.git "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && cd "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && install.bat
```

**方式 B：GitHub 慢/打不开时**（用镜像），粘贴：

```bat
git clone --depth 1 https://ghproxy.com/https://github.com/wufrancis16-sketch/video-ai-cut.git "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && cd "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && install.bat
```

`install.bat` 会自动完成：
1. 安装到 WorkBuddy 技能目录 `%USERPROFILE%\.workbuddy\skills\video-ai-cut`
2. 安装到 Codex 技能目录 `%USERPROFILE%\.codex\skills\video-ai-cut`
3. 安装 Python 依赖（自动 `pip install -r requirements.txt`）
4. 检测/自动装 FFmpeg（装不上会提示手动安装）
5. 运行 `verify_skill.py` 自检（9 项，全 PASS 即可用）

> 前提：装好 [Git](https://git-scm.com/download/win) 和 [Python 3.10+](https://www.python.org/downloads/)（安装时勾选 Add to PATH）。

---

## 🐧 macOS / Linux 用户

```bash
git clone --depth 1 https://github.com/wufrancis16-sketch/video-ai-cut.git ~/.workbuddy/skills/video-ai-cut && cd ~/.workbuddy/skills/video-ai-cut && bash install.sh
```

---

## 📦 手动安装（不想用脚本）

```bash
# WorkBuddy
git clone https://github.com/wufrancis16-sketch/video-ai-cut.git ~/.workbuddy/skills/video-ai-cut
# Codex（可选，二选一或都要）
git clone https://github.com/wufrancis16-sketch/video-ai-cut.git ~/.codex/skills/video-ai-cut
# 依赖
pip install -r ~/.workbuddy/skills/video-ai-cut/requirements.txt
# FFmpeg 手动装好后，自检
python ~/.workbuddy/skills/video-ai-cut/verify_skill.py
```

---

## ✅ 装完怎么用

| 平台 | 用法 |
|------|------|
| **WorkBuddy** | 新对话直接说「帮我剪辑这个视频」并拖入视频，或命令行 `python <技能目录>\main.py 视频.mp4` |
| **Codex** | 新会话输入 `$video-ai-cut` 直接调用，或自然描述「剪辑这个视频：加字幕、删停顿、消敏感音」 |
| **命令行** | `python main.py "视频.mp4"` 一键全自动 |

## 🔄 升级到最新版

```bash
git -C ~/.workbuddy/skills/video-ai-cut pull
# Codex 也装了的话
git -C ~/.codex/skills/video-ai-cut pull
```

## 🎬 视频号草稿同步（可选）

```bash
pip install playwright
python main.py sync "成片.mp4" --title "标题" --headed   # 首次扫码，之后免扫码
```

详见 `INSTALL.md`「三.5、视频号草稿同步」章节。

---

## ⚠️ 安装注意事项（必读）

脚本能自动装依赖，但以下 3 件事**代码无法替你自动完成**，装完请逐项确认：

| # | 注意点 | 说明 |
|---|--------|------|
| 1 | **FFmpeg 是硬依赖** | 脚本会尝试 `winget install --id Gyan.FFmpeg` 自动装；失败时需手动下载（https://www.gyan.dev/ffmpeg/builds/ → ffmpeg-release-full.7z），解压后把 `bin` 目录加入系统 PATH。装完 `ffmpeg -version` 能输出版本号即可 |
| 2 | **首次 ASR 自动下载模型** | 第一次剪辑时 faster-whisper 会自动下载 `small` 模型（约 460MB），需要几分钟，属正常；下载完缓存到 `~/.cache/aveditor/models/`，之后复用 |
| 3 | **Codex 用户需新开会话** | 技能放进 `~/.codex/skills/` 后，**必须新开一个 Codex 会话**才会被识别；旧会话看不到。WorkBuddy 同理，新对话才加载最新技能 |

其他高频坑：

| 问题 | 解决 |
|------|------|
| `pip install` 很慢/超时 | 加镜像：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple` |
| GitHub clone 超时 | 用镜像前缀 `https://ghproxy.com/https://github.com/...`（见上方命令） |
| verify 报「未检测到 ffmpeg」 | FFmpeg 装了但没加 PATH，或 PATH 是旧终端会话——**重开命令行**再跑 |
| 视频号同步报「未检测到登录态」 | 加 `--headed` 重跑扫码一次（headless 模式登录态可能不被服务端认可） |
| 同一台机器想换视频号账号 | 删除 `~/.workbuddy/channels_profile/` 后重新 `--headed` 扫码 |

---

## ⏱️ 时长说明（处理耗时 & 成片压缩）

### 处理耗时（⚠️ 实测值，含首次运行开销；视机器性能浮动）

| 视频时长 | ASR 识别 | 企微 OCR 全片扫描 | 渲染（QSV） | **合计（实测参考）** |
|---------|----------|------------------|------------|---------------------|
| 10 分钟 | ~2-3 min | ~20-30 min | ~4-6 min | **~30-45 min** |
| 30 分钟 | ~5-8 min | **~70-90 min** | ~10 min | **~1.5-2 小时** |
| 60 分钟 | ~10-15 min | ~2-3 小时 | ~20-30 min | **~3-4 小时** |

> ⚠️ **实测**：2026-08-24 剪辑 32 分钟化工演示视频（1080P / CPU），从提交到交付**约 2 小时**。其中：
> - **企微 OCR 全片扫描是最主要耗时**（约 70-90 分钟）：全片步长 3s 逐帧抽帧 + RapidOCR 识别，召回优先设计，**无论有无企微界面都会跑**；本机单帧 OCR 约 2-8s（文字密集帧更慢）
> - **渲染约 10 分钟**：一次编码，QSV 硬件加速（若用 libx264 软编码会更慢）
> - **首次运行额外开销**：模型下载（~460MB）、首帧 OCR 引擎加载、失败重试等，约 +10-20 分钟
> - ASR 约 4-5 倍速；有独立显卡（cuda）ASR 可快 3-5 倍，OCR 仍是瓶颈

### 怎么让耗时可控

| 手段 | 效果 |
|------|------|
| 复用 ASR 缓存 | 同视频第二次剪辑跳过转写（省 ASR 时间） |
| `--skip-review` | 跳过人工确认交互 |
| 配置多模态 LLM | 高风险画面检测更快更准，减少 OCR 全片兜底 |
| 更高性能机器/GPU | OCR 与渲染显著提速 |

### 成片时长（会自动压缩）

剪辑会**自动删掉**：无效停顿（>1s 裁短、>3s 删除）、议价内容、企微隐私界面、腾讯会议开场 → 成片通常比原片短。

| 场景 | 原片 | 成片 | 压缩比参考 |
|------|------|------|-----------|
| 化工产品演示（实测） | 1930s（32 分钟）| 1035s（17 分钟）| **压缩 46%** |
| 通用会议/访谈 | — | — | 通常压缩 30-50% |

- 压缩幅度取决于视频里停顿/废话/议价占比，**不保证固定比例**
- 若不想压缩停顿：`AVEditor_PAUSE_MODE=off python main.py 视频.mp4`（只做字幕/消音/删敏感内容，不裁停顿）
- 成片时长会在渲染结束时打印：`[渲染] 完成 xxx.mp4 2160x1080 @ 30.297fps 1035.598s`
