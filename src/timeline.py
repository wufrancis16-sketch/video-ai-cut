"""统一剪辑时间轴（EditPlan）。

这是**分析阶段**与**视频处理阶段**之间唯一的契约：
  分析阶段：ASR + 文本分析 + 少量关键帧确认  ->  产出 EditPlan(JSON)
  处理阶段：只读 EditPlan，一次 filter_complex + 一次编码  ->  成片

因此修改剪辑规则时，只需重算 EditPlan（可全部走缓存），不必重跑 ASR、
不必重新处理原始视频。

JSON 结构：
{
  "source": "xxx.mp4", "duration": 2400.0, "fps": 30.0,
  "width": 1920, "height": 1080,
  "intro_trim": 12.0,                      # 开头腾讯会议等开场，等价于删除 [0, 12]
  "delete_segments": [                     # 整段删除（议价 / 高风险画面 / 长停顿）
    {"start":1250.0,"end":1328.0,"type":"negotiation","reason":"讨论价格及优惠"}
  ],
  "mute_segments": [                       # 局部消音 + 哔声（企业经营数据）
    {"start":2310.2,"end":2314.8,"type":"sensitive_business_data",
     "reason":"客户提到营业额"}
  ],
  "speed_segments": [                      # 可选变速（默认不用，见 pause_mode）
    {"start":820.0,"end":850.0,"speed":1.5}
  ],
  "subtitle": true,
  "subtitle_cues": [{"start":12.5,"end":15.8,"text":"..."}],   # 原始时间轴、已脱敏
  "review_items": [...],                   # 待人工确认（不自动删除）
  "cover": {"title":"...", "frame_ts":0.0},
  "stats": {...}
}

时间轴语义约定（非常重要）：
1. delete/mute/speed 的 start/end 全部是**原始视频的绝对时间（秒）**。
2. subtitle_cues 也是原始时间轴；渲染时字幕滤镜在 select 之前应用，
   帧被丢弃后字幕自动随帧走，因此**不会错位**。
   导出外挂 SRT 时用 remap_cues() 换算到成片时间轴。
3. 所有区间在 normalize() 后保证：已裁剪到 [0, duration]、已合并重叠、
   mute/speed 已减去被删除的部分（不会指向不存在的画面）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

Range = Tuple[float, float]

# 类型常量（与需求文档一致，使用英文键便于程序判断，中文名用于审核展示）
T_NEGOTIATION = "negotiation"            # 议价
T_HIGH_RISK = "high_risk_screen"         # 高风险画面
T_LONG_PAUSE = "long_pause"              # 长停顿
T_INTRO = "intro"                        # 开场（腾讯会议等）
T_SENSITIVE_DATA = "sensitive_business_data"  # 企业经营敏感数据（消音）
T_FILLER = "filler"                      # 无意义口头禅整句

TYPE_LABELS = {
    T_NEGOTIATION: "议价",
    T_HIGH_RISK: "高风险画面",
    T_LONG_PAUSE: "长停顿",
    T_INTRO: "开场片段",
    T_SENSITIVE_DATA: "敏感业务数据",
    T_FILLER: "口头禅",
}


def type_label(t: str) -> str:
    """类型 -> 中文标签。支持合并后的复合类型（如 negotiation+long_pause）。"""
    parts = [p for p in str(t or "").split("+") if p]
    out: List[str] = []
    for p in parts:
        lab = TYPE_LABELS.get(p, p)
        if lab not in out:
            out.append(lab)
    return "+".join(out) or "未知"


# ---------------------------------------------------------------------------
# 区间工具
# ---------------------------------------------------------------------------
def merge_ranges(ranges: List[Range], eps: float = 1e-6) -> List[Range]:
    """排序并合并重叠/相邻区间。"""
    clean = [(float(s), float(e)) for s, e in ranges if e - s > eps]
    if not clean:
        return []
    clean.sort()
    out = [list(clean[0])]
    for s, e in clean[1:]:
        if s <= out[-1][1] + eps:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(a, b) for a, b in out]


def complement(ranges: List[Range], total: float,
               eps: float = 1e-6) -> List[Range]:
    """在 [0, total] 内求补集（即「保留区间」）。"""
    out: List[Range] = []
    cur = 0.0
    for s, e in merge_ranges(ranges):
        s = max(0.0, min(s, total))
        e = max(0.0, min(e, total))
        if s - cur > eps:
            out.append((cur, s))
        cur = max(cur, e)
    if total - cur > eps:
        out.append((cur, total))
    return out


def subtract(base: List[Range], cut: List[Range],
             eps: float = 1e-6) -> List[Range]:
    """base 各区间减去 cut 覆盖的部分。"""
    cuts = merge_ranges(cut)
    out: List[Range] = []
    for s, e in merge_ranges(base):
        cur = s
        for cs, ce in cuts:
            if ce <= cur or cs >= e:
                continue
            if cs > cur + eps:
                out.append((cur, min(cs, e)))
            cur = max(cur, ce)
            if cur >= e - eps:
                break
        if e - cur > eps:
            out.append((cur, e))
    return out


def snap_range(s: float, e: float, fps: float) -> Range:
    """把区间对齐到视频帧栅格。

    这是保证音画同步的关键：视频只能按帧切，音频可按采样点切。若不对齐，
    每个切点都会引入至多半帧的误差，几十上百个切点后会累积成可感知的
    音画不同步。对齐后视频保留的帧数与音频保留的时长严格相等。
    """
    if fps <= 0:
        return (float(s), float(e))
    a = round(s * fps)
    b = round(e * fps)
    if b <= a:
        b = a + 1
    return (a / fps, b / fps)


def snap_ranges(ranges: List[Range], fps: float) -> List[Range]:
    return merge_ranges([snap_range(s, e, fps) for s, e in ranges])


def fmt_hms(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def fmt_hms_short(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# EditPlan
# ---------------------------------------------------------------------------
@dataclass
class EditPlan:
    source: str = ""
    duration: float = 0.0
    fps: float = 30.0
    width: int = 1920
    height: int = 1080

    intro_trim: float = 0.0
    delete_segments: List[Dict[str, Any]] = field(default_factory=list)
    mute_segments: List[Dict[str, Any]] = field(default_factory=list)
    speed_segments: List[Dict[str, Any]] = field(default_factory=list)

    subtitle: bool = True
    subtitle_cues: List[Dict[str, Any]] = field(default_factory=list)
    # 已有字幕检测结果：None 表示未检测/未命中；dict 表示原视频已含字幕
    #   {"type":"embedded", ...} 内嵌字幕流
    #   {"type":"burned", ...}   硬字幕（烧在画面里）
    # 命中后 analyze 会跳过 subtitle_cues 生成，render 跳过烧录与外挂 SRT。
    existing_subtitle: Optional[Dict[str, Any]] = None

    review_items: List[Dict[str, Any]] = field(default_factory=list)
    cover: Dict[str, Any] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)

    # -- 构造 --------------------------------------------------------------
    def add_delete(self, start: float, end: float, type_: str, reason: str = "",
                   **extra):
        item = {"start": float(start), "end": float(end), "type": type_,
                "reason": reason}
        item.update(extra)
        self.delete_segments.append(item)

    def add_mute(self, start: float, end: float, reason: str = "",
                 type_: str = T_SENSITIVE_DATA, **extra):
        item = {"start": float(start), "end": float(end), "type": type_,
                "reason": reason}
        item.update(extra)
        self.mute_segments.append(item)

    def add_review(self, start: float, end: float, type_: str, reason: str = "",
                   suggestion: str = "delete"):
        self.review_items.append({
            "start": float(start), "end": float(end), "type": type_,
            "reason": reason, "suggestion": suggestion, "action": "待人工确认",
        })

    # -- 规范化 ------------------------------------------------------------
    def normalize(self) -> "EditPlan":
        """裁剪越界、合并重叠、去掉被删除区覆盖的 mute/speed。

        注意：只做几何规范化，不做任何"聪明"的语义修改，避免悄悄改变
        用户/AI 的删除意图。
        """
        dur = float(self.duration or 0.0)

        def clip(items):
            out = []
            for it in items:
                s = max(0.0, min(float(it.get("start", 0.0)), dur))
                e = max(0.0, min(float(it.get("end", 0.0)), dur))
                if e - s <= 1e-3:
                    continue
                it = dict(it)
                it["start"], it["end"] = s, e
                out.append(it)
            return sorted(out, key=lambda x: x["start"])

        self.intro_trim = max(0.0, min(float(self.intro_trim or 0.0), dur))
        self.delete_segments = _merge_items(clip(self.delete_segments))
        self.mute_segments = clip(self.mute_segments)
        self.speed_segments = clip(self.speed_segments)
        self.review_items = clip(self.review_items)

        # mute / speed 去掉落在删除区内的部分
        dels = self.delete_ranges()
        self.mute_segments = _reslice_items(self.mute_segments, dels)
        self.speed_segments = _reslice_items(self.speed_segments, dels)
        return self

    # -- 区间视图 ----------------------------------------------------------
    def delete_ranges(self, snap: bool = False) -> List[Range]:
        """全部删除区间（含 intro_trim），已合并。"""
        rs: List[Range] = []
        if self.intro_trim > 1e-3:
            rs.append((0.0, self.intro_trim))
        rs += [(d["start"], d["end"]) for d in self.delete_segments]
        rs = merge_ranges(rs)
        return snap_ranges(rs, self.fps) if snap else rs

    def keep_ranges(self, snap: bool = True) -> List[Range]:
        """保留区间（成片实际使用的原始时间段）。默认对齐帧栅格。"""
        dels = self.delete_ranges(snap=snap)
        keeps = complement(dels, float(self.duration))
        return snap_ranges(keeps, self.fps) if snap else keeps

    def mute_ranges(self, snap: bool = True) -> List[Range]:
        rs = [(m["start"], m["end"]) for m in self.mute_segments]
        rs = merge_ranges(rs)
        return snap_ranges(rs, self.fps) if snap else rs

    def pieces(self, snap: bool = True) -> List[Dict[str, Any]]:
        """保留区间按变速切分后的最小单元：[{start,end,speed}]。

        供时间映射与（可选的）变速渲染使用。
        """
        speeds = [(s["start"], s["end"], float(s.get("speed", 1.0)))
                  for s in self.speed_segments if float(s.get("speed", 1.0)) > 0]
        out: List[Dict[str, Any]] = []
        for ks, ke in self.keep_ranges(snap=snap):
            cuts = {ks, ke}
            for ss, se, _sp in speeds:
                if se > ks and ss < ke:
                    cuts.add(max(ks, ss))
                    cuts.add(min(ke, se))
            pts = sorted(cuts)
            for a, b in zip(pts, pts[1:]):
                if b - a <= 1e-6:
                    continue
                mid = (a + b) / 2
                sp = 1.0
                for ss, se, s_sp in speeds:
                    if ss <= mid < se:
                        sp = s_sp
                        break
                out.append({"start": a, "end": b, "speed": sp})
        return out

    def output_duration(self) -> float:
        return sum((p["end"] - p["start"]) / p["speed"] for p in self.pieces())

    # -- 时间映射（原始时间 -> 成片时间）---------------------------------
    def map_time(self, t: float) -> Optional[float]:
        """返回成片中的时间；t 落在被删除区间时返回 None。"""
        acc = 0.0
        for p in self.pieces():
            span = (p["end"] - p["start"]) / p["speed"]
            if t < p["start"]:
                return None            # 落在该片段之前的删除区
            if t <= p["end"]:
                return acc + (t - p["start"]) / p["speed"]
            acc += span
        return None

    def map_time_clamped(self, t: float) -> float:
        """把 t 映射到成片时间；落在删除区则贴到最近保留边界。"""
        acc = 0.0
        last = 0.0
        for p in self.pieces():
            span = (p["end"] - p["start"]) / p["speed"]
            if t < p["start"]:
                return last
            if t <= p["end"]:
                return acc + (t - p["start"]) / p["speed"]
            acc += span
            last = acc
        return last

    def remap_cues(self, cues: Optional[List[Dict[str, Any]]] = None
                   ) -> List[Dict[str, Any]]:
        """把字幕从原始时间轴换算到成片时间轴。

        跨删除边界的字幕会被拆成多条，确保「字幕跟着剪辑后的画面走」。
        仅用于导出外挂 SRT/ASS；烧录路径不需要（见模块 docstring）。
        """
        src = self.subtitle_cues if cues is None else cues
        out: List[Dict[str, Any]] = []
        pieces = self.pieces()
        for cue in src:
            cs, ce = float(cue["start"]), float(cue["end"])
            acc = 0.0
            for p in pieces:
                span = (p["end"] - p["start"]) / p["speed"]
                ov_s = max(cs, p["start"])
                ov_e = min(ce, p["end"])
                if ov_e - ov_s > 0.05:      # 忽略过短碎片
                    item = dict(cue)
                    item["start"] = acc + (ov_s - p["start"]) / p["speed"]
                    item["end"] = acc + (ov_e - p["start"]) / p["speed"]
                    out.append(item)
                acc += span
        out.sort(key=lambda x: x["start"])
        return out

    # -- 校验 --------------------------------------------------------------
    def validate(self) -> List[str]:
        warns: List[str] = []
        dur = float(self.duration or 0.0)
        if dur <= 0:
            warns.append("duration 未设置或为 0")
        keeps = self.keep_ranges()
        if not keeps:
            warns.append("严重：所有内容都被删除，成片为空")
        out_dur = self.output_duration()
        if dur > 0 and out_dur < dur * 0.3:
            warns.append(
                f"删除比例过高：成片 {out_dur:.1f}s / 原始 {dur:.1f}s "
                f"（保留 {out_dur / dur * 100:.0f}%），请人工复核是否过度剪辑")
        for m in self.mute_segments:
            if m["end"] - m["start"] > 60:
                warns.append(f"消音段过长 {fmt_hms_short(m['start'])}"
                             f"-{fmt_hms_short(m['end'])}，请复核")
        for s in self.speed_segments:
            sp = float(s.get("speed", 1.0))
            if sp <= 0 or sp > 4:
                warns.append(f"异常倍速 {sp} @ {fmt_hms_short(s['start'])}")
        return warns

    # -- 审核清单 ----------------------------------------------------------
    def review_report(self) -> str:
        """人工审核清单（对应需求「十八、人工审核」）。"""
        rows: List[Tuple[float, str]] = []
        if self.intro_trim > 1e-3:
            rows.append((0.0,
                         f"⚠️ {fmt_hms_short(0)}-{fmt_hms_short(self.intro_trim)}\n"
                         f"   类型：开场片段\n   原因：腾讯会议/等候室开场\n"
                         f"   操作：删除"))
        for d in self.delete_segments:
            label = type_label(d.get("type", ""))
            rows.append((d["start"],
                         f"⚠️ {fmt_hms_short(d['start'])}-{fmt_hms_short(d['end'])}"
                         f"  ({d['end'] - d['start']:.1f}s)\n"
                         f"   类型：{label}\n   原因：{d.get('reason', '') or '-'}\n"
                         f"   操作：删除"))
        for m in self.mute_segments:
            rows.append((m["start"],
                         f"🔇 {fmt_hms_short(m['start'])}-{fmt_hms_short(m['end'])}"
                         f"  ({m['end'] - m['start']:.1f}s)\n"
                         f"   类型：{type_label(m.get('type', '') or T_SENSITIVE_DATA)}\n"
                         f"   原因：{m.get('reason', '') or '-'}\n"
                         f"   操作：消音+哔声（画面保留）"))
        for s in self.speed_segments:
            rows.append((s["start"],
                         f"⏩ {fmt_hms_short(s['start'])}-{fmt_hms_short(s['end'])}\n"
                         f"   类型：变速\n   操作：{s.get('speed', 1.0)}x"))
        for r in self.review_items:
            label = type_label(r.get("type", ""))
            rows.append((r["start"],
                         f"❓ {fmt_hms_short(r['start'])}-{fmt_hms_short(r['end'])}\n"
                         f"   类型：{label}（待人工确认）\n"
                         f"   原因：{r.get('reason', '') or '-'}\n"
                         f"   操作：**未处理**，需人工确认是否删除"))
        rows.sort(key=lambda x: x[0])

        out_dur = self.output_duration()
        head = [
            "=" * 60,
            "剪辑方案审核清单",
            "=" * 60,
            f"源视频      : {os.path.basename(self.source)}",
            f"原始时长    : {fmt_hms_short(self.duration)} ({self.duration:.1f}s)",
            f"成片时长    : {fmt_hms_short(out_dur)} ({out_dur:.1f}s)",
            f"删除总时长  : {self.duration - out_dur:.1f}s",
            f"分辨率/帧率 : {self.width}x{self.height} @ {self.fps:.3f}fps",
            f"删除段 {len(self.delete_segments)} 个 | 消音段 "
            f"{len(self.mute_segments)} 个 | 变速段 {len(self.speed_segments)} 个 "
            f"| 待确认 {len(self.review_items)} 个",
            "-" * 60,
        ]
        warns = self.validate()
        tail = []
        if warns:
            tail = ["-" * 60, "校验提示："] + [f"  ! {w}" for w in warns]

        # ---- 生产保护：显式审核清单摘要（企业微信疑似 / 价格疑似）----
        wechat_del = [d for d in self.delete_segments if d.get("type") == T_HIGH_RISK]
        wechat_rev = [r for r in self.review_items if r.get("type") == T_HIGH_RISK]
        price_mutes = self.mute_segments
        summary: List[str] = []
        summary.append("【企业微信疑似时间段】")
        if wechat_del or wechat_rev:
            for d in wechat_del:
                summary.append(f"  ✂️ 删除   {fmt_hms_short(d['start'])}-{fmt_hms_short(d['end'])}"
                               f"  ({d['end'] - d['start']:.1f}s)  {d.get('reason', '')}")
            for r in wechat_rev:
                summary.append(f"  ❓ 待确认 {fmt_hms_short(r['start'])}-{fmt_hms_short(r['end'])}"
                               f"  ({r['end'] - r['start']:.1f}s)  {r.get('reason', '')}")
        else:
            summary.append("  （无）")
        summary.append("【价格疑似时间段（已消音+哔声，画面保留）】")
        if price_mutes:
            for m in price_mutes:
                summary.append(f"  🔇 {fmt_hms_short(m['start'])}-{fmt_hms_short(m['end'])}"
                               f"  ({m['end'] - m['start']:.1f}s)  {m.get('reason', '')}")
        else:
            summary.append("  （无）")
        summary.append("-" * 60)

        body = summary + ([r[1] for r in rows] or ["（无需要审核的编辑项）"])
        return "\n".join(head + body + tail)

    # -- 序列化 ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["output_duration"] = round(self.output_duration(), 3)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EditPlan":
        known = {f for f in cls.__dataclass_fields__}       # noqa
        kw = {k: v for k, v in d.items() if k in known}
        return cls(**kw)

    def save(self, path: str) -> str:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: str) -> "EditPlan":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


# ---------------------------------------------------------------------------
def _merge_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并重叠的删除项，保留全部类型与原因（便于审核追溯）。"""
    if not items:
        return []
    items = sorted(items, key=lambda x: x["start"])
    out = [dict(items[0])]
    for it in items[1:]:
        prev = out[-1]
        if it["start"] <= prev["end"] + 1e-6:
            prev["end"] = max(prev["end"], it["end"])
            if it.get("type") != prev.get("type"):
                prev["type"] = f"{prev.get('type')}+{it.get('type')}"
            r1 = prev.get("reason", "")
            r2 = it.get("reason", "")
            if r2 and r2 not in r1:
                prev["reason"] = (r1 + "；" + r2).strip("；")
        else:
            out.append(dict(it))
    return out


def _reslice_items(items: List[Dict[str, Any]],
                   cuts: List[Range]) -> List[Dict[str, Any]]:
    """把 items 中被 cuts 覆盖的部分切掉，剩余部分保持原属性。"""
    out: List[Dict[str, Any]] = []
    for it in items:
        for s, e in subtract([(it["start"], it["end"])], cuts):
            nit = dict(it)
            nit["start"], nit["end"] = s, e
            out.append(nit)
    return sorted(out, key=lambda x: x["start"])
