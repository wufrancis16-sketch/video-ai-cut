# AI Video Auto Editor（video-ai-cut）

上传一段销售 / 客户沟通 / 会议类视频（mp4），一键全自动处理成适合企业宣传与短视频发布的成片：
**带字幕、敏感信息已脱敏消音、无效停顿已压缩、议价与高风险隐私画面已删除、含 16:9 封面片头、可一键同步到微信视频号草稿箱。**

面向新媒体运营、销售演示、产品讲解等长视频（数十分钟亦可），全片**只做一次完整重编码**，所有剪辑决策集中在 `plan.json` 里以绝对时间区间表达，改规则可秒级重算。

> 🌟 **新功能：视频号草稿同步** — 剪辑成片后可直接上传到微信视频号草稿箱（**不发布**），首次扫码一次后永久免扫码。详见下方「📺 视频号草稿同步」独立章节。

---

## 功能清单

| # | 功能 | 实现方式 |
|---|------|----------|
| 1 | **🎬 视频号草稿同步**（独立章节） | Playwright 驱动本机 Chrome 操作视频号助手网页版；登录态用独立 profile 持久化，**首次扫码一次后永久免扫码**；自动上传成片 + 填标题 + 点「保存草稿」**不发布**；render 完成后可自动触发 |
| 2 | 自动生成中文字幕 | 提取音频 -> faster-whisper ASR -> 生成中文 ASS 字幕并**烧录**，同时导出与成片对齐的 SRT |
| 2 | 敏感商业信息检测 + 消音 | 关键词+正则+可选 LLM 语义判断；命中段**原声替换为哔声**，画面保留，字幕同步脱敏为 `【哔——】` |
| 3 | 压缩无效停顿 / 口头禅 | 基于 ASR 句间隔：≤1s 保持；1~3s 裁短；>3s 删除（保留过渡）；纯口头禅整句删除（`pause_mode`: trim 默认 / speed 变速 / off 关闭） |
| 4 | 腾讯会议开场裁剪 | 两阶段检测蓝色开场屏 + 白底等候室 -> 软件界面跳变点，自动裁掉开头 |
| 5 | 行业术语纠错 | `glossary.TRADE_TERMS` 高置信错词表（商贸/五金/门店/对账…），用户反馈新错词可在此追加 |
| 6 | 议价内容整段删除 | 价格/报价/折扣/付款等商务谈判；LLM 上下文分析优先，无 LLM 退化为关键词聚类合并成段 |
| 7 | 高风险敏感画面删除 | **两种互补检测器**：`analyze` 内 `risk_screen`（ASR 关键词 + 可选视觉 LLM，通用高风险界面）；`inspect` 子命令 `screen_inspect`（OCR 文字精准识别**企业微信**，区分企微与畅捷通/好生意等同构 SaaS 左栏）。拿不准一律标「待人工确认」 |
| 8 | 16:9 横屏封面片头 | 抽软件界面帧 -> 裁黑边 -> 标题居中叠加（5 套预设）-> 转 3s 静态视频拼入片头（同一次编码内 concat） |

---

## 视频号草稿同步（一行概述）

成片可一键上传到**微信视频号草稿箱**（只存草稿不发布）：`python main.py sync "成片.mp4" --title "标题" --headed`（首次扫码一次，之后免扫码；登录态持久化在 `~/.workbuddy/channels_profile`）。render 后可用 `AVEditor_SYNC_CHANNEL_ENABLED=true` 自动触发。详细流程见 `INSTALL.md`「三.5、视频号草稿同步」章节。

---

## 技术方案

- **后端**：Python 3.10+
- **架构**：两阶段（`analyze` 只产出 `plan.json` -> 可选 `inspect`/`review` -> `render` 只读 plan 做一次编码）+ 统一剪辑时间轴
- **视频处理**：FFmpeg（帧级精确 `select` 删除 + 字幕烧录 + 封面拼接；多切点走「单 select 内联 -> 分窗单命令 -> 无损中间片段」三级路径规避 OOM）
- **音频处理**：Python `wave`+采样级精确剪辑（消音->哔声->按保留区间拼接->片头静音）
- **语音识别**：faster-whisper（本地开源 ASR，结果按文件指纹缓存）
- **AI 分析**：OpenAI 兼容大模型 API（敏感语义 / 议价分析 / 封面标题 / 视觉分类）；缺失自动降级为关键词/启发式，流水线不中断
- **图像处理**：Pillow（封面绘制）；RapidOCR（企业微信左栏中文识别，纯 pip 无需外部 tesseract）

---

## 安装

**分平台安装说明**（WorkBuddy 用户 / Codex 用户）：见 **[`INSTALL-PLATFORMS.md`](INSTALL-PLATFORMS.md)**（一键 clone + install.bat / install.sh，自动装依赖 + 检测 FFmpeg + 自检）。

