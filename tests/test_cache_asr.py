"""Step 1 验证：ASR 缓存层。

检查点：
1. 首次调用会执行 ASR（且会触发音频提取）。
2. 第二次调用命中缓存：不再执行 ASR，也不执行音频提取（惰性 producer）。
3. 修改 ASR 模型参数 -> 缓存键变化 -> 重新计算。
4. 视频文件内容变化（size/mtime）-> 缓存自然失效。
5. 缓存文件为合法 JSON，可直接读取复用。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import asr, cache  # noqa: E402
from src.config import Config  # noqa: E402

FAKE_SEGS = [
    {"start": 0.0, "end": 2.5, "text": "今天给大家演示库存管理", "words": []},
    {"start": 3.0, "end": 5.0, "text": "我们去年营业额8000万", "words": []},
]


def main():
    tmp = tempfile.mkdtemp(prefix="cachetest_")
    try:
        video = os.path.join(tmp, "demo.mp4")
        with open(video, "wb") as f:
            f.write(b"x" * 1024)

        cfg = Config(workdir=os.path.join(tmp, "_work"), asr_model="small",
                     asr_language="zh")
        os.makedirs(cfg.workdir, exist_ok=True)

        calls = {"asr": 0, "audio": 0}

        def fake_transcribe_with(model, audio_path, language="zh"):
            calls["asr"] += 1
            return FAKE_SEGS

        asr.transcribe_with = fake_transcribe_with  # monkeypatch

        def audio_provider():
            calls["audio"] += 1
            return os.path.join(tmp, "a.wav")

        class Holder:
            def get(self):
                return object()

        # --- 1) 首次：应真实执行 ---
        r1 = asr.transcribe_video_cached(video, audio_provider, cfg, Holder())
        assert calls == {"asr": 1, "audio": 1}, calls
        assert r1 == FAKE_SEGS
        print("[1] 首次执行 ASR + 音频提取         OK")

        # --- 2) 二次：命中缓存，两者都不再执行 ---
        r2 = asr.transcribe_video_cached(video, audio_provider, cfg, Holder())
        assert calls == {"asr": 1, "audio": 1}, f"缓存未命中: {calls}"
        assert r2 == FAKE_SEGS
        print("[2] 二次命中缓存，未重复 ASR/提取音频  OK")

        # --- 3) 缓存文件合法且内容正确 ---
        key = asr.asr_cache_key(video, cfg)
        p = cache.path_for(cfg, key, "asr")
        assert os.path.exists(p), p
        with open(p, encoding="utf-8") as f:
            payload = json.load(f)
        assert payload["kind"] == "asr" and payload["data"] == FAKE_SEGS
        assert payload["data"][0]["start"] == 0.0
        print(f"[3] 缓存文件合法: {os.path.basename(p)}       OK")

        # --- 4) 换 ASR 模型 -> 键变化 -> 重算 ---
        cfg_tiny = Config(workdir=cfg.workdir, asr_model="tiny",
                          asr_language="zh")
        asr.transcribe_video_cached(video, audio_provider, cfg_tiny, Holder())
        assert calls["asr"] == 2, calls
        print("[4] 换模型 small->tiny 触发重算       OK")

        # --- 4b) device 变化不应失效（CPU/GPU 结果可互用）---
        cfg_gpu = Config(workdir=cfg.workdir, asr_model="small",
                         asr_language="zh", device="cuda")
        asr.transcribe_video_cached(video, audio_provider, cfg_gpu, Holder())
        assert calls["asr"] == 2, f"device 变化不应重算: {calls}"
        print("[4b] 切换 cpu->cuda 仍复用缓存        OK")

        # --- 5) 视频变化 -> 缓存失效 ---
        time.sleep(1.1)
        with open(video, "ab") as f:
            f.write(b"y" * 512)
        asr.transcribe_video_cached(video, audio_provider, cfg, Holder())
        assert calls["asr"] == 3, f"视频变化应重算: {calls}"
        print("[5] 视频内容变化触发重算             OK")

        # --- 6) 关闭缓存 -> 每次都算 ---
        cfg_nc = Config(workdir=cfg.workdir, asr_model="small",
                        asr_language="zh", use_cache=False)
        before = calls["asr"]
        asr.transcribe_video_cached(video, audio_provider, cfg_nc, Holder())
        asr.transcribe_video_cached(video, audio_provider, cfg_nc, Holder())
        assert calls["asr"] == before + 2, calls
        print("[6] use_cache=False 时不使用缓存      OK")

        print("\nStep 1 ASR 缓存层：全部通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
