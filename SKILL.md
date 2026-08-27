---
name: video-ai-cut
description: 销售/客户沟通/会议类视频一键全自动剪辑 Skill。提交一个 mp4，自动完成以下处理：①语音识别并烧录中文字幕；②敏感商业信息(营业额/利润/价格/联系方式/库存等)检测并消音(原声替换为哔声、字幕脱敏)；③删除无效停顿让视频更紧凑；④自动检测并裁剪腾讯会议蓝色开场/白底等候室(直到软件界面出现)；⑤领域术语纠错(商贸行业错词)；⑥自动识别并整段删除「议价内容」(价格/报价/折扣/付款等商务谈判)；⑦自动识别「高风险敏感画面」(企业微信/微信/通讯录等含客户隐私界面，确认后整段删除，无法确定边界则标记人工确认不删)；⑧截取产品页面帧生成 16:9 横屏封面(无黑边、标题居中叠加)并插入视频片头；⑨若原视频已有字幕(内嵌字幕流或硬字幕)则跳过字幕识别与烧录。所有自动删除片段均写入 审核清单.txt / plan.json 供人工复核。当用户提交视频并要求"剪辑/自动处理/生成字幕封面""把敏感信息打码""让访谈更紧凑""删掉腾讯会议开头""做个封面""字幕不对修正一下""把谈价/报价那段删掉""把微信界面删掉"时触发。基于 Python + FFmpeg + faster-whisper + OpenAI 兼容 LLM + Pillow。
agent_created: true
---

# Video AI Cut

> ⚠️ **新建对话必读「本机运行环境（Windows 实测铁律）」章节（在「配置」之前）**：本机有几个环境特殊性（ffmpeg 路径、模型缓存、VFR 帧率、render 残留清理），不遵守会直接报错或音画漂移。所有修复均以代码为准，本文档与代码保持同步（2026-08-21 核对）。
>
> 🛡️ **换对话/首次使用前，先跑一键自检**：`python verify_skill.py`（环境 + 企微检测 + 字幕单行 + 开场检测共 9 项，全 PASS 才可放心剪辑）。正确性规则已**硬化在代码里**（一键 `python main.py input.mp4`，剪辑决策不依赖对话记忆），但前提是本机环境就绪——自检脚本 30 秒确认。

一键把销售/客户沟通/会议类视频处理成适合企业宣传与短视频发布的成片。面向新媒体运营、销售演示、产品讲解等长视频（数十分钟亦可），输出带字幕、已脱敏、已压缩停顿、已删除议价与高风险画面、带片头封面的成片。

## 设计理念（与旧版的关键区别）

旧版把长视频切成 5 分钟分片、每片各自重编码、最后 concat —— 40 分钟视频会被 libx264 **重复重编码 4 次**，耗时 2~4 小时，且分片拼接易引入音画漂移。

新版采用 **两阶段 + 统一剪辑时间轴（plan.json）+ 一次编码**：

```
原始视频
   ↓ [阶段一] analyze  —— 只做分析与计划，绝不编码视频
       ASR(一次+缓存) → 术语纠错 → 敏感标注 → 客户问答识别
       → 议价检测(delete) → 敏感数据(局部消音 mute) → 长停顿/口头禅(delete)
       → 开场裁剪(intro_trim) → 高风险画面(delete/review)
       → 字幕 cue(脱敏) → 封面主题 → 规范化
   ↓
   EditPlan(plan.json)  +  审核清单.txt
   ↓ [人工审核] review（可选；待确认项不自动删除）
   ↓ [阶段二] render  —— 只读 plan.json，一次 filter_complex + 一次 libx264 编码
       音频：Python 采样级精确剪辑(消音+哔声+删除拼接+片头静音)
       视频：select 帧级精确保留区间 + 字幕烧录 + 封面拼接
   ↓
   成片 mp4（含字幕、已脱敏、已压停顿、已删议价/高风险、含片头封面）
```

**整条流水线全片只有一次 libx264 重编码**，所有剪辑决策都在 plan.json 里以绝对时间区间表达，改规则只需重算 plan（可全部走缓存），不必重跑 ASR、不必重新处理原始视频。

## 适用场景

- 用户提供一段 mp4（销售话术、客户访谈、产品讲解、腾讯会议录制、复盘录像）。
- 要求：自动加字幕、敏感信息脱敏消音、压缩无效停顿、裁剪会议开场、删除议价内容、删除高风险隐私画面、生成横屏封面。
- 不强求外部 LLM Key：**封面标题由智能体（WorkBuddy/Codex）自带的 LLM 生成并传给 `render --cover-title`，同事安装即用、零 Key 配置**。敏感检测/议价检测即使无 Key 也退化为关键词+正则/聚类，流水线不中断。纯命令行独立运行 `main.py`（无智能体托管）且未配置 `AVEditor_LLM_*` 时，封面标题留空（需手动指定或安装时配置 Key）。高风险画面检测在无视觉 LLM 时退化为弱启发式（建议配置多模态模型）。

## 优先级（所有取舍以此为准）

**隐私安全 > 剪辑准确性 > 音画同步 > 画质 > 速度**

- 高风险画面拿不准 → 标「待人工确认」，绝不自动删除（宁可多留）。
- 删除一律帧级精确（select），不用 stream copy（关键帧会让边界不精确、隐私片段可能残留）。
- 敏感数据用「精确消音+哔声」保留画面，而非整段删除。

## Hard Rules（正确性，必须遵守）

