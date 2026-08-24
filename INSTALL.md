# video-ai-cut 安装指南（同事电脑部署）

> 视频一键自动剪辑 Skill：字幕烧录 / 敏感信息消音 / 压缩停顿 / 删议价 / 删企微隐私界面 / 封面 + 片头。
> 兼容 **WorkBuddy** 与 **Codex**（通用 SKILL.md 标准），复制链接即可安装。

## 零、一键安装（推荐 · 复制即装）

仓库链接：**`https://github.com/wufrancis16-sketch/video-ai-cut.git`**

**Windows** 在命令行粘贴这一条（自动装到 WorkBuddy + Codex + 装依赖 + 检测 FFmpeg + 自检）：

```bat
git clone --depth 1 https://github.com/wufrancis16-sketch/video-ai-cut.git "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && cd "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && install.bat
```

GitHub 慢/打不开时用镜像：

```bat
git clone --depth 1 https://ghproxy.com/https://github.com/wufrancis16-sketch/video-ai-cut.git "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && cd "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && install.bat
```

**macOS / Linux**：

```bash
git clone --depth 1 https://github.com/wufrancis16-sketch/video-ai-cut.git ~/.workbuddy/skills/video-ai-cut && cd ~/.workbuddy/skills/video-ai-cut && bash install.sh
```

> `install.bat` / `install.sh` 自动完成：① 装到 WorkBuddy 技能目录 ② 装到 Codex 技能目录 ③ `pip install -r requirements.txt` ④ 检测/装 FFmpeg ⑤ `verify_skill.py` 自检（9 项全 PASS 即可用）。
> 前提：装好 Git + Python 3.10+（勾选 Add to PATH）。

**装完怎么用**：

| 平台 | 用法 |
|------|------|
| WorkBuddy | 新对话直接说「帮我剪辑这个视频」并拖入视频 |
| Codex | 新会话输入 `$video-ai-cut` 或自然描述剪辑需求 |
| 命令行 | `python <技能目录>\main.py 视频.mp4` |

**升级**：`git -C ~/.workbuddy/skills/video-ai-cut pull`（Codex 目录同样）。

**详细说明**：见仓库根 `INSTALL-QUICK.md`（复制即装速查）。

---

## 一、安装步骤（3 步，约 5 分钟 · 离线 zip 方式）

### 第 1 步：解压技能到 WorkBuddy skills 目录

把 `video-ai-cut.zip` 里的 **`video-ai-cut` 文件夹**解压到本机：

```
C:\Users\<你的用户名>\.workbuddy\skills\video-ai-cut\
```

（如果已有同名文件夹，先删掉旧的在解压新的）

**验证**：文件夹里应该有 `SKILL.md`、`main.py`、`src/` 目录。

### 第 2 步：安装 Python 依赖

打开命令行（Win+R → cmd），执行：

```bash
cd C:\Users\<你的用户名>\.workbuddy\skills\video-ai-cut
pip install -r requirements.txt
```

- 需要 **Python 3.10+**。
- `playwright`（视频号同步用）为可选，装了更好；不装不影响剪辑。
- faster-whisper 首次跑会自动下载模型（~460MB，small），稍等即可。

### 第 3 步：安装 FFmpeg（必装）

下载 https://www.gyan.dev/ffmpeg/builds/ 的 full build，解压后把 `bin` 目录加入系统 PATH，然后新开命令行验证：

```bash
ffmpeg -version
```

能输出版本号即可。

## 二、一键自检（推荐先跑）

```bash
python verify_skill.py
```

9 项检查全 PASS 说明环境就绪，可以放心剪辑。

## 三、使用

### 一键剪辑（最常见）

在 WorkBuddy 对话里把视频拖给 AI，说"帮我剪辑这个视频"即可；
或命令行直接跑：

```bash
python main.py "D:\视频\xxx.mp4"
```

输出：视频同目录 `edit\final_video.mp4`（成片）+ `cover.png`（封面）。

