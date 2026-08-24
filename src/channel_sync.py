"""视频号草稿同步模块（独立新增，不触碰任何现有剪辑逻辑）。

功能：把剪辑好的成片（final_video.mp4）自动上传到微信视频号助手
      （https://channels.weixin.qq.com）的**草稿箱**（不发布）。

设计原则：
- 完全独立：本模块不 import 剪辑链路的任何内部状态，只接收
  video 路径 + 标题 +（可选）描述 + 封面图；
- 登录态复用（方案 B）：用 `launch_persistent_context` 打开**独立全局
  profile 目录**（默认 ~/.workbuddy/channels_profile），cookies 与真实
  Chrome 隔离但会**永久持久化**——首次扫码一次，之后免扫码自动上传；
  登录过期会自动弹二维码重新扫码；
- 安全失败：任何一步失败只 warn 并返回 False，绝不影响成片本身；
- 草稿不发布：上传 + 填标题后**停在草稿箱**，不点「发表」。

用法：
    from .channel_sync import sync_to_channels
    ok = sync_to_channels(video, title, workdir, headless=True, description="")
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from typing import Optional

# ---- 常量 -----------------------------------------------------------------
CHANNELS_HOME = "https://channels.weixin.qq.com"
LOGIN_URL = "https://channels.weixin.qq.com/login.html"
QR_SHOT = "channels_qr.png"             # 登录二维码截图（存 workdir 供用户扫描）

# 独立全局 profile（与真实 Chrome 隔离；cookies 在此目录持久化，免重复扫码）
DEFAULT_PROFILE = os.path.join(os.path.expanduser("~"), ".workbuddy",
                               "channels_profile")

# 上传页关键元素的候选选择器（基于视频号助手真实 DOM 探测，2026-08-24 验证）
# 登录后路径：内容管理 → 发表视频 → URL /platform/post/create
SEL_MENU_CONTENT = "text=内容管理"        # 左侧折叠主菜单
SEL_MENU_PUBLISH = "text=发表视频"        # 内容管理下的子菜单
UPLOAD_PAGE_URL = "https://channels.weixin.qq.com/platform/post/create"
SEL_FILE_INPUT = 'input[type="file"]'     # 全页面唯一的视频/图片上传
SEL_TITLE_INPUT = 'input[placeholder*="短标题"]'   # 短标题输入框
SEL_DESC_INPUT = 'textarea[placeholder*="添加描述"]'  # 视频描述 textarea
SEL_SAVE_DRAFT = 'text=保存草稿'           # 草稿按钮（先点这个，不点"发表"）


def _qr_path(workdir: str) -> str:
    return os.path.join(workdir, QR_SHOT)


def _import_playwright():
    """惰性导入 playwright（未安装时给出可执行提示，不抛堆栈）。"""
    try:
        from playwright.sync_api import sync_playwright  # noqa
        return sync_playwright
    except ImportError:
        print("  [视频号] ❌ 未安装 playwright，无法自动上传。", file=sys.stderr)
        print("           安装: pip install playwright", file=sys.stderr)
        print("           （使用本机 Chrome，无需 playwright install）", file=sys.stderr)
        return None


def _chrome_path() -> str:
    for p in (
        r"C:/Program Files/Google/Chrome/Application/chrome.exe",
        r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        r"C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    ):
        if os.path.exists(p):
            return p
    return None


def _wait_scan_qr(page, workdir: str, timeout_s: int = 180) -> bool:
    """等待用户扫码：把二维码截图存到 workdir，轮询直到真正登录。

    ⚠️ 不能用 body 文本里的「内容管理/发表视频」判定登录——
    视频号助手**登录页宣传文案本身就含这些词**（假阳性）。
    只认：URL 离开 login 页 + 出现真实登录标志（退出/创作者主页入口）。
    """
    qr_path = _qr_path(workdir)
    print(f"  [视频号] 📱 请用微信扫二维码登录（截图: {qr_path}）")
    print(f"  [视频号]    最长等待 {timeout_s}s …")
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            page.screenshot(path=qr_path, full_page=False)
        except Exception:  # noqa
            pass
        url = page.url
        if "login" not in url.lower():
            # 离开 login 页：再确认有真实登录态（出现「退出」）
            try:
                body = page.inner_text("body", timeout=2500)
                if "退出" in body:
                    print(f"  [视频号] ✅ 登录成功: {url}")
                    return True
            except Exception:  # noqa
                pass
        page.wait_for_timeout(1500)
    print("  [视频号] ⚠️ 扫码超时，本次跳过上传", file=sys.stderr)
    return False


def sync_to_channels(video: str, title: str, workdir: str,
                     description: str = "", headless: bool = True,
                     cover: Optional[str] = None,
                     profile: Optional[str] = None) -> bool:
    """上传成片到视频号草稿箱。返回是否成功（失败不影响成片）。

    参数:
        video:       成片 mp4 绝对路径（必须存在）
        title:       视频号标题（优先用封面标题）
        workdir:     工作目录（存二维码截图/上传截图）
        description: 视频描述（可选，默认空）
        headless:    是否无头模式（首次登录建议 False 便于肉眼确认；
                     之后 True 自动，cookies 已持久化在 profile）
        cover:       封面图路径（可选，视频号可自动抽帧，可不传）
        profile:     persistent_context 用户数据目录（默认
                     ~/.workbuddy/channels_profile，与真实 Chrome 隔离）
    """
    video = os.path.abspath(video)
    if not os.path.exists(video):
        print(f"  [视频号] ❌ 成片不存在: {video}", file=sys.stderr)
        return False
    os.makedirs(workdir, exist_ok=True)

    sp = _import_playwright()
    if sp is None:
        return False
    chrome = _chrome_path()
    if chrome is None:
        print("  [视频号] ❌ 未找到 Chrome/Edge，无法自动上传", file=sys.stderr)
        return False
    profile_dir = os.path.abspath(profile or DEFAULT_PROFILE)
    os.makedirs(profile_dir, exist_ok=True)

    try:
        with sp() as p:
            # 方案 B：persistent_context —— cookies 与 profile 目录绑定并持久化，
            # 首次扫码一次，之后免扫码。与真实 Chrome 的 user-data-dir 完全隔离。
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=headless,
                executable_path=chrome,
                viewport={"width": 1440, "height": 900},
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(CHANNELS_HOME, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)

            url = page.url

            # ---- 未登录 → 引导扫码（只看 URL 是否仍停留 login 页）----
            if "login" in url.lower():
                print("  [视频号] 未检测到登录态，准备扫码登录…")
                if not _wait_scan_qr(page, workdir):
                    ctx.close()
                    return False
                # persistent_context 会自动把 cookies 写入 profile 目录，无需手动保存
                print(f"  [视频号] 登录态已持久化 -> {profile_dir}")
                page.goto(CHANNELS_HOME, wait_until="domcontentloaded",
                          timeout=45000)
                page.wait_for_timeout(2500)

            # ---- 进入上传页（直接 goto 最稳，避开"内容管理→视频"与首页"发表视频"按钮的歧义）----
            print("  [视频号] 进入发表视频页…")
            page.goto(UPLOAD_PAGE_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            # 等 file input 真正出现
            try:
                page.wait_for_selector('input[type="file"]', state="attached", timeout=15000)
            except Exception:  # noqa
                print("  [视频号] ⚠️ 15s 内 file input 未出现，尝试点「内容管理」…")
                if _click_publish(page):
                    try:
                        page.wait_for_selector('input[type="file"]', state="attached", timeout=15000)
                    except Exception:  # noqa
                        pass

            # ---- 上传视频 ----
            print(f"  [视频号] 上传成片: {video}")
            file_input = page.locator(SEL_FILE_INPUT).first
            try:
                file_input.set_input_files(video, timeout=30000)
            except Exception as e:  # noqa
                print(f"  [视频号] ❌ 设置文件失败: {e}", file=sys.stderr)
                ctx.close()
                return False
            # 等上传完成：进度条消失 / "保存草稿" 按钮变可点
            print("  [视频号] 等待上传+封面+描述生成（最长 900s）…")
            _wait_upload_done(page)
            page.screenshot(path=os.path.join(workdir, "channels_uploaded.png"))
            print("  [视频号] 上传后截图 -> channels_uploaded.png")

            # ---- 填标题 / 描述 ----
            t = (title or "").strip()
            if t:
                print(f"  [视频号] 填写标题: {t}")
                _fill_text(page, SEL_TITLE_INPUT, t)

            if description:
                print("  [视频号] 填写描述")
                _fill_text(page, SEL_DESC_INPUT, description)

            # 封面（可选；视频号通常自动取第一帧）
            if cover and os.path.exists(cover):
                try:
                    cover_inputs = page.locator(
                        'input[type="file"][accept*="image"], input[accept*="jpg"], input[accept*="png"]')
                    if cover_inputs.count() > 0:
                        cover_inputs.first.set_input_files(os.path.abspath(cover),
                                                           timeout=20000)
                        print(f"  [视频号] 已设置封面: {cover}")
                except Exception as e:  # noqa
                    print(f"  [视频号] ⚠️ 设置封面失败（可接受）: {e}")

            # 标题/封面设置后等按钮可用
            page.wait_for_timeout(2500)

            # ---- 存草稿（不发布）----
            print("  [视频号] 点击「保存草稿」…")
            saved = _save_draft(page)
            if saved:
                # 等待保存完成（按钮变回"保存草稿"或页面跳转）
                page.wait_for_timeout(3500)
                cur = page.url
                print(f"  [视频号] 保存后 URL: {cur}")
                # 主动检查草稿箱是否多了一条（严格数 N>0）
                if _verify_draft_added(page):
                    print("  [视频号] ✅ 已存入草稿箱（未发布，已验证）")
                else:
                    print("  [视频号] ⚠️ 「保存草稿」已点击，但草稿箱数仍为 0（可能保存中/失败）")
            else:
                print("  [视频号] ⚠️ 未找到「保存草稿」按钮，停留在编辑页", file=sys.stderr)
            page.screenshot(path=os.path.join(workdir, "channels_after_save.png"))
            ctx.close()
            return saved
    except Exception as e:  # noqa
        print(f"  [视频号] ❌ 同步失败: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return False


# ---- 内部工具（选择器尽量宽松，抓可见文本兜底）-----------------------------
def _click_publish(page) -> bool:
    """点左侧菜单 内容管理 → 发表视频。

    「内容管理」是折叠菜单（带下拉箭头），必须先点展开，
    找到子菜单中的「发表视频」再点。URL 跳到 /platform/post/create 成功。
    """
    try:
        # 先点「内容管理」展开子菜单
        for sel in (SEL_MENU_CONTENT, "li:has-text('内容管理')",
                    "[class*='menu']:has-text('内容管理')"):
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=1500):
                el.click(timeout=3000)
                page.wait_for_timeout(1500)  # 等待子菜单展开
                break
        else:
            return False
    except Exception:  # noqa
        return False
    # 再点子菜单「发表视频」
    try:
        for sel in ("text=发表视频", "li:has-text('发表视频')",
                    "a:has-text('发表视频')", "div:has-text('发表视频')"):
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=1500):
                el.click(timeout=3000)
                page.wait_for_timeout(2500)
                if "/post/create" in page.url or "/post/publish" in page.url:
                    return True
                return True
    except Exception:  # noqa
        pass
    return False


def _wait_upload_done(page, timeout_s: int = 900) -> None:
    """轮询直到视频**真正可保存**。

    视频号助手上传完成后还要做：封面生成 + 视频描述生成 + 页面初始化，
    这期间"保存草稿"按钮无效。必须等这些"生成中/初始化中"提示全部消失。

    56MB 视频通常需要 5-10 分钟处理完，所以默认 900s（15 分钟）。
    """
    t0 = time.time()
    last_msg = ""
    while time.time() - t0 < timeout_s:
        try:
            body = page.inner_text("body", timeout=3000)
        except Exception:
            page.wait_for_timeout(2500)
            continue
        # 检测"处理中"提示
        pending_kws = ["上传中", "处理中", "生成中", "页面初始化中", "初始化中"]
        pending = sum(1 for kw in pending_kws if kw in body)
        if pending > 0 and last_msg != "processing":
            print(f"    ⏳ 处理中（{pending} 项: {','.join(k for k in pending_kws if k in body)}）",
                  flush=True)
            last_msg = "processing"
        if pending == 0 and last_msg == "processing":
            # 进入"无处理中"状态，等 2s 确认稳定
            page.wait_for_timeout(2000)
            try:
                body2 = page.inner_text("body", timeout=3000)
            except Exception:
                body2 = ""
            if not any(kw in body2 for kw in pending_kws):
                print("  [视频号] ✓ 视频可保存（生成全部完成）")
                return
        page.wait_for_timeout(2500)
    print("  [视频号] ⚠️ 等待超时（视频可能未真正处理完），尝试保存",
          file=sys.stderr)


def _verify_draft_added(page) -> bool:
    """验证草稿是否真的进了草稿箱。

    跳转到 /platform/post/draftListManager，等列表渲染完毕，
    数列表行数（tr 元素）；行数 > 0 才算成功。

    注意：视频号「草稿箱」页左侧菜单直接显示「草稿箱 (N)」标题，
    body 文本里就有这个数字；表格行延迟渲染，要多等。
    """
    try:
        page.goto("https://channels.weixin.qq.com/platform/post/draftListManager",
                  wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(6000)
        # 视频号显示「草稿箱 (N)」在左侧菜单标题里（最可靠信号）
        body = page.inner_text("body", timeout=5000)
        import re
        m = re.search(r"草稿箱\s*[（(]\s*(\d+)\s*[)）]", body)
        if m:
            return int(m.group(1)) > 0
        # 兜底：数表格行
        rows = page.locator("table tr")
        return rows.count() > 1   # >1 排除表头
    except Exception:  # noqa
        return False


def _fill_text(page, selector, text: str) -> bool:
    """填充输入框（多个候选选择器逐个尝试）。"""
    sels = [selector] if isinstance(selector, str) else selector
    for sel in sels:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=1500):
                el.click(timeout=2000)
                el.fill(text, timeout=5000)
                return True
        except Exception:  # noqa
            continue
    # 兜底：找任何可见 input/textarea 填
    try:
        el = page.locator('input[type="text"], textarea').first
        if el.count() > 0 and el.is_visible(timeout=1500):
            el.fill(text, timeout=5000)
            return True
    except Exception:  # noqa
        pass
    return False


def _save_draft(page) -> bool:
    """点击「保存草稿」（**不点"发表"**），找不到时尝试常见文案。"""
    for sel in (SEL_SAVE_DRAFT, "button:has-text('保存草稿')",
                "span:has-text('保存草稿')", "text=存草稿",
                "button:has-text('存草稿')", "text=保存草稿"):
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=1500):
                el.click(timeout=3000)
                page.wait_for_timeout(2000)
                return True
        except Exception:  # noqa
            continue
    return False


# ---- CLI（独立运行调试用）---------------------------------------------------
def main_cli(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="上传视频到视频号草稿箱")
    ap.add_argument("video", help="成片 mp4 路径")
    ap.add_argument("--title", default="", help="视频号标题")
    ap.add_argument("--desc", default="", help="视频描述")
    ap.add_argument("--workdir", default="./_work", help="工作目录（存登录态）")
    ap.add_argument("--cover", default=None, help="封面图路径（可选）")
    ap.add_argument("--headed", action="store_true", help="有头模式（首次登录建议）")
    ap.add_argument("--profile", default=None,
                    help="persistent profile 目录（默认 ~/.workbuddy/channels_profile）")
    args = ap.parse_args(argv)
    ok = sync_to_channels(args.video, args.title, args.workdir,
                          description=args.desc, headless=not args.headed,
                          cover=args.cover, profile=args.profile)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main_cli()