1. **分析阶段绝不编码视频**：`analyze()` 只产出 `plan.json`，不调用任何视频编码命令，不把整段视频交给大模型。所有耗时结果走缓存，改规则秒级重算。
2. **删除不用 stream copy**：最终删除全程走 `select` 帧级精确，关键帧对齐会让删除边界不精确、敏感画面可能残留几百毫秒。绝不依赖关键帧的 `-c copy` 裁剪。
3. **高风险画面不能按关键词 ±N 秒简单删**：v7 起**始终对全片低帧率扫描**（`risk_screen_sample_step`，召回优先——静默展示企微也必须删），每帧 OCR 置信度评分；确认后**向前/向后按 pad+REFINE_BUF 扩展**得到完整展示区间才删；无法可靠确定边界 / 置信度不足 / 疑似 → 一律标记「待人工确认」，**不自动删除**。严禁全片逐帧视觉 LLM。⚠ 决定性关键词**精确匹配**（模糊匹配会误删好生意产品页），腾讯会议否决仅在无强导航词时生效。
4. **帧栅格对齐保证音画同步**：保留区间在 `timeline.snap_range` 中对齐到 1/fps 帧栅格，音频保留时长 = 视频保留帧数 / fps，两者严格相等。绝不用 ffmpeg `aselect`/`atrim+concat`（音频 ~21ms 帧栅格与视频 1/fps 不对齐，累积漂移）。⚠️ **render 帧重排铁律见「本机运行环境」第 6 条**：VFR 源必须用显式 `setpts=N/{fps:.6f}/TB`，这是 2026-08-19 修复的真实漂移 bug（旧 `setpts=N/FRAME_RATE/TB` 让 40 分钟片尾差 20s）。
5. **敏感数据保留画面、只消音**：命中敏感短语的区间原声替换为正弦「哔」提示音（带淡入淡出），画面照常保留；字幕中敏感词脱敏（不出现原始数值）。
6. **LLM 只接触文本与必要图片，绝不接收整段视频/base64**：敏感/议价/封面只喂字幕文本；高风险画面分类仅抽取单帧缩略图（≤1280 宽）以 base64 发给视觉 LLM。任何视频像素操作交给 FFmpeg。
7. **字幕时间轴按成片重算**：烧录字幕在 `select` 之前（随帧丢弃自动对齐）；外挂 SRT 用 `timeline.remap_cues()` 换算到成片时间轴，已删片段的字幕不泄漏。
8. **字幕路径在 filter 内用纯文件名**：Windows 盘符冒号 `C:` 会破坏 filtergraph 解析。render 在编码前 `os.chdir(workdir)`，字幕用 `subtitles='burn.ass'`（basename）引用（见 `utils.safe_subtitle_path` + `render._build_command`）。
9. **多切点渲染走「单 select 内联 → 分窗单命令 → 无损中间片段」三级路径**（`render.py`）。两条已实测的 FFmpeg 9.0 硬限制决定了这个架构，**改动前必读**：
   - **(A) 单条 select 表达式约 90 个 `between()` 词项 / ~2300 字符即解析期 OOM**（实测 n=90 OK、n=100 `Error while parsing expression` + `Cannot allocate memory`）。
   - **(B) 多分支 select 并行读同一输入会 N 倍解码 → 内存 OOM**（实测 81 分支读同一 40 分钟源，1m47s 后 `get_buffer() failed / no frame!`）。**「多 select + concat」比单条 select 更差，不要再试。**
   - 另：`utils.ffmpeg_supports_filter_complex_script()` 在 FFmpeg 9.0 返回 False（真不支持，`ffmpeg -h` 探测为假阴性），滤镜图只能内联。但 subprocess 不经 cmd.exe，命令行上限是 `CreateProcessW` 的 **32767** 字符（不是 cmd.exe 的 8191），容量比早期假设大得多。

   三级路径：
   1. **内联单 select**（滤镜图 ≤ `SAFE_INLINE_GRAPH`=2000 字符，约 ≤65 切点）：单条 `select='not(between(...)+...)'`，只解码一次源，一次编码。
   2. **分窗单命令**（默认的多切点路径）：按保留区间**索引**分窗（每窗 `WINDOW_KEEP_SEGMENTS`=40 段），每窗是一个独立的 `-ss/-to -i` 输入（各自独立解码器、只解码自身时间窗 → 不触发 (B)），窗内单条 select 词项 ≤40 → 不触发 (A)，concat 后**只编码一次、无中间文件**。容量上限 `MAX_CMD_GRAPH`=24000 字符（约 800 切点）。
   3. **无损中间片段**（分窗滤镜图仍超 24000 时）：逐窗渲染为 libx264 `-qp 0` 无损中间片段再 concat，最终仍只有一次有损编码；代价是中间文件占磁盘。
   
   **`-ss` 语义（实测）**：输入定位会把解码后时间戳**重置为 0**。因此每窗解码后必须先 `setpts=PTS+ss/TB` 把时间轴搬回原始位置，再按**原始时间**做 select——否则烧录字幕（ASS 是原始时间轴）会整体偏移 ss 秒。该组合已实测帧级精确。
10. **LLM 缺失时自动降级**：敏感检测退化为关键词+正则，议价检测退化为关键词聚类。封面标题由**智能体**在调用 `render` 前用自身 LLM 生成并通过 `--cover-title` 注入（见「执行方式」），无需外部 Key；纯命令行无智能体且无 `AVEditor_LLM_*` 时标题留空。**不要让缺失的 LLM 阻断整条流水线。**
11. **所有中间产物放入独立 workdir，最终成片输出到输入文件同级 `edit/` 目录**，不污染原视频。
12. **所有自动删除/待确认必须留痕**：`plan.json` 含 `delete_segments`/`mute_segments`/`review_items`；`analyze.write_review_report` 写出人类可读的 `审核清单.txt`（序号/时间/类型/原因/操作）。