```bash
# 快速安装（自动克隆到当前平台技能目录 + 装依赖 + 自检）
# Windows: git clone --depth 1 https://github.com/wufrancis16-sketch/video-ai-cut.git "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && cd "%USERPROFILE%\.workbuddy\skills\video-ai-cut" && install.bat
# macOS/Linux: git clone --depth 1 https://github.com/wufrancis16-sketch/video-ai-cut.git ~/.workbuddy/skills/video-ai-cut && cd ~/.workbuddy/skills/video-ai-cut && bash install.sh

# 手动安装（FFmpeg 需自行装好并加入 PATH）
pip install -r requirements.txt
```

> 中文 ASR 首次运行会自动下载 faster-whisper 模型（`small` 约 1.5GB，缓存后免重下）。
> 企业微信 OCR 检测依赖 `rapidocr-onnxruntime`（已列入 requirements），首次运行自动拉取 onnxruntime 等。
> 中文字体：Windows 已带微软雅黑（`msyh.ttc` 已随 skill 提供）；Linux 需安装中文字体。

---

## 使用

```bash
# 在 skill 根目录执行
cd <skill>

# 全自动（分析 -> 高风险画面巡检 -> 若有「待确认项」则交互审核 -> 渲染）
python main.py input.mp4

# 指定输出
python main.py input.mp4 -o final_video.mp4
```

### 分阶段（适合长视频 / 需要人工复核）

```bash
python main.py analyze input.mp4                 # 只分析，产出 plan.json + 审核清单.txt
python main.py inspect input.mp4 --plan <workdir>/plan.json   # v5 OCR 巡检，命中企微强词自动删
python main.py confirm --plan <workdir>/plan.json --action delete --items 1,2   # 人工确认删除指定候选
python main.py render  input.mp4 --plan <workdir>/plan.json -o out.mp4
```

### 跳过交互审核（安全默认：所有待确认项一律保留，不自动删除）

```bash
python main.py input.mp4 --skip-review
```

### 不使用 LLM（仅关键词敏感检测 + 首句作封面标题）

```bash
AVEditor_USE_LLM=false python main.py input.mp4
```

### 配置 LLM（可选，强烈建议用于敏感语义/议价/封面质量）

```bash
export AVEditor_LLM_API_KEY=sk-xxxx
export AVEditor_LLM_BASE_URL=https://api.deepseek.com/v1   # 任意 OpenAI 兼容
export AVEditor_LLM_MODEL=deepseek-chat
python main.py input.mp4
```

常用环境变量（`AVEditor_` 前缀）：

| 变量 | 说明 | 默认 |
|------|------|------|
| `AVEditor_ASR_MODEL` | faster-whisper 模型尺寸 | `small` |
| `AVEditor_ASR_LANGUAGE` | 识别语言 | `zh` |
| `AVEditor_DEVICE` | `cpu` / `cuda` | `cpu` |
| `AVEditor_USE_LLM` | 是否启用 LLM | `true` |
| `AVEditor_WORKDIR` | 中间文件目录 | `./_work` |
| `AVEditor_LLM_API_KEY` / `BASE_URL` / `MODEL` | LLM 连接 | 空（关闭则降级）|

---

## 处理流程

```
上传视频
   ↓ [阶段一] analyze   —— 只读分析，产出 plan.json（不编码视频）
       ASR(缓存) -> 术语纠错 -> 敏感标注 -> 客户问答识别
       -> 议价检测(delete) -> 敏感数据(局部消音 mute) -> 长停顿/口头禅(delete/speed)
       -> 开场裁剪(intro_trim) -> 高风险画面 risk_screen(delete/review)
   ↓ [阶段二] inspect   —— 高风险画面巡检（v5 OCR 精准识别企业微信）
       全片抽帧 -> 左栏 RapidOCR -> 命中强词(邮件/文档/日程/会议)自动写 delete_segments
   ↓ [人工审核 review]（可选；待确认项不自动删除）
   ↓ [阶段三] render   —— 只读 plan.json，一次 filter_complex + 一次 libx264 编码
       音频采样级精确剪辑(消音+哔声；keep 拼接；片头静音) -> 视频 select 帧级保留
       -> 字幕烧录 -> 封面拼接 -> 外挂 SRT(remap 重算成片时间轴)
   ↓
   成片 mp4（含字幕、已消敏、已压停顿、已删议价/高风险、含 3s 片头封面）
```

所有自动删除/待确认均留痕：`plan.json`（`delete_segments`/`mute_segments`/`review_items`）+ 人类可读的 `审核清单.txt`。

---

## 时长说明（处理耗时 & 成片压缩）

**处理耗时**（⚠️ 实测值，含首次运行开销；1080P / CPU 参考，视机器浮动）：

