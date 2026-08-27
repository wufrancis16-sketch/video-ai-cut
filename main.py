#!/usr/bin/env python
"""AI Video Auto Editor - CLI 入口

新架构（两阶段 + 统一剪辑时间轴 plan.json）：
    分析阶段 analyze  -> 只产 plan.json（不编码）
    审核阶段 review   -> 人工确认「待人工确认」项，写回 plan.json
    渲染阶段 render   -> 只读 plan.json，一次 filter_complex + 一次编码

用法:
  # 全自动（分析 -> 高风险画面巡检 -> 若有待确认项则交互审核 -> 渲染）
  python main.py input.mp4
  python main.py input.mp4 -o result/final_video.mp4 --cover result/cover.png

  # 分阶段（适合长视频：先 analyze，再 inspect 巡检，人工确认后再 render）
  python main.py analyze input.mp4                 # 只做分析，产出 plan.json + 审核清单.txt
  python main.py inspect input.mp4 --plan <workdir>/plan.json   # 高风险画面两级巡检，候选写入 review_items（不自动删除）
  python main.py confirm --plan <workdir>/plan.json --action delete --items 1,2  # 人工确认删除指定候选
  python main.py render  input.mp4 --plan <workdir>/plan.json -o out.mp4

  # 跳过交互审核（安全默认：所有待确认项一律保留，不自动删除）
  python main.py input.mp4 --skip-review

  # 不使用 LLM（仅关键词敏感检测；封面标题需配置 LLM 或手动指定）
  AVEditor_USE_LLM=false python main.py input.mp4

  # 配置 LLM (OpenAI 兼容，例如 DeepSeek / 通义 / 智谱)
  export AVEditor_LLM_API_KEY=sk-xxx
  export AVEditor_LLM_BASE_URL=https://api.deepseek.com/v1
  export AVEditor_LLM_MODEL=deepseek-chat
  python main.py input.mp4
"""
from __future__ import annotations

import argparse
import importlib
import os
import shutil
import sys
import time

# Windows 控制台默认 GBK，emoji/部分中文打印会抛 UnicodeEncodeError。
# 重配置为 utf-8，避免收尾打印崩溃。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))


def _ensure_ffmpeg() -> bool:
    from src.utils import ffmpeg_available
    return ffmpeg_available()


def _ensure_deps(auto_install: bool) -> bool:
    """检查 Python 依赖，缺失时尝试自动安装。"""
    # 核心依赖（缺任何一个，主流程会在中途崩 / 触发企微检测时才报 ImportError）。
    # 只要缺任一，即触发 `pip install -r requirements.txt` 一次性补齐（含 rapidocr / playwright）。
    # playwright 是视频号同步的可选依赖，保持惰性导入（channel_sync 内 try/except），不在此强制，
    # 避免无网环境下因装不上 playwright 而阻断纯剪辑主流程。
    required = {
        "faster_whisper": "faster-whisper",
        "openai": "openai",
        "PIL": "pillow",
        "numpy": "numpy",
        "requests": "requests",
        "rapidocr_onnxruntime": "rapidocr-onnxruntime",
    }
    missing = []
    for mod, pkg in required.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)

    if not missing:
        return True

    print(f"⚠️ 缺少 Python 依赖: {missing}")
    if not auto_install:
        print("  请手动执行: pip install -r requirements.txt", file=sys.stderr)
        return False

    print("  正在尝试自动安装 (pip install -r requirements.txt) ...")
    req = os.path.join(HERE, "requirements.txt")
    proc = _run_pip(["install", "-r", req])
    if proc.returncode != 0:
        print("❌ 自动安装失败，请手动执行: pip install -r requirements.txt",
              file=sys.stderr)
        return False
    print("  ✅ 依赖已安装")
    return True


def _run_pip(args):
    import subprocess
    return subprocess.run([sys.executable, "-m", "pip", *args],
                          capture_output=True, text=True)


def _build_config(args) -> "Config":
    from src.config import Config
    # workdir 归一化为绝对路径：否则相对路径会让 render.py 里
    # os.path.join(workdir, "render_audio.wav") 生成相对音频路径，
    # 而 _run_in_workdir 会 chdir 到 workdir，导致相对路径二次解析错位。
    wd = getattr(args, "workdir", None)
    if not wd:
        wd = "./_work"  # 与 Config 默认值保持一致，但归一化为绝对路径
    wd = os.path.abspath(wd)
    return Config.load(
        asr_model=getattr(args, "asr_model", None),
        device=getattr(args, "device", None),
        workdir=wd,
        cover_title=getattr(args, "cover_title", None),
        chunk_seconds=getattr(args, "chunk_seconds", None),
        resume=(not getattr(args, "no_resume", False)),
        model_cache_dir=getattr(args, "model_cache_dir", None),
    )