## 工作流程

```
用户提交视频 (mp4, 可能 40 分钟以上)
   ↓
[阶段一] analyze  —— 只读分析，产出 plan.json（不编码）
   · 腾讯会议开场检测与裁剪：两阶段扫描（高频抓蓝屏 + 低频扫白底会议室→软件界面跳变）
     → intro_trim（等价于删除 [0, trim]）
   · ASR（一次 + 文件指纹缓存；LazyModel 命中缓存不加载模型）
   · 文本后处理：术语纠错(glossary) → 敏感标注(sensitive) → 客户问答识别
   · 议价检测(bargaining) → delete_segments
   · 敏感数据(sensitive_spans) → mute_segments（精确消音+哔声，画面保留）
   · 长停顿/口头禅 → delete_segments（pause_mode: trim 裁短 / off 不动 / speed 变速）
   · 高风险画面(risk_screen) → delete_segments 或 review_items（边界扩展，不确定则交人工）
   · 字幕 cue（脱敏文本；**原视频已有字幕则跳过识别与烧录**，见「本机运行环境」第 9 条与 `subtitle_detect.py`）、封面主题（由智能体在 render 前用自身 LLM 提炼并注入，analyze 阶段先置空，详见「执行方式」）
   ↓
   plan.json  +  审核清单.txt  +  （缓存的 ASR/议价/风险屏结果）
   ↓
[阶段二] inspect  —— 高风险画面巡检（v7：OCR 强词**精确匹配**+Windows否决+腾讯会议条件否决，精准区分企微/同构 SaaS/Windows 桌面/腾讯会议）
   · 抽帧：全片每 2.0 秒抽一帧（缩放 1280px 宽，保证左栏文字清晰可读；步长铁律见「本机运行环境」第 7 条，勿改 1.0s）
   · 对每帧最左侧导航栏区域（最左 0~12% 宽，放大 2.2x）跑 **RapidOCR**
     （中文 PP-OCR 模型，纯 pip 自带、无需外部 tesseract 二进制）
      → 判定（v7）：左栏命中「企业微信」**或** ≥2 个强词（邮件/文档/日程/会议）→ 判企微删除；命中 Windows 桌面词（此电脑/回收站/控制面板/网络/快速访问）→ **否决不删**；单强词+左栏结构 → 标待确认。
      ⚠ **决定性关键词全部精确匹配，绝不用模糊匹配（edit distance≤1）**——好生意等 ERP 页面文字密集，
        模糊匹配会把 日期→日程、协议→会议、销售→消息、客户账本→客户群 误判成企微特征 → 产品页被误删
        （2026-08-21 实测 t=1500/1800 误判 8.5/8.0 分）。
      ⚠ 腾讯会议否决**仅当无强导航词时生效**，且只认「腾讯会议」产品名+精确"会议号"——
        快速会议/预定会议/加入会议/等候室 是**企微自己的会议按钮**，用作否决会把真企微帧漏删（v6 实测全漏）。
   · 相邻企微帧（间隔 ≤2.5s）合并为连续段
   · **边界精修**（`_refine_edge`）：以 0.25s 步长从段首尾锚点向两端延伸抽帧 OCR，
     命中即继续外扩；未命中（窗口载入/关闭动画、桌面）时允许纳入最多
     `seg_click_buffer=1.2s` 的「非企微过渡帧」（覆盖「点开/点走企业微信」动作本身），
     超过 `seg_refine_max=3.0s` 或视频边界即停；最终再 +`seg_pad_sec=0.3s` 安全外扩
   · conf=0.95 ≥ 0.70 → **自动写入 delete_segments**（直接删除，无需人工）
   · 每个候选抽取代表帧缩略图（区间中点，640px 宽）写入 plan.json 的 thumbnail 字段
   · 依赖：rapidocr-onnxruntime + onnxruntime（纯 pip，无需外部 tesseract）
   ↓
   plan.json（含 inspect 增量检测结果）
   ↓
[人工审核] review（可选）
   · 交互式：逐项 [d]删除 / [k]保留 / [e]改起止后删除 / [s]跳过
   · 全局 [A]全删 / [K]全留；非交互(stdin 非 tty)自动跳过不阻塞
   · 写回 plan.json + 刷新 审核清单.txt
   ↓
[阶段三] render  —— 只读 plan.json，一次 filter_complex + 一次 libx264 编码
   · 音频：Python 采样级精确剪辑（mute→哔声；keep 区间拼接；片头静音）→ 单条 WAV
   · 视频：fps 强制 CFR → select 帧级精确保留 → setpts → 字幕烧录 → 封面拼接
   · 外挂 SRT（remap_cues 重算成片时间轴）
   ↓
   成片 mp4（已含字幕、已消音、已压停顿、已删议价/高风险、含 3s 片头封面）
```

> 分阶段价值：长视频可先 `analyze` 产出 plan + 审核清单，人工 review 后再 `render`，无需一次性占用整条流水线。所有分析结果（ASR/议价/风险屏）按「文件指纹+参数签名」缓存，改规则重算 plan 时秒级完成。

### 关键实现要点（避坑）

