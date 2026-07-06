"""
OptikLink 每日自动登录脚本 v4.11 (CloakBrowser版)
原理：用 CloakBrowser 打开页面，注入 Discord Token 完成 OAuth2 授权

新增记录 v4.11:
  - 【修复】"Login to Panel" 点到广告了。原因：a:has-text("Login to Panel") 选择器
    会匹配到广告链接，而真正的触发按钮是 Bootstrap modal 链接，
    HTML 为 <a data-toggle="modal" data-target="#logintopanel">
    改为精确匹配 [data-target="#logintopanel"] 属性，彻底避开广告
  - 密码读取改用 JS evaluate 直接访问 document.getElementById('password'/'pass')
    的 innerText，不受 is_visible 限制，CSS hidden 元素也能正常读取

新增记录 v4.10:
  - 【修复】上一版误判 control.optiklink.net/auth/login 为 SSO 自动跳转地址，
    实测它是一个真正的登录表单页。改为：先在 optiklink.net 首页
    "Login to Panel" 弹窗里读取专用的面板账号密码，再到面板登录页填表单提交
  - 新增 get_panel_credentials()：从首页弹窗解析 Panel Username / Panel Password

新增记录 v4.9:
  - 【修复】control.optiklink.net 是独立面板系统，需要单独 SSO 登录才有会话
    新增 login_control_panel()：先访问 control.optiklink.net/auth/login 建立面板会话，
    再进入 /server/{id} 检测状态，否则页面拿不到真实状态，只会读到 UNKNOWN
  - 状态检测改为等待 OFFLINE/ONLINE/STARTING/STOPPING 关键字出现（最多 8s），
    不再依赖固定 sleep，应对面板异步 WebSocket 拉取状态的情况
  - 新增 NO_ACCESS 状态：若打开 /server/{id} 后被重定向离开，说明面板会话未建立成功

新增记录 v4.8:
  - 【新功能】登录成功后自动跳转 control.optiklink.net/server/{ID} 检测服务器运行状态
    若为 OFFLINE，自动点击 START 按钮启动；支持通过 SERVER_IDS（逗号分隔）配置多台服务器
  - 推送消息新增"服务器状态"表格，展示每台服务器的检测结果与是否已自动启动

修复记录 v4.6:
  - 【录屏修复】抛弃 scrot/import 逐帧截屏方案（黑屏问题）
    改用 ffmpeg x11grab 直接从 Xvfb :99 录制视频流，无需逐帧截图再合成
    后台进程用 Popen 管理，彻底绕开 Playwright 线程限制
  - 【弹窗修复】新增 Google Vignette 弹窗广告自动关闭逻辑
    点击 Discord 按钮后检测 #google_vignette 并逐层关闭所有遮罩层
  - 新增通用弹窗/广告拦截器，在页面加载后自动清除常见广告弹窗
  - Discord OAuth 授权页增强：新增 "同意" 按钮处理，支持中文界面

修复记录 v4.7:
  - 截图改为无条件保存（移除 ENABLE_SCREENSHOT 开关），与 Zytrano 保持一致
  - 录屏改为 workflow_dispatch 手动触发时可选（true=录屏 / false=不录屏），默认 false

修复记录 v4.5:
  - 新增录屏功能：环境变量 ENABLE_SCREENRECORD=true 开启，默认 false
  - 录屏文件保存至 recordings/ 目录

修复记录 v4.4:
  - Discord 按钮实际为 <a href="login" class="hyperlink_abs w-inline-block">（无文字，图标为图片）
  - 将 a[href="login"].hyperlink_abs / a[href="login"] 加入选择器列表并置于首位

修复记录 v4.3:
  - 在点击 Discord 按钮前，自动关闭 Cookie/GDPR 同意弹窗（fc- 前缀）
"""

import os
import re
import sys
import json
import time
import subprocess
import logging
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 配置（全部从 GitHub Secrets / 环境变量读取）
# ─────────────────────────────────────────────────────────────
DISCORD_TOKEN        = os.environ["DISCORD_TOKEN"]
WXPUSHER_TOKEN       = os.environ["WXPUSHER_TOKEN"]
WXPUSHER_UID         = os.environ["WXPUSHER_UID"]
EXPIRE_DATE          = os.environ.get("EXPIRE_DATE", "")
PROXY_URL            = os.environ.get("PROXY_URL", "socks5://127.0.0.1:10808")
ENABLE_SCREENRECORD  = os.environ.get("ENABLE_SCREENRECORD",  "false").lower() == "true"
PANEL_USERNAME       = os.environ.get("PANEL_USERNAME", "")
PANEL_PASSWORD       = os.environ.get("PANEL_PASSWORD", "")
PANEL_REMEMBER_COOKIE = os.environ.get("PANEL_REMEMBER_COOKIE", "")

# v4.8: 新增 —— 登录后自动检测服务器状态，OFFLINE 则自动 START
# 支持多个服务器 ID，逗号分隔，例如 "90a93db8,abcd1234"
# 完全从环境变量 SERVER_IDS 读取（GitHub Secrets 未配置或为空时，此处兜底默认值仅用于本地调试）
SERVER_IDS = [
    s.strip() for s in (os.environ.get("SERVER_IDS") or "90a93db8").split(",") if s.strip()
]

BASE_URL      = "https://optiklink.net"
AUTH_URL      = f"{BASE_URL}/auth"
DASHBOARD_URL = BASE_URL
CONTROL_BASE_URL = "https://control.optiklink.net/server"

VIEWPORT_W = 1280
VIEWPORT_H = 754  # 必须为偶数，h264 编码器要求宽高均可被 2 整除（原 753 奇数导致录屏 0 字节）

