"""把分块 OCR 扫描结果（scan_samples.json）合入 plan.json（v7 企微检测）。"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src import config as cfgmod
from src import risk_screen as rs

VIDEO = r"E:/工作视频/演示视频ai剪辑测试/20260818161552-增长中心-石克的快速会议-视频-1.mp4"
WD = r"E:/工作视频/演示视频ai剪辑测试/20260818161552-增长中心-石克的快速会议-视频-1_work"

cfg = cfgmod.Config.load(workdir=WD)
samples = json.load(open(os.path.join(WD, "scan_samples.json"), encoding="utf-8"))
dur = rs.get_duration(VIDEO)
deletes, reviews = rs._expand_runs(samples, VIDEO, dur, cfg)
print(f"[build] 企微删除段 {len(deletes)} 个：")
for d in sorted(deletes, key=lambda x: x["start"]):
    print(f"   [{d['start']:.1f} -> {d['end']:.1f}] ({d['end']-d['start']:.1f}s) {d['reason'][:70]}")
print(f"[build] review 项 {len(reviews)} 个")

plan_path = os.path.join(WD, "plan.json")
plan = json.load(open(plan_path, encoding="utf-8"))
n_before = len(plan.get("delete_segments", []))
plan["delete_segments"] = plan.get("delete_segments", []) + deletes
plan["review_items"] = plan.get("review_items", []) + reviews
plan["intro_trim"] = 0.40          # v7：白底等候室裁到 0.40s
plan["wechat_detect_v"] = 7
if "cover" not in plan or not isinstance(plan.get("cover"), dict):
    plan["cover"] = {}
plan["cover"]["title"] = "化工产品进销存管理全流程"
plan["cover"]["duration"] = 3.0
json.dump(plan, open(plan_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"[build] plan.json 已更新：delete_segments {n_before} -> {len(plan['delete_segments'])} 段，"
      f"intro_trim={plan['intro_trim']}，wechat_detect_v={plan['wechat_detect_v']}")
