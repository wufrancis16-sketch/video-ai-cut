# video-ai-cut 安装指南（同事电脑部署）

> 视频一键自动剪辑 Skill：字幕烧录 / 敏感信息消音 / 压缩停顿 / 删议价 / 删企微隐私界面 / 封面 + 片头。
> 本文档给**在另一台电脑的 WorkBuddy 上安装使用**的同事看。

## 一、安装步骤（3 步，约 5 分钟）

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

## 四、常见问题

| 问题 | 解决 |
|------|------|
| `❌ 未检测到 ffmpeg` | 没装 FFmpeg 或没加 PATH，见第一步 |
| 渲染时音画轻微不同步（片尾冻结） | 已知 VFR 分窗边界行为，可接受，不影响内容 |
| 出现两行字幕 | 播放器同时加载了外挂 SRT 与烧录字幕，把同名 `.srt` 移走或重命名即可 |
| OCR 检测很慢 | 正常，40 分钟视频全片扫描约 20-40 分钟，属召回优先设计 |
| 想彻底删掉企微段 | 高置信自动删；中置信写入待确认清单，`--skip-review` 则保留不删 |
