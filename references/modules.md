# 模块参考（video-ai-cut · 新架构：两阶段 + 统一剪辑时间轴 plan.json）

skill 根目录的 `src/` 为 Python 包 `src`（命名空间包，无需 `__init__.py`），入口 `main.py` 位于 skill 根目录。流程：`analyze` 只产 `plan.json`（不编码）→ 可选 `inspect` 高风险画面巡检 → 可选 `review` 人工审核 → `render` 只读 plan 做**一次** filter_complex + **一次** libx264 编码。所有路径、参数以代码为准；本文件归纳关键函数签名与数据结构，供改代码前快速定位。

## 数据结构

### ASR segment（单条语音段）
```
{
  "text": str,            # 识别文本（经 glossary.correct_segments 纠错后）
  "start": float,         # 原始视频绝对起点（秒）
  "end": float,           # 原始视频绝对终点（秒）
  "sensitive": bool,      # sensitive.annotate_segments 写入，命中敏感则为 True
  "redacted_text": str,   # 脱敏后文本（敏感词替换），字幕烧录用此
  "words": [...],         # 词级时间戳（faster-whisper 默认开启）
  "is_question": bool,    # analyze._mark_customer_turns 写入
  "customer_turn": bool,  # 客户提问/自述业务场景（受保护，不过度剪辑）
  "protected": bool,      # 客户问答邻域保护标记
}
```

### EditPlan（统一剪辑时间轴，`timeline.EditPlan`，落盘为 plan.json）
```
{
  "source": str, "duration": float, "fps": float,
  "width": int, "height": int,
  "intro_trim": float,                 # 开场裁剪秒数（等价于删 [0, intro_trim]）
  "delete_segments": [{"start","end","type","reason", ...}],   # 整段删除
  "mute_segments":   [{"start","end","type","reason"}],         # 局部消音+哔声（画面保留）
  "speed_segments":  [{"start","end","speed"}],                 # 变速（默认不用）
  "subtitle": bool,
  "subtitle_cues": [{"start","end","text","redacted":bool}],    # 原始时间轴、已脱敏
  "review_items":   [{"start","end","type","reason","suggestion","action"}],  # 待人工确认
  "cover": dict, "stats": dict, "meta": dict
}
```
区间语义：delete/mute/speed 的 start/end 均为**原始视频绝对时间**。字幕 cue 也是原始时间轴；渲染时烧录在 select 之前随帧移动，外挂 SRT 用 `remap_cues()` 重算成片时间轴。

### 关键方法（`timeline.EditPlan`）
- `keep_ranges(snap=True)`：delete 的补集（帧栅格对齐）＝成片实际使用的原始时间段。
- `delete_ranges(snap=True)`：全部删除区间（含 intro_trim）。
- `mute_ranges(snap=True)` / `speed_ranges(snap=True)`。
- `pieces(snap=True)`：保留区间按变速切分的最小单元 `[{start,end,speed}]`，供时间映射。
- `map_time(t)` / `remap_cues(cues=None)`：原始时间→成片时间 / 字幕 cue 重算。
- `output_duration()`：成片时长。
- `normalize()`：裁剪越界、合并重叠、剥离被删区覆盖的 mute/speed（**只做几何规范化，不改语义**）。
- `review_report()`：人类可读审核清单（含已确定删除/消音 + 待确认项）。
- `save(path)` / `load(path)` / `from_dict` / `to_dict`。

---

## config.py
`Config` dataclass + 类方法 `load(**overrides)`（先读 `AVEditor_*` 环境变量再套用覆盖）。关键字段见 SKILL.md「配置」表。LLM Key 缺失时 `use_llm` 自动置 False。

## utils.py
- `run(cmd, check=True, **kw)`：执行 ffmpeg 命令，失败打印 stderr 并抛出。
- `ffmpeg_available() -> bool`：探测 ffmpeg/ffprobe 是否在 PATH。
- `ffmpeg_supports_filter_complex_script() -> bool`：探测本机 FFmpeg 是否支持 `-filter_complex_script`（FFmpeg 9.0 不支持 → render 回退内联）。
- `get_duration(path)` / `get_resolution(path)` / `probe_video(path) -> dict`：ffprobe 探测。
- `safe_subtitle_path(path) -> str`：返回纯文件名，规避 Windows 盘符冒号破坏滤镜解析。
- `concat_videos(video_list, out_path)`：用 **concat 滤镜**（非 demuxer）合并。