- **开场检测**（`intro.detect_intro`）：蓝屏极短（仅 1 帧），必须用 ≥10fps 采样；白底会议室可能持续数分钟，用色彩占比/亮度方差跳变判定软件界面起点。阈值笔误 `base_color + 2.0`（比例 0~1）会让跳变永不触发，正确值为 `base_color + 0.02`。
- **议价检测**（`bargaining.detect`）：不凭单关键词删一句话，须合并成完整对话段。LLM 路径返回 `start_idx/end_idx` 句编号映射到绝对时间并外延 `bargain_pad`；无 LLM 时 `_heuristic_spans` 用 `STRONG_KEYWORDS` 命中句为锚，向前后吞并间隔 ≤`bargain_gap` 且含弱信号的相邻句。
- **高风险画面**（`risk_screen.detect`，v7）：**始终对全片按 `risk_screen_sample_step` 低帧率扫描**（召回优先，不依赖 ASR 是否提到企微——静默展示企微的段也必须删）→ 每帧 左栏高分辨率 OCR（左 14% 裁切 ~1100px）+ 整帧 OCR → **置信度评分**：强导航(邮件/文档/日程/会议，精确命中)×1 +3、×2 再 +2；弱导航(消息/通讯录/待办)各 +1(≤2)；企微专有词/联系人/绿气泡各 +2、企微蓝 +0.5；≥5 自动删除、3~5 review、<3 保留。**决定性关键词全部精确匹配**（模糊匹配会误删好生意产品页）。产品页词(商品/库存/价格…)在**无强词**时把 score 压到 ≤2 → 产品页不可能被删。否决：Windows 桌面词 → -100；腾讯会议（产品名/精确"会议号"）**且无强导航词** → -100。边界 `_expand_runs` 用采样极端帧 ±(pad+REFINE_BUF)，无密集 OCR 精修。
  > 注：`candidate_windows`/`_classify_frame` 已随 v7 重构废弃（曾依赖 ASR 关键词定位 + 视觉 LLM，导致静默企微漏删）。
- **时间轴核心**（`timeline.EditPlan`）：所有 delete/mute/speed/review/字幕都是**原始视频绝对时间**；`keep_ranges()` = delete 的补集（帧栅格对齐）；`remap_cues()` 把字幕 cue 换算到成片时间轴。`normalize()` 只做几何规范化（裁剪越界、合并重叠、剥离被删区覆盖的 mute），不做语义修改。
- **音频精确剪辑**（`audio_edit.render_audio`）：直接用 `wave`+`array`（无 numpy）在 PCM 采样上操作——mute 区间原声置零混入正弦哔声（带淡入淡出），再按 keep 区间拼接采样（采样级精确），最后对齐到期望样本数（= 视频帧数/fps）。拼接处 4ms 淡入淡出消除硬切爆音。
- **封面**（`cover.generate_cover`）：`intro.pick_product_page_frame` 截取**产品页帧**（按 方差×色彩 评分，跳过开场/企微删除段/白底等候室，**不是首帧**）→ 裁黑边 → 标题居中叠加（紫蓝/红橙/青绿/深色/金橙 5 套预设）→ 转静态视频 `trim=end_frame` 拼入片头（同一次编码内 concat，仍只编码一次）。

## 执行方式

项目代码在 skill 根目录（`main.py` 为入口，`src/` 为 Python 包）。当用户给出视频并要求处理后，直接运行：

```bash
cd <skill>

# 全自动（分析 → 高风险画面巡检 → 若有「待确认项」则交互审核 → 渲染）
python main.py "<用户视频绝对路径>"

# 分阶段（适合长视频：先 analyze，再 inspect 巡检，人工兜底确认后再 render）
python main.py analyze "<视频>"               # 只做分析，产出 plan.json + 审核清单.txt
python main.py inspect "<视频>" --plan <workdir>/plan.json   # v6: OCR 判定左栏（强词+Windows否决），命中企微即自动删
python main.py confirm --plan <workdir>/plan.json --action delete --items 1,2   # 人工确认删除指定候选
python main.py render  "<视频>" --plan <workdir>/plan.json -o out.mp4

# 跳过交互审核（安全默认：所有待确认项一律保留，不自动删除）
python main.py "<视频>" --skip-review

# 不使用 LLM（仅关键词敏感检测；封面标题走智能体生成或手动指定）
AVEditor_USE_LLM=false python main.py "<视频>"
```

### 智能体生成封面标题 + 描述 + 话题标签（零外部 Key · 推荐给同事）

`video-ai-cut` 经由智能体（WorkBuddy / Codex）调用时，**封面标题、视频号短标题、视频描述、话题标签均由智能体自带的 LLM 生成**，无需为同事配置任何外部 LLM Key。流程为「先分析拿字幕 → 智能体出标题+描述+话题 → 注入 render → sync 填入」，避免重跑 ASR：

```bash
# 1) 分析（无需 Key，敏感/议价走关键词兜底）：产出 plan.json + 审核清单.txt
python main.py analyze "<用户视频绝对路径>" --workdir <wd>

# 2) 读取 <wd>/plan.json 里 subtitle_cues[*].text 拼接成字幕，用下面 prompt 让智能体一次性生成：
#    - 封面标题 TITLE（≤30 字，成片封面用，概括更全；超 3 行自动缩小字号）
#    - 视频号短标题 SHORT_TITLE（≤16 字符！视频号后台硬限制，超出无法保存草稿）
#    - 描述（50~150 字，视频内容摘要，末尾带 #话题标签）
#    - 3 个话题标签（如 #进销存 #财务软件 #商贸管理）

# 3) 渲染并把【封面标题】注入 plan.cover（封面标题不受 16 字符限制，可 ≥16 字）
python main.py render "<用户视频绝对路径>" --plan <wd>/plan.json --cover-title "<TITLE>" -o <成片路径>

# 4) 上传视频号草稿（短标题必须 ≤16 字符，用 SHORT_TITLE；自动填标题+描述+话题；首次加 --headed 扫码一次）
python main.py sync "<成片路径>" --title "<SHORT_TITLE>" \
     --desc "<生成的描述>" --topics <话题1> <话题2> <话题3>
```

