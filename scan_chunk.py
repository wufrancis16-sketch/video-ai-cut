"""Resumable foreground OCR scan for video-ai-cut risk_screen.

Processes the full-video low-frame-rate OCR scan in budgeted foreground chunks:
- auto-stops after --max-seconds of wall time (safe under the ~8-min foreground kill limit)
- saves scored samples to <workdir>/scan_samples.json incrementally (resumable)
- skips frames already scored (within 0.4s) so re-runs continue, not restart
- prints per-frame progress with timestamps so foreground output is live

Run repeatedly (foreground) until it prints "SCAN COMPLETE".
"""
import os, sys, time, json, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src import config as cfgmod
from src import risk_screen as rs
from src import utils as U


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--max-seconds", type=float, default=360.0)
    ap.add_argument("--step", type=float, default=None)
    ap.add_argument("--start-t", type=float, default=0.0)
    ap.add_argument("--end-t", type=float, default=None)
    args = ap.parse_args()

    cfg = cfgmod.Config.load(workdir=args.workdir)
    step = args.step or cfg.risk_screen_sample_step
    wd = os.path.abspath(cfg.workdir)
    os.makedirs(wd, exist_ok=True)
    spath = os.path.join(wd, "scan_samples.json")

    samples = []
    if os.path.exists(spath):
        try:
            samples = json.load(open(spath, encoding="utf-8"))
            print(f"[resume] 已加载 {len(samples)} 个已扫描帧", flush=True)
        except Exception:
            samples = []
    done = {round(s["t"], 1) for s in samples}

    dur = rs.get_duration(args.video)
    end_t = args.end_t if args.end_t is not None else dur
    print(f"[scan] dur={dur:.1f}s step={step} budget={args.max_seconds:.0f}s "
          f"range=[{args.start_t:.0f},{end_t:.0f}]", flush=True)

    import tempfile
    tmp = tempfile.mkdtemp(prefix="scn_")
    t = args.start_t
    cnt = 0
    t0 = time.time()
    while t <= end_t + 1e-6:
        tk = round(t, 1)
        if tk in done:
            t += step
            continue
        if time.time() - t0 > args.max_seconds:
            print(f"[scan] 达到时间预算 {args.max_seconds:.0f}s，本批停止于 t={t:.1f}s", flush=True)
            break
        img = os.path.join(tmp, f"r{int(round(t * 1000))}.png")
        ts = time.time()
        if not rs._extract_frame(args.video, t, img):
            print(f"[scan] t={t:.1f}s 抽帧失败，跳过", flush=True)
            t += step
            continue
        try:
            s = rs._score_frame(img, cfg)
        except Exception as e:
            print(f"[scan] t={t:.1f}s OCR 异常: {e}", flush=True)
            t += step
            continue
        s["t"] = round(t, 3)
        samples.append(s)
        done.add(tk)
        cnt += 1
        el = time.time() - ts
        if s["score"] >= 3:
            print(f"[scan] *** 删除候选 t={t:.1f}s score={s['score']} {s['reason']}", flush=True)
        if cnt % 10 == 0:
            print(f"[scan] 进度 t={t:.1f}s 累计{len(samples)}帧 本帧{el:.1f}s", flush=True)
        # 增量保存（防被杀丢进度）
        if cnt % 10 == 0:
            json.dump(samples, open(spath, "w", encoding="utf-8"), ensure_ascii=False)
        t += step

    json.dump(samples, open(spath, "w", encoding="utf-8"), ensure_ascii=False)
    total = len(samples)
    max_t = max((s["t"] for s in samples), default=0.0)
    # 完成判定：扫到视频末尾（容忍一个最细步长），而非仅看帧数
    if max_t >= dur - 3.0 - 1e-6:
        print(f"[scan] 本批新增 {cnt} 帧，累计 {total} 帧，已扫至 t={max_t:.1f}s", flush=True)
        print("SCAN COMPLETE", flush=True)
    else:
        print(f"[scan] 本批新增 {cnt} 帧，累计 {total} 帧，已扫至 t={max_t:.1f}s", flush=True)
        print(f"[scan] 还需继续：再运行一次本命令（自动续扫）", flush=True)


if __name__ == "__main__":
    main()