## asr.py
- `ensure_model(model_size, cache_dir)`：确保 faster-whisper 模型就绪（本地目录或自动下载）。
- `LazyModel(cfg)`：惰性加载包装；命中 ASR 缓存时不加载模型。
- `transcribe_video_cached(video, audio_getter, cfg, holder) -> List[segment]`：带文件指纹缓存的转写入口（一次 ASR + 缓存）。

## cache.py
- `signature(video, payload) -> str`：按视频指纹 + 参数签名生成缓存键。
- `get_or_create(cfg, key, kind, factory, label=None)`：命中缓存直接返回，否则执行 `factory()` 并落盘。所有分析结果（ASR/议价/风险屏/开场/封面）均走此缓存。

## glossary.py
- `TRADE_TERMS: List[(误识, 正确)]`：保守高置信行业错词表（商贸/五金/门店/对账…）。**用户反馈新错词时在此追加。**
- `correct_segments(segments)`：就地修正每段 `text`（在 `sensitive.annotate_segments` 之前调用，使脱敏文本基于纠错后内容）。

## sensitive.py
- `find_sensitive_spans(text) -> List[(start,end,kind)]`：关键词+正则（手机号/金额/营业额等）找敏感区间。
- `annotate_segments(segments)`：就地标 `sensitive` 并计算 `redacted_text`（敏感词替换为 `【哔——】`）。
- `add_llm_spans(segments, hits)`：融合 LLM 语义命中的敏感区间（仅文本）。
- `mask_digits(text) -> str`：对数字做掩码（审核清单/plan 也不落敏感数值）。

## subtitle.py
- `write_ass(cues, w, h, cfg, out_path)`：生成并写出 ASS 字幕（原始时间轴，烧录用；用 `redacted_text`）。
- `write_srt(cues, out_path)`：写出与成片对齐的 SRT（remap 后的 cue）。

## cover.py
- `generate_cover(frame_path, title, out_path, width=1920, height=1080, style="purple", font_size=110, title_position=0.5, bg_darken=0.20, cover_duration=3.0, crop_black=True)`：抽帧→裁黑边→cover-fit→压暗→渐变 pill→居中描边标题。风格见 `STYLE_PRESETS`（purple/red/green/dark/gold）。

## intro.py（腾讯会议开场检测与裁剪）
- `_frame_features(path) -> (white_ratio, color_ratio, luma_variance)`：区分会议室（低色彩/低方差）与软件界面（高色彩/高方差）。
- `detect_intro(source, sample_fps=10.0, max_scan=240.0, blue_thr=30.0, min_intro=0.05) -> float`：返回需删除的开场秒数（0=无）。两阶段：高频抓蓝屏(≤0.1s) + 低频扫会议室→软件跳变点；阈值 `base_color+0.02`（**非 2.0**）。
- `trim_intro(source, out_path, intro_end, crf=20, preset="veryfast")`：重编码裁掉 [0, intro_end]。

## bargaining.py（议价内容整段删除）
- `STRONG_KEYWORDS` / `WEAK_KEYWORDS` / `DEMO_KEYWORDS`：强信号（命中即疑似议价）、弱信号（仅用于上下文扩展）、明显产品演示（用于截断扩展）。
- `detect(segments, llm, cfg) -> List[{"start","end","reason","type":"议价"}]`：优先 LLM 上下文分析（`_llm_spans` 返回 `start_idx/end_idx` 句编号映射绝对时间 + `bargain_pad` 外延）；无 LLM 或失败回退 `_heuristic_spans`（关键词聚类合并成段）。
- `_heuristic_spans(segments, cfg)`：`STRONG_KEYWORDS` 命中句为锚，向前后吞并间隔 ≤`bargain_gap` 且含弱信号的相邻句，遇 `DEMO_KEYWORDS` 句停止；合并成整段而非单句。