| 视频时长 | ASR 识别 | 企微 OCR 全片扫描 | 渲染（QSV） | 合计（实测参考） |
|---------|----------|------------------|------------|-----------------|
| 10 分钟 | ~2-3 min | ~20-30 min | ~4-6 min | **~30-45 min** |
| 30 分钟 | ~5-8 min | **~70-90 min** | ~10 min | **~1.5-2 小时** |
| 60 分钟 | ~10-15 min | ~2-3 小时 | ~20-30 min | **~3-4 小时** |

> **实测**：2026-08-24 剪辑 32 分钟化工演示（1080P/CPU）从提交到交付约 2 小时。**企微 OCR 全片扫描是最主要耗时**（步长 3s 逐帧 RapidOCR，召回优先，有无企微都会跑，单帧 2-8s）；渲染约 10 分钟（QSV 硬件）；首次运行另有模型下载/引擎加载/失败重试约 +10-20 分钟。有独立显卡（cuda）ASR 快 3-5 倍，但 OCR 仍是瓶颈。

**成片时长**：自动删停顿（>1s 裁短、>3s 删除）、议价、企微隐私界面、腾讯会议开场 → 通常**压缩 30-50%**（实测：32 分钟化工演示 → 17 分钟，压缩 46%）。不想裁停顿：`AVEditor_PAUSE_MODE=off python main.py 视频.mp4`。成片时长在渲染结束行打印（如 `1035.598s`）。

---

## 目录结构

```
ai_video_auto_editor/
├── main.py                 # CLI 入口（analyze / inspect / confirm / review / render）
├── requirements.txt        # Python 依赖
├── msyh.ttc                # 中文字体（封面/字幕）
├── SKILL.md                # Skill 说明（设计理念 / Hard Rules / 工作流 / 配置）
├── README.md               # 本文件
├── references/
│   └── modules.md          # 各模块职责、关键函数、数据结构
├── src/                    # Python 包（命名空间包，无需 __init__.py）
│   ├── config.py           # 配置（环境变量 AVEditor_* + 数据类）
│   ├── utils.py            # FFmpeg 调用 / 媒体探测 / 时间格式
│   ├── asr.py              # faster-whisper 转写（缓存）
│   ├── cache.py            # 分析结果缓存（文件指纹 + 参数签名）
│   ├── glossary.py         # 行业术语纠错词典
│   ├── sensitive.py        # 敏感短语检测与脱敏
│   ├── intro.py            # 腾讯会议开场检测与裁剪
│   ├── bargaining.py       # 议价内容整段删除
│   ├── analyze.py          # 阶段一：分析（不编码）
│   ├── timeline.py         # 统一剪辑时间轴 EditPlan（plan.json）
│   ├── audio.py            # 音频提取 / 抽帧
│   ├── audio_edit.py       # 采样级精确音频剪辑（消音+哔声+拼接）
│   ├── subtitle.py         # ASS / SRT 字幕生成
│   ├── cover.py            # 16:9 封面生成
│   ├── pauses.py           # 停顿剪辑滤镜（speed 模式）
│   ├── risk_screen.py      # 高风险画面（ASR 关键词 + 边界扩展，analyze 内）
│   ├── screen_inspect.py   # 高风险画面巡检（v5 OCR 企业微信，inspect 子命令）
│   ├── review.py           # 人工审核（review_items）
│   ├── llm.py              # LLM 封装（OpenAI 兼容，缺失降级）
│   └── render.py           # 阶段三：渲染（只读 plan，一次编码）
└── tests/                  # 单元测试 / 集成测试
```

> 旧架构 `pipeline.py` 与初版 `sensitive_screen.py` 已归档至 `_archive/`，新流程不再使用。

---

## 已知限制

- 高风险画面检测第一版（`sensitive_screen.py`）仅「整段删除 / 交人工确认」不打码；当前 `risk_screen` 启发式（无视觉 LLM 时）较弱，置信度不足者已标记「待人工确认」，建议配置多模态模型（`AVEditor_LLM_MODEL` 用支持看图者）提升识别率。
- 停顿检测基于 ASR 间隔；极端重叠语音可能误判。客户问答段内停顿更宽松（`protect_customer_qa`）。
- 敏感消音精确到词级时间区间（有词级时间戳时），非逐字；无词级时间戳退化为整句。
- 议价检测依赖 ASR 文本质量，tiny 模型误识率较高，建议 small/medium；无法 100% 覆盖口语化议价，建议结合 `审核清单.txt` 人工复核。
- 字幕样式固定（白字黑描边 / 底部居中），可通过 `subtitle_*` 配置字体/字号。
- 封面标题风格可选 5 套（`cover_style`：purple/red/green/dark/gold）。

## 暂不开发

自动配 BGM、自动生成动画、多账号管理、视频号**自动发布**（当前只同步到草稿箱，点「发表」这一步保留人工确认）。
