"""项目配置：支持环境变量与代码参数覆盖。

环境变量前缀 AVEditor_：
  AVEditor_ASR_MODEL        faster-whisper 模型尺寸 (tiny/base/small/medium)
                            也可直接传本地模型目录路径
  AVEditor_ASR_LANGUAGE    识别语言 (zh/en/...)
  AVEditor_DEVICE           cpu / cuda
  AVEditor_CHUNK_SECONDS    视频切片时长，默认 300 (5 分钟)
  AVEditor_RESUME           是否支持断点续处理 (true/false)，默认 true
  AVEditor_MODEL_CACHE_DIR  模型缓存目录
  AVEditor_LLM_API_KEY      LLM API Key (不填则关闭 LLM 能力，自动回退)
  AVEditor_LLM_BASE_URL     OpenAI 兼容 base url
  AVEditor_LLM_MODEL        模型名
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

CACHE_HOME = os.path.expanduser("~/.cache/aveditor")


@dataclass
class Config:
    # ---- ASR ----
    asr_model: str = "small"
    asr_language: str = "zh"
    device: str = "cpu"
    model_cache_dir: str = os.path.join(CACHE_HOME, "models")

    # ---- 切片 / 断点 ----
    chunk_seconds: int = 300          # 每个分片时长（秒）
    resume: bool = True               # 支持断点续处理

    # ---- 缓存（ASR / 分析结果 / 剪辑时间轴）----
    use_cache: bool = True            # 命中缓存则不重复 ASR 与 AI 分析
    cache_dir: str = ""               # 留空 = <workdir>/cache

    # ---- 腾讯会议开场自动裁剪（蓝屏 + 白底等候室，只删开头）----
    trim_intro: bool = True           # 自动检测并删除开头的腾讯会议开场
    intro_blue_threshold: float = 30.0   # 蓝-红/蓝-绿 差值阈值（0~255）
    intro_max_scan: float = 240.0     # 最多扫描开头多少秒（仅用于兜底探测）
    intro_min_seconds: float = 0.05   # 蓝色连续短于此值不视为开场（腾讯会议屏可短至 0.1s）
    intro_max_seconds: float = 60.0   # 开场最多裁剪时长：只删开头蓝屏+短等候室，防误删真实内容
    # 2026-08-21 新增：白底等候室/会议进行中界面文字检测（OCR 定位「真实内容开始前
    # 最后一帧会议界面」）。旧版只认蓝屏 → 无蓝屏的白底等候室开场漏裁。
    intro_meeting_step: float = 1.0   # 等候室文字检测抽帧步长（秒）
    intro_meeting_scan: float = 20.0  # 等候室文字检测窗口（秒，从开场起最多扫这么长）
    trim_intro_crf: int = 20          # 裁剪重编码质量
    trim_intro_preset: str = "veryfast"

    # ---- 自动删除：议价内容（价格/报价/折扣/付款等商务谈判）----
    detect_bargaining: bool = True    # 自动检测并整段删除议价对话
    bargain_pad: float = 0.5          # 议价区间两侧外延保护（秒）
    bargain_gap: float = 6.0          # 启发式：相邻议价句最大间隔（秒）内合并成段

    # ---- 自动删除：高风险敏感画面（企业微信/微信/通讯录等含客户隐私界面）----
    detect_sensitive_screen: bool = True  # 自动检测并整段删除高风险画面
    sensitive_screen_mode: str = "auto"   # auto / vision / heuristic
    sensitive_screen_scene_thr: float = 0.35  # 关键帧 scene-change 阈值（旧路径保留）
    sensitive_screen_max_frames: int = 250    # 关键帧上限（超出均匀抽稀，旧路径保留）
    sensitive_screen_interval: float = 4.0    # 兜底均匀抽帧间隔（秒，旧路径保留）
    sensitive_screen_pad: float = 1.0         # 高风险区间两侧外延（秒）
    sensitive_screen_conf_thr: float = 0.6    # 置信度阈值，低于则标「待人工确认」

    # ---- 高风险画面检测（重构架构：ASR 关键词召回 + 候选窗口局部 OCR 置信度评分，
    #      见 risk_screen.py。废弃旧 screen_inspect.run 全片 OCR 扫描，默认关闭）----
    # 候选检测窗口：ASR 命中企微关键词前后各 keyword_pad 秒（仅定位，非删除范围）。
    # 2026-08-21 重构：8s → 30s（用户要求命中词前后各扩 30s）
    risk_screen_keyword_pad: float = 30.0
    # 全片低帧率 OCR 扫描步长（秒）：2026-08-21 改为"始终全片扫描"以保证召回
    # （不依赖 ASR 是否提到企微）。3.0s 为旧版经验值（32min 视频 ≈ 644 帧，
    # 配合周期重置引擎稳定跑完）；连续企微界面召回足够，边界由 _expand_runs 外扩覆盖。
    risk_screen_sample_step: float = 3.0
    # 从候选窗口起最多向前/向后扩展的秒数（边界精修用）；候选已 ±30s，再扩 15s 兜底。
    risk_screen_max_expand: float = 15.0
    # 弱启发式模式下，判定为高风险所需的气泡/标题栏最低占比阈值（保留兼容）
    risk_screen_heuristic_thr: float = 0.01
    # 高风险界面最短展示时长（秒）；低于此值删除段降级为 review，防误删闪帧
    risk_screen_min_screen: float = 1.0
    # 静音展示企微兜底扫描步长（秒）：仅当视频完全无 ASR 句时，对全片按此步长
    # 粗抽帧 OCR 捕捉纯视觉企微界面。3s 一帧对连续展示的企微界面召回足够，
    # 且 OCR 量 = 时长/3（30min≈600帧），配合周期重置引擎可稳定跑完。
    silent_fallback_step: float = 3.0
    # OCR 引擎周期重置帧数：每这么多帧把 RapidOCR 释放并惰性重载，防止长视频
    # 累积内存泄漏触发 OOM（稳定性优先，代价是少量重载耗时）。
    ocr_reset_every: int = 40

    # ---- 全片 OCR 扫描开关（旧 screen_inspect.run，已弃用）----
    # 2026-08-21 起静音展示企微兜底已统一到 risk_screen.detect 内部：当视频完全无
    # ASR 句时自动全片粗步长 OCR 扫描（见 silent_fallback_step）。以下两个开关保留为
    # 兼容字段，不再触发旧版易 OOM 的全片分支。
    screen_inspect_full_scan: bool = False
    screen_inspect_auto_fallback: bool = True

    # ---- LLM (OpenAI 兼容) ----
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    use_llm: bool = True

    # ---- 停顿处理 (秒) ----
    pause_keep_threshold: float = 1.0     # <= 此值：完全保持
    pause_speed_threshold: float = 3.0    # > 此值：删除（只留很短过渡）
    pause_speed_factor: float = 1.5       # speed 模式下 1~3s 停顿的倍率

    # 停顿处理模式：
    #   trim  (默认，推荐) 把过长停顿「裁短」为纯删除区间 —— 不做变速，
    #         因此不存在 atempo 音质损失与音画不同步风险，且渲染更快。
    #   speed 1~3s 停顿用 setpts/atempo 变速（片段多时慢，且有音质代价）
    #   off   不处理停顿
    pause_mode: str = "trim"
    pause_trim_to: float = 0.8            # trim 模式：1~3s 停顿保留时长
    pause_delete_keep: float = 0.25       # >3s 停顿保留的过渡时长（防硬切）

    # 客户提问优先保留：客户问答区间内的停顿更宽松，避免把问题剪碎
    protect_customer_qa: bool = True
    customer_pause_keep: float = 0.6      # 客户问答段内停顿至少保留
    qa_protect_pad: float = 1.5           # 客户问答段前后保护外延（秒）

    # 无意义口头禅：仅当**整句**都是口头禅时才删除（保守，不切碎正常句子）
    remove_filler: bool = True

    # ---- 最终编码（唯一一次完整重编码）----
    final_crf: int = 19                   # 18~20：ERP 界面文字清晰
    final_preset: str = "fast"                # 长视频加速（原 medium）
    # 视频编码器：auto / libx264 / h264_qsv / h264_nvenc / h264_d3d12va
    #   auto  → 运行时探测，优先 NVENC，其次 QSV（Intel 核显），再回退软件 libx264
    #   libx264 → 始终 CPU 软编（最稳，但长视频极慢，易超时/OOM）
    #   h264_qsv / h264_nvenc → 硬件编码，长视频提速 5~30 倍，彻底规避超时
    final_encoder: str = "auto"
    # 硬件编码质量（global_quality / cq），留空则复用 final_crf
    final_hw_quality: Optional[int] = None
    final_audio_bitrate: str = "192k"
    final_audio_sr: int = 48000
    force_cfr: bool = True                # 统一为恒定帧率，保证切点帧级精确
    keep_external_subtitle: bool = True   # 额外导出与成片对齐的 SRT
    burn_subtitle: bool = True            # 烧录字幕到画面

    # ---- 已有字幕检测：原视频若已含字幕则跳过识别/烧录（避免重复叠加）----
    skip_subtitle_if_exists: bool = True      # 检测到原视频已有字幕则跳过加字幕
    detect_burned_subtitle: bool = True       # 是否用 OCR 检测硬字幕（底部字幕带）
    burned_subtitle_samples: int = 6          # 硬字幕检测采样帧数
    burned_subtitle_min_hits: int = 3         # 至少命中多少帧才判为硬字幕

    # ---- 编辑滤镜分批 (规避 Windows 命令行长度上限) ----
    # 单个 ffmpeg -filter_complex 里最多容纳的编辑片段数；超过则拆成多个
    # 子滤镜分批渲染，再用 concat 合并。0 = 不限制（由调用方控制）。
    filter_batch: int = 60

    # ---- 哔声提示音 ----
    beep_freq: float = 1000.0
    beep_duration: float = 30.0           # 单个敏感段最长一般不超过此值
    beep_volume: float = 0.12             # 哔声音量（0~1，0.12≈12% 满幅；2026-08-24 按用户反馈从 0.2 调低）

    # ---- 封面样式（16:9 横屏，文字叠在画面上）----
    cover_style: str = "purple"            # 风格预设：purple / red / green / dark / gold
    cover_font_size: int = 110            # 标题字号（更大更醒目）
    cover_title_position: float = 0.5      # 标题纵向位置（0=顶 1=底，0.5=画面正中）
    cover_bg_darken: float = 0.20         # 背景压暗系数（0~1）
    cover_width: int = 1920              # 封面输出宽度（16:9）
    cover_height: int = 1080             # 封面输出高度
    cover_duration: float = 3.0          # 封面片头持续秒数
    cover_frame_samples: int = 16        # 挑选产品页封面帧时的采样帧数
    cover_title: str = ""                # 强制封面标题（非空时覆盖 LLM 提炼）

    # ---- 字幕样式 (基于视频分辨率坐标) ----
    subtitle_font: str = "Microsoft YaHei"
    subtitle_fontsize: int = 48
    subtitle_primary: str = "&H00FFFFFF"   # 白
    subtitle_outline: str = "&H00000000"    # 黑描边

    # ---- 工作目录 ----
    workdir: str = "./_work"

    # ---- 视频号草稿同步（独立模块 channel_sync，render 完成后可选自动上传）----
    sync_channel_enabled: bool = False   # render 完成后自动上传到视频号草稿
    channel_headless: bool = True        # 无头模式（首次登录建议 False 便于肉眼确认）
    channel_title: str = ""              # 视频号标题（空 = 用封面标题/自动提炼）
    channel_desc: str = ""               # 视频描述（可选）

    @classmethod
    def load(cls, **overrides) -> "Config":
        def env(name, default):
            v = os.environ.get(f"AVEditor_{name}")
            return v if v is not None else default

        cfg = cls(
            asr_model=env("ASR_MODEL", cls.asr_model),
            asr_language=env("ASR_LANGUAGE", cls.asr_language),
            device=env("DEVICE", cls.device),
            model_cache_dir=env("MODEL_CACHE_DIR", cls.model_cache_dir),
            chunk_seconds=int(env("CHUNK_SECONDS", cls.chunk_seconds)),
            resume=env("RESUME", "true").lower() in ("1", "true", "yes"),
            llm_api_key=env("LLM_API_KEY", cls.llm_api_key),
            llm_base_url=env("LLM_BASE_URL", cls.llm_base_url),
            llm_model=env("LLM_MODEL", cls.llm_model),
            use_llm=env("USE_LLM", "true").lower() in ("1", "true", "yes"),
            workdir=env("WORKDIR", cls.workdir),
        cover_title=env("COVER_TITLE", cls.cover_title),
            use_cache=env("USE_CACHE", "true").lower() in ("1", "true", "yes"),
            cache_dir=env("CACHE_DIR", cls.cache_dir),
            final_encoder=env("FINAL_ENCODER", cls.final_encoder),
            final_hw_quality=(int(env("FINAL_HW_QUALITY", ""))
                              if env("FINAL_HW_QUALITY", "") else None),
            sync_channel_enabled=env("SYNC_CHANNEL_ENABLED", "false").lower()
            in ("1", "true", "yes"),
            channel_headless=env("CHANNEL_HEADLESS", "true").lower()
            in ("1", "true", "yes"),
            channel_title=env("CHANNEL_TITLE", cls.channel_title),
            channel_desc=env("CHANNEL_DESC", cls.channel_desc),
            beep_volume=float(env("BEEP_VOLUME", cls.beep_volume)),
        )
        # LLM Key 缺失时自动关闭
        if not cfg.llm_api_key:
            cfg.use_llm = False
        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg
