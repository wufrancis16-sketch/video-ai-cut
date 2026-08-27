"""视频号草稿同步模块（独立新增，不触碰任何现有剪辑逻辑）。

功能：把剪辑好的成片（final_video.mp4）自动上传到微信视频号助手
      （https://channels.weixin.qq.com）的**草稿箱**（不发布）。

设计原则：
- 完全独立：本模块不 import 剪辑链路的任何内部状态，只接收
  video 路径 + 标题 + 描述 + 话题标签 +（可选）封面图；
- 登录态复用（方案 B）：用 `launch_persistent_context` 打开**独立全局
  profile 目录**（默认 ~/.workbuddy/channels_profile），cookies 与真实
  Chrome 隔离但会**永久持久化**——首次扫码一次，之后免扫码自动上传；
  登录过期会自动弹二维码重新扫码；
- 安全失败：任何一步失败只 warn 并返回 False，绝不影响成片本身；
- 草稿不发布：上传 + 填标题/描述/话题后**停在草稿箱**，不点「发表」。

用法：
    from .channel_sync import sync_to_channels
    ok = sync_to_channels(video, title, workdir, description="...", topics=["#进销存", "#财务软件"], headless=True)
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import traceback
from typing import List, Optional

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

# 短标题输入框（多候选：placeholder 可能变化）
SEL_TITLE_INPUTS = [
    'input[placeholder*="短标题"]',
    'input[placeholder*="标题"]',
    '[class*="title"] input',
    'input[name*="title"]',
]

# 视频描述 textarea / contenteditable（多候选，适配视频号 UI 可能用 div[contenteditable]）
SEL_DESC_INPUTS = [
    'textarea[placeholder*="添加描述"]',
    'textarea[placeholder*="描述"]',
    '[class*="desc"] textarea',
    'textarea[name*="desc"]',
    'textarea[class*="desc"]',
    '[contenteditable="true"][placeholder*="描述"]',
    '[contenteditable="true"][placeholder*="添加描述"]',
    'div[contenteditable="true"]',
    '[class*="desc"] [contenteditable="true"]',
]

# 话题按钮（#话题）
SEL_TOPIC_BTN = [
    'text=#话题',
    'button:has-text("话题")',
    'span:has-text("话题")',
    'div:has-text("#话题")',
    '[class*="topic"]:has-text("话题")',
]

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
    # Windows：Chrome / Edge 常见安装路径
    for p in (
        r"C:/Program Files/Google/Chrome/Application/chrome.exe",
        r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
        r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        r"C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    ):
        if os.path.exists(p):
            return p
    # macOS：/Applications 与用户目录 ~/Applications
    if sys.platform == "darwin":
        for base in ("/Applications", os.path.expanduser("~/Applications")):
            for name in ("Google Chrome", "Microsoft Edge", "Chromium"):
                cand = os.path.join(base, f"{name}.app/Contents/MacOS/{name}")
                if os.path.exists(cand):
                    return cand
    # PATH 兜底（Linux / 自定义安装）
    for name in ("google-chrome", "chromium", "chromium-browser",
                 "microsoft-edge", "google-chrome-stable"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _wait_scan_qr(page, workdir: str, timeout_s: int = 600) -> bool:
    """等待用户扫码：把二维码截图存到 workdir，轮询直到真正登录。

    判定逻辑：URL 离开 login 页，且 body 出现真实登录标志。
    可识别的登录标志包括「退出」「昨日数据」「创作者中心」「内容管理」菜单
    以及常见创作者主页元素；避免只依赖单一文本导致假阴性。
    """
    qr_path = _qr_path(workdir)
    print(f"  [视频号] 📱 请用微信扫二维码登录（截图: {qr_path}）")
    print(f"  [视频号]    二维码已刷新，最长等待 {timeout_s}s，请尽快扫码…")
    print(f"  [视频号]    （有头模式可直接扫浏览器窗口里的二维码；无头模式可打开上方截图扫）")
    t0 = time.time()
    # 登录页宣传文案通常含「内容管理/发表视频」，故不用它们做判定。
    # 以下信号只在登录后页面出现：创作者中心/数据中心/昨日数据/退出等。
    login_signals = ["退出", "昨日数据", "创作者中心", "数据中心", "帮助中心"]
    while time.time() - t0 < timeout_s:
        try:
            page.screenshot(path=qr_path, full_page=False)
        except Exception:  # noqa
            pass
        url = page.url
        if "login" not in url.lower():
            # 离开 login 页：再确认有真实登录态（任一标志即可）
            try:
                body = page.inner_text("body", timeout=2500)
                if any(sig in body for sig in login_signals):
                    print(f"  [视频号] ✅ 登录成功: {url}")
                    return True
            except Exception:  # noqa
                pass
        page.wait_for_timeout(1500)
    print("  [视频号] ⚠️ 扫码超时，本次跳过上传", file=sys.stderr)
    return False


def sync_to_channels(video: str, title: str, workdir: str,
                     description: str = "", topics: Optional[List[str]] = None,
                     headless: bool = True,
                     cover: Optional[str] = None,
                     profile: Optional[str] = None) -> bool:
    """上传成片到视频号草稿箱。返回是否成功（失败不影响成片）。

    参数:
        video:       成片 mp4 绝对路径（必须存在）
        title:       视频号短标题（优先用封面标题）
        workdir:     工作目录（存二维码截图/上传截图）
        description: 视频描述（建议 50~150 字的内容摘要 + 话题标签）
        topics:      话题标签列表（如 ["#进销存", "#财务软件", "#商贸管理"]），
                     会点击「#话题」按钮逐个添加；若 description 已含 #标签
                     则可省略此参数（代码会从 description 自动提取）
        headless:    是否无头模式（首次登录建议 False 便于肉眼确认；
                     之后 True 自动，cookies 已持久化在 profile）
        cover:       封面图路径（可选，视频号可自动抽帧，可不传）
        profile:     persistent_context 用户数据目录（默认
                     ~/.workbuddy/channels_profile，与真实 Chrome 隔离）
    """
    # ⚠️ 视频号后台「短标题」硬限制 16 字符，超出无法保存草稿！
    # 无论标题来自封面标题(≤20字)、配置还是智能体生成，填入前必须截断到 16 字符。
    # （封面标题可 ≥16 字，那是另一条路径：render --cover-title 只写 plan.cover["title"]，不走这里。）
    TITLE_MAX_CHARS = 16
    if title and len(title) > TITLE_MAX_CHARS:
        cut = title[:TITLE_MAX_CHARS]
        print(f"  [视频号] ⚠️ 短标题超 {TITLE_MAX_CHARS} 字符（视频号硬限制，超出无法保存草稿）\n"
              f"           原：「{title}」\n"
              f"           截断：「{cut}」")
        title = cut
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
                if not _wait_scan_qr(page, workdir, timeout_s=600):
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

            # 上传后提前检测阻断性弹窗（无权限/需管理员扫码），尽早失败，避免假成功
            _early_block = _detect_blocking_dialog(page)
            if _early_block == "no_permission":
                print("  [视频号] ❌ 阻断：当前登录账号不是该视频号的管理员或运营者，平台禁止保存/发表。"
                      " 请用「视频号管理员或运营者」账号登录，或让管理员把当前账号加为运营者后重试。",
                      file=sys.stderr)
                page.screenshot(path=os.path.join(workdir, "channels_blocked.png"))
                ctx.close()
                return False
            if _early_block == "admin_verify":
                # 平台要求「管理员本人验证」：暂停等待管理员扫码，通过则继续，否则放弃
                if not _wait_admin_verify(page, workdir):
                    page.screenshot(path=os.path.join(workdir, "channels_blocked.png"))
                    ctx.close()
                    return False
                # 验证通过，继续填写

            # ---- 诊断：打印真实可编辑字段（便于修正选择器）----
            _dump_fields(page)

            # ---- 填写短标题（必填，影响流量推荐）----
            t = (title or "").strip()
            if t:
                print(f"  [视频号] 填写短标题: {t}")
                ok_title = _fill_text(page, SEL_TITLE_INPUTS, t)
                if not ok_title:
                    print("  [视频号] ⚠️ 短标题未填入（选择器可能变了），尝试 JS 兜底…", file=sys.stderr)
                    _fill_text_js(page, t, is_title=True)
            else:
                print("  [视频号] ⚠️ 标题为空，跳过填写（短标题影响流量推荐，建议传入）",
                      file=sys.stderr)

            # ---- 填写视频描述（内容摘要 + 话题标签写在一起）----
            raw_desc = (description or "").strip()
            topic_list = list(topics or [])
            # 把话题统一成 #xxx 格式
            normalized_topics = []
            for t in topic_list:
                t = t.strip().lstrip('#')
                if t:
                    normalized_topics.append(f"#{t}")
            # 描述 + 话题合并成一段（用户要求：先描述，后面接话题一起）
            if normalized_topics:
                desc = raw_desc + "\n\n" + " ".join(normalized_topics)
            else:
                desc = raw_desc
            if desc:
                print(f"  [视频号] 填写视频描述 ({len(desc)} 字，含 {len(normalized_topics)} 个话题)")
                ok_desc = _fill_text(page, SEL_DESC_INPUTS, desc)
                if not ok_desc:
                    print("  [视频号] ⚠️ 描述未填入（选择器可能变了），尝试 JS 兜底…", file=sys.stderr)
                    _fill_text_js(page, desc, is_title=False)
            else:
                print("  [视频号] ⚠️ 描述为空，跳过填写", file=sys.stderr)
            # 描述里已包含 #话题，平台会自动识别，不再单独点「#话题」按钮（避免重复/冲突）
            all_topics = []

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

            # 所有字段填完后等一下让页面稳定
            page.wait_for_timeout(2000)

            # 截图记录填写结果（方便排查）
            page.screenshot(path=os.path.join(workdir, "channels_filled.png"))
            print("  [视频号] 填写完成截图 -> channels_filled.png")

            # ---- 存草稿（不发布）----
            # 保存前先确认无阻断弹窗（无权限/需管理员扫码），避免点击 disabled 按钮假成功
            block = _detect_blocking_dialog(page)
            if block == "no_permission":
                print("  [视频号] ❌ 阻断：当前登录账号不是该视频号的管理员或运营者，平台禁止保存。",
                      file=sys.stderr)
                print("        处理办法：①用视频号管理员/运营者账号重新登录；②或让管理员把当前账号加为运营者。",
                      file=sys.stderr)
                page.screenshot(path=os.path.join(workdir, "channels_blocked.png"))
                ctx.close()
                return False
            if block == "admin_verify":
                # 平台要求「管理员本人验证」：暂停等待管理员扫码，通过则继续，否则放弃
                if not _wait_admin_verify(page, workdir):
                    page.screenshot(path=os.path.join(workdir, "channels_blocked.png"))
                    ctx.close()
                    return False
            # 保存前先读草稿箱数量，保存后必须「多一条」才算成功
            before_count = _get_draft_count(page, ctx)
            print(f"  [视频号] 保存前草稿箱数量: {before_count}")
            print("  [视频号] 点击「保存草稿」…")
            saved = _save_draft(page, workdir)
            if saved:
                # 等待保存完成（按钮变回"保存草稿"或页面跳转）
                page.wait_for_timeout(3500)
                cur = page.url
                print(f"  [视频号] 保存后 URL: {cur}")
                # 严格验证：草稿箱数量必须比保存前多 1
                if _verify_draft_added(page, before_count=before_count):
                    print(f"  [视频号] ✅ 已存入草稿箱（未发布，已验证，数量 {before_count} → {before_count+1}）")
                else:
                    # 区分「管理员验证」「账号无权限」与「普通保存失败」
                    block_after = _detect_blocking_dialog(page)
                    if block_after == "admin_verify":
                        # 点击保存时触发「管理员本人验证」闸：暂停等管理员扫码，通过后重试保存
                        print("  [视频号] 🔐 保存时触发「管理员本人验证」，请管理员扫码，通过后自动重试保存…")
                        if _wait_admin_verify(page, workdir):
                            print("  [视频号] ✅ 管理员验证通过，重新点击「保存草稿」…")
                            if _save_draft(page, workdir):
                                page.wait_for_timeout(3500)
                                if _verify_draft_added(page, before_count=before_count):
                                    print(f"  [视频号] ✅ 已存入草稿箱（未发布，已验证，数量 {before_count} → {before_count+1}）")
                                else:
                                    print("  [视频号] ⚠️ 重新保存后仍未验证到草稿新增，请到视频号后台「内容管理→草稿箱」人工确认。",
                                          file=sys.stderr)
                            else:
                                print("  [视频号] ⚠️ 验证通过但未能再次点击「保存草稿」", file=sys.stderr)
                        else:
                            print("  [视频号] ❌ 管理员验证未完成，草稿未保存。", file=sys.stderr)
                    elif block_after:
                        print(f"  [视频号] ❌ 保存未生效：检测到平台阻断弹窗（{block_after}）。这是账号权限问题，不是代码问题。",
                              file=sys.stderr)
                        print(f"        请按提示用有权限的账号登录 / 完成管理员扫码后重试。", file=sys.stderr)
                    elif before_count < 0:
                        print(f"  [视频号] ⚠️ 已点击「保存草稿」但无法自动验证（草稿箱数量读取失败 before={before_count}）。"
                              f" 可能已存入，请到视频号后台「内容管理→草稿箱」人工确认。", file=sys.stderr)
                    else:
                        print(f"  [视频号] ⚠️ 「保存草稿」已点击，但草稿箱数量未增加（保存前 {before_count}），可能保存失败/被挤下线",
                              file=sys.stderr)
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


def _is_btn_enabled(el) -> bool:
    """判断按钮是否真正可点击。

    Playwright 的 is_enabled() 只识别 HTML disabled 属性，但视频号网页端用
    CSS 类 weui-desktop-btn_disabled 表示禁用（不设 disabled 属性），
    直接 is_enabled 会误判为可点。这里额外检查 CSS 类与 aria-disabled。
    """
    try:
        if not el.is_enabled(timeout=300):
            return False
    except Exception:  # noqa
        return False
    try:
        cls = el.get_attribute("class") or ""
        aria = (el.get_attribute("aria-disabled") or "").lower()
        if "weui-desktop-btn_disabled" in cls or aria == "true":
            return False
    except Exception:  # noqa
        pass
    return True


def _wait_upload_done(page, timeout_s: int = 1200) -> None:
    """轮询直到视频**真正可保存**。

    视频号助手上传完成后还要做：封面生成 + 视频描述生成 + 页面初始化，
    这期间"保存草稿"按钮尚未出现/不可点。**唯一可靠的"就绪"信号就是
    「保存草稿」按钮出现且可见**。

    编辑区（标题/描述/保存按钮）位于**嵌套 iframe** 内，因此必须在
    **所有 frame** 中查找，不能只查主 document（否则永远等不到）。
    """
    save_sels = [
        "button:has-text('保存草稿')",
        "button:has-text('存草稿')",
        "span:has-text('保存草稿')",
        "text=保存草稿",
    ]
    def _save_btn_ready():
        """保存草稿按钮存在、可见且真正可点击（含 CSS 类禁用判断）。"""
        for frame in page.frames:
            for sel in save_sels:
                try:
                    el = frame.locator(sel).first
                    if el.count() > 0 and el.is_visible(timeout=600) and _is_btn_enabled(el):
                        return True
                except Exception:  # noqa
                    pass
        return False

    def _still_uploading():
        """上传/处理区域仍显示进度或「处理中/生成中/上传中」等提示 → 未真正完成。

        注意：不能只看 0%（进度可能是任意百分比），也不能依赖按钮 enabled
        （weui 用 CSS 类禁用按钮，is_enabled 会误判为可点）。这里直接检查
        上传/处理中的可见提示文本（含 视频处理中/封面生成中/正在处理 等
        平台处理阶段的文案），全部消失才算真正就绪。
        """
        uploading_kws = ["取消上传", "上传中", "文件上传中", "视频处理中",
                         "生成中", "正在处理", "处理中，请等待"]
        for frame in page.frames:
            try:
                txt = frame.inner_text("body", timeout=800) or ""
                if txt and any(k in txt for k in uploading_kws):
                    return True
            except Exception:  # noqa
                pass
        return False

    t0 = time.time()
    last_log = 0
    while time.time() - t0 < timeout_s:
        btn_ready = _save_btn_ready()
        still_up = _still_uploading()
        if btn_ready and not still_up:
            print("  [视频号] ✓ 上传完成（无上传中提示）且「保存草稿」按钮真正可点击，页面已就绪")
            return
        elapsed = int(time.time() - t0)
        if elapsed - last_log >= 30:
            last_log = elapsed
            if still_up:
                print(f"    ⏳ 视频仍在上传/处理中，继续等待… 已等待 {elapsed}s")
            elif not btn_ready:
                print(f"    ⏳ 「保存草稿」按钮尚未真正可点（CSS 禁用），已等待 {elapsed}s")
            else:
                print(f"    ⏳ 仍在等待视频处理+生成封面/描述… 已 {elapsed}s")
        page.wait_for_timeout(3000)
    print("  [视频号] ⚠️ 等待上传完成超时（视频可能未真正处理完），仍尝试保存",
          file=sys.stderr)


def _get_draft_count(page, ctx=None) -> int:
    """读取当前草稿箱数量（左侧菜单「草稿箱 (N)」）。

    若传入 ctx，则**新开一个 page** 读草稿数，原编辑页不受影响
    （避免 page.goto 跳转丢掉正在编辑的视频）；否则在当前 page 上跳转读取。
    """
    def _read(p):
        try:
            p.goto("https://channels.weixin.qq.com/platform/post/draftListManager",
                   wait_until="domcontentloaded", timeout=30000)
            p.wait_for_timeout(4000)
            body = p.inner_text("body", timeout=5000)
            import re
            m = re.search(r"草稿箱\s*[（(]\s*(\d+)\s*[)）]", body)
            if m:
                return int(m.group(1))
        except Exception:  # noqa
            pass
        return -1  # 读取失败

    if ctx is not None:
        try:
            np = ctx.new_page()
            cnt = _read(np)
            np.close()
            return cnt
        except Exception:  # noqa
            pass
    return _read(page)


def _verify_draft_added(page, before_count: int = -1) -> bool:
    """验证草稿是否真的新增了至少一条。

    保存前取 before_count，保存后跳转草稿箱页读 after_count，
    after_count > before_count 才算真正保存成功（避免原有草稿导致误判）。
    """
    try:
        after_count = _get_draft_count(page)
        if before_count >= 0 and after_count >= 0:
            return after_count > before_count
        # 读不到 before 时兜底：至少 > 0
        return after_count > 0
    except Exception:  # noqa
        return False


def _fill_text(page, selectors: List[str], text: str) -> bool:
    """填充输入框（多个候选选择器、多个 frame 逐个尝试）。"""
    for frame in page.frames:
        for sel in selectors:
            try:
                el = frame.locator(sel).first
                if el.count() > 0 and el.is_visible(timeout=1500):
                    el.click(timeout=2000)
                    el.fill(text, timeout=5000)
                    print(f"    ✓ frame '{frame.name or 'main'}' 选择器 '{sel}' 匹配成功，已填入")
                    return True
            except Exception as e:
                continue
    print(f"    ✗ 所有 {len(selectors)} 个选择器在全部 {len(page.frames)} 个 frame 中均未匹配", file=sys.stderr)
    return False


def _fill_text_js(page, text: str, is_title: bool = True) -> None:
    """JS 兜底：按 label 文本找最近邻的可编辑字段并填入。

    策略：先精确匹配 label（短标题/视频描述），然后从 label 的
    「下一个兄弟元素」开始找，找不到再往上 2 层父级、在父级后续兄弟里找。
    对描述额外增加「#话题按钮向上查找 contenteditable」兜底，因为视频号
    的描述区经常是一个自定义 contenteditable，label 最近邻策略容易找偏。

    contenteditable 使用 document.execCommand('insertText') 模拟真实输入，
    更容易触发 React 状态更新；input/textarea 使用原生 value setter。
    填入后会回读验证，不匹配则全选重填一次。
    """
    kind = "title" if is_title else "desc"
    label_text = "短标题" if is_title else "视频描述"
    js_text = text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")

    label_js = f"""
    (() => {{
        const LABEL = '{label_text}';
        const TEXT = `{js_text}`;
        const IS_TITLE = {str(is_title).lower()};

        function isEditable(el) {{
            if (!el) return false;
            const tag = el.tagName.toLowerCase();
            if (tag === 'textarea') return true;
            if (tag === 'input') {{
                const t = (el.getAttribute('type') || 'text').toLowerCase();
                if (t !== 'text' && t !== 'search') return false;
                const ph = (el.getAttribute('placeholder') || '').toLowerCase();
                if (!IS_TITLE && ph.includes('短标题')) return false;
                return true;
            }}
            return el.getAttribute('contenteditable') === 'true';
        }}

        function currentValue(el) {{
            const tag = el.tagName.toLowerCase();
            if (tag === 'input' || tag === 'textarea') return el.value || '';
            return el.innerText || el.textContent || '';
        }}

        function fillEl(el, verify=false) {{
            el.focus();
            const tag = el.tagName.toLowerCase();
            if (tag === 'input' || tag === 'textarea') {{
                el.select && el.select();
                const proto = tag === 'textarea' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
                Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, TEXT);
            }} else {{
                // contenteditable：用 execCommand 模拟真实输入，再补 innerText
                el.innerText = TEXT;
                try {{ document.execCommand('insertText', false, TEXT); }} catch(e) {{}}
            }}
            el.dispatchEvent(new Event('focus', {{ bubbles: true }}));
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            el.dispatchEvent(new Event('blur', {{ bubbles: true }}));
            if (verify) {{
                // 校验是否真正写入
                return currentValue(el).includes(TEXT.slice(0, 8));
            }}
            return true;
        }}

        function prefer(el) {{
            if (!isEditable(el)) return 0;
            const tag = el.tagName.toLowerCase();
            if (IS_TITLE) return tag === 'input' ? 2 : (tag === 'textarea' ? 1 : 0);
            return tag === 'textarea' || el.getAttribute('contenteditable') === 'true' ? 2 : 0;
        }}

        function findBest(startEl) {{
            let sib = startEl.nextElementSibling;
            for (let i = 0; i < 4 && sib; i++) {{
                let best = null, bestScore = -1;
                let sc = prefer(sib);
                if (sc > bestScore) {{ best = sib; bestScore = sc; }}
                const inner = sib.querySelectorAll('input[type="text"], input[type="search"], textarea, [contenteditable="true"]');
                inner.forEach(n => {{
                    sc = prefer(n);
                    if (sc > bestScore) {{ best = n; bestScore = sc; }}
                }});
                if (best) return best;
                sib = sib.nextElementSibling;
            }}
            return null;
        }}

        // 描述额外兜底：通过 #话题 按钮向上找 contenteditable
        function findByTopicBtn() {{
            const all = document.querySelectorAll('*');
            let btn = null;
            for (const el of all) {{
                const t = (el.textContent || '').trim();
                if (t === '#话题' || t.includes('#话题')) {{ btn = el; break; }}
            }}
            if (!btn) return null;
            let cur = btn;
            while (cur && cur !== document.body) {{
                if (cur.getAttribute && cur.getAttribute('contenteditable') === 'true') return cur;
                cur = cur.parentElement;
            }}
            return null;
        }}

        // label 最近邻策略
        const all = document.querySelectorAll('*');
        const labels = [];
        all.forEach(el => {{
            const t = (el.textContent || '').trim();
            if (t === LABEL || t.includes(LABEL)) labels.push(el);
        }});

        for (const label of labels) {{
            let target = findBest(label);
            if (!target && label.parentElement) target = findBest(label.parentElement);
            if (!target && label.parentElement && label.parentElement.parentElement) {{
                target = findBest(label.parentElement.parentElement);
            }}
            if (target) {{
                if (!fillEl(target, true)) fillEl(target, false);
                return true;
            }}
        }}

        // 描述再兜底：#话题按钮向上查找
        if (!IS_TITLE) {{
            const t = findByTopicBtn();
            if (t) {{
                if (!fillEl(t, true)) fillEl(t, false);
                return true;
            }}
        }}
        return false;
    }})()
    """

    for frame in page.frames:
        try:
            result = frame.evaluate(label_js)
            if result:
                print(f"    ✓ JS 兜底填入成功（{'标题' if is_title else '描述'}）")
                return
        except Exception as e:
            print(f"    [调试] JS 兜底在 frame 失败: {e}", file=sys.stderr)
            continue
    print(f"    ✗ JS 兜底未找到可填字段（{'标题' if is_title else '描述'}）", file=sys.stderr)


def _fill_topics(page, topics: List[str]) -> None:
    """在视频号编辑页添加话题标签。

    流程：点击「#话题」按钮 → 弹出输入框 → 输入话题文字（不含#）→
          回车 / 点击搜索结果确认 → 重复下一个话题。

    视频号的 #话题 是一个特殊组件（类似 mention/tag picker），不是纯文本输入，
    所以不能用 fill 直接写入 textarea。
    """
    for i, topic in enumerate(topics):
        # 清理格式：确保以 # 开头
        raw = topic.strip().lstrip('#')
        if not raw:
            continue
        display = f"#{raw}"
        print(f"    [{i+1}/{len(topics)}] 添加话题: {display}")

        # 步骤 1：点击 #话题 按钮
        clicked = False
        for sel in SEL_TOPIC_BTN:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=2000):
                    btn.click(timeout=3000)
                    clicked = True
                    print(f"      ✓ 点击了话题按钮 ({sel})")
                    break
            except Exception:
                continue
        if not clicked:
            print(f"      ✗ 未找到 #话题 按钮，跳过此话题", file=sys.stderr)
            continue

        # 步骤 2：等弹出话题输入/搜索框
        page.wait_for_timeout(1000)

        # 步骤 3：在弹出的输入框中输入话题关键词（不含 #）
        topic_entered = False
        # 尝试多种可能的输入方式
        try:
            # 方式 A：找刚出现的 input（可能是搜索框）
            active_input = page.locator('input[class*="search"], '
                                       'input[placeholder*="搜索"], '
                                       'input[placeholder*="话题"], '
                                       '.ant-select input, '
                                       '[class*="tag"] input, '
                                       '[class*="topic"] input').first
            if active_input.count() > 0 and active_input.is_visible(timeout=2000):
                active_input.fill(raw, timeout=3000)
                topic_entered = True
                print(f"      ✓ 输入了话题关键词: {raw}")
        except Exception:
            pass

        if not topic_entered:
            # 方式 B：键盘直接输入（焦点可能在弹出的输入框上）
            try:
                page.keyboard.type(raw, delay=50)
                topic_entered = True
                print(f"      ✓ 键盘输入了话题关键词: {raw}")
            except Exception:
                pass

        if not topic_entered:
            print(f"      ✗ 无法输入话题关键词，跳过", file=sys.stderr)
            # 按 Escape 关闭可能的弹窗
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            continue

        # 步骤 4：等搜索结果出现，回车或点击第一个结果确认
        page.wait_for_timeout(1500)
        try:
            # 尝试按回车确认
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)
            print(f"      ✓ 回车确认话题: {display}")
        except Exception:
            try:
                # 回车不行就尝试点击搜索结果
                result_item = page.locator('[class*="option"]:visible, '
                                           '[class*="item"]:visible, '
                                           '[class*="result"]:visible, '
                                           'li:visible').first
                if result_item.count() > 0:
                    result_item.click(timeout=2000)
                    print(f"      ✓ 点击确认话题: {display}")
            except Exception:
                print(f"      ⚠️ 话题可能未完全确认（但已尝试）", file=sys.stderr)

        # 等话题标签渲染到描述区
        page.wait_for_timeout(800)

    print(f"  [视频号] 话题标签添加完毕")


def _dump_fields(page) -> None:
    """诊断：打印所有 frame 的可编辑字段 + 保存/发表按钮，便于修正选择器。"""
    for frame in page.frames:
        try:
            info = frame.evaluate("""() => {
                const out = [];
                // 可编辑字段
                document.querySelectorAll('input, textarea, [contenteditable=\"true\"]').forEach(el => {
                    const tag = el.tagName.toLowerCase();
                    if (tag === 'input') {
                        const t = (el.getAttribute('type')||'text').toLowerCase();
                        if (!['text','search'].includes(t)) return;
                    }
                    const ph = el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || '';
                    const ar = el.getAttribute('aria-label') || el.getAttribute('aria-placeholder') || '';
                    const cls = (el.className||'').toString().slice(0,90);
                    out.push('edit | ' + tag + ' | ph=' + ph + ' | aria=' + ar + ' | class=' + cls);
                });
                // 保存/发表类按钮
                document.querySelectorAll('button, [role="button"], a, div').forEach(el => {
                    const text = (el.textContent || '').trim();
                    if (/保存草稿|存草稿|保存为草稿|发表|发布/.test(text)) {
                        const cls = (el.className||'').toString().slice(0,80);
                        out.push('btn | ' + el.tagName.toLowerCase() + ' | text=' + text + ' | class=' + cls);
                    }
                });
                return out;
            }""")
            if info:
                print(f"  [诊断] frame '{frame.name or frame.url or 'main'}' 元素:")
                for line in info:
                    print("    - " + line)
        except Exception as e:
            print(f"  [诊断] frame '{frame.name or frame.url or 'main'}' 扫描失败: {e}", file=sys.stderr)


def _detect_blocking_dialog(page) -> str:
    """扫描所有 frame 的可见文本，检测是否出现**阻断性弹窗**。

    返回阻断类型：
      - "no_permission": 「你还不能发表视频 当前登录账号不是…管理员或运营者」
      - "admin_verify":  「管理员本人验证 需管理员扫码验证」
      - "" : 无阻断

    这两类弹窗是平台级**账号权限**问题，代码无法绕过，必须人工处理
    （换用管理员/运营者账号登录，或让管理员扫码验证）。
    """
    checks = {
        # 注意：admin_verify 必须优先于 no_permission 判定。
        # 当「管理员本人验证」弹窗出现时，页面往往同时带着 no_permission 文案，
        # 必须先识别为 admin_verify 并进入「等待管理员扫码」流程，否则会误判为
        # 无权限而直接放弃（这正是此前反复失败的根因）。
        "admin_verify": ["管理员本人验证", "扫码验证"],
        "no_permission": ["你还不能发表视频", "管理员或运营者"],
    }
    blob = ""
    for frame in page.frames:
        try:
            blob += " " + (frame.inner_text("body", timeout=2000) or "")
        except Exception:  # noqa
            pass
    for kind, kws in checks.items():
        if all(k in blob for k in kws):
            return kind
    return ""


def _wait_admin_verify(page, workdir: str, timeout_s: int = 300) -> bool:
    """平台要求「管理员本人验证」时，暂停等待管理员用手机微信扫页面二维码。

    返回 True 表示验证已通过（阻断弹窗消失、保存按钮可点）；
    返回 False 表示超时，或验证弹窗消失后仍是纯 no_permission（非管理员账号）。

    背景：视频号网页端近期对发表/存草稿加了「管理员本人验证」安全闸，
    即便是账号管理员，也需在当前浏览器会话内用微信「扫一扫」扫一次验证码。
    此前脚本碰到此弹窗会直接放弃（误判为无权限），现在改为暂停等待人工
    扫码通过后再继续保存草稿。
    """
    print("  [视频号] 🔐 平台要求「管理员本人验证」：请现在用【视频号管理员】的手机微信")
    print("          打开微信「扫一扫」，扫描浏览器页面上的「管理员本人验证」二维码。")
    print(f"          最长等待 {timeout_s}s，扫码通过后会自动继续保存草稿…")
    qr_path = os.path.join(workdir, "channels_admin_verify.png")
    try:
        page.screenshot(path=qr_path, full_page=False)
        print(f"  [视频号] 📷 验证二维码截图 -> {qr_path}（无头模式可打开此图扫）")
    except Exception:  # noqa
        pass
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        block = _detect_blocking_dialog(page)
        if block == "":
            print("  [视频号] ✅ 「管理员本人验证」已通过，阻断弹窗消失，继续保存…")
            return True
        if block == "no_permission":
            # admin_verify 弹窗已消失，但仍是纯 no_permission → 真·非管理员/运营者账号
            print("  [视频号] ⚠️ 管理员验证弹窗已消失，但仍是「非管理员/运营者」状态，"
                  "当前登录账号确实无权限。", file=sys.stderr)
            return False
        # 仍为 admin_verify：继续轮询等待扫码
        page.wait_for_timeout(3000)
    print(f"  [视频号] ⚠️ 等待「管理员本人验证」扫码超时（>{timeout_s}s）。", file=sys.stderr)
    return False



def _save_draft(page, workdir: str = "", timeout_s: int = 240) -> bool:
    """点击「保存草稿」（**不点"发表"**）。

    编辑区位于**嵌套 iframe** 内，必须**跨 frame** 查找按钮。
    视频处理中「保存草稿」按钮是 disabled 的，必须**等到它 enabled** 再点，
    否则点击无效（disabled 按钮用 JS 也点不动）。所以这里带一个等待循环：
    找到按钮但 disabled 时持续等待其变为可点击，超时后才放弃。
    """
    sels = [
        "button:has-text('保存草稿')",
        "button:has-text('存草稿')",
        "span:has-text('保存草稿')",
        "text=保存草稿",
        "a:has-text('保存草稿')",
        "[role='button']:has-text('保存草稿')",
    ]
    t0 = time.time()
    last_note = 0
    while time.time() - t0 < timeout_s:
        # 等待期间若弹出「管理员本人验证」，暂停等管理员扫码
        if workdir:
            blk = _detect_blocking_dialog(page)
            if blk == "admin_verify":
                print("    [调试] 保存前触发「管理员本人验证」闸，等待管理员扫码…")
                if not _wait_admin_verify(page, workdir):
                    return False
                continue
            if blk == "no_permission":
                print("  [视频号] ❌ 阻断：当前登录账号不是该视频号的管理员或运营者，平台禁止保存。",
                      file=sys.stderr)
                return False
        for frame in page.frames:
            for sel in sels:
                try:
                    el = frame.locator(sel).first
                    if el.count() > 0 and el.is_visible(timeout=1000):
                        if not _is_btn_enabled(el):
                            now = time.time()
                            if now - last_note >= 15:
                                last_note = now
                                print("    [调试] 「保存草稿」按钮仍 disabled（CSS 禁用，视频处理中），继续等待可点击…")
                            break  # 该 frame 已找到按钮，跳出 sel 循环，进入下一轮等待
                        el.click(timeout=3000)
                        page.wait_for_timeout(2500)
                        print("    ✓ 已点击「保存草稿」")
                        return True
                except Exception:  # noqa
                    continue
        page.wait_for_timeout(2000)
    print("    [调试] 等待「保存草稿」可点击超时（>%ds），尝试 JS 兜底" % timeout_s, file=sys.stderr)
    # ---- JS 兜底：跨 frame 按文本找并点击（跳过 disabled/hidden）----
    js_click = """(() => {
        const kws = ['保存草稿', '保存为草稿'];
        const all = document.querySelectorAll('button, [role="button"], a, span, div');
        for (const el of all) {
            const text = (el.textContent || '').trim();
            if (kws.some(k => text === k || text.startsWith(k + ' '))) {
                if (el.disabled || el.getAttribute('aria-disabled') === 'true' ||
                    el.classList.contains('weui-desktop-btn_disabled')) continue;
                const st = getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                el.scrollIntoView({behavior:'auto', block:'center'});
                el.click();
                return text;
            }
        }
        return false;
    })()"""
    for frame in page.frames:
        try:
            clicked = frame.evaluate(js_click)
            if clicked:
                print(f"    ✓ JS 兜底点击「{clicked}」（frame 内）")
                page.wait_for_timeout(2500)
                return True
        except Exception as e:
            print(f"    [调试] JS 兜底在 frame 失败: {e}", file=sys.stderr)
    return False