# ─────────────────────────────────────────────────────────────
# 截图
# ─────────────────────────────────────────────────────────────
SCREENSHOT_DIR = Path("./screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

def take_screenshot(page, name: str):
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = str(SCREENSHOT_DIR / f"{ts}_{name}.png")
        page.screenshot(path=path, full_page=False)
        log.info(f"📸 截图已保存: {path}")
    except Exception as e:
        log.warning(f"截图失败: {e}")

# ─────────────────────────────────────────────────────────────
# 录屏 v4.6 — 用 ffmpeg x11grab 直接从 Xvfb :99 录制视频流
# 原因：scrot/import 截 Xvfb 常出现黑屏；ffmpeg x11grab 直接抓显示流更可靠
# 后台 Popen 不接触任何 Playwright 对象，彻底绕开 greenlet 限制
# ─────────────────────────────────────────────────────────────
RECORDING_DIR = Path("./recordings")
RECORDING_DIR.mkdir(exist_ok=True)


def start_page_recording(page=None):
    """
    开始录屏 — 用 ffmpeg x11grab 直接从 Xvfb :99 录制视频流。
    无需逐帧截图再合成，一条命令搞定。
    page 参数保留以兼容旧调用，实际不使用。
    """
    if not ENABLE_SCREENRECORD:
        return None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = str(RECORDING_DIR / f"{ts}_recording.mp4")

    # ffmpeg x11grab：直接从 X11 显示抓取视频流
    cmd = [
        "ffmpeg", "-y",
        "-f", "x11grab",
        "-video_size", f"{VIEWPORT_W}x{VIEWPORT_H}",
        "-framerate", "2",
        "-i", ":99",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "ultrafast",
        "-crf", "28",
        out_path,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        # 等待 2s，确认 ffmpeg 真正开始录制而非立即退出
        time.sleep(2)
        if proc.poll() is not None:
            stderr_out = proc.stderr.read().decode(errors="replace")
            log.error(f"🎬 ffmpeg 启动后立即退出，可能是 Xvfb :99 未就绪。stderr:\n{stderr_out}")
            return None
        log.info(f"🎬 录屏已开始 (ffmpeg x11grab :99) → {out_path}")
    except FileNotFoundError:
        log.error("🎬 ffmpeg 未安装，录屏不可用")
        return None
    except Exception as e:
        log.error(f"🎬 启动录屏失败: {e}")
        return None

    return {"ts": ts, "proc": proc, "path": out_path}


def stop_page_recording(rec):
    """
    停止 ffmpeg 录屏进程。
    """
    if rec is None:
        return

    proc = rec.get("proc")
    path = rec.get("path", "unknown")

    if proc and proc.poll() is None:
        try:
            # 发送 'q' 让 ffmpeg 优雅退出
            proc.stdin.write(b"q")
            proc.stdin.flush()
            _, stderr_bytes = proc.communicate(timeout=15)
            if stderr_bytes:
                log.info(f"🎬 ffmpeg stderr:\n{stderr_bytes.decode(errors='replace')[-2000:]}")
            log.info(f"🎬 录屏已停止: {path}")
        except subprocess.TimeoutExpired:
            log.warning("ffmpeg 未在 15s 内退出，强制终止")
            proc.kill()
            _, stderr_bytes = proc.communicate(timeout=5)
            if stderr_bytes:
                log.info(f"🎬 ffmpeg stderr:\n{stderr_bytes.decode(errors='replace')[-2000:]}")
        except Exception as e:
            log.warning(f"停止录屏异常: {e}")
            try:
                proc.kill()
            except Exception:
                pass

    # 检查输出文件
    if Path(path).exists():
        size_mb = Path(path).stat().st_size / (1024 * 1024)
        log.info(f"🎬 录屏文件: {path} ({size_mb:.1f} MB)")
    else:
        log.warning(f"🎬 录屏文件未生成: {path}")

# ─────────────────────────────────────────────────────────────
# WxPusher 推送
# ─────────────────────────────────────────────────────────────
def wxpush(title: str, content: str):
    import urllib.request
    payload = json.dumps({
        "appToken":    WXPUSHER_TOKEN,
        "content":     content,
        "summary":     title,
        "contentType": 3,
        "uids":        [WXPUSHER_UID],
    }).encode()
    try:
        req = urllib.request.Request(
            "https://wxpusher.zjiecode.com/api/send/message",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if result.get("success"):
                log.info("📨 WxPusher 推送成功")
            else:
                log.warning(f"📨 WxPusher 推送失败: {result}")
    except Exception as e:
        log.warning(f"📨 WxPusher 推送异常: {e}")

# ─────────────────────────────────────────────────────────────
# Discord Token 注入工具
# ─────────────────────────────────────────────────────────────
def inject_discord_token(page, token: str):
    """向 Discord 页面注入 Token（localStorage），然后刷新"""
    page.evaluate("""(token) => {
        const f = document.createElement('iframe');
        f.style.display = 'none';
        document.body.appendChild(f);
        f.contentWindow.localStorage.setItem('token', '"' + token + '"');
        try { localStorage.setItem('token', '"' + token + '"'); } catch(e) {}
        document.body.removeChild(f);
    }""", token)
    log.info("Token 已注入 localStorage")

# ─────────────────────────────────────────────────────────────
# Discord OAuth 授权页处理
# ─────────────────────────────────────────────────────────────
def handle_oauth_page(page):
    log.info("处理 Discord OAuth 授权页...")
    page.wait_for_timeout(3000)  # 多等一会让 JS 渲染完

    # ── 先尝试点击外层可见的授权/同意按钮 ──
    # Discord 中文版可能直接显示 "授权" 按钮，不需要滚动
    for outer_sel in [
        'button:has-text("授权")',
        'button:has-text("Authorize")',
        'button:has-text("同意")',
        'button:has-text("Agree")',
        'button:has-text("Continue")',
        'button:has-text("继续")',
        'button[type="submit"]',
        'div[class*="footer"] button',
        'button[class*="primary"]',
        'button[class*="button"] span:has-text("授权")',
    ]:
        try:
            btn = page.locator(outer_sel).last
            if not btn.is_visible(timeout=1500):
                continue
            text = btn.inner_text(timeout=500).strip()
            if not text:
                continue
            # 跳过取消/拒绝按钮
            if any(k in text.lower() for k in ("取消", "cancel", "deny", "拒绝", "decline")):
                continue
            if btn.is_disabled():
                continue
            log.info(f"点击授权/同意按钮: {text}")
            btn.click()
            page.wait_for_timeout(2000)
            if "discord.com" not in page.url:
                log.info("已离开 Discord，OAuth 完成")
                return
        except Exception:
            continue

    # ── 如果外层按钮没找到，滚动权限列表找底部按钮 ──
    for _ in range(30):
        if "discord.com" not in page.url:
            log.info("已离开 Discord，OAuth 完成")
            return

        btn_text = ""
        try:
            for sel in ['button[type="submit"]', 'div[class*="footer"] button', 'button[class*="primary"]']:
                btn = page.locator(sel).last
                if btn.is_visible():
                    btn_text = btn.inner_text().strip().lower()
                    break
        except Exception:
            pass

        if "authorize" in btn_text or "授权" in btn_text or "同意" in btn_text:
            break

        # 滚动权限列表
        page.evaluate("""() => {
            const sels = ['[class*="scroller"]','[class*="oauth2"]','[class*="permissionList"]',
                '[class*="content"] [class*="scroll"]','[class*="listScroller"]',
                'div[class*="modal"] div[style*="overflow"]','div[class*="root"] div[style*="overflow"]'];
            let scrolled = false;
            for (const sel of sels) {
                for (const el of document.querySelectorAll(sel)) {
                    const s = getComputedStyle(el);
                    if (el.scrollHeight > el.clientHeight &&
                        ['auto','scroll'].some(v => s.overflowY === v || s.overflow === v))
                        { el.scrollTop = el.scrollHeight; scrolled = true; }
                }
            }
            if (!scrolled) document.querySelectorAll('div').forEach(el => {
                if (el.scrollHeight > el.clientHeight + 10) {
                    const s = getComputedStyle(el);
                    if (['auto','scroll','hidden'].includes(s.overflowY)) el.scrollTop = el.scrollHeight;
                }
            });
            scrollTo(0, document.body.scrollHeight);
        }""")
        page.wait_for_timeout(800)

    # ── 再尝试点击底部授权按钮 ──
    for _ in range(10):
        if "discord.com" not in page.url:
            return
        for sel in [
            'button:has-text("授权")',
            'button:has-text("Authorize")',
            'button:has-text("同意")',
            'button:has-text("Agree")',
            'button[type="submit"]',
            'div[class*="footer"] button',
            'button[class*="primary"]',
        ]:
            try:
                btn = page.locator(sel).last
                if not btn.is_visible(timeout=1000):
                    continue
                text = btn.inner_text(timeout=500).strip()
                if not text:
                    continue
                if any(k in text.lower() for k in ("取消", "cancel", "deny", "拒绝", "decline")):
                    continue
                if btn.is_disabled():
                    page.wait_for_timeout(1000)
                    break
                log.info(f"点击授权按钮: {text}")
                btn.click()
                page.wait_for_timeout(2000)
                if "discord.com" not in page.url:
                    return
                break
            except Exception:
                continue
        page.wait_for_timeout(1500)

    log.warning("OAuth 授权页处理完毕，但可能未成功点击授权按钮")

# ─────────────────────────────────────────────────────────────
# v4.6 新增：关闭页面弹窗/广告
# ─────────────────────────────────────────────────────────────
def close_popups_and_overlays(page):
    """
    用 JS 关闭页面上所有可能的弹窗、广告遮罩层。
    包括 Google Vignette、Cookie 弹窗、各类 modal overlay。
    返回关闭的弹窗数量。
    """
    closed_count = page.evaluate("""() => {
        let closed = 0;

        // 1. Google Vignette 弹窗（#google_vignette 相关）
        const vignetteSelectors = [
            '#google-vignette', '.google-vignette', '[id*="vignette"]',
            '#credential_picker_container', '#credential-picker-container',
        ];
        for (const sel of vignetteSelectors) {
            for (const el of document.querySelectorAll(sel)) {
                el.remove();
                closed++;
            }
        }

        // 2. 常见高 z-index 遮罩 (遮罩层通常 z-index > 1000)
        const allDivs = document.querySelectorAll('div');
        for (const el of allDivs) {
            const style = getComputedStyle(el);
            const z = parseInt(style.zIndex) || 0;
            if (z > 1000 && (
                style.position === 'fixed' || style.position === 'absolute'
            )) {
                const rect = el.getBoundingClientRect();
                // 覆盖大面积 (>50% 视口) 的遮罩
                if (rect.width > window.innerWidth * 0.5 &&
                    rect.height > window.innerHeight * 0.5) {
                    el.remove();
                    closed++;
                }
            }
        }

        // 3. 通用 iframe 广告
        for (const iframe of document.querySelectorAll('iframe')) {
            const src = (iframe.src || '').toLowerCase();
            if (src.includes('google') || src.includes('doubleclick') ||
                src.includes('ad') || src.includes('adsense') ||
                src.includes('vignette')) {
                iframe.remove();
                closed++;
            }
        }

        // 4. 恢复 body 滚动 (弹窗通常设置 overflow:hidden)
        document.body.style.overflow = '';
        document.documentElement.style.overflow = '';

        return closed;
    }""")
    if closed_count > 0:
        log.info(f"🧹 已关闭 {closed_count} 个弹窗/广告/遮罩")
    return closed_count


def handle_google_vignette(page):
    """
    专门处理 Google Vignette 弹窗 — 
    表现为 URL hash 出现 #google_vignette 且页面被遮罩挡住。
    """
    current_url = page.url
    if "google_vignette" not in current_url:
        return False

    log.info("⚠️ 检测到 Google Vignette 弹窗，正在关闭...")

    # 方法一：JS 暴力清除所有遮罩层
    page.evaluate("""() => {
        document.querySelectorAll('*').forEach(el => {
            const s = getComputedStyle(el);
            const z = parseInt(s.zIndex) || 0;
            if (z > 100 && (s.position === 'fixed' || s.position === 'absolute')) {
                const r = el.getBoundingClientRect();
                if (r.width >= window.innerWidth * 0.3 || r.height >= window.innerHeight * 0.3) {
                    el.remove();
                }
            }
        });
        document.body.style.overflow = '';
        document.body.style.position = '';
        document.documentElement.style.overflow = '';
        if (window.location.hash.includes('google_vignette')) {
            history.replaceState(null, '', window.location.pathname + window.location.search);
        }
    }""")
    page.wait_for_timeout(1000)

    # 方法二：用 Playwright 点击可能的关闭按钮
    for close_sel in [
        'button[aria-label="Close"]',
        'button[aria-label="关闭"]',
        '[class*="close"]',
        '[class*="dismiss"]',
        'button:has-text("Close")',
        'button:has-text("关闭")',
        'a[aria-label="Close"]',
    ]:
        try:
            el = page.locator(close_sel).first
            if el.is_visible(timeout=1000):
                el.click()
                log.info(f"已点击关闭按钮: {close_sel}")
                page.wait_for_timeout(1000)
                break
        except Exception:
            continue

    # 方法三：按 Escape
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    except Exception:
        pass

    current_url_after = page.url
    if "google_vignette" not in current_url_after:
        log.info("✅ Google Vignette 已关闭")
        return True

    log.warning("⚠️ Google Vignette 仍存在，尝试重新点击 Discord 按钮")
    return False

# ─────────────────────────────────────────────────────────────
# 主登录流程
# ─────────────────────────────────────────────────────────────
def do_login(page) -> bool:
    log.info(f"[A] 打开登录页: {AUTH_URL}")
    try:
        page.goto(AUTH_URL, timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        log.warning(f"goto 超时/异常: {e}")
    take_screenshot(page, "01_auth_page")

    page.wait_for_timeout(2000)

    # v4.6: 页面加载后先清理一次弹窗
    close_popups_and_overlays(page)

    # 服务条款确认按钮
    try:
        confirm_btn = page.locator("button#confirm-login, button:has-text('同意'), button:has-text('Agree'), button:has-text('Accept')")
        if confirm_btn.first.is_visible(timeout=3000):
            confirm_btn.first.click()
            log.info("已点击服务条款确认按钮")
            page.wait_for_timeout(1500)
    except Exception:
        pass

    # FIX v4.3: 关闭 Cookie/GDPR 同意弹窗（fc- 前缀）
    for consent_sel in [
        'button.fc-cta-consent',
        'button.fc-button.fc-cta-consent',
        'button.fc-vendor-preferences-accept-all',
        'button.fc-data-preferences-accept-all',
        'button:has-text("Consent")',
        'button:has-text("Accept all")',
        'button:has-text("同意")',
    ]:
        try:
            btn = page.locator(consent_sel).first
            if btn.is_visible(timeout=1500):
                btn.click()
                log.info(f"已关闭 Cookie 弹窗: {consent_sel}")
                page.wait_for_timeout(1000)
                break
        except Exception:
            continue

    # v4.6: 再清理一轮弹窗
    close_popups_and_overlays(page)

    # 点击 Discord 登录按钮
    log.info("[B] 点击 Discord 登录按钮...")
    clicked = False
    for sel in [
        'a[href="login"].hyperlink_abs',
        'a[href="login"]',
        'div.nav_login_block_extra a[href="login"]',
        'button:has-text("DISCORD")',
        'button:has-text("Discord")',
        'button:has-text("discord")',
        'a:has-text("DISCORD")',
        'a:has-text("Discord")',
        'button[class*="discord"]',
        '[class*="discord-btn"]',
        '[class*="discordBtn"]',
        'a[href*="discord.com/oauth2"]',
        'a[href*="oauth2/authorize"]',
        'a:has-text("Sign in with Discord")',
        'a:has-text("Login with Discord")',
        '.discord-btn',
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=3000):
                btn.click()
                log.info(f"已点击登录按钮: {sel}")
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        log.error("未找到 Discord 登录按钮，开始打印页面元素调试信息...")
        try:
            elements = page.locator("button, a").all()
            for el in elements:
                try:
                    tag  = el.evaluate("el => el.tagName")
                    text = el.inner_text(timeout=500).strip()[:80]
                    cls  = el.get_attribute("class") or ""
                    href = el.get_attribute("href") or ""
                    log.info(f"  [{tag}] text='{text}' class='{cls[:60]}' href='{href[:60]}'")
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"调试元素打印失败: {e}")
        take_screenshot(page, "01b_click_fail")
        return False

    # ──────── v4.6: Google Vignette 弹窗检测与关闭 ────────
    page.wait_for_timeout(2000)

    if "google_vignette" in page.url:
        log.info("检测到 Google Vignette 弹窗，正在处理...")
        take_screenshot(page, "01c_google_vignette")
        handle_google_vignette(page)
        page.wait_for_timeout(1500)
        close_popups_and_overlays(page)

        if "discord.com" not in page.url and "google_vignette" not in page.url:
            log.info("弹窗已关闭，重新点击 Discord 按钮...")
            for sel in ['a[href="login"]', 'a[href="login"].hyperlink_abs']:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=3000):
                        btn.click()
                        log.info(f"重新点击: {sel}")
                        page.wait_for_timeout(2000)
                        break
                except Exception:
                    continue

    for _ in range(3):
        if "google_vignette" in page.url:
            log.info("Google Vignette 仍存在，尝试 Escape...")
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(1000)
            except Exception:
                pass
            close_popups_and_overlays(page)
        else:
            break

    # 等待跳转到 discord.com
    log.info("[C] 等待跳转到 Discord...")
    try:
        page.wait_for_url(re.compile(r"discord\.com"), timeout=15000)
        log.info(f"已到达 Discord: {page.url}")
    except Exception as e:
        log.warning(f"等待 Discord 超时: {e}，当前URL: {page.url}")
        take_screenshot(page, "02_discord_timeout")
        return False

    take_screenshot(page, "02_discord_page")

    # 注入 Token
    log.info("[D] 注入 Discord Token...")
    inject_discord_token(page, DISCORD_TOKEN)
    page.reload(wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)

    if re.search(r"discord\.com/login", page.url):
        log.error("Token 注入失败，仍在登录页")
        take_screenshot(page, "03_token_failed")
        return False
    log.info("Token 注入成功")
    take_screenshot(page, "03_token_injected")

    # 处理 OAuth 授权页
    try:
        page.wait_for_url(re.compile(r"discord\.com/oauth2/authorize"), timeout=6000)
        page.wait_for_timeout(2000)
        if "discord.com" in page.url:
            handle_oauth_page(page)
            if "discord.com" in page.url:
                try:
                    page.wait_for_url(re.compile(r"optiklink\.net"), timeout=20000)
                except Exception:
                    pass
    except Exception:
        if "discord.com" in page.url:
            handle_oauth_page(page)

    take_screenshot(page, "04_after_oauth")

    # 等待跳回 optiklink.net
    log.info("[E] 等待跳回 OptikLink...")
    try:
        page.wait_for_url(re.compile(r"optiklink\.net"), timeout=20000)
        log.info(f"已跳回: {page.url}")
    except Exception as e:
        log.warning(f"等待跳回超时: {e}，当前URL: {page.url}")
        if "optiklink.net" not in page.url:
            take_screenshot(page, "05_redirect_timeout")
            return False

    current = page.url
    if "optiklink.net" in current and "/auth" not in current:
        log.info(f"已在 OptikLink: {current}")
    else:
        log.info("手动导航到首页...")
        try:
            page.goto(DASHBOARD_URL, timeout=20000, wait_until="domcontentloaded")
        except Exception as e:
            log.warning(f"goto 首页超时: {e}")

    take_screenshot(page, "05_home_page")
    return True

# ─────────────────────────────────────────────────────────────
# 读取 Dashboard 信息
# ─────────────────────────────────────────────────────────────
def read_dashboard(page) -> dict:
    log.info("[F] 读取 Dashboard 信息...")
    info = {
        "logged_in":       False,
        "username":        "N/A",
        "expire_date":     EXPIRE_DATE,
        "running_servers": "N/A",
    }

    try:
        page.wait_for_timeout(3000)
        html = page.content()
        text = page.inner_text("body")
    except Exception as e:
        log.warning(f"读取页面失败: {e}")
        return info

    current_url = page.url.lower()

    is_logged_in = (
        "/auth" not in current_url
        and "optiklink.net" in current_url
        and any(kw in html.upper() for kw in ("DASHBOARD", "MY PLAN", "SERVER", "LOGOUT", "SIGN OUT"))
    )

    if is_logged_in:
        info["logged_in"] = True
        log.info(f"✅ 确认已登录，URL: {page.url}")
    else:
        log.warning(f"当前URL: {page.url}，未检测到登录态关键字")
        log.warning(f"页面片段: {text[:200]}")
        return info

    for pat in [
        r'Welcome\s+(?:<[^>]+>)?(\w+)(?:<[^>]+>)?\s+to',
        r'"username"\s*:\s*"([^"]+)"',
        r'Hello,?\s+(\w+)',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            info["username"] = m.group(1) if m.lastindex else m.group(0)
            break

    for pat in [
        r'(\d{2}\.\d{2}\.\d{4})',
        r'date:\s*(\d{2}\.\d{2}\.\d{4})',
        r'expire[^:]*:\s*(\d{2}\.\d{2}\.\d{4})',
    ]:
        m = re.search(pat, text, re.I)
        if m:
            info["expire_date"] = m.group(1)
            break

    m2 = re.search(r'(\d+)\s*(?:running\s*)?servers?', text, re.I)
    if m2:
        info["running_servers"] = m2.group(1)

    log.info(f"Dashboard 信息: {info}")
    return info

# ─────────────────────────────────────────────────────────────
# v4.10: control.optiklink.net/auth/login 实际是一个真正的登录表单页
# （不是自动 SSO 跳转）。需要的账号密码就是主站首页 "Login to Panel"
# 弹窗里展示的那组专用面板账号：
#   Your Panel Username: xxxxx
#   Your Panel Password: xxxxx
# 流程：先在主站首页读出这组账号密码 → 打开面板登录页 → 填表单提交
# ─────────────────────────────────────────────────────────────
CONTROL_LOGIN_URL = "https://control.optiklink.net/auth/login"


def get_panel_credentials(page):
    """从环境变量（GitHub Secrets）读取面板专用账号密码。
    PANEL_USERNAME / PANEL_PASSWORD 直接配置在 Secrets 里，无需从 DOM 解析。
    """
    log.info("[G0a] 读取面板专用账号密码...")
    take_screenshot(page, "05a_panel_credentials_modal")

    username = PANEL_USERNAME.strip()
    password = PANEL_PASSWORD.strip()

    if username and password:
        log.info(f"✅ 已从环境变量获取面板账号: {username}，密码长度: {len(password)}")
        return username, password

    log.warning("环境变量 PANEL_USERNAME / PANEL_PASSWORD 未配置，无法登录控制面板")
    return None


def login_control_panel(page) -> bool:
    """登录控制面板。
    优先策略：注入 remember_me cookie 直接免登录（绕过 reCAPTCHA）。
    兜底策略：账号密码 + reCAPTCHA token 表单登录。
    """
    log.info("[G0] 登录控制面板 (control.optiklink.net)...")

    # ── 策略一：注入 remember_me cookie，直接免登录 ──
    if PANEL_REMEMBER_COOKIE:
        log.info("[G0-cookie] 尝试注入 remember_me cookie...")
        try:
            # 先访问域名建立上下文，再添加 cookie
            page.goto("https://control.optiklink.net/", timeout=20000, wait_until="domcontentloaded")
        except Exception:
            pass

        # 找到 remember_web_ cookie 的名字（从响应里固定的那个长名字）
        REMEMBER_COOKIE_NAME = "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"
        try:
            page.context.add_cookies([{
                "name":   REMEMBER_COOKIE_NAME,
                "value":  PANEL_REMEMBER_COOKIE,
                "domain": "control.optiklink.net",
                "path":   "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }])
            log.info("[G0-cookie] cookie 已注入，验证登录态...")
            page.goto("https://control.optiklink.net/", timeout=20000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            current = page.url.lower()
            if "control.optiklink.net" in current and "/auth/login" not in current:
                log.info(f"✅ cookie 免登录成功: {page.url}")
                take_screenshot(page, "05b_control_cookie_login_ok")
                return True
            else:
                log.warning(f"cookie 免登录失败（当前URL: {page.url}），降级到账号密码登录")
        except Exception as e:
            log.warning(f"cookie 注入异常: {e}，降级到账号密码登录")

    # ── 策略二：账号密码 + reCAPTCHA 表单登录（兜底）──
    creds = get_panel_credentials(page)

    try:
        page.goto(CONTROL_LOGIN_URL, timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        log.warning(f"打开控制面板登录页异常: {e}")
    page.wait_for_timeout(2500)
    close_popups_and_overlays(page)
    take_screenshot(page, "05b_control_panel_login_page")

    current = page.url.lower()
    if "control.optiklink.net" in current and "/auth/login" not in current:
        log.info(f"✅ 已处于登录状态: {page.url}")
        return True

    if not creds:
        log.warning("没有可用的面板账号密码，无法自动登录控制面板")
        return False

    username, password = creds

    # 等输入框真正可交互（避免 fill 静默失败）
    user_sel = 'input[name="username"]'
    try:
        page.wait_for_selector(user_sel, state="visible", timeout=10000)
    except Exception:
        log.warning("等待用户名输入框超时")
        take_screenshot(page, "05c_login_form_user_notfound")
        return False

    # 用 click + type 代替 fill，更接近真实用户操作，避免框架拦截
    try:
        el = page.locator(user_sel).first
        el.click()
        page.wait_for_timeout(300)
        el.fill("")  # 先清空
        page.keyboard.type(username, delay=80)
        actual = el.input_value()
        log.info(f"已填写用户名输入框，实际值长度: {len(actual)}")
        filled_user = len(actual) > 0
    except Exception as e:
        log.warning(f"填写用户名失败: {e}")
        filled_user = False

    if not filled_user:
        log.warning("用户名未能填入输入框")
        take_screenshot(page, "05c_login_form_user_notfound")
        return False

    pw_sel = 'input[type="password"]'
    try:
        page.wait_for_selector(pw_sel, state="visible", timeout=5000)
        el_pw = page.locator(pw_sel).first
        el_pw.click()
        page.wait_for_timeout(300)
        el_pw.fill("")
        page.keyboard.type(password, delay=80)
        actual_pw = el_pw.input_value()
        log.info(f"已填写密码输入框，实际值长度: {len(actual_pw)}")
        filled_pw = len(actual_pw) > 0
    except Exception as e:
        log.warning(f"填写密码失败: {e}")
        filled_pw = False

    if not filled_pw:
        log.warning("未找到面板登录密码输入框")
        take_screenshot(page, "05c_login_form_pw_notfound")
        return False

    take_screenshot(page, "05c_login_form_filled")

    # 面板登录页有 reCAPTCHA（invisible/v3），直接点按钮会被静默拒绝
    # 解法：等 grecaptcha 加载完成后，手动执行 execute() 拿 token，
    # 再注入到隐藏的 g-recaptcha-response 字段，最后再提交表单
    RECAPTCHA_SITE_KEY = "6Lc-KlcsAAAAAOeYsd-aO8MZSf5nsNpZSIEt4k0H"
    log.info("等待 reCAPTCHA 加载并获取 token...")
    recaptcha_token = None
    try:
        # 等待 grecaptcha 对象可用（最多15s）
        page.wait_for_function("typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'", timeout=15000)
        # 执行 grecaptcha.execute 获取 token
        recaptcha_token = page.evaluate(f"""
            () => new Promise((resolve, reject) => {{
                grecaptcha.ready(() => {{
                    grecaptcha.execute('{RECAPTCHA_SITE_KEY}', {{action: 'login'}})
                        .then(resolve)
                        .catch(reject);
                }});
            }})
        """)
        log.info(f"✅ reCAPTCHA token 已获取，长度: {len(recaptcha_token) if recaptcha_token else 0}")
    except Exception as e:
        log.warning(f"reCAPTCHA token 获取失败（继续尝试提交）: {e}")

    # 将 token 注入隐藏的 g-recaptcha-response 字段（表单提交时会带上）
    if recaptcha_token:
        try:
            page.evaluate(f"""
                (token) => {{
                    // 注入到所有可能的 recaptcha response 字段
                    ['g-recaptcha-response', 'g-recaptcha-response-100000'].forEach(id => {{
                        let el = document.getElementById(id);
                        if (!el) {{
                            el = document.createElement('textarea');
                            el.name = id;
                            el.id = id;
                            el.style.display = 'none';
                            document.querySelector('form')?.appendChild(el);
                        }}
                        el.value = token;
                    }});
                }}
            """, recaptcha_token)
            log.info("reCAPTCHA token 已注入表单")
        except Exception as e:
            log.warning(f"注入 recaptcha token 失败: {e}")

    submitted = False
    for sel in [
        'button[type="submit"]',
        'button:has-text("Login")',
        'button:has-text("Sign in")',
        'button:has-text("登录")',
        'input[type="submit"]',
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn.click()
                submitted = True
                log.info(f"已提交面板登录表单: {sel}")
                break
        except Exception:
            continue

    if not submitted:
        try:
            page.locator('input[type="password"]').first.press("Enter")
            submitted = True
            log.info("未找到登录按钮，改为在密码框按 Enter 提交")
        except Exception:
            pass

    page.wait_for_timeout(4000)
    take_screenshot(page, "05d_after_panel_login_submit")

    current = page.url.lower()
    ok = "control.optiklink.net" in current and "/auth/login" not in current
    if ok:
        log.info(f"✅ 控制面板登录成功: {page.url}")
    else:
        log.warning(f"⚠️ 控制面板登录仍然失败，当前URL: {page.url}")
    return ok

# ─────────────────────────────────────────────────────────────
# v4.8: 检测服务器状态，OFFLINE 则自动点击 START
# ─────────────────────────────────────────────────────────────
def _read_server_status_js(page) -> str:
    """
    用 JS 精确读取 Pterodactyl 面板的服务器状态指示器文字。
    先滚动到页面底部让状态区域进入视口，再从已知的状态徽标元素里读文字。
    返回大写状态字符串，如 'ONLINE' / 'OFFLINE' / 'STARTING' / 'STOPPING'，
    读不到则返回空字符串。
    """
    # 先滚动到底部，确保状态区域可见（Pterodactyl 面板状态在页面下方）
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(500)
    except Exception:
        pass

    status = page.evaluate("""() => {
        // Pterodactyl / Wings 面板状态徽标的常见 CSS 类名
        const selectors = [
            // 新版 Pterodactyl 状态 pill
            '[class*="StatusIndicator"]',
            '[class*="status-indicator"]',
            '[class*="ServerStatus"]',
            '[class*="server-status"]',
            // 通用：包含状态关键字的 span/div（精确匹配，排除导航/按钮区域）
            'span[class*="text-"][class*="status"]',
            'div[class*="text-"][class*="status"]',
            // 兜底：查找包含状态关键字且可见的小型徽标元素
        ];

        const keywords = ['OFFLINE', 'ONLINE', 'STARTING', 'STOPPING', 'RUNNING'];

        // 方法1：从已知选择器里找
        for (const sel of selectors) {
            for (const el of document.querySelectorAll(sel)) {
                const t = (el.innerText || el.textContent || '').trim().toUpperCase();
                for (const kw of keywords) {
                    if (t === kw || t.startsWith(kw)) return kw;
                }
            }
        }

        // 方法2：遍历所有小型文字元素（span/div/p），精确匹配状态关键字
        // 避免匹配到按钮文字（START/STOP 等）
        for (const el of document.querySelectorAll('span, p, small, [class*="badge"], [class*="pill"], [class*="tag"], [class*="chip"]')) {
            const t = (el.innerText || el.textContent || '').trim().toUpperCase();
            // 精确匹配：文字本身就是状态词，或状态词后接空格
            for (const kw of keywords) {
                if (t === kw) return kw;
            }
        }

        // 方法3：读取页面右上角/顶部状态区域（Pterodactyl 把状态放在 console header 里）
        // 找 class 包含 "Console" 或 "console" 的容器下面的状态文字
        for (const container of document.querySelectorAll('[class*="Console"], [class*="console"], [id*="console"]')) {
            const t = (container.innerText || '').toUpperCase();
            for (const kw of keywords) {
                if (t.includes(kw)) return kw;
            }
        }

        return '';
    }""")
    return (status or "").upper().strip()


def _wait_for_server_status(page, timeout_ms=20000) -> str:
    """
    轮询等待服务器状态从 UNKNOWN 变为已知状态。
    先用 wait_for_selector 等状态关键字出现在 DOM 里，
    再用 JS 精确读取，避免误匹配导航/按钮文字。
    """
    # 先滚动到底部
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)
    except Exception:
        pass

    # 等待状态关键字出现在页面上（最多 timeout_ms）
    try:
        page.wait_for_selector(
            "text=/^(OFFLINE|ONLINE|STARTING|STOPPING|RUNNING)$/i",
            timeout=timeout_ms,
        )
    except Exception:
        # 选择器匹配不到，改用轮询兜底
        pass

    # 轮询读取精确状态（最多再等 10s，每 1s 一次）
    for _ in range(10):
        status = _read_server_status_js(page)
        if status in ("OFFLINE", "ONLINE", "STARTING", "STOPPING", "RUNNING"):
            return status
        page.wait_for_timeout(1000)

    # 最终兜底：从 body 全文里找（可能误匹配，但总比 UNKNOWN 好）
    try:
        body = page.inner_text("body").upper()
        for kw in ("STARTING", "STOPPING", "RUNNING", "ONLINE", "OFFLINE"):
            if kw in body:
                log.warning(f"状态精确读取失败，从 body 全文兜底读到: {kw}（可能误匹配）")
                return kw
    except Exception:
        pass

    return "UNKNOWN"


def check_and_start_server(page, server_id: str) -> dict:
    """
    打开 control.optiklink.net/server/{server_id}，读取运行状态；
    若为 OFFLINE，则滚动到 START 按钮并点击。
    """
    result = {"server_id": server_id, "status": "UNKNOWN", "started": False}
    control_url = f"{CONTROL_BASE_URL}/{server_id}"

    log.info(f"[G] 检测服务器状态: {control_url}")
    try:
        page.goto(control_url, timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        log.warning(f"打开控制面板超时/异常: {e}")

    # 页面加载后先等 React 渲染 + WebSocket 推送状态（至多 20s）
    page.wait_for_timeout(2000)
    close_popups_and_overlays(page)

    # 若被重定向离开了这个服务器的详情页，说明面板会话没建立成功
    if server_id not in page.url:
        log.warning(
            f"服务器 [{server_id}] 页面被重定向到 {page.url}，"
            f"控制面板可能未登录成功，无法读取真实状态"
        )
        result["status"] = "NO_ACCESS"
        take_screenshot(page, f"06_{server_id}_no_access")
        return result

    # 用精确 JS 轮询读取状态，避免误匹配导航/按钮文字
    status = _wait_for_server_status(page, timeout_ms=20000)
    result["status"] = status
    log.info(f"服务器 [{server_id}] 当前状态: {status}")
    take_screenshot(page, f"06_{server_id}_control")

    if result["status"] not in ("OFFLINE",):
        log.info(f"服务器 [{server_id}] 无需启动（当前状态: {result['status']}）")
        return result

    # ── 状态为 OFFLINE，滚动到 START 按钮并点击 ──
    log.info(f"服务器 [{server_id}] 离线，滚动并尝试点击 START 按钮...")

    # 先滚动到底部确保按钮可见
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)
    except Exception:
        pass

    clicked = False
    for sel in [
        'button:has-text("START")',
        'button:has-text("Start")',
        'button:has-text("启动")',
        'button:has-text("start")',
    ]:
        try:
            btn = page.locator(sel).first
            # scroll_into_view_if_needed 确保按钮进入视口
            btn.scroll_into_view_if_needed(timeout=3000)
            page.wait_for_timeout(300)
            if not btn.is_visible(timeout=3000):
                continue
            if btn.is_disabled():
                log.warning(f"START 按钮不可点击（禁用状态）: {sel}")
                continue
            btn.click()
            clicked = True
            log.info(f"已点击 START 按钮: {sel}")
            break
        except Exception:
            continue

    if not clicked:
        log.warning(f"服务器 [{server_id}] 未找到可点击的 START 按钮")
        take_screenshot(page, f"06_{server_id}_start_notfound")
        return result

    result["started"] = True
    # 等待状态从 OFFLINE 变为 STARTING/ONLINE（最多 10s）
    page.wait_for_timeout(2000)
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(500)
    except Exception:
        pass
    take_screenshot(page, f"07_{server_id}_after_start")

    # 再读一次状态确认
    status_after = _wait_for_server_status(page, timeout_ms=8000)
    if status_after != "UNKNOWN":
        log.info(f"服务器 [{server_id}] 点击 START 后状态: {status_after}")

    return result


def check_and_start_all_servers(page) -> list:
    login_control_panel(page)
    results = []
    for sid in SERVER_IDS:
        try:
            results.append(check_and_start_server(page, sid))
        except Exception as e:
            log.error(f"检测服务器 [{sid}] 异常: {e}")
            results.append({"server_id": sid, "status": "ERROR", "started": False})
    return results

# ─────────────────────────────────────────────────────────────
# 构建推送消息
# ─────────────────────────────────────────────────────────────
def build_message(info: dict, server_results: list | None = None) -> tuple[str, str]:
    now_utc = datetime.now(timezone.utc)
    status = "✅ 登录成功" if info["logged_in"] else "❌ 登录失败"

    days_left = -1
    if info.get("expire_date"):
        try:
            expire_dt = datetime.strptime(info["expire_date"], "%d.%m.%Y").replace(tzinfo=timezone.utc)
            days_left = (expire_dt - now_utc).days
        except Exception:
            pass

    if days_left == -1:
        warning = "\n\n> ⚠️ 未能获取到期日期，请手动检查"
        title = f"OptikLink 签到 | {status} | 到期日期未知"
    elif days_left <= 3:
        warning = f"\n\n---\n## 🚨 紧急：服务即将到期！\n\n> **距到期仅剩 {days_left} 天，请立即续期！**"
        title = f"🚨 OptikLink 签到 | 紧急：{days_left}天后到期！"
    elif days_left <= 7:
        warning = f"\n\n---\n## ⚠️ 服务即将到期\n\n> 距到期还剩 **{days_left}** 天，请尽快续期。"
        title = f"⚠️ OptikLink 签到 | 警告：{days_left}天后到期"
    else:
        warning = f"\n\n> 📅 服务到期还剩 **{days_left}** 天" if 0 < days_left <= 30 else ""
        title = f"OptikLink 签到 | {status}"

    server_lines = ""
    if server_results:
        rows = []
        for r in server_results:
            if r["status"] == "OFFLINE" and r["started"]:
                s = "🟢 已自动启动"
            elif r["status"] == "OFFLINE" and not r["started"]:
                s = "🔴 离线，启动失败/未找到按钮"
            elif r["status"] == "ONLINE":
                s = "🟢 在线"
            elif r["status"] in ("STARTING", "STOPPING"):
                s = f"🟡 {r['status']}"
            elif r["status"] == "NO_ACCESS":
                s = "⚠️ 控制面板未登录成功，无法读取状态"
            else:
                s = f"⚪ {r['status']}"
            rows.append(f"| {r['server_id']} | {s} |")
        server_lines = "\n\n### 服务器状态\n\n| 服务器 | 状态 |\n|------|------|\n" + "\n".join(rows)

    content = f"""## OptikLink 每日自动登录报告

| 项目 | 内容 |
|------|------|
| 状态 | {status} |
| 用户名 | {info['username']} |
| 运行服务器 | {info['running_servers']} 个 |
| 服务到期 | {info['expire_date'] or '未知'} |
| 剩余天数 | {days_left if days_left >= 0 else '未知'} 天 |
| 执行时间 | {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC |
{warning}{server_lines}
"""
    return title, content

# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def main():
    log.info("=" * 55)
    log.info("  OptikLink 自动登录脚本 v4.11 (CloakBrowser)")
    log.info("=" * 55)
    log.info(f"  截图: 始终开启  |  录屏: {'开启' if ENABLE_SCREENRECORD else '关闭'}")

    from cloakbrowser import launch, ensure_binary
    ensure_binary()

    log.info("启动 CloakBrowser...")
    # headless=False：录屏依赖 ffmpeg x11grab 从 Xvfb :99 抓屏
    # headless=True 时浏览器不渲染到显示器，录屏文件会是 0 字节黑屏
    browser = launch(
        headless=False,
        humanize=True,
        proxy=PROXY_URL,
        geoip=True,
    )
    page = browser.new_page()
    try:
        page.set_viewport_size({"width": VIEWPORT_W, "height": VIEWPORT_H})
    except Exception:
        pass

    # 录屏：页面创建后开始（v4.6 改用 scrot/import 截 Xvfb 屏幕）
    recorder = start_page_recording(page)

    try:
        success = do_login(page)

        if not success:
            wxpush(
                "OptikLink 签到 ❌ 失败",
                f"## 执行失败\n\n**错误：** 登录流程未完成，请查看日志\n\n"
                f"时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
            )
            sys.exit(1)

        info = read_dashboard(page)

        # v4.8: 登录成功后检测服务器状态，OFFLINE 则自动启动
        server_results = check_and_start_all_servers(page)

        title, content = build_message(info, server_results)
        wxpush(title, content)

        if not info["logged_in"]:
            log.error("Dashboard 未出现登录状态，脚本标记为失败")
            sys.exit(1)

        log.info("✅ 全部完成！")

    except Exception as e:
        import traceback
        log.error(f"未预期异常: {e}")
        traceback.print_exc()
        take_screenshot(page, "99_error")
        wxpush(
            "OptikLink 签到 ❌ 异常",
            f"## 执行异常\n\n```\n{e}\n```\n\n"
            f"时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        )
        sys.exit(1)
    finally:
        # 录屏：无论成功失败都停止并保存
        stop_page_recording(recorder)
        time.sleep(3)
        browser.close()
        log.info("浏览器已关闭")


if __name__ == "__main__":
    main()