**智能体生成内容的 prompt（直接照用，一次性产出标题+描述+话题）：**

```
你是短视频运营。根据下面的视频字幕内容，生成以下四项内容：

【1】封面标题 TITLE（≤30 字，印在成片封面上，能概括视频核心内容；超 3 行会自动缩小字号）
要求：
① 前 8 字内必须有钩子（痛点 / 疑问 / 数字 / 反差）；
② 必须包含具体行业或场景词（如 化工批发 / 进销存 / 对账 / Excel / 库存）；
③ 落到痛点或收益，不要只做平铺概括；
④ 可适当展开（≤30 字），把视频解决的问题和方案讲得更完整。

【2】视频号短标题 SHORT_TITLE（≤16 字符，⚠️ 视频号后台硬限制，超出无法保存草稿！）
要求：
① 从 TITLE 精简而来，保留钩子和行业词，砍掉修饰语；
② 严格 ≤16 个字符（中文字、数字、字母都算 1 个），超了会被截断丢语义；
③ 语义完整独立，不依赖 TITLE 才能看懂。

【3】视频描述（50~150 字）
要求：
① 用 2~3 句话概括视频核心内容（讲了什么问题、给了什么方案、有什么好处）；
② 口语化、有吸引力，像在跟朋友推荐；
③ 末尾换行附上 3 个 #话题标签（见下方第 4 项）。

【4】话题标签（3 个，格式 #词）
要求：
① 必须与视频内容强相关（行业词 / 痛点词 / 场景词）；
② 覆盖不同角度（不要 3 个都是同一个意思）；
③ 每个标签 2~6 字，简洁有力。
示例：#进销存 #商贸管理 #库存盘点

输出格式（严格按此格式，方便程序解析）：
TITLE: <封面标题(≤30字)>
SHORT_TITLE: <视频号短标题(≤16字符)>
DESC: <描述>
TOPICS: #话题1 #话题2 #话题3

<字幕内容>
```

> 说明：纯命令行独立运行 `main.py`（无智能体托管）时，标题/描述/话题只能来自 `AVEditor_CHANNEL_*`（可选配置）或手动参数，否则留空。经智能体使用时无需任何配置，开箱即用。

- 默认输出：`<视频所在目录>/edit/final_video.mp4`、`<视频所在目录>/edit/cover.png`；`plan.json` 与 `审核清单.txt` 在 `workdir`（默认 `./_work`，可用 `--workdir` 指定）。
- 依赖缺失时 `main.py` 会**自动 `pip install`**；ffmpeg 缺失会提示安装方式。

### 常用参数（`main.py -h`）

| 参数 | 说明 | 默认 |
|------|------|------|
| `input` | 输入视频路径 (必填) | — |
| `-o/--output` | 成片输出路径 | `<输入目录>/edit/final_video.mp4` |
| `--cover` | 封面输出路径 | `<输入目录>/edit/cover.png` |
| `--asr-model` | `tiny/base/small/medium` 或本地模型目录 | `small` |
| `--device` | `cpu` / `cuda` | `cpu` |
| `--workdir` | 中间产物目录 | `./_work` |
| `--chunk-seconds` | 视频切片时长（秒，仅影响 ASR 缓存分块） | `300` |
| `--no-resume` | 关闭断点续处理 | 默认开启 |
| `--no-auto-install` | 关闭依赖自动安装 | 默认开启 |
| `--skip-review` | 跳过交互审核（待确认项一律保留） | 默认关闭 |

### 子命令

| 子命令 | 作用 |
|--------|------|
| `analyze <input>` | 仅分析，产出 `plan.json` + `审核清单.txt`，不渲染 |
| `inspect <input> --plan <plan.json>` | 高风险画面巡检（**v6：OCR 强词+Windows否决**），对每帧左栏跑 RapidOCR（1280 宽抽帧），命中企微专属词或 ≥2 强词(邮件/文档/日程/会议) 且无 Windows 否决即自动写 `delete_segments`，精准区分企微与畅捷通等同构 SaaS 及 Windows 桌面 |
| `confirm --plan <plan.json> --action delete\|keep [--items 1,2\|--all]` | 人工确认待确认项：将指定项转 `delete_segments`（delete）或从清单移除（keep），写回 plan |
| `review --plan <plan.json>` | 交互式审核待确认项（终端 TUI），写回 plan.json |
| `render <input> --plan <plan.json> -o <out> [--cover-title "标题"]` | 按 plan 渲染成片（只读 plan，不重新分析）；`--cover-title` 注入智能体生成的标题并写入 plan.cover（**封面标题不受 16 字符限制**，可 ≤30 字，超 3 行自动缩小字号） |
| `sync <input> --title "标题" --desc "描述" --topics #话题1 #话题2 [--cover 封面]` | 把成片上传到**视频号草稿箱**（不发布）。自动填好**短标题 + 视频描述 + 3 个话题标签**，保存后可直接点「发表」。**⚠️ 短标题硬限制 16 字符，超出无法保存草稿——代码会强制截断到 16 字符并打印提示**（封面标题走另一条路径不受此限）。登录态用 `launch_persistent_context` 持久化在 `~/.workbuddy/channels_profile`（与真实 Chrome 隔离）：**首次加 `--headed` 扫码一次**，之后**免扫码**直接上传。上传后等"封面/描述/页面初始化"全部完成再点「保存草稿」，并验证草稿箱数量 >0。全自动模式下可用 `AVEditor_SYNC_CHANNEL_ENABLED=true` 在 render 后自动触发 |
| （无子命令）`<input>` | 全自动：analyze → **inspect（v6 OCR 判定，命中企微自动删）** → render |

