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

### 处理耗时（约 2 倍速 + 渲染，视机器性能浮动）

| 视频时长 | ASR 识别 | 企微 OCR 全片扫描 | 渲染（QSV 硬件） | **合计参考** |
|---------|----------|------------------|-----------------|-------------|
| 10 分钟 | ~1-2 min | ~5-10 min | ~3-5 min | **~10-15 min** |
| 30 分钟 | ~4-6 min | ~20-30 min | ~7-10 min | **~35-50 min** |
| 60 分钟 | ~8-12 min | ~40-60 min | ~12-20 min | **~60-90 min** |

- **ASR**：本地 faster-whisper small，约 4-5 倍速（10 分钟音频约 2 分钟转完）
- **企微 OCR 扫描**：全片步长 3s 逐帧 OCR（召回优先设计），是**最耗时环节**；无企微界面时结果为空但扫描仍会跑
- **渲染**：一次编码（QSV 硬件加速），约 2-4 倍速
- 以上为 1080P/CPU 推理参考值；有独立显卡（cuda）ASR 可再快 3-5 倍

### 成片时长（会自动压缩）

剪辑会**自动删掉**：无效停顿（>1s 裁短、>3s 删除）、议价内容、企微隐私界面、腾讯会议开场 → 成片通常比原片短。

| 场景 | 原片 | 成片 | 压缩比参考 |
|------|------|------|-----------|
| 化工产品演示（实测） | 1930s（32 分钟）| 1035s（17 分钟）| **压缩 46%** |
| 通用会议/访谈 | — | — | 通常压缩 30-50% |

- 压缩幅度取决于视频里停顿/废话/议价占比，**不保证固定比例**
- 若不想压缩停顿：`AVEditor_PAUSE_MODE=off python main.py 视频.mp4`（只做字幕/消音/删敏感内容，不裁停顿）
- 成片时长会在渲染结束时打印：`[渲染] 完成 xxx.mp4 2160x1080 @ 30.297fps 1035.598s`