### 常用参数

| 用途 | 命令 |
|------|------|
| 指定封面标题 | `python main.py "视频.mp4" --cover-title "标题"` |
| 跳过人工确认（安全默认） | `python main.py "视频.mp4" --skip-review` |
| 仅分析不渲染 | `python main.py analyze "视频.mp4"` |
| 按已生成的 plan 渲染 | `python main.py render "视频.mp4" --plan <workdir>\plan.json` |
| 上传成片到视频号草稿 | `python main.py sync "成片.mp4" --title "标题" --headed`（首次扫码） |

### 完整流程说明

- 自动删除的片段都会写入 `审核清单.txt` 和 `plan.json`，可人工复核。
- 企业微信/微信等客户隐私界面会被识别并删除（检测器 v7 精确匹配）。
- 长视频（数十分钟）也可处理，渲染用 QSV 硬件加速约 10 分钟。
- 大模型 API（openai 兼容）**可选**：不配 Key 会自动降级为关键词检测，封面标题用首句。

## 三.5、视频号草稿同步（打通流程）

> 把剪辑好的成片自动上传到**微信视频号草稿箱**（只存草稿，**不发布**）。
> 原理：Playwright 驱动本机 Chrome/Edge 操作视频号助手网页版（`channels.weixin.qq.com`），
> 登录态用独立 profile 目录持久化（`~/.workbuddy/channels_profile`，与真实 Chrome 隔离）。

### 前置

```bash
pip install playwright   # 已含在 requirements.txt；用本机 Chrome，无需 playwright install
```

### 首次使用（只需扫码一次）

```bash
python main.py sync "成片.mp4" --title "标题" --headed --workdir <workdir>
```

- 会弹出浏览器窗口 → 用手机微信扫二维码登录视频号助手 → 自动上传 → 自动点「保存草稿」
- 登录态写入 `~/.workbuddy/channels_profile`，**之后永久免扫码**

### 之后每次使用（免扫码）

```bash
python main.py sync "成片.mp4" --title "标题" --workdir <workdir>
```

### 全自动模式（render 后自动同步）

```bash
AVEditor_SYNC_CHANNEL_ENABLED=true python main.py "视频.mp4" --cover-title "标题"
```

### 验证与注意事项

| 项 | 说明 |
|----|------|
| 确认成功 | 脚本会跳转草稿箱页解析「草稿箱 (N)」，**N > 0 才报成功**（不模糊匹配） |
| 上传耗时 | 56MB 视频需等封面/描述生成完成，最长等待 15 分钟（正常 5-10 分钟） |
| 标题 | 默认用 `--title`；视频号可能自动补充/改写标题，可在草稿箱手动改 |
| 不发布 | 只点「保存草稿」，绝不点「发表」 |
| 换账号 | 删除 `~/.workbuddy/channels_profile` 目录后重新 `--headed` 扫码 |

## 四、常见问题

| 问题 | 解决 |
|------|------|
| `❌ 未检测到 ffmpeg` | 没装 FFmpeg 或没加 PATH，见第一步 |
| 渲染时音画轻微不同步（片尾冻结） | 已知 VFR 分窗边界行为，可接受，不影响内容 |
| 出现两行字幕 | 播放器同时加载了外挂 SRT 与烧录字幕，把同名 `.srt` 移走或重命名即可 |
| OCR 检测很慢 | 正常，40 分钟视频全片扫描约 20-40 分钟，属召回优先设计 |
| 想彻底删掉企微段 | 高置信自动删；中置信写入待确认清单，`--skip-review` 则保留不删 |
| sync 报「未检测到登录态」 | 用 `--headed` 重跑扫码一次（headless 模式登录态可能不被服务端认可） |
| sync 报「草稿箱数仍为 0」 | 上传处理未完成就点了保存；重跑一次（等待逻辑已设 15 分钟上限） |
| sync 一直扫码超时 | 确认弹出的浏览器窗口可见；二维码截图在工作目录 `channels_qr.png` |
