"""视频号草稿同步模块（独立新增，不触碰任何现有剪辑逻辑）。

功能：把剪辑好的成片（final_video.mp4）自动上传到微信视频号助手
      （https://channels.weixin.qq.com）的**草稿箱**（不发布）。

设计原则：
- 完全独立：本模块不 import 剪辑链路的任何内部状态，只接收
  video 路径 + 标题 +（可选）描述 + 封面图；
- 登录态复用：首次需扫码（微信扫二维码），成功后把 cookie /
  storage_state 保存到 <cfg.workdir>/channels_state.json，之后
  免扫码自动上传；登录过期（>24h）会自动弹二维码重新扫码；
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
STATE_FILE = "channels_state.json"      # 存放于 workdir
QR_SHOT = "channels_qr.png"             # 登录二维码截图（存 workdir 供用户扫描）

# 上传页关键元素的候选选择器（随页面改版动态扩展）
SEL_MENU_CONTENT = 'text=内容管理'      # 左侧主菜单
SEL_MENU_PUBLISH = 'text=发表视频'      # 内容管理下的子项
SEL_FILE_INPUT = 'input[type="file"]'   # 上传视频的文件输入
SEL_TITLE_INPUT = 'input[placeholder*="标题"], input[placeholder*="标题(必填)"], [class*="title"] input, textarea[placeholder*="标题"]'
SEL_DESC_INPUT = 'textarea[placeholder*="描述"], [class*="desc"] textarea'
SEL_SAVE_DRAFT = 'text=存草稿'          # 草稿按钮
SEL_LOGIN_BTN = 'text=登录'             # 登录页按钮


def _state_path(workdir: str) -> str:
    return os.path.join(workdir, STATE_FILE)


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
                     cover: Optional[str] = None) -> bool:
    """上传成片到视频号草稿箱。返回是否成功（失败不影响成片）。

    参数:
        video:       成片 mp4 绝对路径（必须存在）
        title:       视频号标题（优先用封面标题）
        workdir:     工作目录（存登录态/二维码截图）
        description: 视频描述（可选，默认空）
        headless:    是否无头模式（首次登录建议 False 便于肉眼确认；
                     之后 True 自动）
        cover:       封面图路径（可选，视频号可自动抽帧，可不传）
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

    try:
        with sp() as p:
            browser = p.chromium.launch(
                headless=headless,
                executable_path=chrome,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(
                viewport={"width": 1440, "height": 900},
                storage_state=_state_path(workdir)
                if os.path.exists(_state_path(workdir)) else None,
            )
            page = ctx.new_page()
            page.goto(CHANNELS_HOME, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)

            url = page.url

            # ---- 未登录 → 引导扫码（只看 URL 是否仍停留 login 页）----
            if "login" in url.lower():
                print("  [视频号] 未检测到登录态，准备扫码登录…")
                if not _wait_scan_qr(page, workdir):
                    browser.close()
                    return False
                # 登录成功后保存登录态，下次免扫码
                try:
                    ctx.storage_state(path=_state_path(workdir))
                    print(f"  [视频号] 登录态已保存 -> {_state_path(workdir)}")
                except Exception as e:  # noqa
                    print(f"  [视频号] ⚠️ 保存登录态失败: {e}")
                page.goto(CHANNELS_HOME, wait_until="domcontentloaded",
                          timeout=45000)
                page.wait_for_timeout(2500)

            # ---- 进入上传页 ----
            print("  [视频号] 进入「内容管理 → 发表视频」…")
            if not _click_publish(page):
                print("  [视频号] ⚠️ 未找到发表视频入口，尝试直连上传页…")
                page.goto("https://channels.weixin.qq.com/platform/post/publish",
                          wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)

            # ---- 上传视频 ----
            print(f"  [视频号] 上传成片: {video}")
            file_input = page.locator(SEL_FILE_INPUT).first
            try:
                file_input.set_input_files(video, timeout=30000)
            except Exception as e:  # noqa
                print(f"  [视频号] ❌ 设置文件失败: {e}", file=sys.stderr)
                browser.close()
                return False
            # 等上传完成（进度条消失 / 出现标题输入框）
            print("  [视频号] 等待上传完成…")
            _wait_upload_done(page)

            # ---- 填标题 / 描述 ----
            t = (title or "").strip()
            if t:
                print(f"  [视频号] 填写标题: {t}")
                _fill_text(page, SEL_TITLE_INPUT, t)

            if description:
                print("  [视频号] 填写描述")
                _fill_text(page, SEL_DESC_INPUT, description)

            # 封面（可选；视频号通常自动取第一帧，传封面更专业）
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

            # ---- 存草稿（不发布）----
            print("  [视频号] 保存草稿…")
            saved = _save_draft(page)
            if saved:
                print("  [视频号] ✅ 已存入草稿箱（未发布）")
            else:
                print("  [视频号] ⚠️ 未找到「存草稿」按钮，已停留在编辑页"
                      "（请手动确认不点「发表」）", file=sys.stderr)
                # 仍然保存登录态，方便下次
            try:
                ctx.storage_state(path=_state_path(workdir))
            except Exception:  # noqa
                pass
            browser.close()
            return True
    except Exception as e:  # noqa
        print(f"  [视频号] ❌ 同步失败: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return False


# ---- 内部工具（选择器尽量宽松，抓可见文本兜底）-----------------------------
def _click_publish(page) -> bool:
    """点左侧菜单 内容管理 → 发表视频。找不到返回 False。"""
    try:
        # 先找「内容管理」主菜单
        for sel in ("text=内容管理", "span:has-text('内容管理')",
                    "a:has-text('内容管理')", "[class*='menu']:has-text('内容管理')"):
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=1500):
                el.click(timeout=3000)
                page.wait_for_timeout(1200)
                break
        else:
            return False
    except Exception:  # noqa
        return False
    # 再找「发表视频」
    try:
        for sel in ("text=发表视频", "span:has-text('发表视频')",
                    "a:has-text('发表视频')", "div:has-text('发表视频')"):
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=1500):
                el.click(timeout=3000)
                page.wait_for_timeout(1500)
                return True
    except Exception:  # noqa
        pass
    return False


def _wait_upload_done(page, timeout_s: int = 300) -> None:
    """轮询直到标题输入框可见或进度条消失。"""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        # 标题输入框出现 ≈ 上传完成进入编辑态
        for sel in (SEL_TITLE_INPUT, "[class*='progress']", "text=上传中"):
            try:
                if sel.startswith("text=") and "上传中" in sel:
                    continue
                el = page.locator(sel).first
                if "progress" in sel or "上传中" in sel:
                    continue
                if el.count() > 0 and el.is_visible(timeout=800):
                    return
            except Exception:  # noqa
                pass
        page.wait_for_timeout(2000)
    print("  [视频号] ⚠️ 上传等待超时，继续尝试填标题", file=sys.stderr)


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
    """点击「存草稿」。找不到时尝试常见文案。"""
    for sel in ("text=存草稿", "button:has-text('存草稿')",
                "span:has-text('存草稿')", "text=保存草稿",
                "button:has-text('保存草稿')"):
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=1500):
                el.click(timeout=3000)
                page.wait_for_timeout(1500)
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
    args = ap.parse_args(argv)
    ok = sync_to_channels(args.video, args.title, args.workdir,
                          description=args.desc, headless=not args.headed,
                          cover=args.cover)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main_cli()