## risk_screen.py（高风险敏感画面，NEW：边界扩展，替代旧 sensitive_screen.py）
- `KEYWORDS`：ASR 触发词（企业微信/微信/通讯录/群聊/交付群/客户群…），仅定位候选窗口。
- `VISION_PROMPT`：视觉 LLM 分类提示词，要求返回 `{risk, confidence, reason}` JSON。
- `candidate_windows(segments, dur, cfg) -> List[(s,e)]`：ASR 关键词 ± `risk_screen_keyword_pad`，合并重叠。
- `_use_vision(llm, cfg)`：auto 模式据模型名决定是否走视觉。
- `_classify_frame(img_path, llm, cfg) -> (risk, conf, reason)`：视觉 LLM 优先，否则 PIL 弱启发式 `_heuristic_risk(img_path, cfg)`。
- `_sample_window(video, t0, t1, step, llm, cfg, tmp)`：候选窗口及 ±`risk_screen_max_expand` 内按 `risk_screen_sample_step` 抽帧分类（帧数有界）。
- `_expand_run(samples, dur, cfg)`：找连续高风险段，用前后非风险样本定边界；边界不确定/置信<`sensitive_screen_conf_thr`/过短 → 标记 `待人工确认`。
- `detect(video, segments, llm, cfg) -> {"delete":[...], "review":[...]}`：主入口。**抽帧数有界**（候选数×扩展/步长），与视频总时长无关。

## screen_inspect.py（高风险画面巡检 · v5 OCR 方案，inspect 子命令专用）
- `WECHAT_STRONG_KEYS = ["邮件","文档","日程","会议"]`：企业微信左栏**强特征词**；这些词在畅捷通/好生意等同构 SaaS 左栏中绝不会出现，命中任一即判企业微信，精准无误删。
- `WECHAT_MID_KEYS = ["消息","待办"]`：中特征词，需同时命中多个才取信（避免畅捷通单独「待办」误判）。
- `_ocr_wechat_texts(img_path) -> List[str]`：取最左 12% 宽左栏区域 → 放大 2.2x → 跑 RapidOCR，返回识别文字。
- `_ocr_is_wechat(img_path) -> (bool, texts)`：命中强词即 True（或中词≥2）。
- `_refine_edge(video, anchor, direction, cfg, dur) -> float`：**边界精修**——以 0.25s 步长从锚点向两端延伸抽帧 OCR，命中继续外扩；未命中（窗口载入/关闭动画、桌面）允许纳入最多 `seg_click_buffer`(1.2s) 过渡帧（覆盖「点开/点走企业微信」动作）；超 `seg_refine_max`(3.0s) 或到视频边界即停。
- `run(video, plan_path, workdir, cfg)`：全片按 `inspect_low_freq_step`(1.0s) 抽帧(1280宽) → 每帧 OCR → 命中帧合并(`seg_gap_sec`=2.5s) → 逐段 `_refine_edge` 精修 → 写 `delete_segments`(conf=0.95)。全部阈值在模块 `DEFAULTS`。
- 与 `risk_screen.py` 的关系：`risk_screen` 在 `analyze` 内、靠 **ASR 关键词 + 可选视觉 LLM** 定位**通用**高风险界面（微信/通讯录/群聊…）；`screen_inspect` 在 `inspect` 子命令、靠 **OCR 文字**精准识别**企业微信**（即使讲解者口头未提“企业微信”也能抓）。两者互补，结果合并进同一 `delete_segments`。

## timeline.py（核心：统一剪辑时间轴，见上「数据结构」）
辅助函数：`merge_ranges` / `complement` / `subtract` / `snap_range(s,e,fps)`（帧栅格对齐，音画同步根本保证）/ `snap_ranges` / `fmt_hms` / `type_label`。`EditPlan` 关键方法见上。