> **阶段顺序（硬性，需求#9）**：`analyze → inspect → review → render`。`inspect`（高风险画面两级巡检）是固定步骤，**必须在 `render` 之前完成**；漏跑会导致企业微信/微信/通讯录等隐私界面残留在成片里。全自动模式（无子命令）已内置该顺序；分阶段手动跑时必须自行在 `render` 前执行 `inspect`（其结果是追加写入 `plan.json`，不会覆盖 `analyze` 已产出的 `delete_segments`/`review_items`）。

## 本机运行环境（Windows 实测铁律 · 新建对话必读）

本 skill 在本机已稳定运行，但有几个**环境特殊性必须严格遵守**，否则新对话会重蹈覆辙（这些坑都是实测踩出来的）：

1. **Python 必须用 managed 路径**：`C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe`。不要裸 `python`/`python3`。
2. **ffmpeg/ffprobe 只认 Windows 风格路径**：传入 `E:/...`、`C:/...`（正/反斜杠均可），**绝不传 Git Bash 的 `/e/`、`/c/`**（原生 Windows 二进制不识别 MSYS 路径，报 `No such file`）。跑命令前先 `export PATH="/e/workbuddy/2026-08-10-16-44-11/ffmpeg/ffmpeg-9.0-full_build/bin:$PATH"`。
3. **ASR 模型缓存路径**：`faster-whisper` 的 `small` 模型必须指向 `C:/Users/Administrator/.cache/aveditor/models/small/`（`C:/Users/Administrator/.cache/huggingface/...` 默认缓存的 config/tokenizer 已损坏为 0 字节，会崩）。`config.model_cache_dir` 已固化，勿改。
4. **OCR 抽帧必须 1280 宽**：`screen_inspect._extract_frame(..., width=1280)`。480 宽时左栏文字过小，OCR 失败导致整段企微漏检。
5. **render 写 `render_audio.wav` 撞 PermissionError**：残留旧 wav 被进程句柄锁住。重跑前先删掉 workdir 下残留的 `render_audio.wav`/`full_audio.wav`/`render_src.wav`（目录本身可写）。
6. **VFR 源帧率铁律**：源是 VFR（r_frame_rate≠avg_frame_rate，如 30/1 vs 30.298971）→ render 帧重排**必须**用显式 `setpts=N/{fps:.6f}/TB`，**禁止** `setpts=N/FRAME_RATE/TB`（FRAME_RATE 取流标称 30，会让 40 分钟片音画漂移 ~20s）。`render._video_chain_single` 与 `render._window_vf` 已修复，改动前必读这两处。
7. **inspect 全片 OCR 步长 + VFR 抽帧铁律**：
   - `DEFAULTS["inspect_low_freq_step"] = 3.0s`（**企微界面连续展示，3s 一帧召回足够；32 分钟视频 → 644 帧 ~55min；1.0s = 1932 帧 = 6+ 小时的元凶，勿改回**）。
   - VFR 源中后段（≥900s）`ffmpeg -vf scale=1280:-1` **偶发失效**，会输出 2160x1080 全分辨率帧，OCR 跑全图变 10-13s/帧。修复：`_extract_frame` 已改为 **ffmpeg 抽原帧 jpg + Pillow 后置强制 resize 到 1280 宽**，保证 OCR 永远稳态 ~2.5s/帧。
   - 32 分钟 VFR 视频全流程预计 **~75 分钟**（analyze 分片 ASR ~9min + inspect ~55min + render ~10min；旧版 1.0s 步长同视频 ~6.4 小时）。
8. **ASR 分片转写铁律**：`asr._transcribe_chunked` 按 `cfg.chunk_seconds`（默认 300s）切片逐片转写——整条 32min 音频的 STFT 特征需 592MiB complex128 会 OOM（本机可用内存常 <3GB）。**实测 0.19x realtime**（60s 音频 11.4s），32min 音频 ~7min。参数保持 `beam_size=5` + `vad_filter=True`（低音量会议音频 RMS~100/-50dBFS，beam=1 或关 VAD 会把语音滤成 0 段）。
8. **源视频真实路径**：40 分钟测试源 `E:/工作视频/演示视频ai剪辑测试/20260807155956-珑预定的会议-视频-1.mp4`（2160x1080/30fps/**VFR**/2500.7s）；`Desktop/录制_2026_08_14_15_01_25_616.mp4` 仅 92s 回归短片，勿混淆。
9. **已有字幕跳过**：`subtitle_detect.detect_existing_subtitle` 先查内嵌字幕流（ffprobe）、再 OCR 底部字幕带判硬字幕；命中则 analyze 不生成 `subtitle_cues`、render 跳烧录。开关 `skip_subtitle_if_exists`（默认开）。
10. **inspect 断点续跑（铁律）**：`screen_inspect.run()` 每 10 帧把 `done_ts` + `suspects` pickle 到 `workdir/_screen_inspect/inspect_progress.pkl`，启动时**自动检测并跳过已 OCR 帧**（按 video/duration/step 严格匹配，匹配不上则从头来）。崩溃/Ctrl+C/超时后重跑同一视频**完全无缝续跑**，无需任何手动操作。完成后自动删 pkl。配套：每 10 帧打 `[OCR 进度] i/total (pct%) | 企微段 N | 预计还需 ~M 分钟` 进度心跳，长任务有可见进展。