def _default_outputs(input_path: str, output: str = None, cover: str = None):
    in_dir = os.path.dirname(os.path.abspath(input_path))
    out_dir = os.path.join(in_dir, "edit")
    os.makedirs(out_dir, exist_ok=True)
    return (output or os.path.join(out_dir, "final_video.mp4"),
            cover or os.path.join(out_dir, "cover.png"))


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------
def _cmd_analyze(args):
    from src import analyze
    from src.llm import LLM
    if not _ensure_ffmpeg():
        _no_ffmpeg()
    if not _ensure_deps(auto_install=not args.no_auto_install):
        sys.exit(1)
    cfg = _build_config(args)
    llm = LLM(cfg)
    video = os.path.abspath(args.input)
    plan_path = os.path.join(cfg.workdir, "plan.json")
    plan = analyze.analyze(video, cfg, llm, plan_path)
    analyze.write_review_report(plan, cfg)
    print(f"[完成] 分析阶段结束。plan.json -> {plan_path}")
    print(f"       待人工确认项：{len(plan.review_items)} 个")
    if plan.review_items:
        print(f"       可执行: python main.py review --plan {plan_path}")


def _cmd_review(args):
    from src import review as review_mod
    review_mod.review_plan(args.plan)


def _cmd_inspect(args):
    """inspect 子命令：高风险画面巡检。"""
    from src.screen_inspect import main_cli
    main_cli(args)


def _cmd_render(args):
    from src import analyze, render
    from src.timeline import EditPlan
    if not _ensure_ffmpeg():
        _no_ffmpeg()
    cfg = _build_config(args)
    plan_path = args.plan or os.path.join(cfg.workdir, "plan.json")
    if not os.path.exists(plan_path):
        print(f"❌ 找不到 plan.json: {plan_path}", file=sys.stderr)
        print("   请先运行: python main.py analyze <input>", file=sys.stderr)
        sys.exit(1)
    plan = EditPlan.load(plan_path)
    # 智能体/外部传入的封面标题优先（避免重跑 ASR）：注入 plan.cover 并落盘，
    # 使 render 烧录封面与后续 sync 视频号都能读到同一标题。
    if getattr(args, "cover_title", None):
        plan.cover = dict(plan.cover or {})
        plan.cover["title"] = args.cover_title
        try:
            plan.save(plan_path)
        except Exception as e:  # noqa
            print(f"  [warn] 写回 plan.cover.title 失败（仍按本次渲染使用）: {e}")
    video = os.path.abspath(args.input)
    output, default_cover = _default_outputs(video, args.output, args.cover)
    render.render(plan, cfg, output, video)
    # 把渲染生成的封面（默认在 workdir/cover.png）复制到用户指定的封面路径
    target_cover = os.path.abspath(args.cover or default_cover)
    src_cover = os.path.join(cfg.workdir, "cover.png")
    if os.path.exists(src_cover) and os.path.abspath(src_cover) != target_cover:
        import shutil
        os.makedirs(os.path.dirname(target_cover) or ".", exist_ok=True)
        shutil.copy(src_cover, target_cover)
        print(f"  [封面] 已复制到 -> {target_cover}")
    print(f"[完成] 渲染阶段结束。成片 -> {output}")


def _cmd_sync(args):
    """sync 子命令：把成片上传到视频号草稿箱（不发布）。

    独立于剪辑链路：只需成片路径 + 标题。登录态保存在 workdir。
    """
    from src import channel_sync
    from src.config import Config

    video = os.path.abspath(args.input)
    if not os.path.exists(video):
        print(f"❌ 成片不存在: {video}", file=sys.stderr)
        sys.exit(1)
    wd = os.path.abspath(args.workdir or "./_work")
    os.makedirs(wd, exist_ok=True)
    title = args.title or getattr(args, "cover_title", "") or ""
    # 话题标签：显式传参 > 从描述自动提取
    topics = getattr(args, "topics", None) or []
    ok = channel_sync.sync_to_channels(
        video, title, wd,
        description=getattr(args, "desc", "") or "",
        topics=topics,
        headless=not getattr(args, "headed", False),
        cover=getattr(args, "cover", None),
        profile=getattr(args, "profile", None))
    sys.exit(0 if ok else 1)


