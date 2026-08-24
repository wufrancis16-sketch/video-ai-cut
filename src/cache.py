"""分析结果缓存层。

目标（对应需求「十七、缓存机制」）：
- 同一个视频第二次处理时，**绝不允许重复 ASR**。
- 修改剪辑规则时，直接复用缓存重新生成剪辑时间轴，不重新处理原始视频。

缓存内容（kind）：
  asr            ASR 原始结果（带词级时间轴）
  subtitle       字幕时间轴（脱敏后）
  bargaining     议价分析结果
  risk_screen    高风险画面检测结果
  intro          开场裁剪检测结果
  plan           统一剪辑时间轴

缓存键：视频文件名 + 大小 + mtime + 相关参数（如 ASR 模型/语言）+ 缓存版本。
任何一项变化都会自然失效，避免脏读。

存放位置：<workdir>/cache/<key>.<kind>.json
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Callable, Dict, Optional

# 缓存格式版本：结构不兼容变更时 +1，可整体作废旧缓存
CACHE_VERSION = 1


def cache_dir(cfg) -> str:
    d = getattr(cfg, "cache_dir", "") or os.path.join(cfg.workdir, "cache")
    os.makedirs(d, exist_ok=True)
    return d


def signature(path: str, extra: Optional[Dict[str, Any]] = None) -> str:
    """基于文件指纹 + 参数生成缓存键（不读全文件，长视频也是瞬时）。"""
    try:
        st = os.stat(path)
        size, mtime = st.st_size, int(st.st_mtime)
    except OSError:
        size, mtime = -1, -1
    base: Dict[str, Any] = {
        "v": CACHE_VERSION,
        "name": os.path.basename(path),
        "size": size,
        "mtime": mtime,
    }
    if extra:
        base["extra"] = {k: extra[k] for k in sorted(extra)}
    raw = json.dumps(base, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def path_for(cfg, key: str, kind: str) -> str:
    return os.path.join(cache_dir(cfg), f"{key}.{kind}.json")


def enabled(cfg) -> bool:
    return bool(getattr(cfg, "use_cache", True))


def load(cfg, key: str, kind: str) -> Optional[Any]:
    """命中返回数据，未命中/损坏返回 None。"""
    if not enabled(cfg):
        return None
    p = path_for(cfg, key, kind)
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:  # noqa
        print(f"  [cache] 读取失败，忽略缓存 {os.path.basename(p)}: {e}")
        return None
    if not isinstance(payload, dict) or "data" not in payload:
        return None
    return payload["data"]


def save(cfg, key: str, kind: str, data: Any) -> str:
    """原子写入（先写 .tmp 再 replace），避免中断产生半截 JSON。"""
    p = path_for(cfg, key, kind)
    if not enabled(cfg):
        return p
    payload = {"version": CACHE_VERSION, "kind": kind, "key": key, "data": data}
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return p


def get_or_create(cfg, key: str, kind: str,
                  producer: Callable[[], Any], label: str = "") -> Any:
    """缓存命中直接返回；否则调用 producer 生成并写缓存。

    producer 是**惰性**的：命中缓存时完全不会执行，因此像 ASR 这类
    需要加载大模型的耗时操作在二次运行时零开销。
    """
    name = label or kind
    hit = load(cfg, key, kind)
    if hit is not None:
        print(f"  [cache] 命中 {name}（{key}.{kind}.json），跳过重新计算")
        return hit
    data = producer()
    if enabled(cfg):
        save(cfg, key, kind, data)
        print(f"  [cache] 已写入 {name} -> "
              f"{os.path.basename(path_for(cfg, key, kind))}")
    return data


def invalidate(cfg, key: str, kinds=None) -> int:
    """删除指定 key 的缓存（kinds=None 删该 key 全部）。返回删除数量。"""
    d = cache_dir(cfg)
    removed = 0
    for f in os.listdir(d):
        if not f.startswith(key + "."):
            continue
        if kinds is not None:
            kind = f[len(key) + 1:].rsplit(".json", 1)[0]
            if kind not in kinds:
                continue
        try:
            os.remove(os.path.join(d, f))
            removed += 1
        except OSError:
            pass
    return removed