## 配置（环境变量 / `src/config.py`）

环境变量前缀 `AVEditor_`。常用项：

| 变量 | 对应字段 | 默认 | 说明 |
|------|----------|------|------|
| `AVEditor_ASR_MODEL` | `asr_model` | `small` | tiny 误识多，中文商贸建议 small/medium |
| `AVEditor_ASR_LANGUAGE` | `asr_language` | `zh` | |
| `AVEditor_DEVICE` | `device` | `cpu` | 有显卡设 `cuda` |
| `AVEditor_CHUNK_SECONDS` | `chunk_seconds` | `300` | ASR 缓存分块时长 |
| `AVEditor_RESUME` | `resume` | `true` | 分析/ASR 缓存复用 |
| `AVEditor_LLM_API_KEY` | `llm_api_key` | 空 | 空则关闭 LLM |
| `AVEditor_LLM_BASE_URL` | `llm_base_url` | openai 官方 | 任意 OpenAI 兼容 |
| `AVEditor_LLM_MODEL` | `llm_model` | `gpt-4o-mini` | 能看图则更佳 |
| `AVEditor_USE_LLM` | `use_llm` | `true` | |
| `AVEditor_WORKDIR` | `workdir` | `./_work` | 中间产物目录 |

**开场裁剪**：`trim_intro`(默认开)、`intro_blue_threshold=30`、`intro_min_seconds=0.05`、`intro_max_seconds=60`、`intro_meeting_step=1.0`、`intro_meeting_scan=20.0`（**仅裁开头：腾讯会议蓝屏 + 白底等候室/会议进行中界面，直到软件界面出现**。2026-08-21 增强：无蓝屏时用 OCR 识别开头腾讯会议专属文字（腾讯会议/会议号/快速会议/创建者…）定位等候室结束点——旧版只认蓝屏，白底等候室开场会漏裁（实测 0.13s 蓝屏外还有 ~0.7s 等候室被保留）。开头既无蓝屏也无腾讯会议文字 → 忽略不裁，绝不误删真实内容）。
**议价删除**：`detect_bargaining`(默认开)、`bargain_pad=0.5`(区间两侧外延秒)、`bargain_gap=6.0`(启发式相邻议价句合并间隔秒)。
**高风险画面**：`detect_sensitive_screen`(默认开)、`sensitive_screen_mode`(auto/vision/heuristic，默认 auto)、`sensitive_screen_conf_thr=0.6`(低于则标待人工确认)、`risk_screen_keyword_pad=8.0`(预留参数，v7 全片扫描后不用于定位)、`risk_screen_sample_step=3.0`(全片抽帧步长，勿低于 2.0——稳定优先)、`risk_screen_min_screen=1.0`(高风险界面最短展示时长，低于视为闪帧标待人工确认)。
**高风险画面巡检（inspect 子命令，src/screen_inspect.py，v6 OCR 方案，手动备用路径）**：全片 `inspect_low_freq_step=2.0s` 抽帧（1280 宽）→ 左栏裁剪放大跑 RapidOCR → 判定规则：命中「企业微信」专属词或 ≥2 强词（邮件/文档/日程/会议）判企微删除；命中 Windows 桌面词（此电脑/回收站/控制面板/网络/快速访问）否决不删；单强词+左栏结构标待确认（OCR 文字模糊匹配 edit distance≤1）。相邻帧 `seg_gap_sec=2.5s` 合并 → 边界精修 `seg_refine_max=3.0s` + 过渡容忍 `seg_click_buffer=1.2s` → 末段 `seg_pad_sec=0.3s` 安全外扩；`conf_delete=0.70` 直接写 `delete_segments`（不需人工）。阈值集中在 `src/screen_inspect.py` 的 `DEFAULTS`。⚠ **默认流水线的企业微信检测在 analyze→`risk_screen.detect`（v7，精确匹配），inspect 子命令是手动备用路径**（保留 v6 模糊匹配仅用于此手动巡检）。
**停顿处理**：`pause_mode`(trim 裁短 / speed 变速 / off 不处理，默认 trim)、`pause_keep_threshold=1.0`(≤保持)、`pause_speed_threshold=3.0`(>删除)、`pause_trim_to=0.8`(1~3s 停顿保留秒)、`pause_delete_keep=0.25`(>3s 停顿保留过渡秒)、`protect_customer_qa`(默认开，客户问答停顿更宽松)。
**局部消音**：`beep_freq=1000.0`(哔声频率 Hz)、`beep_duration=30.0`(单段最长)。
**封面样式**：`cover_style`(purple/red/green/dark/gold)、`cover_font_size=110`、`cover_title_position=0.5`(正中)、`cover_bg_darken=0.20`、`cover_width=1920`、`cover_height=1080`、`cover_duration=3.0`。**封面标题支持 ≤30 字**：自动换行 + 自适应字号（超过 3 行按 0.88 比例缩小字号重排，下限 64px），长标题不溢出、不挤。
**字幕样式**：`subtitle_font`(Microsoft YaHei)、`subtitle_fontsize=48`、白字黑描边。**字幕强制单行（2026-08-21）**：analyze `_build_cues` 按单行容量拆分长句（`_split_cue`，每行 ≤ 可用宽度/字号 ≈ 34 字 @1080p），长 ASR 段拆成多条 cue 并**用词级时间戳精确分配时间**（无词时间戳则按字数比例均分）——彻底消除 libass 自动折行导致的两行字幕；外挂 SRT 同步受益。
**已有字幕跳过（subtitle_detect.py）**：`skip_subtitle_if_exists`(默认开，总开关)、`detect_burned_subtitle`(默认开)、`burned_subtitle_samples=6`(硬字幕采样帧数)、`burned_subtitle_min_hits=3`(至少命中几帧判硬字幕)。先 ffprobe 查内嵌字幕流（零成本），没有再 OCR 底部 22% 字幕带（水平居中，≥3 帧命中 + 跨帧文字变化，排除静止 UI 底栏）判硬字幕；命中则 analyze 不生成 `subtitle_cues`、render 不烧录也不出外挂 SRT，ASR 仍照常跑（敏感/议价/停顿/风险依赖它）。
**最终编码**：`final_crf=19`(ERP 文字清晰)、`final_preset=medium`、`final_audio_bitrate=192k`、`final_audio_sr=48000`、`force_cfr=true`(统一恒定帧率保证切点帧级精确)、`burn_subtitle=true`、`keep_external_subtitle=true`。