def _cmd_confirm(args):
    """confirm 子命令：人工确认待确认项（删除/保留），写回 plan.json。

    序号与审核画廊/审核清单一致（从 1 开始）。
    删除项会转入 delete_segments，随后 render 才会真正裁掉。
    """
    from src import review as review_mod
    from src.timeline import EditPlan

    plan_path = os.path.abspath(args.plan)
    if not os.path.exists(plan_path):
        print(f"❌ 找不到 plan.json: {plan_path}", file=sys.stderr)
        sys.exit(1)

    plan = EditPlan.load(plan_path)
    n_before = len(plan.review_items)

    if args.all:
        n = review_mod.apply_all(plan, args.action)
        print(f"已对全部 {n} 项执行：{args.action}")
    else:
        if not args.items:
            print("❌ 请指定 --items 序号（逗号分隔，从 1 开始）或 --all",
                  file=sys.stderr)
            sys.exit(1)
        try:
            idxs = [int(x) - 1 for x in args.items.split(",") if x.strip() != ""]
        except ValueError:
            print("❌ --items 必须是逗号分隔的数字，如 1,2,3", file=sys.stderr)
            sys.exit(1)
        bad = [i + 1 for i in idxs if not (0 <= i < n_before)]
        if bad:
            print(f"❌ 序号超出范围：{bad}（当前共 {n_before} 项）", file=sys.stderr)
            sys.exit(1)
        # 从高到低执行，避免 pop 导致序号错位
        for i in sorted(idxs, reverse=True):
            review_mod.apply_decision(plan, i, args.action)

    plan.normalize()
    plan.save(plan_path)
    try:
        review_mod._write_report(plan, plan_path)
    except Exception:  # noqa
        pass
    print(f"✅ 已写回 {plan_path}")
    print(f"   剩余待确认: {len(plan.review_items)} 项 | "
          f"删除段: {len(plan.delete_segments)} 个")
    print(f"   下一步渲染: python main.py render --plan {plan_path} -o <输出>")