## analyze.py（阶段一：分析，不编码）
- `analyze(video, cfg, llm, plan_path) -> EditPlan`：ASR(缓存) → glossary → sensitive(annotate + `_apply_llm_sensitive`) → `_mark_customer_turns`（客户问答识别 + 邻域保护）→ 议价(`_cached_bargaining`→delete) → 敏感(`_build_mute_segments`→mute) → 停顿/口头禅(`_build_pause_plan`/`_filler_segments`→delete) → 开场(`_cached_intro`→intro_trim) → 高风险画面(`_detect_risk_screen`→delete/review) → 字幕 cue(`_build_cues`) → 封面(`_build_cover_info`) → `normalize()` + `save`。
- `_mark_customer_turns(segments)`：提问句式 + 客户自述场景启发式标记 `customer_turn`，并对后随 1~3 句销售回答标 `protected`（避免过度剪辑）。
- `_build_mute_segments(segments, cfg)`：把敏感短语词级时间轴（`_char_span_to_time`）转精确消音区间（含略微前后Padding）；无词级时间戳则退化为整句消音。
- `_build_pause_plan(segments, duration, cfg)`：基于 ASR 句间隔生成长停顿删除/变速区间；`pause_mode=off` 不处理，`trim` 裁短，`speed` 变速（客户问答保护区更宽松）。
- `_filler_segments(segments)`：仅当整句去标点后完全由口头禅构成才删除（保守，不切碎正常句）。
- `write_review_report(plan, cfg, path=None)`：写出 `审核清单.txt`。

## audio_edit.py（采样级精确音频剪辑，纯标准库无 numpy）
- `render_audio(src_wav, out_wav, keep_ranges, mute_ranges=(), beep_freq=1000.0, beep_volume=0.2, lead_silence=0.0, expect_duration=None) -> dict`：mute 区间原声置零混入正弦哔声（带淡入淡出）→ 按 keep 区间**采样级精确**拼接 → 拼接处 4ms 淡入淡出 → 对齐到期望样本数（=视频帧数/fps）。返回 `{path, sample_rate, channels, duration, mute_applied, joins, aligned}`。
- `_apply_beep(buf, ch, sr, ranges, freq, volume)`：把 ranges 内原声替换为哔声。
- `make_silence(out_wav, duration, sr, ch)` / `wav_duration(path)`。

## render.py（阶段二：渲染，只读 plan，一次编码）
- `build_video_select_expr(keep, delete, fps, duration) -> str`：生成帧级精确 select 表达式（保留式/删除式取更短者；半帧内缩保证保留帧数严格 = 区间帧数）。
- `expected_output_frames(keep, fps)` / `render.render(plan, cfg, out_path, video=None) -> dict`：抽原始音频 → `audio_edit.render_audio`（消音+拼接）→ 写 ASS（原始时间轴）→ 生成封面 PNG → 一次 ffmpeg（封面 concat + 视频 select + 字幕烧录 + 已剪辑音频封装）→ 校验 + 外挂 SRT(`remap_cues`)。**全片仅一次 libx264 编码**。
- `_cover_seconds(plan, cfg, fps)`：封面片头精确帧数（整帧）。
- `_build_command(...)`：组装**内联单 select**路径的 ffmpeg 命令与 filtergraph；字幕用 `safe_subtitle_path(basename)` + `os.chdir(workdir)` 规避冒号；FFmpeg 9.0 不支持 `-filter_complex_script` 时回退内联 `-filter_complex`。
- `_video_chain_single(keep, delete, fps, duration, vf_pre) -> (chain, label)`：单条 `select` 的视频链（只解码一次源，内存安全）。
- `_inline_graph_len(keep, delete, fps, duration, has_audio) -> int`：估算内联滤镜图长度，用于三级路径选择。
- `_keep_windows(keep, fps) -> [(ss, to, wk)]`：按保留区间**索引**分窗（每窗 `WINDOW_KEEP_SEGMENTS`=40 段），前后各留 2 帧解码余量。
- `_window_vf(ss, wk, fps, duration, ass_path) -> str|None`：单窗滤镜链。**关键**：`-ss` 会把时间戳重置为 0，故先 `setpts=PTS+ss/TB` 搬回原始时间轴（保证 ASS 字幕对齐），再按原始时间 select。
- `_build_windowed_command(...) -> (cmd, graph)`：**分窗单命令**路径——每窗一个 `-ss/-to -i` 独立输入 + 单条 select，concat 后只编码一次，**无中间文件**。多切点场景的默认路径。
- `_render_chunked_video(...)`：**无损中间片段**兜底路径（分窗滤镜图 > `MAX_CMD_GRAPH`=24000 字符时），逐窗渲染 libx264 `-qp 0` 后 concat，最终仍只一次有损编码。
- 三级路径选择与 FFmpeg 9.0 的两条 OOM 硬限制详见 SKILL.md Hard Rule 9。