术语纠错词典在 `src/glossary.py` 的 `TRADE_TERMS`（商贸/五金/门店/对账等保守高置信短语）；用户反馈其他错词时往此表追加 `(误识, 正确)` 即可。

## 模块参考

各 `.py` 模块的职责、关键函数与参数详见 `references/modules.md`。修改代码前先读该文件定位入口。包结构（skill 根目录）：`main.py`（入口）、`src/`（`config` / `utils` / `asr` / `cache` / `glossary` / `sensitive` / `intro` / `bargaining` / `analyze` / `timeline` / `audio` / `audio_edit` / `subtitle` / `subtitle_detect` / `cover` / `pauses` / `risk_screen` / `screen_inspect` / `review` / `llm` / `render`）。旧架构 `pipeline.py` 与初版 `sensitive_screen.py` 已归档至 `_archive/`，新流程不再使用。

## 交付给用户

处理完成后，向用户报告：
- 成片路径 `final_video.mp4`（已含字幕、已消音、已压停顿、已删议价与高风险画面、含 3s 片头封面）。
- 封面路径 `cover.png`（16:9 横屏，**截取产品页帧**非首帧，标题居中叠加在软件界面上）。
- 软字幕 `final.srt`（按成片时间轴对齐，供分发与二次编辑；**原视频已有字幕则跳过不生成**）。
- **审核清单** `审核清单.txt` / `plan.json`（全部自动删除片段与待人工确认项的时间轴与原因）。
- 关键统计：识别语音段数、命中敏感片段数、停顿编辑片段数、会议开场裁剪秒数、议价删除段数+累计秒、高风险画面删除段数+累计秒、待人工确认段数、封面标题文本。

## 已知限制

- **命令行长度 / select 表达式 OOM / 多分支解码 OOM**：已由「单 select 内联 → 分窗单命令 → 无损中间片段」三级路径彻底解决（见 Hard Rule 9），任意切点数量都不会 OOM 或撞命令行上限。仅第 3 级（>约 800 切点）会产生无损中间文件占磁盘。`config.filter_batch` 字段已弃用。
- **真实长视频端到端**需要本机安装 `faster-whisper` + 可选 LLM + 原始视频文件。本环境若无 faster-whisper，可用合成视频 + patch ASR 验证流水线；40 分钟量级压力测试（合成源 + 密集剪辑方案，含 149 切点密集分窗与 80 切点长窗两种形态）已验证架构稳定。
- 字幕样式固定（白字黑描边/底部居中），可通过 `subtitle_*` 配置字体/字号。
- 敏感消音精确到段（有词级时间戳则按词），非逐字。
- 停顿检测基于 ASR 间隔；极端重叠语音可能误判。
- 封面标题风格可选 5 套（`cover_style`）；如需新增风格在 `cover.STYLE_PRESETS` 扩展。
- 议价检测依赖 ASR 文本质量，tiny 模型误识率较高，建议 small/medium；无法 100% 覆盖口语化议价，建议结合 审核清单 人工复核。
- 高风险画面检测第一版仅「整段删除 / 交人工确认」不打码；启发式（`heuristic` 模式）较弱，建议配置视觉 LLM（`sensitive_screen_mode=vision/auto` + 多模态模型）。置信度不足者已标记「待人工确认」。
- **视频号上传的账号权限硬障碍（非代码问题）**：`sync` 上传前会自动检测两类阻断弹窗并**立即失败、给出明确处理办法**，不再假报"已点击/可能保存失败"：① `no_permission` —— 编辑页显示「你还不能发表视频 当前登录账号不是视频号…的管理员或运营者」，`保存草稿`按钮处于 `weui-desktop-btn_disabled`；② `admin_verify` —— 出现「管理员本人验证 需管理员扫码验证」弹窗。两者都需人工处理：用**视频号管理员/运营者**账号重新登录（删 `~/.workbuddy/channels_profile` 让其重新扫码），或让管理员把当前账号加为运营者，或完成管理员扫码验证后重跑。草稿箱数量读取依赖左侧「草稿箱(N)」文本，无权限账号会被重定向首页读到 `-1`，此时以"点击成功即可能已存"提示用户去视频号后台人工确认。