def _cmd_auto(args):
    """默认模式：analyze -> 企业微信检测(内置静音兜底) -> 审核 -> 渲染。

    生产保护设计（用户要求：普通同事只上传/填标题/点开始）：
    - 企业微信检测在 analyze 内完成（ASR 关键词召回 + 候选窗口 OCR 确认；
      完全无语音时自动全片粗步长兜底扫描），不再单独跑易 OOM 的旧全片分支。
    - 渲染前自动备份 plan.json（plan.json.bak），删除失败也能恢复方案。
    - 渲染包 try/except：原视频**绝不删除**，渲染失败仅丢弃半成品成片并明确报错。
    - 每步打印耗时日志，便于同事/运维定位卡点。
    """
    from src import analyze, render, review as review_mod
    from src.llm import LLM
    from src.timeline import EditPlan

    if not _ensure_ffmpeg():
        _no_ffmpeg()
    if not _ensure_deps(auto_install=not args.no_auto_install):
        sys.exit(1)

    cfg = _build_config(args)
    llm = LLM(cfg)
    video = os.path.abspath(args.input)
    output, cover = _default_outputs(video, args.output, args.cover)
    plan_path = os.path.join(cfg.workdir, "plan.json")

    # 1) 分析 + 企业微信检测（检测静音兜底在 analyze 内 risk_screen.detect 完成）
    _t = time.time()
    print(f"\n[步骤1/3] 分析视频（ASR + 敏感数据 + 企业微信检测）……", flush=True)
    plan = analyze.analyze(video, cfg, llm, plan_path)
    analyze.write_review_report(plan, cfg)
    print(f"          耗时 {time.time() - _t:.1f}s | 删除段 {len(plan.delete_segments)} "
          f"| 消音段 {len(plan.mute_segments)} | 待确认 {len(plan.review_items)}")

    # 2) 审核（仅处理中置信兜底项；高置信已自动删除）
    _t = time.time()
    if plan.review_items and not args.skip_review:
        print(f"\n[步骤2/3] 人工审核（中置信待确认项 {len(plan.review_items)} 个）……",
              flush=True)
        review_mod.review_plan(plan_path, cfg)
        plan = EditPlan.load(plan_path)
    elif args.skip_review and plan.review_items:
        plan.review_items = []
        plan.normalize()
        plan.save(plan_path)
        print(f"[步骤2/3] --skip-review：{len(plan.review_items)} 项按保留处理 "
              f"({time.time() - _t:.1f}s)")
    else:
        print(f"\n[步骤2/3] 无待确认项，跳过审核（{time.time() - _t:.1f}s）")

    # 3) 渲染（先备份 plan.json，失败保留原视频）
    _t = time.time()
    print(f"\n[步骤3/3] 渲染成片（原视频不会被删除）……", flush=True)
    if os.path.exists(plan_path):
        try:
            shutil.copy(plan_path, plan_path + ".bak")
        except Exception as e:  # noqa
            print(f"  [warn] 备份 plan.json 失败: {e}")
    try:
        render.render(plan, cfg, output, video)
    except Exception as e:
        import traceback
        print(f"  ❌ 渲染失败：{type(e).__name__}: {e}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        # 删除半成品成片，避免同事误用；原视频始终保留
        if os.path.exists(output):
            try:
                os.remove(output)
            except OSError:
                pass
        print(f"  ℹ️ 原视频未受影响：{video}", file=sys.stderr)
        print(f"  ℹ️ 剪辑方案已备份：{plan_path}.bak", file=sys.stderr)
        sys.exit(2)
    print(f"          耗时 {time.time() - _t:.1f}s")
    print(f"[完成] 成片 -> {output}")

    # ---- 视频号草稿自动同步（可选，失败不影响成片）----
    if getattr(cfg, "sync_channel_enabled", False) and os.path.exists(output):
        print("\n[步骤4/4] 同步到视频号草稿箱（不发布）……", flush=True)
        try:
            from src import channel_sync
            title = (getattr(cfg, "channel_title", "") or "").strip() \
                or (getattr(plan, "cover", {}) or {}).get("title", "") \
                or getattr(cfg, "cover_title", "") or ""
            desc = getattr(cfg, "channel_desc", "") or ""
            # 话题标签：从配置读取（逗号/空格分隔的字符串 → 列表）
            raw_topics = getattr(cfg, "channel_topics", "") or ""
            topics_list = [t.strip() for t in raw_topics.replace("，", ",").split(",") if t.strip()] if raw_topics else []
            ok = channel_sync.sync_to_channels(
                output, title, cfg.workdir,
                description=desc,
                topics=topics_list if topics_list else None,
                headless=getattr(cfg, "channel_headless", True),
                cover=cover)
            if ok:
                print("  ✅ 已存入视频号草稿箱（未发布）")
            else:
                print("  ⚠️ 视频号同步未完成（见上方原因），成片不受影响",
                      file=sys.stderr)
        except Exception as e:  # noqa
            import traceback
            print(f"  ⚠️ 视频号同步异常（成片不受影响）: {e}", file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI 解析
# ---------------------------------------------------------------------------
def _no_ffmpeg():
    print("❌ 未检测到 ffmpeg/ffprobe。", file=sys.stderr)
    print("   安装方式: winget install ffmpeg  (或前往 https://ffmpeg.org 下载并加入 PATH)",
          file=sys.stderr)
    sys.exit(1)


def _add_common(p: argparse.ArgumentParser):
    p.add_argument("--asr-model", default=None,
                   help="faster-whisper 模型尺寸 (tiny/base/small/medium) "
                        "或直接传本地模型目录路径")
    p.add_argument("--device", default=None, help="cpu / cuda")
    p.add_argument("--workdir", default=None, help="中间文件目录")
    p.add_argument("--chunk-seconds", type=int, default=None,
                   help="视频切片时长（秒），默认 300（5 分钟）")
    p.add_argument("--no-resume", action="store_true",
                   help="关闭断点续处理（从头重跑）")
    p.add_argument("--model-cache-dir", default=None,
                   help="Whisper 模型缓存目录")
    p.add_argument("--no-auto-install", action="store_true",
                   help="关闭依赖自动安装")


def main():
    raw = sys.argv[1:]
    if not raw:
        print(__doc__)
        sys.exit(1)

    if raw[0] in ("analyze", "review", "render", "inspect", "sync"):
        parser = argparse.ArgumentParser(
            prog="main.py",
            description="AI Video Auto Editor (analyze / inspect / review / render)")
        sub = parser.add_subparsers(dest="cmd", required=True)

        pa = sub.add_parser("analyze", help="仅分析，产出 plan.json")
        pa.add_argument("input", help="输入视频路径 (mp4)")
        pa.add_argument("--cover-title", default=None,
                        help="强制指定封面标题（覆盖 LLM 自动提炼）")
        _add_common(pa)

        pi = sub.add_parser("inspect",
                           help="高风险画面巡检：两级抽帧检测企业微信/微信/通讯录等隐私界面，"
                                "写入 plan.json 的 review_items（待人工确认，**不自动删除**）")
        pi.add_argument("input", help="输入视频路径 (mp4)")
        pi.add_argument("--plan", default=None,
                       help="plan.json 路径（默认 <workdir>/plan.json）")
        pi.add_argument("-o", "--output", default=None,
                       help="巡检报告输出路径（默认 <workdir>/screen_inspect_report.json）")
        _add_common(pi)

        pc = sub.add_parser("confirm",
                           help="人工确认待确认项：将指定项转删除段(delete)或从清单移除(keep)")
        pc.add_argument("--plan", required=True, help="plan.json 路径")
        pc.add_argument("--action", choices=["delete", "keep"], default="delete",
                        help="对指定项执行的操作（默认 delete）")
        pc.add_argument("--items", default=None,
                        help="逗号分隔的序号（从 1 开始），如 1,3；与 --all 二选一")
        pc.add_argument("--all", action="store_true",
                        help="对全部待确认项应用 --action")
        _add_common(pc)

        pr = sub.add_parser("review", help="交互式审核待确认项")
        pr.add_argument("--plan", required=True, help="plan.json 路径")

        pr2 = sub.add_parser("render", help="按 plan.json 渲染成片")
        pr2.add_argument("input", help="输入视频路径 (mp4)")
        pr2.add_argument("--plan", default=None, help="plan.json 路径")
        pr2.add_argument("-o", "--output", default=None, help="输出视频路径")
        pr2.add_argument("--cover", default=None,
                        help="封面输出路径（默认 <输入目录>/edit/cover.png）")
        pr2.add_argument("--cover-title", default=None,
                        help="强制指定封面/视频号标题（优先于 plan.json，"
                             "供智能体调用自身 LLM 生成后注入，避免重跑 ASR）")
        _add_common(pr2)

        ps = sub.add_parser("sync", help="把成片上传到视频号草稿箱（不发布）")
        ps.add_argument("input", help="成片视频路径 (mp4)")
        ps.add_argument("--title", default=None,
                        help="视频号标题（默认用封面标题）")
        ps.add_argument("--desc", default=None, help="视频描述（可选，建议 50~150 字内容摘要）")
        ps.add_argument("--topics", nargs="+", default=None,
                        help="话题标签（如 #进销存 #财务软件 #商贸管理，3~5 个）")
        ps.add_argument("--cover", default=None, help="封面图路径（可选）")
        ps.add_argument("--headed", action="store_true",
                        help="有头模式（首次登录建议开启，便于扫码确认）")
        ps.add_argument("--profile", default=None,
                        help="persistent profile 目录（默认 ~/.workbuddy/channels_profile）")
        _add_common(ps)

        args = parser.parse_args(raw)
        if args.cmd == "analyze":
            _cmd_analyze(args)
        elif args.cmd == "inspect":
            _cmd_inspect(args)
        elif args.cmd == "review":
            _cmd_review(args)
        elif args.cmd == "render":
            _cmd_render(args)
        elif args.cmd == "sync":
            _cmd_sync(args)
        elif args.cmd == "confirm":
            _cmd_confirm(args)
    else:
        # 默认：全自动模式
        parser = argparse.ArgumentParser(
            description="AI Video Auto Editor - 自动分析/审核/渲染")
        parser.add_argument("input", help="输入视频路径 (mp4)")
        parser.add_argument("-o", "--output", default=None,
                            help="最终视频输出路径 "
                                 "(默认 <输入目录>/edit/final_video.mp4)")
        parser.add_argument("--cover", default=None,
                            help="封面输出路径 (默认 <输入目录>/edit/cover.png)")
        parser.add_argument("--cover-title", default=None,
                            help="强制指定封面标题（覆盖 LLM 自动提炼）")
        _add_common(parser)
        parser.add_argument("--skip-review", action="store_true",
                            help="跳过交互审核（待确认项一律保留，不自动删除）")
        args = parser.parse_args(raw)
        _cmd_auto(args)


if __name__ == "__main__":
    main()