## review.py（人工审核，仅作用于 review_items）
- `parse_timecode(s) -> float`：支持 `MM:SS` / `HH:MM:SS` / `HH:MM:SS.mmm` / 纯秒数。
- `apply_decision(plan, index, decision, new_start=None, new_end=None, reason=None) -> str`：核心、可单测、无 stdin 依赖。`'delete'`→按边界转 delete_segments；`'keep'`→仅移除（画面保留）；其他/`'skip'`→保持原样（拿不准默认保留）。边界越界裁剪到 [0,duration]；过短(<1ms)按 keep。
- `apply_all(plan, "delete"/"keep") -> int`：批量。
- `review_plan(plan_path, cfg=None) -> EditPlan`：交互式（先打印完整审核清单 → 全局 `[1]逐项/[A]全删/[K]全留/[Q]退出` → 逐项 `[d]删除/[k]保留/[e]改起止后删除/[s]跳过`）。**非交互（stdin 非 tty）自动跳过不阻塞**。结束 `normalize()` + 写回 plan.json + 刷新同目录 `审核清单.txt`。

## llm.py
`LLM` 封装（OpenAI 兼容，缺失自动降级）。
- `available() -> bool`：Key 是否存在。
- `chat_json(prompt, temperature=0) -> Any`：通用 JSON 抽取；失败返回 None（调用方回退）。议价检测用它。
- `classify_image(prompt, image_path) -> dict|None`：视觉分类，单帧图 base64 发给多模态模型，返回 `{risk, confidence, reason}`；失败返回 None（回退启发式）。高风险画面检测用它。
- `detect_sensitive(segments) -> hits`：仅喂字幕文本做语义敏感判断。
- `cover_title(transcript) -> str`：由字幕文本生成封面标题（短视频风格，3 候选选优）；未配置 LLM 时返回空字符串，调用方不再以首句兜底，需显式指定或人工补充。

## main.py（入口，位于 skill 根目录）
子命令：
- `analyze <input>`：仅分析，产出 `plan.json` + `审核清单.txt`（不渲染）。
- `inspect <input> --plan <plan.json>`：**高风险画面两级巡检（v5 OCR）**，对每帧左栏跑 RapidOCR，命中企微强词即自动写 `delete_segments`；精准区分企微与同构 SaaS 左栏。
- `confirm --plan <plan.json> --action delete|keep [--items 1,2|--all]`：人工确认待确认项，写回 plan。
- `review --plan <path>`：交互式审核待确认项（终端 TUI），写回 plan.json。
- `render <input> --plan <path> -o <out>`：按 plan 渲染成片（只读 plan，不重新分析）。
- （无子命令）`<input>`：全自动 `analyze → inspect →（有待确认项则 review）→ render`。
- `--skip-review`：安全默认把全部待确认项按「保留」处理（不自动删除）。
- 输出默认 `<输入目录>/edit/final_video.mp4`；plan/审核清单在 `workdir`（默认 `./_work`）。依赖缺失自动 `pip install`；ffmpeg 缺失提示安装。

> 旧 `pipeline.py` 与初版 `sensitive_screen.py` 已归档至 `_archive/`（旧架构遗留，新流程不再使用）。当前 `src/` 为命名空间包，无 `__init__.py`，不影响 `from src import ...` 导入。
