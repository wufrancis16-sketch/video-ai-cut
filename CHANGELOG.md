# 更新日志 (CHANGELOG)

本文件记录 video-ai-cut 的所有重要变更。**同事拉取更新前先看这里**：每条都标注了「要不要更新」。

格式参考 [Keep a Changelog](https://keepachangelog.com/)，版本号用日期（`vYYYY.MM.DD`）。

---

## 怎么判断要不要更新 / 怎么更新

- **怎么更新**：`git -C ~/.workbuddy/skills/video-ai-cut pull`（WorkBuddy 用户）/ `git -C ~/.codex/skills/video-ai-cut pull`（Codex 用户）。**pull 完必须重开对话**才生效；用 zip 装的不能 pull，需重新 clone。
- **只想纯剪辑、现在能正常用** → 可暂不更新。
- **想要高质量封面标题且不想配 Key** → 建议更新（v2026.08.26 起封面标题由智能体自带 LLM 生成，零 Key）。
- **要使用视频号上传且希望自动填好标题/描述/话题** → **必须更新到 v2026.08.27**（之前版本上传后标题和描述为空，需手动填写）。
- **担心短标题超 16 字符保存不了草稿** → **必须更新**（v2026.08.27 起代码自动截断到 16 字符，封面标题与视频号短标题分开生成）。
- **要使用视频号上传** → 建议更新到 v2026.08.24 之后，并看下方的「首次扫码」说明。

---

## [v2026.08.27] - 2026-08-27

### 要不要更新
- **要使用视频号上传且希望自动填好标题/描述/话题** → **必须更新**（之前版本上传后短标题和视频描述为空，需手动填写才能发布）。
- 智能体使用时希望**一次性产出标题+描述+话题标签** → 建议更新。

### Added
- **视频号自动填入「短标题 + 视频描述 + 3 个话题标签」**：`channel_sync.py` 新增 `_fill_topics()` 话题标签填入功能（点击 #话题按钮 → 输入关键词 → 回车确认），以及 `_fill_text_js()` JS 兜底（Playwright 选择器全部失效时用 DOM 操作兜底）。
- `main.py sync` 新增 `--topics` 参数（支持传入多个话题标签）。
- `config.py` 新增 `channel_topics` 字段（环境变量 `AVEditor_CHANNEL_TOPICS`，逗号分隔）。
- **智能体 prompt 升级**：从只出标题 → 一次性产出「标题(≤20字) + 描述(50~150字内容摘要) + 3个#话题标签」，格式化输出方便程序解析。
- 填写完成后截图 `channels_filled.png` 存 workdir，方便排查"为什么没填上"。

### Changed
- 标题/描述选择器从单候选改为**多候选列表**（4 个 fallback），提高匹配成功率。
- SKILL.md sync 子命令文档更新，示例命令包含 `--desc` 和 `--topics`。
- **智能体生成标题改为「封面标题 + 视频号短标题」分开**：封面标题 `TITLE`（≤30 字，成片封面用，不受 16 字符限制，概括更全）；视频号短标题 `SHORT_TITLE`（**≤16 字符，视频号后台硬限制，超出无法保存草稿**）。prompt 输出格式新增 `SHORT_TITLE:` 行，sync 命令改用 `--title "<SHORT_TITLE>"`。
- **封面标题上限放宽到 ≤30 字 + 自适应字号**：`cover.py` 标题自动换行，超过 3 行按 0.88 比例缩小字号重排（下限 64px）——30 字标题也能 3 行放下、不溢出不挤（实测 30 字 → 96px / 3 行）。
- **macOS 适配（同事用 Mac 可全功能使用）**：① `channel_sync._chrome_path()` 新增 macOS 分支（`/Applications/Google Chrome.app`、Edge、Chromium、`~/Applications`、PATH 兜底）——视频号上传在 Mac 上不再报"找不到 Chrome"；② 编码器探测新增 **`h264_videotoolbox`**（Apple Silicon 硬件编码，auto 模式自动探测，长视频提速；失败回退 libx264）；③ SKILL.md 已知限制补充 macOS 使用说明。

### Fixed
- **修复视频号草稿页标题和描述为空的问题**：之前版本选择器可能未匹配到元素，或智能体调用时未传 description 参数导致跳过。现在加固选择器 + JS 兜底 + 智能体必传描述和话题。
- **⚠️ 修复短标题超 16 字符导致无法保存草稿的问题**：`sync_to_channels()` 填入短标题前**强制截断到 16 字符**并打印截断提示——无论标题来自封面标题（可 ≥16 字）、配置还是智能体，视频号短标题一律 ≤16 字符，保证草稿一定能保存。封面标题走 `render --cover-title`（写 plan.cover），不受此限制，两者互不影响。
- **加固依赖自检（稳定性）**：`_ensure_deps()` 原只检查 4 个包，漏检 `rapidocr-onnxruntime`（默认企微检测必用）与 `requests`。若一台机器核心包已装但单独缺这俩，会被误判为"依赖就绪"，跑到企微检测才崩且不触发自动安装。现已把核心依赖补全到检查列表（playwright 仍保持惰性导入、不阻断纯剪辑）。建议所有用户更新，尤其新装 / 换机器场景。

---

## [v2026.08.26] - 2026-08-26

### 要不要更新
- 经**智能体（WorkBuddy / Codex）使用**、希望封面有高质量标题 → **建议更新**。
- 想**零 Key、零交互**安装即用 → **建议更新**（安装时不再问 LLM Key）。

### Added
- 封面标题改由**智能体自带 LLM 生成**（短视频风格：前 8 字钩子 + 含行业词 + 3 候选选优），经 `render --cover-title` 注入 `plan.cover.title`，**同事无需配置任何外部 LLM Key**。
- 新增 `.env` 持久化配置机制（`src/config.py` 零依赖读取技能根目录 `.env`），优先级：显式参数 > 环境变量 > `.env` > 默认。

### Changed
- 安装脚本（`install.bat` / `install.sh`）**移除 LLM 配置步骤**：封面标题走智能体免 Key，纯命令行独立运行才需配 `AVEditor_LLM_*`。
- 封面标题**不再用「首句兜底」**（质量不佳）；无 LLM 时留空并提示。

### Fixed
- 修正 `analyze.py` 封面标题注释，反映智能体免 Key 架构。

---

## [v2026.08.24] - 2026-08-24

### 要不要更新
- 要用**视频号上传** → **必须更新到这一版之后**（新增 `sync` 模块）。

### Added
- **一键安装脚本** `install.bat` / `install.sh` + Codex 兼容 `openai.yaml` + 复制即装指南。
- **视频号草稿同步模块** `channel_sync`（`main.py sync`）：成片自动存**视频号草稿箱（不发布，停在草稿箱等手动发表）**。
  - 登录态用 `launch_persistent_context` 持久化在 `~/.workbuddy/channels_profile`，与真实 Chrome 隔离；**首次 `--headed` 扫码一次，之后免扫码自动上传**。
  - 上传后等「封面/描述/页面初始化」全部完成再点「保存草稿」，并验证草稿箱数量 > 0。
- 分平台安装文档 `INSTALL-PLATFORMS.md`（WorkBuddy / Codex 用户分别说明）。

### Changed
- **视频号同步默认关闭**（`sync_channel_enabled=false`）：只说「剪辑」不会自动传；需明确说「上传到视频号」或设 `AVEditor_SYNC_CHANNEL_ENABLED=true` 才触发。
- **哔声音量默认 0.2 → 0.12**（更不刺耳），可用 `AVEditor_BEEP_VOLUME` 调节。
- 处理耗时说明按实测值：32 分钟视频约 **2 小时**（OCR 全片扫描为主瓶颈，非 libx264 重复编码）。

### Fixed
- `channel_sync`：修登录态假阳性判定、等「生成中」真正完成再保存、严格验证草稿箱数量 > 0 才算成功。

### 已知坑（首次上传视频号必读）
- 默认 `channel_headless=true`（无头）：首次扫码**不会弹浏览器窗口**，而是把二维码截图存成 `channels_qr.png`（在 workdir 里），需手动打开图片扫。
- 想要真正的**弹窗扫码页面**：首次在 `.env` 写 `AVEditor_CHANNEL_HEADLESS=false`，或让智能体第一次用 `main.py sync --headed` 跑。
- 上传依赖 `playwright` + 本机 `Chrome/Edge`；纯剪辑不需要。未安装时 sync 优雅失败、不影响已剪好的成片。

---

> 更早的变更见 git 提交历史：`git log --pretty=format:"%h %ad %s" --date=short`
