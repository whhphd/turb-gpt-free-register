# -*- coding: utf-8 -*-
"""通过 RoxyBrowser 指纹浏览器 + Selenium 执行 ChatGPT 注册。"""
from __future__ import annotations

import logging
import random
import string
import time
import uuid
from pathlib import Path

from config import roxybrowser as _cfg
from config import twofa as _twofa_cfg
from core.account_export import save_account_data, post_register_dwell
from core.email_provider import wait_for_otp, resolve_email_source, registration_otp_attempts
from core.humanize import delay as human_delay
from core.roxybrowser_client import RoxyBrowserClient, RoxyOpenResult

logger = logging.getLogger(__name__)


def _log_prefix(driver=None) -> str:
    """按当前浏览器实现返回注册日志前缀。

    CloakBrowser 复用 Roxy 的页面操作函数；这些共享函数必须跟随实际 driver
    输出 `[Cloak注册]`，避免 Cloak 流程里混入 `[Roxy注册]` 日志。
    """
    try:
        explicit = str(getattr(driver, "_registration_log_prefix", "") or "").strip()
        if explicit:
            return explicit
        if driver is not None and driver.__class__.__name__ == "CloakSeleniumDriver":
            return "[Cloak注册]"
    except Exception:
        pass
    return "[Roxy注册]"


def _build_driver(opened: RoxyOpenResult):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.remote.webdriver import WebDriver as RemoteWebDriver

    if opened.debugger_address:
        logger.info("[Roxy] Selenium 连接 debuggerAddress=%s", opened.debugger_address)
        options = Options()
        # 页面里长轮询/风控脚本偶尔会让 driver.get 等到超时；eager 只等 DOMContentLoaded。
        options.page_load_strategy = "eager"
        options.add_experimental_option("debuggerAddress", opened.debugger_address)
        driver_path = ""
        try:
            raw_data = opened.raw.get("data") if isinstance(opened.raw, dict) else {}
            if isinstance(raw_data, dict):
                driver_path = str(raw_data.get("driver") or raw_data.get("driverPath") or raw_data.get("driver_path") or "").strip()
        except Exception:
            driver_path = ""
        if driver_path:
            logger.info("[Roxy] 使用 Roxy chromedriver=%s", driver_path)
            driver = webdriver.Chrome(service=Service(executable_path=driver_path), options=options)
        else:
            driver = webdriver.Chrome(options=options)
        _apply_browser_automation_mask(driver)
        return driver

    if opened.webdriver_url:
        logger.info("[Roxy] Selenium 连接 webdriver_url=%s", opened.webdriver_url)
        options = Options()
        options.page_load_strategy = "eager"
        driver = RemoteWebDriver(command_executor=opened.webdriver_url, options=options)
        _apply_browser_automation_mask(driver)
        return driver

    raise RuntimeError("Roxy 未返回可连接的 Selenium 地址")


def _center_browser_window(driver) -> None:
    """把可见的 Roxy 窗口移动到 Windows 主屏工作区中央。"""
    if bool(getattr(_cfg, "ROXY_OPEN_HEADLESS", False)):
        return
    try:
        import platform
        if platform.system().lower() != "windows":
            return
        import ctypes

        class _Rect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        work_area = _Rect()
        if not ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_area), 0):
            raise OSError("无法读取 Windows 工作区")
        size = driver.get_window_size()
        width = max(1, int(size.get("width") or 1))
        height = max(1, int(size.get("height") or 1))
        x = int(work_area.left + max(0, (work_area.right - work_area.left - width) // 2))
        y = int(work_area.top + max(0, (work_area.bottom - work_area.top - height) // 2))
        driver.set_window_position(x, y)
        logger.info("[Roxy] 浏览器窗口已居中：x=%s y=%s width=%s height=%s", x, y, width, height)
    except Exception as exc:
        logger.warning("[Roxy] 浏览器窗口居中失败，继续执行：%s", exc)


def _wait(driver, timeout: int | None = None):
    from selenium.webdriver.support.ui import WebDriverWait
    return WebDriverWait(driver, timeout or int(_cfg.ROXY_SELENIUM_TIMEOUT))


def _safe_get(driver, url: str, *, timeout: int = 45, attempts: int = 2, accept_hosts: tuple[str, ...] = ()) -> None:
    """带容错的页面跳转。

    Roxy/Chrome 150 偶发 `Timed out receiving message from renderer`，实际页面可能已经可用。
    这里超时后先 `window.stop()`，只要当前 URL/DOM 已进入目标页就继续；否则重试一次。
    """
    from selenium.common.exceptions import TimeoutException, WebDriverException

    last_exc: Exception | None = None
    old_timeout = int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)
    hosts = tuple(h.lower() for h in (accept_hosts or ()))
    for attempt in range(1, max(1, attempts) + 1):
        try:
            try:
                driver.set_page_load_timeout(max(10, int(timeout)))
                driver.set_script_timeout(8)
            except Exception:
                pass
            driver.get(url)
            return
        except TimeoutException as exc:
            last_exc = exc
            logger.warning(
                "%s 页面加载超时，尝试停止加载后检查 DOM：url=%s attempt=%s/%s error=%s",
                _log_prefix(driver), url, attempt, attempts, str(exc).splitlines()[0] if str(exc) else "TimeoutException",
            )
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            time.sleep(1.0)
            try:
                current = str(driver.current_url or "").lower()
            except Exception:
                current = ""
            try:
                ready = str(driver.execute_script("return document.readyState || ''") or "")
                has_body = bool(driver.execute_script("return !!document.body"))
            except Exception:
                ready = ""
                has_body = False
            target_ok = any(h in current for h in hosts) if hosts else (url.split("/", 3)[2].lower() in current)
            if target_ok and has_body:
                logger.info(
                    "%s 页面加载虽超时但 DOM 可用，继续流程：current=%s readyState=%s",
                    _log_prefix(driver), current[:180], ready or "-",
                )
                return
            if attempt < attempts:
                try:
                    driver.get("about:blank")
                except Exception:
                    pass
                time.sleep(1.5 * attempt)
                continue
        except WebDriverException as exc:
            last_exc = exc
            if attempt < attempts:
                logger.warning("%s 页面跳转失败，准备重试：url=%s attempt=%s/%s error=%s", _log_prefix(driver), url, attempt, attempts, exc)
                time.sleep(1.5 * attempt)
                continue
            raise
        finally:
            try:
                driver.set_page_load_timeout(old_timeout)
            except Exception:
                pass
    raise last_exc or RuntimeError(f"页面跳转失败: {url}")


def _visible(el) -> bool:
    try:
        return el.is_displayed() and el.is_enabled()
    except Exception:
        return False


def _browser_actions_enabled() -> bool:
    try:
        from config import humanize as _hcfg
        return bool(getattr(_hcfg, "ENABLE_HUMANIZE_BROWSER_ACTIONS", True))
    except Exception:
        return True


def _apply_browser_automation_mask(driver) -> None:
    """连接 Selenium 后尽量降低明显自动化特征；失败不影响主流程。"""
    if not _browser_actions_enabled():
        return
    try:
        script = r"""
        Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => undefined});
        if (!window.chrome) window.chrome = {};
        if (!window.chrome.runtime) window.chrome.runtime = {};
        const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
        if (originalQuery) {
          window.navigator.permissions.query = (parameters) => (
            parameters && parameters.name === 'notifications'
              ? Promise.resolve({ state: Notification.permission })
              : originalQuery(parameters)
          );
        }
        """
        if hasattr(driver, "execute_cdp_cmd"):
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": script})
        try:
            driver.execute_script(script)
        except Exception:
            pass
        logger.info("%s 已注入浏览器自动化特征弱化脚本", _log_prefix(driver))
    except Exception as exc:
        logger.debug("%s 注入自动化特征弱化脚本失败：%s", _log_prefix(driver), exc)


def _is_dom_element_handle(el) -> bool:
    """判断是否为可传给 execute_script 的真实元素句柄（非 dict/None）。

    Cloak/Playwright 若把 DOM 塞进返回 dict，json_value 后会变成普通对象，
    再调 scrollIntoView/click 会报 el.scrollIntoView is not a function。
    """
    if el is None or isinstance(el, (dict, list, tuple, str, int, float, bool)):
        return False
    name = el.__class__.__name__
    if name in ("CloakElement", "WebElement", "ElementHandle"):
        return True
    # Selenium remote webelement 等
    if hasattr(el, "tag_name") and hasattr(el, "id"):
        return True
    if hasattr(el, "locator") or hasattr(el, "handle"):
        return True
    return False


def _human_scroll_to(driver, el) -> None:
    if not _is_dom_element_handle(el):
        return
    try:
        block = random.choice(["center", "nearest", "center"])
        # 必须守卫 typeof scrollIntoView，避免 Cloak 传坏句柄时整单炸死
        driver.execute_script(
            r"""
            const el = arguments[0];
            const block = arguments[1] || 'center';
            if (!el || typeof el.scrollIntoView !== 'function') return false;
            try { el.scrollIntoView({block: block, inline: 'nearest'}); } catch (e) { return false; }
            return true;
            """,
            el,
            block,
        )
        if _browser_actions_enabled():
            time.sleep(random.uniform(0.08, 0.35))
            # 轻微滚动抖动，避免每次都精准居中。
            driver.execute_script("window.scrollBy(0, arguments[0]);", random.randint(-90, 90))
            time.sleep(random.uniform(0.05, 0.22))
            driver.execute_script(
                r"""
                const el = arguments[0];
                if (!el || typeof el.scrollIntoView !== 'function') return false;
                try { el.scrollIntoView({block:'center', inline:'nearest'}); } catch (e) { return false; }
                return true;
                """,
                el,
            )
    except Exception:
        try:
            driver.execute_script(
                r"""
                const el = arguments[0];
                if (!el || typeof el.scrollIntoView !== 'function') return false;
                try { el.scrollIntoView({block:'center'}); } catch (e) { return false; }
                return true;
                """,
                el,
            )
        except Exception:
            pass


def _human_click(driver, el, *, label: str = "") -> None:
    """快速人工化点击。

    之前用 ActionChains 在 Roxy/Chrome 150 上偶发卡住 1-2 分钟，导致邮箱提交很慢。
    这里改为 CDP 派发鼠标事件；没有 CDP 时再用 JS/原生 click 兜底。
    """
    if not _is_dom_element_handle(el):
        raise RuntimeError(
            f"human_click 收到非 DOM 元素句柄 label={label or '-'} type={type(el).__name__}；"
            f"Cloak 路径请在 JS 内完成 click，不要把 Element 塞进 dict 回传"
        )
    _human_scroll_to(driver, el)
    if not _browser_actions_enabled():
        time.sleep(0.2)
        el.click()
        return
    try:
        human_delay("click")
        point = driver.execute_script(r"""
        const el = arguments[0];
        if (!el || typeof el.getBoundingClientRect !== 'function') return {};
        const r = el.getBoundingClientRect();
        const x = r.left + r.width * (0.30 + Math.random() * 0.40);
        const y = r.top + r.height * (0.35 + Math.random() * 0.30);
        return {x, y, w:r.width, h:r.height};
        """, el) or {}
        x = float(point.get("x") or 0)
        y = float(point.get("y") or 0)
        if hasattr(driver, "execute_cdp_cmd") and x > 0 and y > 0:
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            time.sleep(random.uniform(0.05, 0.22))
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            time.sleep(random.uniform(0.035, 0.13))
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        else:
            driver.execute_script(r"""
            const el = arguments[0];
            if (!el) return;
            el.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true, cancelable:true, pointerType:'mouse'}));
            el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
            el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
            if (typeof el.click === 'function') el.click();
            """, el)
    except Exception as exc:
        logger.debug("%s 人工化点击失败，回退 el.click label=%s err=%s", _log_prefix(driver), label, exc)
        time.sleep(random.uniform(0.12, 0.45))
        try:
            driver.execute_script(
                "const el=arguments[0]; if (el && typeof el.click==='function') el.click();",
                el,
            )
        except Exception:
            el.click()


def _human_type_text(driver, el, value: str, *, clear: bool = True) -> None:
    """按字符/小段输入，触发真实 key events；失败时回退 JS setter。"""
    text = str(value)

    # Cloak/Playwright 适配层：逐字 + 反复 click 容易把光标点到中间导致乱码。
    # 邮箱/密码等受控输入框用一次性 fill 更稳。
    if driver.__class__.__name__ == "CloakSeleniumDriver":
        try:
            _human_scroll_to(driver, el)
            try:
                _human_click(driver, el, label="input_focus")
            except Exception:
                try:
                    driver.execute_script("arguments[0].focus();", el)
                except Exception:
                    pass
            if hasattr(el, "fill_text"):
                el.fill_text(text)
            else:
                if clear:
                    try:
                        el.clear()
                    except Exception:
                        pass
                el.send_keys(text)
            driver.execute_script(
                "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                el,
            )
            return
        except Exception as exc:
            logger.debug("%s Cloak 一次性填入失败，回退 JS setter err=%s", _log_prefix(driver), exc)
            _set_element_value(driver, el, text)
            return

    if not _browser_actions_enabled():
        if clear:
            try:
                el.clear()
            except Exception:
                pass
        el.send_keys(text)
        return
    try:
        _human_scroll_to(driver, el)
        try:
            _human_click(driver, el, label="input_focus")
        except Exception:
            driver.execute_script("arguments[0].focus();", el)
        if clear:
            from selenium.webdriver.common.keys import Keys
            mod = Keys.COMMAND
            try:
                import platform
                if platform.system().lower() != "darwin":
                    mod = Keys.CONTROL
            except Exception:
                pass
            try:
                el.send_keys(mod, "a")
                time.sleep(random.uniform(0.04, 0.16))
                el.send_keys(Keys.BACKSPACE)
            except Exception:
                try:
                    el.clear()
                except Exception:
                    pass
        i = 0
        while i < len(text):
            # 邮箱/密码整体仍逐字符，但偶尔 2 字符一组，节奏更自然。
            step = 2 if random.random() < 0.12 and i + 1 < len(text) else 1
            el.send_keys(text[i:i + step])
            i += step
            human_delay("keystroke")
            if i < len(text) and random.random() < 0.08:
                human_delay("typing_pause")
        driver.execute_script(
            "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
            el,
        )
    except Exception as exc:
        logger.debug("%s 人工化输入失败，回退 JS setter err=%s", _log_prefix(driver), exc)
        _set_element_value(driver, el, text)


def _page_warmup(driver, *, reason: str = "") -> None:
    if not _browser_actions_enabled():
        return
    try:
        human_delay("page_warmup")
        if hasattr(driver, "execute_cdp_cmd"):
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": random.randint(80, 360),
                "y": random.randint(80, 260),
            })
    except Exception:
        pass


def _find_any(driver, selectors: list[str], timeout: int | None = None):
    from selenium.webdriver.common.by import By

    end = time.time() + (timeout or int(_cfg.ROXY_SELENIUM_TIMEOUT))
    last = None
    while time.time() < end:
        for selector in selectors:
            try:
                by = By.XPATH if selector.startswith("//") else By.CSS_SELECTOR
                items = driver.find_elements(by, selector)
                for item in items:
                    if _visible(item):
                        return item
            except Exception as exc:
                last = exc
        time.sleep(0.4)
    raise RuntimeError(f"找不到页面元素: {selectors}; last={last}")


def _click_any(driver, selectors: list[str], timeout: int | None = None) -> None:
    el = _find_any(driver, selectors, timeout)
    _human_click(driver, el, label="click_any")


def _type_any(driver, selectors: list[str], value: str, timeout: int | None = None, clear: bool = True) -> None:
    el = _find_any(driver, selectors, timeout)
    _human_type_text(driver, el, value, clear=clear)


_EMAIL_INPUT_SELECTORS = [
    "input[type='email']",
    "input[name='email']",
    "input[name='username']",
    "input#email-input",
    "input[autocomplete='email']",
]


def _email_entry_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled;
        const attrText = el => [
          el.id, el.getAttribute('name'), el.getAttribute('type'), el.getAttribute('autocomplete'),
          el.getAttribute('data-testid'), el.getAttribute('data-test-id'), el.getAttribute('data-provider'),
          el.getAttribute('data-auth-provider'), el.getAttribute('href'), el.getAttribute('action'),
          el.getAttribute('formaction'), el.getAttribute('value')
        ].filter(Boolean).join(' ').toLowerCase();
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', value: el.value || ''
        })).slice(0, 30);
        const actions = [...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')]
          .filter(visible).map(el => ({tag: el.tagName, type: el.getAttribute('type') || '', attrs: attrText(el)})).slice(0, 40);
        return {url: location.href, title: document.title, inputs, actions};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _find_visible_email_input_js(driver):
    return driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && !el.readOnly;
    const selectors = [
      'input[type="email"]',
      'input[name="email"]',
      'input[name="username"]',
      'input[name="loginfmt"]',
      'input[name="identifier"]',
      'input#email-input',
      'input#username',
      'input[autocomplete="email"]',
      'input[autocomplete="username"]',
      'input[inputmode="email"]'
    ];
    for (const sel of selectors) {
      const el = [...document.querySelectorAll(sel)].find(visible);
      if (el) return el;
    }
    // 兜底：按 placeholder / aria-label / name 打分选邮箱框
    const inputs = [...document.querySelectorAll('input')].filter(visible);
    const score = el => {
      const hay = [el.type, el.name, el.id, el.autocomplete, el.placeholder,
        el.getAttribute('aria-label'), el.getAttribute('data-testid')].join(' ').toLowerCase();
      if (/(email|mail|username|loginfmt|identifier|メール|邮箱|電子郵件)/i.test(hay)) return 100;
      if (!el.type || ['text','email','search'].includes(String(el.type||'').toLowerCase())) return 10;
      return 0;
    };
    let best = null, bestScore = 0;
    for (const el of inputs) {
      const s = score(el);
      if (s > bestScore) { best = el; bestScore = s; }
    }
    return bestScore >= 100 ? best : null;
    """)


def _is_oauth_consent_like(driver) -> bool:
    """检测是否已到 OAuth 授权/consent 页。这里不能再点任何邮箱分支或全局提交按钮。

    注意：`/oauth/authorize` 是登录入口，不是 consent。
    真正 consent 类似 `/sign-in-with-chatgpt/codex/consent`。
    旧逻辑把含 authorize 的 URL 一律当 consent，会导致无法点 Continue with email。
    """
    try:
        return bool(driver.execute_script(r"""
        const url = String(location.href || '').toLowerCase();
        // 登录/标识/验证码/绑手机：明确不是 consent
        if (/\/oauth\/authorize\b|\/log-in\b|\/login\b|\/signup\b|\/create-account\b|identifier|email-verification|add-phone|phone-verification|password/.test(url)) {
          return false;
        }
        // 真正 consent / workspace
        if (/\/consent\b|sign-in-with-chatgpt\/codex\/consent|\/workspace\b|about-you|select-workspace/.test(url)) {
          return true;
        }
        const formsWithEmail = [...document.querySelectorAll('form')]
          .some(form => form.querySelector('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]'));
        if (formsWithEmail) return false;
        // 有可见邮箱输入框也不是 consent
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        if ([...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]')].some(visible)) {
          return false;
        }
        const bodyText = String(document.body && document.body.innerText || '').toLowerCase();
        // consent 常见文案：Allow / Authorize / Grant access，且无 email 输入
        if (/(allow access|grant access|authorize this|allow this app|允许访问|授权)/i.test(bodyText)
            && !/(continue with email|sign in with email|email address|邮箱|メール)/i.test(bodyText)) {
          return true;
        }
        return false;
        """))
    except Exception:
        return False


def _is_external_idp_url(url: str) -> bool:
    u = str(url or '').lower()
    return any(x in u for x in (
        'accounts.google.', 'google.com/o/oauth', 'appleid.apple.', 'login.microsoftonline.',
        'login.live.', 'github.com/login/oauth', 'facebook.com/', 'saml', 'sso'
    ))


def _assert_not_external_idp(driver, label: str = '') -> None:
    try:
        current = str(driver.current_url or '')
    except Exception:
        current = ''
    if _is_external_idp_url(current):
        raise RuntimeError(f"误入第三方账号授权页（{label}）：{current}")


def _click_email_entry_option(driver) -> bool:
    """点击“邮箱方式”入口。

    优先看 data-provider/testid 等技术属性；若无结果再看可见文案
    （Continue with email / メールで続行 等），并显式排除 Google/Apple 等第三方。
    """
    if _is_oauth_consent_like(driver):
        logger.info("%s 当前疑似 OAuth 授权页，跳过邮箱入口兜底点击", _log_prefix(driver))
        return False
    target = driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const attrText = el => {
      const own = [
        el.id, el.getAttribute('name'), el.getAttribute('type'), el.getAttribute('autocomplete'),
        el.getAttribute('data-testid'), el.getAttribute('data-test-id'), el.getAttribute('data-provider'),
        el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'), el.getAttribute('href'), el.getAttribute('action'),
        el.getAttribute('formaction'), el.getAttribute('value'), el.getAttribute('aria-label'), el.className
      ].filter(Boolean).join(' ');
      const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
        .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
          x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
          .filter(Boolean).join(' ')).join(' ');
      return `${own} ${desc}`.toLowerCase();
    };
    const textOf = el => String(el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    // 第三方坏词：不要用 oauth/provider/authorize 这类太宽泛词，会误伤邮箱按钮
    const badAttr = /google|apple|microsoft|github|facebook|saml|\bsso\b|social|oidc|\bidp\b/;
    const badText = /google|apple|microsoft|github|facebook|继续使用 google|sign in with google|continue with google|continue with apple|continue with microsoft/;
    const goodAttr = /(^|[^a-z])(email|mail|username|passwordless|otp|magic)([^a-z]|$)/;
    const goodText = /continue with email|sign up with email|log in with email|sign in with email|use email|email address|メールで続行|メールアドレス|使用邮箱|使用電子郵件|邮箱登录|電子郵件/;

    const nodes = [...document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"]')].filter(visible);
    // 1) 技术属性命中
    const byAttr = nodes
      .map(el => ({el, attrs: attrText(el), text: textOf(el).toLowerCase(), hasLogo: !!el.querySelector('img,svg,use')}))
      .filter(x => goodAttr.test(x.attrs) && !badAttr.test(x.attrs) && !badText.test(x.text));
    if (byAttr.length === 1) {
      byAttr[0].el.scrollIntoView({block:'center'});
      return byAttr[0].el;
    }
    // 2) 可见文案命中（Codex/OpenAI 登录页常见）
    const byText = nodes
      .map(el => ({el, attrs: attrText(el), text: textOf(el).toLowerCase()}))
      .filter(x => goodText.test(x.text) && !badAttr.test(x.attrs) && !badText.test(x.text));
    if (byText.length >= 1) {
      // 优先更长、更明确的 “continue with email”
      byText.sort((a,b) => (b.text.includes('continue with email')?2:0) - (a.text.includes('continue with email')?2:0) || b.text.length - a.text.length);
      byText[0].el.scrollIntoView({block:'center'});
      return byText[0].el;
    }
    // 3) 属性多候选时取第一个较干净的
    if (byAttr.length > 1) {
      byAttr[0].el.scrollIntoView({block:'center'});
      return byAttr[0].el;
    }
    return null;
    """)
    if target:
        _human_click(driver, target, label="email_entry")
        return True
    return False


def _type_email_address(driver, email: str, timeout: int | None = None) -> None:
    """进入邮箱登录/注册方式并填写邮箱。全程不依赖页面可见文字，避免非日本出口本地化后误点 Google。"""
    end = time.time() + (timeout or int(_cfg.ROXY_SELENIUM_TIMEOUT))
    last_state = None
    clicked_email_option = False
    while time.time() < end:
        el = _find_visible_email_input_js(driver)
        if el:
            _human_type_text(driver, el, email, clear=True)
            return
        last_state = _email_entry_state(driver)
        if not clicked_email_option and _click_email_entry_option(driver):
            clicked_email_option = True
            time.sleep(1.0)
            _assert_not_external_idp(driver, "点击邮箱入口后")
            continue
        time.sleep(0.4)
    raise RuntimeError(f"找不到邮箱输入框/邮箱入口（未使用文字识别），state={last_state}")


def _submit_nearest_form_for_active_input(driver) -> bool:
    if _is_oauth_consent_like(driver):
        logger.info("%s 当前疑似 OAuth 授权页，禁止执行邮箱提交", _log_prefix(driver))
        return False
    result = driver.execute_script(r"""
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete="email"]')]
      .find(visible);
    if (!input) return {ok:false, reason:'missing_email_input'};
    const value = String(input.value || '').trim();
    if (!value || !value.includes('@')) return {ok:false, reason:'email_value_not_ready', value};
    const form = input.closest('form');
    if (!form) return {ok:false, reason:'missing_form'};

    const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|sso|saml|idp|provider|authorize|consent|grant|allow/;
    const attrText = el => {
      const own = [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
        el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'),
        el.getAttribute('aria-label'), el.getAttribute('href'), el.getAttribute('formaction'), el.value, el.className]
        .filter(Boolean).join(' ');
      const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
        .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
          x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
          .filter(Boolean).join(' '))
        .join(' ');
      return `${own} ${desc}`.toLowerCase();
    };
    const inputRect = input.getBoundingClientRect();
    const formId = form.getAttribute('id') || '';
    const scopedButtons = [
      ...form.querySelectorAll('button,input[type="submit"]'),
      ...(formId ? [...document.querySelectorAll(`button[form="${CSS.escape(formId)}"],input[type="submit"][form="${CSS.escape(formId)}"]`)] : [])
    ].filter((el, idx, arr) => arr.indexOf(el) === idx);
    const rawButtons = scopedButtons
      .filter(visible)
      .map((el, idx) => {
        const r = el.getBoundingClientRect();
        const attrs = attrText(el);
        const hasLogo = !!el.querySelector('img,svg,use');
        const isBad = bad.test(attrs) || hasLogo;
        const belowInput = r.top >= inputRect.bottom - 10;
        const distance = Math.max(0, r.top - inputRect.bottom) + Math.abs((r.left + r.right) / 2 - (inputRect.left + inputRect.right) / 2) / 10;
        const cls = String(el.className || '').toLowerCase();
        const type = String(el.getAttribute('type') || '').toLowerCase();
        // ChatGPT 新版邮箱页的主按钮形如：
        // <button class="... btn-primary ... w-full ..." type="submit"><div>続行</div></button>
        // 优先选择同 form 下的 primary submit，而不是因为多个按钮距离接近误判歧义。
        const isPrimarySubmit = (el.tagName === 'BUTTON' || el.tagName === 'INPUT') && type === 'submit'
          && (/\bbtn-primary\b/.test(cls) || /\b_primary_/.test(cls) || /\bw-full\b/.test(cls));
        const score = (isPrimarySubmit ? 1000 : 0) + (type === 'submit' ? 100 : 0) - distance;
        return {el, idx, attrs, isBad, hasLogo, belowInput, distance, score, isPrimarySubmit, tag: el.tagName, type};
      });
    const safe = rawButtons.filter(x => !x.isBad && x.belowInput)
      .sort((a,b) => b.score - a.score || a.distance - b.distance || a.idx - b.idx);
    if (!safe.length) {
      return {ok:false, reason:'no_safe_submit', buttons: rawButtons.map(x => ({idx:x.idx, isBad:x.isBad, hasLogo:x.hasLogo, belowInput:x.belowInput, primary:x.isPrimarySubmit, attrs:x.attrs.slice(0,160), type:x.type}))};
    }
    // 多个安全按钮时，若没有明确 primary submit，且距离接近，才认为页面歧义。
    if (!safe[0].isPrimarySubmit && safe.length > 1 && Math.abs(safe[0].distance - safe[1].distance) < 8) {
      return {ok:false, reason:'ambiguous_submit', buttons: safe.slice(0,3).map(x => ({idx:x.idx, distance:x.distance, score:x.score, primary:x.isPrimarySubmit, attrs:x.attrs.slice(0,160), type:x.type}))};
    }
    const target = safe[0].el;
    target.scrollIntoView({block:'center'});
    window.__roxy_email_submit_debug = {at: Date.now(), targetAttrs: safe[0].attrs.slice(0,240), buttonCount: rawButtons.length, primary:safe[0].isPrimarySubmit};
    return {ok:true, reason:safe[0].isPrimarySubmit ? 'primary_submit' : 'safe_submit', target, targetAttrs:safe[0].attrs.slice(0,160), primary:safe[0].isPrimarySubmit};
    """) or {}
    if result.get("ok"):
        target = result.get("target")
        if target:
            _human_click(driver, target, label="email_submit")
        else:
            logger.warning("%s 邮箱提交未返回目标元素，回退 requestSubmit", _log_prefix(driver))
            driver.execute_script("document.querySelector('form')?.requestSubmit?.();")
        logger.info("%s 邮箱表单安全提交：%s", _log_prefix(driver), result)
        time.sleep(0.8)
        _assert_not_external_idp(driver, "提交邮箱后")
        return True
    logger.warning("%s 未执行邮箱提交：%s", _log_prefix(driver), result)
    return False


def _current_email_input_value(driver) -> str:
    try:
        state = _email_input_value_state(driver)
        for item in state.get("inputs") or []:
            value = str(item.get("value") or "").strip()
            if "@" in value:
                return value
    except Exception:
        pass
    return ""


def _stabilize_email_input_before_submit(driver, email: str) -> dict:
    """提交前把 DOM value / React 受控状态 / blur-change 状态统一稳定下来。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_email_input'};

        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        input.scrollIntoView({block:'center', inline:'nearest'});
        input.focus();
        if (setter) setter.call(input, email); else input.value = email;

        // 让 React/表单校验尽量收到完整输入链路。
        try { input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'insertText', data:email})); } catch (_) {}
        try { input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email})); } catch (_) {
          input.dispatchEvent(new Event('input', {bubbles:true}));
        }
        input.dispatchEvent(new Event('change', {bubbles:true}));
        input.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
        input.blur();
        input.focus();

        const form = input.closest('form');
        const submit = form?.querySelector('button[type="submit"],input[type="submit"]');
        return {
          ok:true,
          value: input.value,
          active: document.activeElement === input,
          hasForm: !!form,
          hasSubmit: !!submit,
          submitDisabled: submit ? (!!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true') : null,
          url: location.href
        };
        """, email) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_form_stable(driver, email: str) -> dict:
    """第一次提交就按“补交成功”的方式执行：稳定 value 后 Enter + DOM click。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
        const editable = el => visible(el) && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(editable);
        if (!input) return {ok:false, reason:'missing_email_input'};
        if (!email || !email.includes('@')) return {ok:false, reason:'empty_email', value: email};

        const form = input.closest('form');
        if (!form) return {ok:false, reason:'missing_form'};

        const bad = /google|apple|microsoft|github|facebook|saml|sso|oauth|social|oidc|idp|provider|authorize|consent|grant|allow/;
        const attrText = el => {
          const own = [el.id, el.name, el.type, el.getAttribute('data-testid'), el.getAttribute('data-test-id'),
            el.getAttribute('data-provider'), el.getAttribute('data-auth-provider'), el.getAttribute('data-idp'),
            el.getAttribute('aria-label'), el.getAttribute('href'), el.getAttribute('formaction'), el.value, el.className]
            .filter(Boolean).join(' ');
          const desc = [...el.querySelectorAll('img,svg,use,[aria-label],[data-provider],[data-testid],[data-test-id]')]
            .map(x => [x.getAttribute('alt'), x.getAttribute('src'), x.getAttribute('href'), x.getAttribute('xlink:href'),
              x.getAttribute('aria-label'), x.getAttribute('data-provider'), x.getAttribute('data-testid'), x.getAttribute('data-test-id'), x.className]
              .filter(Boolean).join(' '))
            .join(' ');
          return `${own} ${desc}`.toLowerCase();
        };

        const formId = form.getAttribute('id') || '';
        const buttons = [
          ...form.querySelectorAll('button,input[type="submit"]'),
          ...(formId ? [...document.querySelectorAll(`button[form="${CSS.escape(formId)}"],input[type="submit"][form="${CSS.escape(formId)}"]`)] : [])
        ].filter((el, idx, arr) => arr.indexOf(el) === idx)
          .filter(el => visible(el) && !bad.test(attrText(el)) && !el.querySelector('img,svg,use'));
        const submit = buttons.find(el => (el.getAttribute('type') || '').toLowerCase() === 'submit') || buttons[0] || null;
        if (!submit) return {ok:false, reason:'missing_safe_submit'};

        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        input.scrollIntoView({block:'center', inline:'nearest'});
        input.focus();
        if (setter) setter.call(input, email); else input.value = email;
        try { input.dispatchEvent(new InputEvent('beforeinput', {bubbles:true, cancelable:true, inputType:'insertText', data:email})); } catch (_) {}
        try { input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email})); } catch (_) {
          input.dispatchEvent(new Event('input', {bubbles:true}));
        }
        input.dispatchEvent(new Event('change', {bubbles:true}));
        input.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
        input.blur();
        input.focus();

        submit.scrollIntoView({block:'center', inline:'nearest'});

        // 不要在 execute_script 同步执行 submit.click()：
        // ChromeDriver 会等前端 submit/navigation，Roxy/Chrome 150 上可能卡到 page/script timeout。
        // setTimeout 让 Selenium 先返回，点击在页面事件循环里异步发生，和补交逻辑一致。
        setTimeout(() => {
          try {
            input.focus();
            input.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            input.dispatchEvent(new KeyboardEvent('keypress', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            input.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            if (submit && !submit.disabled) submit.click();
            else if (form && typeof form.requestSubmit === 'function') form.requestSubmit();
          } catch (_) {}
        }, 80);

        window.__roxy_email_submit_debug = {
          at: Date.now(),
          mode: 'stable_async_enter_click',
          value: input.value,
          submitAttrs: attrText(submit).slice(0, 240)
        };
        return {
          ok:true,
          reason:'stable_async_enter_click',
          value: input.value,
          submitDisabled: !!submit.disabled || String(submit.getAttribute('aria-disabled') || '').toLowerCase() === 'true',
          submitAttrs: attrText(submit).slice(0, 180),
          url: location.href
        };
        """, email) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_step(driver, email: str | None = None) -> None:
    # 不再优先走浏览器内 NextAuth fetch：
    # Roxy/Chrome 150 下 execute_async_script + fetch 偶发卡到 script timeout；
    # 实测 UI 首次提交后若停在 /auth/login?email=...，由 _recover_email_submit_if_stuck 补交表单更稳定。
    email_value = str(email or _current_email_input_value(driver) or "").strip()
    stable = _stabilize_email_input_before_submit(driver, email_value)
    logger.info("%s 邮箱提交前状态稳定：%s", _log_prefix(driver), stable)
    time.sleep(random.uniform(0.8, 1.8) if _browser_actions_enabled() else 0.4)

    stable_submit = _submit_email_form_stable(driver, email_value)
    if stable_submit.get("ok"):
        logger.info("%s 邮箱稳定表单提交：%s", _log_prefix(driver), stable_submit)
        time.sleep(1.0)
        _assert_not_external_idp(driver, "稳定表单提交邮箱后")
        return
    logger.warning("%s 邮箱稳定表单提交失败，回退 UI 点击提交：%s", _log_prefix(driver), stable_submit)
    if _submit_nearest_form_for_active_input(driver):
        return
    raise RuntimeError(f"无法提交邮箱步骤（拒绝按页面文字或首个 submit 兜底，避免误点第三方登录），state={_email_entry_state(driver)}")


def _recover_email_submit_if_stuck(driver, email: str) -> dict:
    """邮箱提交后停在 /auth/login?email= 且输入框被清空时，补一次原生表单提交。"""
    try:
        return driver.execute_script(r"""
        const email = String(arguments[0] || '').trim();
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const input = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_email_input'};
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        input.focus();
        if (setter) setter.call(input, email); else input.value = email;
        input.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:email}));
        input.dispatchEvent(new Event('change', {bubbles:true}));
        const form = input.closest('form');
        const submit = form?.querySelector('button[type="submit"],input[type="submit"]');
        setTimeout(() => {
          try {
            input.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            input.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, cancelable:true, key:'Enter', code:'Enter'}));
            if (submit && !submit.disabled) submit.click();
            else if (form && typeof form.requestSubmit === 'function') form.requestSubmit();
          } catch (_) {}
        }, 80);
        return {ok:true, reason:'resubmitted_email_form', value: input.value, hasForm: !!form, hasSubmit: !!submit};
        """, email) or {}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _submit_email_via_browser_nextauth(driver, email: str) -> dict:
    """在 Roxy 浏览器上下文里调用 ChatGPT NextAuth signin。

    UI submit 在 Roxy/Chrome 150 上会偶发只跳到 `/auth/login?email=...` 后停住。
    这里改走浏览器页面内 fetch，仍使用当前 Roxy 浏览器的 cookie / 指纹环境，
    拿到 auth.openai.com authorize URL 后让浏览器跳转。
    """
    try:
        current = str(getattr(driver, "current_url", "") or "")
        if "chatgpt.com" not in current:
            return {"ok": False, "reason": "not_on_chatgpt", "url": current[:180]}
    except Exception:
        current = ""

    did = str(uuid.uuid4())
    auth_log_id = str(uuid.uuid4())
    old_script_timeout = int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)
    try:
        try:
            driver.set_script_timeout(25)
        except Exception:
            pass
        result = driver.execute_async_script(r"""
        const email = String(arguments[0] || '').trim();
        const did = String(arguments[1] || '');
        const authLogId = String(arguments[2] || '');
        const done = arguments[arguments.length - 1];
        (async () => {
          try {
            const csrfResp = await fetch('/api/auth/csrf', {
              method: 'GET',
              credentials: 'include',
              headers: {
                'accept': 'application/json',
                'cache-control': 'no-cache',
                'pragma': 'no-cache'
              }
            });
            const csrfText = await csrfResp.text();
            let csrfData = {};
            try { csrfData = JSON.parse(csrfText); } catch (_) {}
            const csrfToken = csrfData.csrfToken || '';
            if (!csrfResp.ok || !csrfToken) {
              done({ok:false, stage:'csrf', status:csrfResp.status, body:csrfText.slice(0, 500)});
              return;
            }

            const q = new URLSearchParams({
              prompt: 'login',
              'ext-oai-did': did,
              auth_session_logging_id: authLogId,
              'ext-passkey-client-capabilities': '11111',
              screen_hint: 'login_or_signup',
              login_hint: email
            });
            const body = new URLSearchParams({
              callbackUrl: 'https://chatgpt.com/',
              csrfToken,
              json: 'true'
            });
            const resp = await fetch('/api/auth/signin/openai?' + q.toString(), {
              method: 'POST',
              credentials: 'include',
              headers: {
                'accept': 'application/json',
                'content-type': 'application/x-www-form-urlencoded',
                'cache-control': 'no-cache',
                'pragma': 'no-cache'
              },
              body: body.toString()
            });
            const text = await resp.text();
            let data = {};
            try { data = JSON.parse(text); } catch (_) {}
            let url = data.url || '';
            if (!resp.ok || !url) {
              done({ok:false, stage:'signin', status:resp.status, body:text.slice(0, 700)});
              return;
            }

            try {
              const u = new URL(url, location.href);
              if (!u.searchParams.get('screen_hint')) u.searchParams.set('screen_hint', 'login_or_signup');
              if (!u.searchParams.get('login_hint')) u.searchParams.set('login_hint', email);
              if (!u.searchParams.get('ext-oai-did')) u.searchParams.set('ext-oai-did', did);
              if (!u.searchParams.get('auth_session_logging_id')) u.searchParams.set('auth_session_logging_id', authLogId);
              url = u.toString();
            } catch (_) {}
            window.location.assign(url);
            done({ok:true, stage:'redirect', url:url.slice(0, 260)});
          } catch (e) {
            done({ok:false, stage:'exception', error:String(e && (e.stack || e.message) || e).slice(0, 700)});
          }
        })();
        """, email, did, auth_log_id) or {}
        return result if isinstance(result, dict) else {"ok": False, "reason": "invalid_result", "result": str(result)[:300]}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            driver.set_script_timeout(old_script_timeout)
        except Exception:
            pass


def _email_input_value_state(driver) -> dict:
    """读取当前可见邮箱框状态，用于提交后确认是否真的进入下一步。"""
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const inputs = [...document.querySelectorAll('input[type="email"],input[name="email"],input[name="username"],input[autocomplete*="email"]')]
          .filter(visible)
          .map(el => ({type: el.getAttribute('type') || '', name: el.name || '', id: el.id || '', autocomplete: el.getAttribute('autocomplete') || '', value: el.value || ''}));
        return {url: location.href, inputs};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _is_email_login_page_still_present(driver) -> bool:
    state = _email_input_value_state(driver)
    return bool(state.get("inputs"))


def _inspect_auth_page_failure(driver) -> dict:
    """识别限流 / 废号 / 普通 auth 错误页。

    返回：
      {
        kind: rate_limit | account_unusable | auth_error | "",
        error_code: str,
        url: str,
        title: str,
        text: str,
      }
    """
    url = ""
    title = ""
    text = ""
    try:
        url = str(getattr(driver, "current_url", "") or "")
    except Exception:
        url = ""
    try:
        snap = driver.execute_script(r"""
        return {
          url: String(location.href || ''),
          title: String(document.title || ''),
          text: String(document.body?.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 1600),
        };
        """) or {}
        url = str(snap.get("url") or url or "")
        title = str(snap.get("title") or "")
        text = str(snap.get("text") or "")
    except Exception:
        pass

    blob = " ".join([url, title, text])
    try:
        from core.openai_auth import (
            detect_account_unusable_text,
            detect_auth_failure_from_url,
            detect_rate_limit_text,
        )
    except Exception:
        detect_account_unusable_text = lambda _t: ""  # type: ignore
        detect_auth_failure_from_url = lambda _u: ("", "")  # type: ignore
        detect_rate_limit_text = lambda _t: ""  # type: ignore

    kind, code = detect_auth_failure_from_url(url)
    if not kind:
        rl = detect_rate_limit_text(blob)
        if rl:
            kind, code = "rate_limit", rl
    if not kind:
        dead = detect_account_unusable_text(blob)
        if dead:
            kind, code = "account_unusable", dead
    if not kind:
        # chatgpt.com/auth/error Oops 页（非 payload）
        low = blob.lower()
        if "/auth/error" in low or "error=undefined" in low or "oops" in low:
            kind, code = "auth_error", "auth_error"
    return {
        "kind": kind or "",
        "error_code": code or "",
        "url": url,
        "title": title,
        "text": text[:400],
    }


def _raise_if_auth_page_terminal(driver, *, where: str = "") -> None:
    """若当前页是限流/废号，抛出带 error_code 的明确异常。"""
    info = _inspect_auth_page_failure(driver)
    kind = info.get("kind") or ""
    code = info.get("error_code") or ""
    url = str(info.get("url") or "")[:220]
    if kind == "rate_limit":
        from core.openai_auth import RateLimitError

        raise RateLimitError(
            f"OpenAI Auth 限流 error_code={code or 'rate_limit_exceeded'} where={where or '-'} url={url}",
            error_code=code or "rate_limit_exceeded",
        )
    if kind == "account_unusable":
        from core.openai_auth import AccountUnusableError

        raise AccountUnusableError(
            f"账号已废 error_code={code or 'account_deactivated'} where={where or '-'} url={url} text={(info.get('text') or '')[:160]}",
            error_code=code or "account_deactivated",
        )


def _auth_error_page_state(driver) -> dict:
    """识别 ChatGPT 登录/注册 Oops 错误页（/auth/error?error=undefined）。"""
    # 先解析 payload 限流/废号，避免被普通 Oops 恢复逻辑吞掉。
    try:
        info = _inspect_auth_page_failure(driver)
        if info.get("kind") in ("rate_limit", "account_unusable"):
            return {
                "ok": True,
                "url": info.get("url") or "",
                "urlHit": True,
                "textHit": True,
                "isError": True,
                "bodyPreview": (info.get("text") or "")[:180],
                "buttonTexts": [],
                "hasGoBack": False,
                "goBackText": "",
                "failureKind": info.get("kind"),
                "errorCode": info.get("error_code") or "",
            }
    except Exception:
        pass
    try:
        return driver.execute_script(r"""
        const url = String(location.href || '');
        const text = String(document.body?.innerText || '').replace(/\s+/g, ' ').trim();
        const lower = text.toLowerCase();
        const urlHit = /\/auth\/error/i.test(url) || /error=undefined/i.test(url);
        const textHit = (
          lower.includes('oops')
          || lower.includes('ran into an issue')
          || lower.includes('take a break')
          || lower.includes('try again soon')
          || text.includes('出了点问题')
          || text.includes('请稍后再试')
        );
        const buttons = [...document.querySelectorAll('button,a,[role="button"]')]
          .map(el => {
            const t = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
            const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            return {text: t, lower: t.toLowerCase(), visible, disabled: !!el.disabled};
          })
          .filter(x => x.visible && x.text);
        const goBack = buttons.find(x => (
          x.lower === 'go back'
          || x.lower === 'back'
          || x.text === '返回'
          || x.text === '后退'
          || x.lower.includes('go back')
        ));
        return {
          ok: true,
          url,
          urlHit,
          textHit,
          isError: Boolean(urlHit || (textHit && (urlHit || goBack))),
          bodyPreview: text.slice(0, 180),
          buttonTexts: buttons.map(x => x.text).slice(0, 10),
          hasGoBack: Boolean(goBack),
          goBackText: goBack ? goBack.text : '',
        };
        """) or {"ok": False, "isError": False}
    except Exception as exc:
        try:
            url = str(getattr(driver, "current_url", "") or "")
        except Exception:
            url = ""
        url_hit = "/auth/error" in url.lower() or "error=undefined" in url.lower()
        return {
            "ok": False,
            "isError": url_hit,
            "url": url,
            "urlHit": url_hit,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _is_auth_error_page(driver) -> bool:
    state = _auth_error_page_state(driver)
    return bool(state.get("isError") or state.get("urlHit"))


def _click_auth_error_go_back(driver) -> bool:
    """点击 Oops 错误页上的 Go back / 返回。"""
    try:
        target = driver.execute_script(r"""
        const buttons = [...document.querySelectorAll('button,a,[role="button"]')];
        for (const el of buttons) {
          const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
          const lower = text.toLowerCase();
          const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          if (!visible || el.disabled) continue;
          if (lower === 'go back' || lower === 'back' || text === '返回' || text === '后退' || lower.includes('go back')) {
            el.scrollIntoView({block:'center'});
            return el;
          }
        }
        return null;
        """)
        if not target:
            return False
        _human_click(driver, target, label="auth_error_go_back")
        return True
    except Exception as exc:
        logger.warning("%s 点击 auth error Go back 失败：%s: %s", _log_prefix(driver), type(exc).__name__, str(exc)[:160])
        return False


def _recover_from_auth_error_page(driver, *, reason: str = "") -> bool:
    """从 /auth/error Oops 页恢复：点 Go back，再重新打开登录页，便于重走邮箱步骤。

    实测该页没有邮箱输入框；若只点返回不强制回 login，下一轮会报“找不到邮箱输入框”。
    """
    state = _auth_error_page_state(driver)
    if not (state.get("isError") or state.get("urlHit")):
        return False

    logger.warning(
        "%s 检测到 ChatGPT 登录错误页，按 Go back -> 重新打开登录页恢复：reason=%s url=%s body=%s buttons=%s",
        _log_prefix(driver),
        reason or "-",
        str(state.get("url") or "")[:180],
        str(state.get("bodyPreview") or "")[:120],
        state.get("buttonTexts"),
    )

    clicked = _click_auth_error_go_back(driver)
    if clicked:
        logger.info("%s 已点击错误页 Go back（text=%s），等待页面回退", _log_prefix(driver), state.get("goBackText") or "Go back")
        time.sleep(1.8)
    else:
        logger.info("%s 错误页未找到 Go back 按钮，将直接打开登录页", _log_prefix(driver))

    # 无论 Go back 是否成功，都回到干净登录页，避免停在半残 SPA 状态。
    try:
        _safe_get(
            driver,
            "https://chatgpt.com/auth/login",
            timeout=min(35, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
            attempts=2,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
        human_delay("navigate")
        _page_warmup(driver, reason="auth_error_recover_login")
        _maybe_accept(driver)
    except Exception as exc:
        logger.warning("%s 错误页恢复后打开登录页失败：%s: %s", _log_prefix(driver), type(exc).__name__, str(exc)[:160])
        return clicked

    if _is_auth_error_page(driver):
        logger.warning("%s 恢复后仍停在错误页：%s", _log_prefix(driver), getattr(driver, "current_url", ""))
        return False
    logger.info("%s 错误页已恢复到登录流程，准备重新填写邮箱：url=%s", _log_prefix(driver), getattr(driver, "current_url", ""))
    return True


def _wait_email_submit_next_state(driver, email: str, timeout: int = 18) -> str:
    """邮箱提交后等待进入 password / otp / logged_in；仍停留邮箱页则返回 email_page。

    Cloak/Playwright 路径里，点击 submit 后页面经常先发生一次 SPA 导航：
    `chatgpt.com/auth/login?email=...`，同时 React 会短暂把 email input 清空。
    旧逻辑一看到空 input 就立刻返回 `email_cleared`，导致在真正跳到
    `auth.openai.com/...` 前过早重填，形成“提交 -> 清空 -> 重填”的循环。
    这里对 email_cleared 做去抖：只记录并继续观察几秒；若期间进入
    password/otp/login_password/logged_in 则按真实状态返回，持续清空才让上层重试。

    另外：OpenAI 偶发跳到 `/auth/error?error=undefined`（Oops + Go back）。
    该状态应尽快返回 `auth_error`，由上层点返回并重开登录页。
    """
    end = time.time() + timeout
    last = None
    cleared_seen_at: float | None = None
    cleared_last_log_at = 0.0
    cleared_recover_done = False
    expected_email = str(email or "").strip().lower()
    while time.time() < end:
        # 限流 / 废号优先：不要等成 unknown 再泛化报错
        fail = _inspect_auth_page_failure(driver)
        if fail.get("kind") == "rate_limit":
            logger.warning(
                "%s 邮箱提交后命中限流：code=%s url=%s",
                _log_prefix(driver), fail.get("error_code"), str(fail.get("url") or "")[:180],
            )
            return "rate_limit"
        if fail.get("kind") == "account_unusable":
            logger.warning(
                "%s 邮箱提交后命中废号页：code=%s url=%s",
                _log_prefix(driver), fail.get("error_code"), str(fail.get("url") or "")[:180],
            )
            return "account_unusable"
        if _is_auth_error_page(driver):
            logger.warning(
                "%s 邮箱提交后进入错误页：%s",
                _log_prefix(driver),
                _auth_error_page_state(driver),
            )
            return "auth_error"
        if _has_access_token(driver):
            return "logged_in"
        if _is_login_password_page(driver):
            return "login_password"
        if _is_email_verification_page(driver):
            return "otp"
        if _is_signup_password_page(driver):
            return "password"
        state = _email_input_value_state(driver)
        last = state
        # 即使无输入框，URL 也可能是 authorize 中间态或 error payload
        url_now = str((state or {}).get("url") or getattr(driver, "current_url", "") or "")
        if "auth.openai.com/error" in url_now.lower() or "payload=" in url_now.lower():
            fail2 = _inspect_auth_page_failure(driver)
            if fail2.get("kind") == "rate_limit":
                return "rate_limit"
            if fail2.get("kind") == "account_unusable":
                return "account_unusable"
        inputs = state.get("inputs") or []
        if inputs:
            values = [str(i.get("value") or "") for i in inputs]
            url = str(state.get("url") or "")
            has_blank = any(v == "" for v in values)
            has_expected = any(v.strip().lower() == expected_email for v in values)
            if has_blank and not has_expected:
                now = time.time()
                if cleared_seen_at is None:
                    cleared_seen_at = now
                # URL 已带 email 查询参数时更像是提交后的中间态，给它更长观察窗口。
                debounce = 18.0 if ("/auth/login" in url and "email=" in url) else 5.0
                if now - cleared_last_log_at > 2.0:
                    logger.info(
                        "%s 邮箱提交后检测到输入框短暂清空，继续等待跳转：elapsed=%.1fs debounce=%.1fs url=%s",
                        _log_prefix(driver), now - cleared_seen_at, debounce, url[:180],
                    )
                    cleared_last_log_at = now
                if (
                    not cleared_recover_done
                    and "/auth/login" in url
                    and "email=" in url
                    and now - cleared_seen_at >= 2.0
                ):
                    recover = _recover_email_submit_if_stuck(driver, email)
                    cleared_recover_done = True
                    logger.info("%s 邮箱提交后仍停留在 login?email，中途补交一次表单：%s", _log_prefix(driver), recover)
                if now - cleared_seen_at >= debounce:
                    return "email_cleared"
            else:
                cleared_seen_at = None
            # 仍是当前邮箱页，继续短等。
        time.sleep(0.8)
    fail = _inspect_auth_page_failure(driver)
    if fail.get("kind") == "rate_limit":
        return "rate_limit"
    if fail.get("kind") == "account_unusable":
        return "account_unusable"
    if _is_auth_error_page(driver):
        return "auth_error"
    logger.info("%s 邮箱提交后等待下一步超时，最后邮箱页状态=%s", _log_prefix(driver), last)
    return "email_page" if _is_email_login_page_still_present(driver) else "unknown"


def _submit_email_and_wait_next(driver, email: str, attempts: int = 3) -> str:
    """填写并提交邮箱，必须确认进入 password/otp/logged_in 才返回。"""
    last_state = None
    for attempt in range(1, attempts + 1):
        # 若上一轮掉进 Oops 错误页，先 Go back 并重开登录页，再填邮箱。
        if _is_auth_error_page(driver):
            _recover_from_auth_error_page(driver, reason=f"before_email_attempt_{attempt}")
        try:
            _type_email_address(driver, email, timeout=20)
        except RuntimeError as exc:
            # 典型：错误页上没有邮箱框。恢复后重试本轮。
            if _is_auth_error_page(driver) or "找不到邮箱输入框" in str(exc):
                logger.warning("%s 填写邮箱失败，尝试从错误页/异常页恢复后重试：%s", _log_prefix(driver), str(exc)[:180])
                recovered = _recover_from_auth_error_page(driver, reason=f"type_email_failed_{attempt}")
                # 即使不是标准 error 页，也强制回登录页再试一次本轮。
                if not recovered and not _is_email_login_page_still_present(driver):
                    try:
                        _safe_get(
                            driver,
                            "https://chatgpt.com/auth/login",
                            timeout=min(35, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
                            attempts=2,
                            accept_hosts=("chatgpt.com", "auth.openai.com"),
                        )
                        human_delay("navigate")
                        _page_warmup(driver, reason="email_type_fail_relogin")
                    except Exception:
                        pass
                try:
                    _type_email_address(driver, email, timeout=20)
                except Exception:
                    last_state = _email_input_value_state(driver)
                    logger.warning("%s 恢复后仍无法填写邮箱：attempt=%s/%s state=%s", _log_prefix(driver), attempt, attempts, last_state)
                    time.sleep(1.0)
                    continue
            else:
                raise
        state = _email_input_value_state(driver)
        last_state = state
        values = [str(i.get("value") or "") for i in (state.get("inputs") or [])]
        if not any(v.strip().lower() == email.strip().lower() for v in values):
            logger.warning("%s 邮箱写入校验失败，准备重试：attempt=%s/%s state=%s", _log_prefix(driver), attempt, attempts, state)
            time.sleep(0.8)
            continue
        logger.info("%s 已填写邮箱并校验通过：%s", _log_prefix(driver), email)
        human_delay("form")
        _submit_email_step(driver, email)
        logger.info("%s 已提交邮箱，等待进入密码页或验证码页（%s/%s）", _log_prefix(driver), attempt, attempts)
        state_name = _wait_email_submit_next_state(driver, email, timeout=20)
        if state_name == "login_password":
            raise RuntimeError(f"邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用: url={getattr(driver, 'current_url', '') or 'https://auth.openai.com/log-in/password'}")
        if state_name == "account_unusable":
            _raise_if_auth_page_terminal(driver, where=f"email_submit_account_unusable_{attempt}")
            from core.openai_auth import AccountUnusableError

            raise AccountUnusableError(
                f"邮箱提交后账号已废 where=email_submit_{attempt} url={getattr(driver, 'current_url', '')}",
                error_code="account_deactivated",
            )
        if state_name == "rate_limit":
            # 同浏览器内短退避再试一轮；仍失败则抛 RateLimitError 给任务层换出口重试（不降并发）
            logger.warning(
                "%s 邮箱提交命中限流，短退避后重试（%s/%s）url=%s",
                _log_prefix(driver), attempt, attempts, getattr(driver, "current_url", ""),
            )
            time.sleep(min(8.0, 3.0 + attempt * 2.0))
            try:
                _safe_get(
                    driver,
                    "https://chatgpt.com/auth/login",
                    timeout=min(35, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
                    attempts=2,
                    accept_hosts=("chatgpt.com", "auth.openai.com"),
                )
                human_delay("navigate")
                _maybe_accept(driver)
            except Exception as exc:
                logger.warning("%s 限流后重开登录页失败：%s", _log_prefix(driver), str(exc)[:160])
            continue
        if state_name in ("password", "otp", "logged_in"):
            logger.info("%s 邮箱提交后已进入下一步：%s", _log_prefix(driver), state_name)
            return state_name
        if state_name == "auth_error" or _is_auth_error_page(driver):
            # 限流/废号不要当 Oops 点 Go back
            terminal = _inspect_auth_page_failure(driver)
            if terminal.get("kind") in ("rate_limit", "account_unusable"):
                _raise_if_auth_page_terminal(driver, where=f"email_submit_auth_error_{attempt}")
            _recover_from_auth_error_page(driver, reason=f"after_email_submit_{attempt}")
            # 错误页恢复后多给一轮机会（不占用 attempt 上限的“空转”感，但仍计入循环）。
            logger.warning("%s 邮箱提交撞到错误页，已 Go back 并重开登录页，准备第 %s/%s 轮重试", _log_prefix(driver), attempt, attempts)
            time.sleep(1.2)
            continue
        logger.warning("%s 邮箱提交后仍未进入下一步：%s，准备重填重试 state=%s", _log_prefix(driver), state_name, _email_input_value_state(driver))
        # unknown 且 URL 像 error 时也走恢复。
        cur_url = str((_email_input_value_state(driver) or {}).get("url") or getattr(driver, "current_url", "") or "")
        if "auth/error" in cur_url.lower() or "payload=" in cur_url.lower():
            term = _inspect_auth_page_failure(driver)
            if term.get("kind") in ("rate_limit", "account_unusable"):
                _raise_if_auth_page_terminal(driver, where=f"email_submit_unknown_{attempt}")
            _recover_from_auth_error_page(driver, reason=f"unknown_error_url_{attempt}")
        time.sleep(1.0)
    # 耗尽尝试：若最后是限流/废号，抛带 error_code 的异常
    _raise_if_auth_page_terminal(driver, where="email_submit_exhausted")
    raise RuntimeError(f"邮箱提交后未进入密码页/验证码页，最后状态={last_state}")


def _type_otp(driver, code: str) -> None:
    from selenium.webdriver.common.by import By

    # 单输入框
    for selector in [
        "input[autocomplete='one-time-code']",
        "input[name='code']",
        "input[inputmode='numeric']",
        "input[type='tel']",
    ]:
        els = [e for e in driver.find_elements(By.CSS_SELECTOR, selector) if _visible(e)]
        if len(els) == 1:
            _human_type_text(driver, els[0], code, clear=True)
            return

    # 6 个分格输入框
    boxes = [e for e in driver.find_elements(By.CSS_SELECTOR, "input") if _visible(e)]
    numeric_boxes = []
    for e in boxes:
        attrs = " ".join(str(e.get_attribute(k) or "") for k in ("inputmode", "autocomplete", "aria-label", "name", "id", "type"))
        if any(x in attrs.lower() for x in ("numeric", "one-time", "code", "otp", "tel")):
            numeric_boxes.append(e)
    if len(numeric_boxes) >= len(code):
        for e, ch in zip(numeric_boxes, code):
            if _browser_actions_enabled():
                _human_scroll_to(driver, e)
                time.sleep(random.uniform(0.04, 0.18))
            e.send_keys(ch)
            if _browser_actions_enabled():
                human_delay("keystroke")
        return

    raise RuntimeError("找不到 OTP 输入框")


def _email_otp_page_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const inputs = [...document.querySelectorAll('input')].filter(visible).map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', inputmode: el.getAttribute('inputmode') || '',
          ariaInvalid: el.getAttribute('aria-invalid') || '', value: el.value || ''
        }));
        const buttons = [...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')].filter(visible).map(el => ({
          tag: el.tagName, type: el.getAttribute('type') || '', value: el.getAttribute('value') || '',
          action: el.getAttribute('data-dd-action-name') || '', aria: el.getAttribute('aria-label') || '',
          disabled: !!el.disabled || String(el.getAttribute('aria-disabled') || '').toLowerCase() === 'true',
          text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120)
        }));
        const errors = [...document.querySelectorAll('.react-aria-FieldError,[slot="errorMessage"],[id$="-error"],[aria-invalid="true"] + *,[class*="error"]')]
          .filter(visible).map(el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
        return {url: location.href, title: document.title, inputs, buttons, errors, text: (document.body?.innerText || '').slice(0, 1200)};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, 'current_url', ''), "error": f"{type(exc).__name__}: {exc}"}


def _is_email_verification_page(driver) -> bool:
    try:
        url = str(driver.current_url or '').lower()
    except Exception:
        url = ''
    if '/log-in/password' in url:
        return False
    # TOTP/Authenticator 页也常有 one-time-code 输入框，不能当成邮箱 OTP
    try:
        from core.totp_login import is_totp_page_driver
        if is_totp_page_driver(driver):
            return False
    except Exception:
        pass
    if 'email-verification' in url:
        return True
    state = _email_otp_page_state(driver)
    attrs = ' '.join(' '.join(str(i.get(k) or '') for k in ('type','name','id','autocomplete','inputmode')) for i in (state.get('inputs') or [])).lower()
    return 'one-time-code' in attrs or 'otp' in attrs or 'code' in attrs


def _clear_otp_inputs(driver) -> None:
    try:
        driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const inputs = [...document.querySelectorAll('input')].filter(visible).filter(el => {
          const attrs = [el.type, el.name, el.id, el.autocomplete, el.inputMode, el.getAttribute('aria-label')].join(' ').toLowerCase();
          return /one-time|otp|code|numeric|tel/.test(attrs);
        });
        for (const el of inputs) {
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(el, ''); else el.value = '';
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
        }
        """)
    except Exception:
        pass


def _safe_element_text(el) -> str:
    """兼容 Selenium / Cloak 元素：CloakElement 可能没有 .text 属性。"""
    if el is None:
        return ""
    for getter in (
        lambda: getattr(el, "text", None),
        lambda: el.get_attribute("innerText"),
        lambda: el.get_attribute("textContent"),
        lambda: el.get_attribute("value"),
        lambda: el.get_attribute("data-dd-action-name"),
        lambda: el.get_attribute("aria-label"),
    ):
        try:
            val = getter()
            if val is not None and str(val).strip():
                return str(val).strip()
        except Exception:
            continue
    return ""


def _is_auth_route_error_page(driver) -> bool:
    """OpenAI auth 路由错误页：Oops / Route Error / Invalid content type。"""
    try:
        state = _email_otp_page_state(driver)
    except Exception:
        state = {}
    title = str(state.get("title") or "").lower()
    text = str(state.get("text") or "").lower()
    url = str(state.get("url") or getattr(driver, "current_url", "") or "").lower()
    if "oops" in title or "oops, an error" in text or "an error occurred" in title:
        return True
    if "route error" in text or "invalid content type" in text:
        return True
    if "error occurred" in text and ("email-verification" in url or "auth.openai.com" in url):
        # 真·验证码错误页通常仍有 OTP 输入框；纯 Oops 页 inputs 为空
        inputs = state.get("inputs") or []
        if not inputs:
            return True
    return False


def _recover_auth_route_error(driver) -> dict:
    """处理 OpenAI Oops/Route Error 页：优先点 Try again。"""
    if not _is_auth_route_error_page(driver):
        return {"ok": False, "reason": "not_error_page"}
    logger.warning(
        "%s[OTP] 检测到 auth 路由错误页，尝试点击 Try again：url=%s",
        _log_prefix(driver),
        getattr(driver, "current_url", ""),
    )
    try:
        clicked = driver.execute_script(r"""
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
        const nodes = [...document.querySelectorAll('button,a,[role=button],input[type=button],input[type=submit]')].filter(visible);
        const score = (el) => {
          const t = ((el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('data-dd-action-name') || '') + '').toLowerCase();
          if (/try again|retry|再试|重试|もう一度|再度/.test(t)) return 100;
          if (/again|再/.test(t)) return 60;
          return 0;
        };
        const target = nodes.map(el => [score(el), el]).filter(x => x[0] > 0 && enabled(x[1])).sort((a,b)=>b[0]-a[0])[0]?.[1];
        if (!target) return {ok:false, reason:'no_try_again'};
        target.scrollIntoView({block:'center'});
        target.click();
        return {ok:true, text:(target.innerText || target.textContent || '').trim().slice(0,80)};
        """) or {}
    except Exception as exc:
        clicked = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    if clicked.get("ok"):
        logger.info("%s[OTP] 已点击错误页恢复按钮：%s", _log_prefix(driver), clicked.get("text") or "Try again")
        time.sleep(random.uniform(1.5, 2.8) if _browser_actions_enabled() else 2.0)
        if not _is_auth_route_error_page(driver):
            return {"ok": True, "action": "try_again", "text": clicked.get("text")}
        return {"ok": False, "need_restart_login": True, "reason": "still_error_after_try_again"}
    return {"ok": False, "need_restart_login": True, "reason": str(clicked.get("reason") or "try_again_missing")}


def _restart_email_login_flow(driver, email: str, *, login_url: str = "https://chatgpt.com/auth/login") -> str:
    """从 ChatGPT 登录页重开邮箱流，返回 next_state（otp/password/...）。"""
    logger.info("%s[OTP] 路由错误后重开登录页并重新提交邮箱：%s", _log_prefix(driver), email)
    try:
        _safe_get(
            driver,
            login_url,
            timeout=35,
            attempts=2,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
    except Exception:
        try:
            driver.get(login_url)
        except Exception as exc:
            raise RuntimeError(f"auth_route_error: 重开登录页失败: {exc}") from exc
    human_delay("navigate")
    _maybe_accept(driver)
    return _submit_email_and_wait_next(driver, email, attempts=3)


def _otp_flow_already_passed(driver) -> bool:
    """OTP 是否其实已经通过（页面已离开验证码流）。

    常见误判：验证码已生效并跳到 chatgpt.com 主站，但等待超时被当成 invalid，
    随后又去点“重新发送”，在主站找不到按钮而失败。
    """
    try:
        if _has_access_token(driver):
            return True
    except Exception:
        pass
    try:
        url = str(getattr(driver, "current_url", "") or "").lower()
    except Exception:
        url = ""
    if "/log-in/password" in url:
        return False
    # 路由错误页不算通过
    try:
        if _is_auth_route_error_page(driver):
            return False
    except Exception:
        pass
    if _is_email_verification_page(driver):
        return False
    # 明确后续页才算通过；不要把任意 auth.openai.com 都当成已通过
    if "chatgpt.com" in url:
        return True
    if any(x in url for x in (
        "about-you", "add-phone", "phone-verification", "consent",
        "workspace", "callback", "you're-all-set", "all-set",
    )):
        return True
    return False


def _click_resend_email_otp(driver, timeout: int = 20) -> dict:
    """点击重新发送邮箱验证码。优先按 DOM 属性识别，文本仅兜底。

    若已不在验证码页（OTP 实际已通过），返回 skipped，不抛错。
    若落在 OpenAI Oops/Route Error 页，先点 Try again；仍失败则抛 auth_route_error。
    若是 Authentication Error / account_deactivated，直接抛 AccountUnusableError。
    """
    end = time.time() + timeout
    last = None
    while time.time() < end:
        # 废号 / 限流页没有 resend，优先识别
        _raise_if_auth_page_terminal(driver, where="resend_otp")
        if _is_auth_route_error_page(driver):
            # 路由错误也可能夹带废号文案
            term = _inspect_auth_page_failure(driver)
            if term.get("kind") in ("rate_limit", "account_unusable"):
                _raise_if_auth_page_terminal(driver, where="resend_route_error")
            rec = _recover_auth_route_error(driver)
            if rec.get("ok"):
                # Try again 后可能回到 OTP 页，继续找 resend
                if _otp_flow_already_passed(driver):
                    return {"ok": True, "skipped": True, "reason": "otp_already_passed_after_try_again"}
                continue
            raise RuntimeError(
                f"auth_route_error: OpenAI OTP 路由错误页无法恢复 reason={rec.get('reason')} "
                f"state={_email_otp_page_state(driver)}"
            )
        if _otp_flow_already_passed(driver):
            logger.info(
                "%s[OTP] 当前已不在验证码页，跳过重新发送：url=%s",
                _log_prefix(driver),
                getattr(driver, "current_url", ""),
            )
            return {"ok": True, "skipped": True, "reason": "otp_already_passed"}
        try:
            btn = driver.execute_script(r"""
            const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
            const candidates = [...document.querySelectorAll('button,a,[role=button],[role=link],input[type=button],input[type=submit]')].filter(visible);
            const attrHit = candidates.find(el => {
              if (!enabled(el)) return false;
              const attrs = [el.id, el.getAttribute('name'), el.getAttribute('value'), el.getAttribute('data-dd-action-name'), el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('data-testid')]
                .join(' ').toLowerCase();
              const name = String(el.getAttribute('name') || '').toLowerCase();
              const value = String(el.getAttribute('value') || '').toLowerCase();
              if (name === 'intent' && value === 'resend') return true;
              // 排除 Try again（那是错误页恢复，不是重发验证码）
              const text = ((el.innerText || el.textContent || '') + '').toLowerCase();
              if (/try again|retry/.test(text)) return false;
              return /resend|send.*new|new.*code|again/.test(attrs);
            });
            if (attrHit) return attrHit;
            // 兜底：多语言文本，避免因页面没有稳定属性时卡死。
            return candidates.find(el => {
              if (!enabled(el)) return false;
              const t = ((el.innerText || el.textContent || '') + '').toLowerCase();
              if (/try again|retry/.test(t)) return false;
              return /resend|send\s+(?:a\s+)?new\s+code|send\s+again|重新发送|重新发送电子邮件|重发|再次发送|再送信|新しい|届かない/.test(t);
            }) || null;
            """)
            if btn:
                text = _safe_element_text(btn)
                _human_click(driver, btn, label="resend_otp")
                logger.info("%s[OTP] 已点击重新发送验证码按钮：%s", _log_prefix(driver), text or '-')
                time.sleep(random.uniform(1.1, 2.4) if _browser_actions_enabled() else 1.5)
                return {"ok": True, "text": text}
        except Exception as exc:
            # 终端异常直接抛出
            from core.openai_auth import AccountUnusableError, RateLimitError

            if isinstance(exc, (AccountUnusableError, RateLimitError)):
                raise
            last = exc
        time.sleep(0.5)
    # 结束前再确认一次：可能等待期间已跳转成功
    if _otp_flow_already_passed(driver):
        logger.info(
            "%s[OTP] 等待重发按钮期间已离开验证码页，按 OTP 已通过处理：url=%s",
            _log_prefix(driver),
            getattr(driver, "current_url", ""),
        )
        return {"ok": True, "skipped": True, "reason": "otp_already_passed"}
    _raise_if_auth_page_terminal(driver, where="resend_otp_timeout")
    if _is_auth_route_error_page(driver):
        raise RuntimeError(
            f"auth_route_error: OpenAI OTP 路由错误页且无 resend 按钮 state={_email_otp_page_state(driver)}"
        )
    raise RuntimeError(f"找不到可点击的重新发送验证码按钮: last={last}, state={_email_otp_page_state(driver)}")


def _wait_after_email_otp_submit(driver, timeout: int = 30) -> str:
    """提交 OTP 后等待页面离开验证码页。

    返回：
      - accepted：已离开验证码页 / OTP 已通过
      - invalid：仍在验证码页且输入错误
      - route_error：OpenAI Oops/Route Error 页
      - account_unusable：账号已废
      - rate_limit：Auth 限流

    只有页面明确出现验证码错误（aria-invalid / 错误文案）才判定为无效；
    网络慢时页面跳转可能超过 10s，超时后只要没有错误标记就按 accepted 处理，
    避免把已提交成功的验证码误判为失败后误点“重新发送”把流程搞乱。
    """
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        time.sleep(0.5)
        fail = _inspect_auth_page_failure(driver)
        if fail.get("kind") == "account_unusable":
            logger.warning(
                "%s[OTP] 提交后落到废号页：code=%s url=%s",
                _log_prefix(driver), fail.get("error_code"), str(fail.get("url") or "")[:180],
            )
            return "account_unusable"
        if fail.get("kind") == "rate_limit":
            logger.warning(
                "%s[OTP] 提交后落到限流页：code=%s url=%s",
                _log_prefix(driver), fail.get("error_code"), str(fail.get("url") or "")[:180],
            )
            return "rate_limit"
        if _is_auth_route_error_page(driver):
            logger.warning(
                "%s[OTP] 提交后落到 auth 路由错误页：%s",
                _log_prefix(driver),
                _email_otp_page_state(driver),
            )
            return "route_error"
        if _otp_flow_already_passed(driver):
            return "accepted"
        if not _is_email_verification_page(driver):
            # 离开验证码页也可能是 Authentication Error（URL 仍可能含 email-verification）
            fail2 = _inspect_auth_page_failure(driver)
            if fail2.get("kind") == "account_unusable":
                return "account_unusable"
            if fail2.get("kind") == "rate_limit":
                return "rate_limit"
            return "accepted"
        last = _email_otp_page_state(driver)
        # 页面 body/errors 里可能直接写 error_code: account_deactivated
        body_blob = " ".join([
            str(last.get("title") or ""),
            str(last.get("text") or ""),
            " ".join(str(x) for x in (last.get("errors") or [])),
        ])
        try:
            from core.openai_auth import detect_account_unusable_text, detect_rate_limit_text

            if detect_account_unusable_text(body_blob):
                return "account_unusable"
            if detect_rate_limit_text(body_blob):
                return "rate_limit"
        except Exception:
            pass
        invalid = any(str(i.get("ariaInvalid") or "").lower() == "true" for i in (last.get("inputs") or []))
        if invalid or (last.get("errors") or []):
            return "invalid"
    fail = _inspect_auth_page_failure(driver)
    if fail.get("kind") == "account_unusable":
        return "account_unusable"
    if fail.get("kind") == "rate_limit":
        return "rate_limit"
    if _is_auth_route_error_page(driver):
        return "route_error"
    if _otp_flow_already_passed(driver) or not _is_email_verification_page(driver):
        return "accepted"
    if _is_email_verification_page(driver):
        # 超时仍停留：若无明确错误标记，判定为提交成功、跳转缓慢，按 accepted 放行。
        last = _email_otp_page_state(driver)
        has_error_mark = bool(last.get('errors')) or any(
            str(i.get('ariaInvalid') or '').lower() == 'true' for i in (last.get('inputs') or [])
        )
        if has_error_mark:
            logger.warning("%s[OTP] 提交后仍停留验证码页且存在错误标记，按验证码无效处理 snapshot=%s", _log_prefix(driver), last)
            return 'invalid'
        logger.warning(
            "%s[OTP] 提交后 %ss 仍在验证码页但无错误标记，按跳转缓慢处理（accepted） snapshot=%s",
            _log_prefix(driver), timeout, last
        )
        return 'accepted'
    return 'accepted'


def _click_continue(driver) -> None:
    _click_any(driver, [
        "button[type='submit']",
        "//button[contains(., 'Continue')]",
        "//button[contains(., '继续')]",
        "//button[contains(., 'Sign up')]",
        "//button[contains(., 'Create')]",
        "//button[contains(., 'Next')]",
    ], timeout=20)


def _maybe_accept(driver) -> None:
    # 只处理明确的 cookie/consent 弹层按钮；不要用 “Continue” 兜底，
    # 非日本出口时 “Continue with Google” 也会命中，导致误点 Google 登录。
    for selectors in ([
        "button#onetrust-accept-btn-handler",
        "button[data-testid='cookie-accept']",
        "button[data-testid='accept-cookies']",
        "//button[contains(., 'Accept')]",
        "//button[contains(., '同意')]",
        "//button[contains(., 'Agree')]",
    ],):
        try:
            _click_any(driver, selectors, timeout=3)
            time.sleep(0.5)
        except Exception:
            pass


def _page_snapshot(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const inputs = [...document.querySelectorAll('input,select,textarea')].map(el => ({
          tag: el.tagName, type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
          id: el.id || '', placeholder: el.getAttribute('placeholder') || '',
          autocomplete: el.getAttribute('autocomplete') || '', aria: el.getAttribute('aria-label') || '',
          value: el.value || '', visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).filter(x => x.visible).slice(0, 30);
        const buttons = [...document.querySelectorAll('button,a[role=button],input[type=submit]')].map(el => ({
          text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim(),
          type: el.getAttribute('type') || '', visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
          disabled: !!el.disabled
        })).filter(x => x.visible).slice(0, 30);
        const widgets = [...document.querySelectorAll('[role=spinbutton], .react-aria-Select, [data-testid="hidden-select-container"] select')].map(el => ({
          tag: el.tagName, role: el.getAttribute('role') || '', dataType: el.getAttribute('data-type') || '',
          aria: el.getAttribute('aria-label') || '', text: (el.innerText || el.textContent || '').trim().slice(0, 80),
          visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).slice(0, 30);
        return {url: location.href, title: document.title, text: (document.body?.innerText || '').slice(0, 2000), inputs, buttons, widgets};
        """) or {}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "url": getattr(driver, 'current_url', '')}


def _has_access_token(driver) -> bool:
    try:
        result = driver.execute_async_script(r"""
        const done = arguments[0];
        fetch('https://chatgpt.com/api/auth/session', {credentials:'include'})
          .then(r => r.json()).then(j => done(Boolean(j && j.accessToken)))
          .catch(() => done(false));
        """)
        return bool(result)
    except Exception:
        return False


def _is_profile_like(snapshot: dict) -> bool:
    """资料页识别：兼容 about-you/profile；年龄/生日控件可能不是 input，而是 React Aria widget。"""
    url = str(snapshot.get('url') or '').lower()
    inputs = snapshot.get('inputs') or []
    widgets = snapshot.get('widgets') or []
    attrs = ' '.join(
        ' '.join(str(i.get(k) or '') for k in ('name', 'id', 'placeholder', 'autocomplete', 'aria', 'type')).lower()
        for i in inputs
    )
    widget_attrs = ' '.join(
        ' '.join(str(i.get(k) or '') for k in ('role', 'dataType', 'aria', 'text', 'tag')).lower()
        for i in widgets
    )
    has_profile_url = any(x in url for x in ('about-you', 'profile', 'signup/profile', 'create-account/profile'))
    has_name_field = (
        'autocomplete name' in attrs
        or ' name ' in f' {attrs} '
        or 'fullname' in attrs
        or 'full_name' in attrs
        or 'firstname' in attrs
        or 'lastname' in attrs
    )
    has_age_or_birth_field = any(x in f' {attrs} {widget_attrs} ' for x in (
        ' age', '-age', '_age', 'birth', 'birthday', 'birthdate',
        ' month', '-month', '_month', 'data-type month',
        ' day', '-day', '_day', 'data-type day',
        ' year', '-year', '_year', 'data-type year',
        'spinbutton', 'react-aria-select', 'type number',
    ))
    # about-you/profile URL 本身已经足够强；部分新版页面会用无 name 的 React Aria 控件。
    return has_profile_url and (has_name_field or has_age_or_birth_field or bool(inputs) or bool(widgets))


def _set_element_value(driver, el, value: str) -> None:
    """兼容 React 受控输入框：用原生 setter 设置值并派发 input/change。"""
    driver.execute_script(r"""
    const el = arguments[0];
    const value = String(arguments[1]);
    const tag = (el.tagName || '').toLowerCase();
    el.scrollIntoView({block:'center'});
    el.focus();
    if (tag === 'select') {
      el.value = value;
    } else {
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, value);
      else el.value = value;
    }
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    el.blur();
    """, el, value)


def _select_or_type(driver, selectors: list[str], value: str, timeout: int = 3) -> bool:
    try:
        el = _find_any(driver, selectors, timeout=timeout)
    except Exception:
        return False
    try:
        tag = (el.tag_name or '').lower()
        if tag == 'select':
            if el.__class__.__name__ == 'CloakElement':
                driver.execute_script(r"""
                const el = arguments[0], value = String(arguments[1]);
                const n = parseInt(value, 10);
                const opts = [...el.options];
                const match = opts.find(o => o.value === value)
                  || opts.find(o => (o.textContent || '').trim() === value)
                  || opts[Math.max(0, n - 1)];
                if (match) el.value = match.value; else el.value = value;
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                """, el, str(value))
            else:
                from selenium.webdriver.support.ui import Select
                sel = Select(el)
                try:
                    sel.select_by_value(str(int(value)))
                except Exception:
                    try:
                        sel.select_by_visible_text(str(int(value)))
                    except Exception:
                        # 月份 select 可能是 0-based，也可能是 1-based；先 value/text，不行再 index。
                        sel.select_by_index(max(0, int(value)-1))
                driver.execute_script("arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", el)
        else:
            _human_type_text(driver, el, str(value), clear=True)
        return True
    except Exception as exc:
        logger.debug('%s 填写字段失败 selectors=%s value=%s err=%s', _log_prefix(driver), selectors, value, exc)
        return False


def _fill_birthday_or_age(driver, birthday: str, age: int) -> str | None:
    """填写 about-you 的年龄/生日控件。

    参考 FlowPilot：优先处理直接年龄 input；否则兼容 hidden birthday/date、原生年月日
    select/input、React Aria hidden native select、role=spinbutton[data-type=year/month/day]。
    返回 age / birthday / ymd / react_select / spinbutton / None。
    """
    y, m, d = birthday.split('-')
    result = driver.execute_script(r"""
    const birthday = String(arguments[0]);
    const year = String(arguments[1]);
    const month = String(Number(arguments[2]));
    const month2 = String(arguments[2]).padStart(2, '0');
    const day = String(Number(arguments[3]));
    const day2 = String(arguments[3]).padStart(2, '0');
    const age = String(arguments[4]);
    const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
      && !el.disabled && !el.readOnly;
    const setValue = (el, value) => {
      if (!el) return false;
      el.scrollIntoView?.({block:'center'});
      el.focus?.();
      const tag = (el.tagName || '').toLowerCase();
      const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype
        : tag === 'select' ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, String(value)); else el.value = String(value);
      if (tag === 'select') {
        [...el.options].forEach(opt => { opt.selected = String(opt.value) === String(value); });
      }
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      el.blur?.();
      return true;
    };
    const ageInput = [...document.querySelectorAll('input[name="age"], input#age, input[id$="-age"], input[type="number"]')]
      .find(visible);
    if (ageInput && setValue(ageInput, age)) return {ok:true, mode:'age'};

    const dateInput = [...document.querySelectorAll('input[name="birthdate"], input[type="date"], input[name="birthday"]')]
      .find(el => visible(el) || String(el.getAttribute('type') || '').toLowerCase() === 'date');
    if (dateInput && setValue(dateInput, birthday)) return {ok:true, mode:'birthday'};

    const setFirst = (selectors, values) => {
      for (const sel of selectors) {
        for (const el of [...document.querySelectorAll(sel)]) {
          if (!visible(el)) continue;
          for (const val of values) {
            if (el.tagName === 'SELECT') {
              const has = [...el.options].some(o => String(o.value) === String(val) || String(o.textContent || '').trim() === String(val));
              if (!has) continue;
            }
            if (setValue(el, val)) return true;
          }
        }
      }
      return false;
    };
    const yOk = setFirst(['select[name="year"]','input[name="year"]','select[id*="year"]','input[id*="year"]'], [year]);
    const mOk = setFirst(['select[name="month"]','input[name="month"]','select[id*="month"]','input[id*="month"]'], [month, month2]);
    const dOk = setFirst(['select[name="day"]','input[name="day"]','select[id*="day"]','input[id*="day"]'], [day, day2]);
    if (yOk && mOk && dOk) {
      const hidden = document.querySelector('input[name="birthday"]');
      if (hidden) setValue(hidden, birthday);
      return {ok:true, mode:'ymd'};
    }

    // React Aria Select 通常有 hidden native select；不依赖标签文字，按 option 数值范围和 DOM 顺序推断年/月/日。
    const selects = [...document.querySelectorAll('[data-testid="hidden-select-container"] select, .react-aria-Select select, select')]
      .filter(el => !el.disabled);
    const nums = sel => [...sel.options].map(o => Number(o.value)).filter(Number.isFinite);
    const maxNum = sel => Math.max(...nums(sel), -Infinity);
    const minNum = sel => Math.min(...nums(sel), Infinity);
    const hasOption = (sel, val) => [...sel.options].some(o => String(o.value) === String(val));
    const yearSelects = selects.filter(sel => hasOption(sel, year) && maxNum(sel) > 1900);
    const smallSelects = selects.filter(sel => !yearSelects.includes(sel));
    const monthSelects = smallSelects.filter(sel => (hasOption(sel, month) || hasOption(sel, month2)) && minNum(sel) <= 1 && maxNum(sel) <= 12);
    const daySelects = smallSelects.filter(sel => (hasOption(sel, day) || hasOption(sel, day2)) && maxNum(sel) >= 28);
    if (yearSelects.length && monthSelects.length && daySelects.length) {
      const ys = yearSelects[0];
      let ms = monthSelects[0];
      let ds = daySelects.find(x => x !== ms) || daySelects[0];
      setValue(ys, year);
      setValue(ms, hasOption(ms, month) ? month : month2);
      setValue(ds, hasOption(ds, day) ? day : day2);
      const hidden = document.querySelector('input[name="birthday"]');
      if (hidden) setValue(hidden, birthday);
      return {ok:true, mode:'react_select'};
    }

    const spinYear = document.querySelector('[role="spinbutton"][data-type="year"]');
    const spinMonth = document.querySelector('[role="spinbutton"][data-type="month"]');
    const spinDay = document.querySelector('[role="spinbutton"][data-type="day"]');
    if (spinYear && spinMonth && spinDay) return {ok:false, mode:'spinbutton_needed'};
    return {ok:false, mode:'missing'};
    """, birthday, y, m, d, str(age)) or {}
    if result.get('ok'):
        return str(result.get('mode') or 'birthday')
    if result.get('mode') != 'spinbutton_needed':
        return None

    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        mod = Keys.COMMAND
        try:
            import platform
            if platform.system().lower() != 'darwin':
                mod = Keys.CONTROL
        except Exception:
            pass
        for selector, value in [
            ('[role="spinbutton"][data-type="year"]', y),
            ('[role="spinbutton"][data-type="month"]', str(m).zfill(2)),
            ('[role="spinbutton"][data-type="day"]', str(d).zfill(2)),
        ]:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].focus();", el)
            time.sleep(0.1)
            el.send_keys(mod, 'a')
            time.sleep(0.05)
            el.send_keys(str(value))
            time.sleep(0.1)
            driver.execute_script("arguments[0].dispatchEvent(new Event('input', {bubbles:true})); arguments[0].dispatchEvent(new Event('change', {bubbles:true})); arguments[0].blur();", el)
        driver.execute_script(r"""
        const hidden = document.querySelector('input[name="birthday"]');
        if (hidden) {
          const value = arguments[0];
          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
          if (setter) setter.call(hidden, value); else hidden.value = value;
          hidden.dispatchEvent(new Event('input', {bubbles:true}));
          hidden.dispatchEvent(new Event('change', {bubbles:true}));
        }
        """, birthday)
        return 'spinbutton'
    except Exception as exc:
        logger.debug('%s spinbutton 生日填写失败：%s', _log_prefix(driver), exc)
        return None


def _generate_roxy_password() -> str:
    """参考 FlowPilot 密码策略：8~64 位，含大小写、数字、符号。"""
    upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
    lower = 'abcdefghjkmnpqrstuvwxyz'
    digits = '23456789'
    symbols = '!@#$%^&*?_-+=' 
    groups = [upper, lower, digits, symbols]
    all_chars = ''.join(groups)
    chars = [random.choice(g) for g in groups]
    while len(chars) < 14:
        chars.append(random.choice(all_chars))
    random.shuffle(chars)
    return ''.join(chars)


def _registration_password() -> str:
    try:
        from config import register as _register_cfg
        configured = str(getattr(_register_cfg, 'REGISTER_PASSWORD', '') or '').strip()
        if configured:
            return configured
    except Exception:
        pass
    return _generate_roxy_password()


def _password_page_state(driver) -> dict:
    try:
        return driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const inputs = [...document.querySelectorAll('input')].map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          autocomplete: el.getAttribute('autocomplete') || '', visible: visible(el), value: el.type === 'password' ? '<password>' : (el.value || '')
        })).slice(0, 30);
        const forms = [...document.querySelectorAll('form')].map(f => ({action: f.getAttribute('action') || ''}));
        const buttons = [...document.querySelectorAll('button,input[type="submit"]')].map(el => ({
          type: el.getAttribute('type') || '', name: el.getAttribute('name') || '', id: el.id || '',
          disabled: !!el.disabled, visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        })).slice(0, 30);
        return {url: location.href, inputs, forms, buttons};
        """) or {}
    except Exception as exc:
        return {"url": getattr(driver, "current_url", ""), "error": f"{type(exc).__name__}: {exc}"}


def _is_signup_password_page(driver) -> bool:
    state = _password_page_state(driver)
    url = str(state.get('url') or '').lower()
    if any(x in url for x in ('/create-account/password', '/u/signup/password', '/signup/password')):
        return True
    if '/log-in/password' in url:
        return False
    inputs = state.get('inputs') or []
    return any(
        i.get('visible') and (
            str(i.get('type') or '').lower() == 'password'
            or 'password' in str(i.get('name') or '').lower()
            or str(i.get('autocomplete') or '').lower() == 'new-password'
        )
        for i in inputs
    )


def _is_login_password_page(driver) -> bool:
    try:
        url = str(driver.current_url or '').lower()
    except Exception:
        url = ''
    if '/log-in/password' in url:
        return True
    state = _password_page_state(driver)
    url = str(state.get('url') or '').lower()
    return '/log-in/password' in url


def _click_passwordless_signup_if_present(driver) -> dict:
    """
    新版注册/登录流在 password 页可能默认要求密码。
    如果页面提供“使用一次性验证码”按钮，优先点击进入邮箱 OTP 页面。

    注意：必须在 JS 内完成 click，并只返回可 JSON 序列化字段。
    Cloak/Playwright 适配层无法把 DOM Element 塞进 dict 再传回 Python
    （evaluate_handle + json_value 会丢 button / 抛错），旧写法会导致
    明明页面有「ワンタイムコードでログインする」却点不到。
    """
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const enabled = el => !el.disabled && String(el.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
        const norm = s => String(s || '').replace(/\s+/g, '').toLowerCase();
        const candidates = [...document.querySelectorAll('button,a,input[type="submit"],[role="button"],[role="link"]')].filter(el => visible(el) && enabled(el));
        const isPasswordlessOtp = el => {
          const name = String(el.getAttribute('name') || '').toLowerCase();
          const value = String(el.getAttribute('value') || '').toLowerCase();
          const attrs = [
            el.id, name, value, el.getAttribute('aria-label'), el.getAttribute('title'),
            el.getAttribute('data-testid'), el.getAttribute('data-dd-action-name'), el.className, el.textContent
          ].join(' ').toLowerCase();
          const text = norm(el.textContent || el.getAttribute('value') || el.getAttribute('aria-label') || '');
          return (
            (name === 'intent' && value.includes('passwordless') && value.includes('send_otp')) ||
            (name === 'intent' && value.includes('passwordless') && value.includes('otp')) ||
            (name === 'intent' && value === 'passwordless_signup_send_otp') ||
            (name === 'intent' && value === 'passwordless_login_send_otp') ||
            attrs.includes('passwordless_signup_send_otp') ||
            attrs.includes('passwordless_login_send_otp') ||
            /passwordless.*otp|otp.*passwordless|one[-_\s]?time.*code|code.*one[-_\s]?time/.test(attrs) ||
            text.includes('使用一次性验证码注册') ||
            text.includes('使用一次性验证码登录') ||
            text.includes('使用一次性验证码') ||
            text.includes('使用一次性驗證碼註冊') ||
            text.includes('使用一次性驗證碼登入') ||
            text.includes('一次性验证码') ||
            text.includes('一次性驗證碼') ||
            text.includes('メールでコード') ||
            text.includes('ワンタイムコード') ||
            text.includes('ワンタイムコードでログイン') ||
            text.includes('認証コード') ||
            text.includes('useonetimeregistrationcode') ||
            text.includes('useaone-timecodetosignup') ||
            text.includes('useaone-timecodetoregister') ||
            text.includes('useaone-timecodetologin') ||
            text.includes('continuewithaone-timecode') ||
            text.includes('loginwithaone-timecode') ||
            text.includes('signupwithaone-timecode') ||
            text.includes('one-timecode')
          );
        };
        const btn = candidates.find(isPasswordlessOtp);
        if (!btn) {
          return {
            ok:false,
            reason:'missing_passwordless_button',
            buttons: candidates.slice(0, 12).map(el => (el.textContent || el.getAttribute('value') || '').trim().slice(0, 40))
          };
        }
        btn.scrollIntoView({block:'center', inline:'nearest'});
        const meta = {
          ok:true,
          reason:'clicked_passwordless_send_otp',
          name: btn.getAttribute('name') || '',
          value: btn.getAttribute('value') || '',
          text: (btn.textContent || btn.getAttribute('value') || '').trim().slice(0, 80),
          tag: (btn.tagName || '').toLowerCase()
        };
        try {
          btn.focus();
          btn.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true, cancelable:true, pointerType:'mouse'}));
          btn.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
          btn.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
          btn.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
          if (typeof btn.click === 'function') btn.click();
        } catch (e) {
          const form = btn.closest('form');
          if (form && typeof form.requestSubmit === 'function') form.requestSubmit(btn);
          else if (form) form.submit();
          else return {ok:false, reason:'click_failed:' + String(e), text: meta.text};
        }
        return meta;
        """) or {"ok": False, "reason": "empty_result"}
        if isinstance(result, dict):
            return result
        return {"ok": False, "reason": f"bad_result_type:{type(result).__name__}"}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}


def _fill_password_page_if_present(driver, email: str, timeout: int = 25) -> str | None:
    """邮箱提交后兼容 create-account/password。返回本次设置的 OpenAI 账号密码；未遇到密码页返回 None。"""
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        if _is_email_verification_page(driver):
            return None
        if _has_access_token(driver):
            return None
        last = _password_page_state(driver)
        is_signup_password = _is_signup_password_page(driver)
        is_login_password = _is_login_password_page(driver)
        if not (is_signup_password or is_login_password):
            time.sleep(0.5)
            continue
        passwordless = _click_passwordless_signup_if_present(driver)
        if passwordless.get('ok'):
            logger.info("%s 检测到 password 页，已点击一次性验证码入口：email=%s detail=%s", _log_prefix(driver), email, passwordless)
            wait_end = time.time() + 20
            while time.time() < wait_end:
                if _is_email_verification_page(driver):
                    logger.info("%s 一次性验证码入口已进入邮箱验证码页", _log_prefix(driver))
                    return None
                if _has_access_token(driver):
                    logger.info("%s 一次性验证码入口后已检测到登录态", _log_prefix(driver))
                    return None
                time.sleep(0.5)
            logger.info("%s 已点击一次性验证码入口，未立即检测到 OTP 页，交给后续 OTP 阶段继续处理", _log_prefix(driver))
            return None
        if is_login_password:
            logger.info("%s 当前是登录密码页但未找到一次性验证码入口，跳过密码填写并交给 OTP 阶段：state=%s", _log_prefix(driver), last)
            return None
        password = _registration_password()
        logger.info("%s 检测到 create-account/password，准备设置密码（%s 位）：email=%s", _log_prefix(driver), len(password), email)
        # 关键：必须在 JS 内完成填密 + 提交，只返回可 JSON 序列化字段。
        # Cloak/Playwright 无法把 DOM Element 塞进 dict 再回传 Python（json_value 丢句柄），
        # 旧写法 return {input, button} 后 _human_type/_human_click 会触发
        # el.scrollIntoView is not a function。
        result = driver.execute_script(r"""
        const password = String(arguments[0] || '');
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled && !el.readOnly;
        const safeScroll = el => {
          try {
            if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView({block:'center', inline:'nearest'});
          } catch (e) {}
        };
        const setValue = (el, value) => {
          const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
          if (setter) setter.call(el, value); else el.value = value;
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
        };
        const input = [...document.querySelectorAll('input[type="password"],input[name*="password" i],input[autocomplete="new-password"]')]
          .find(visible);
        if (!input) return {ok:false, reason:'missing_password_input'};
        if (!password) return {ok:false, reason:'empty_password'};
        const form = input.closest('form');
        const scope = form || document;
        const buttons = [...scope.querySelectorAll('button,input[type="submit"]')]
          .filter(el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
          .map((el, idx) => {
            const r = el.getBoundingClientRect();
            const ir = input.getBoundingClientRect();
            return {el, idx, below: r.top >= ir.bottom - 10, dist: Math.max(0, r.top - ir.bottom) + Math.abs((r.left+r.right-ir.left-ir.right)/2)/10};
          })
          .filter(x => x.below)
          .sort((a,b) => a.dist - b.dist || a.idx - b.idx);
        if (!buttons.length) return {ok:false, reason:'missing_submit'};
        const btn = buttons[0].el;
        safeScroll(input);
        try { input.focus(); } catch (e) {}
        setValue(input, password);
        const filled = String(input.value || '').length > 0;
        if (!filled) return {ok:false, reason:'password_not_filled'};
        safeScroll(btn);
        let submitMethod = 'click';
        try {
          btn.focus();
          btn.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true, cancelable:true, pointerType:'mouse'}));
          btn.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
          btn.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
          btn.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
          if (typeof btn.click === 'function') btn.click();
        } catch (e) {
          if (form && typeof form.requestSubmit === 'function') {
            form.requestSubmit(btn);
            submitMethod = 'requestSubmit';
          } else if (form) {
            form.submit();
            submitMethod = 'form.submit';
          } else {
            return {ok:false, reason:'submit_failed:' + String(e)};
          }
        }
        return {
          ok:true,
          reason:'password_filled_and_submitted',
          submitMethod,
          buttonText: (btn.innerText || btn.textContent || btn.value || '').trim().slice(0, 80),
          passwordLen: password.length,
          url: location.href
        };
        """, password) or {}
        if not result.get('ok'):
            raise RuntimeError(f"密码页处理失败：{result} state={last}")
        human_delay("form", minimum=0.4, maximum=1.4)
        logger.info("%s 已填写并提交密码页：%s", _log_prefix(driver), result)
        # 提交密码后通常进入邮箱验证码页，最多等一段时间。
        wait_end = time.time() + 20
        while time.time() < wait_end:
            if _is_email_verification_page(driver):
                logger.info("%s 密码提交后已进入邮箱验证码页", _log_prefix(driver))
                return password
            if _has_access_token(driver):
                logger.info("%s 密码提交后已检测到登录态", _log_prefix(driver))
                return password
            if not _is_signup_password_page(driver):
                return password
            time.sleep(0.5)
        return password
    logger.info("%s 未检测到密码页，继续后续流程 last=%s", _log_prefix(driver), last)
    return None


def _accept_profile_consents(driver) -> int:
    """about-you/profile 下出现韩国/日本个人信息同意协议时，默认全部勾选。

    不依赖可见文字；优先处理 allCheckboxes，再处理所有必选 consent checkbox。
    """
    try:
        result = driver.execute_script(r"""
        const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none'
          && !el.disabled;
        const isChecked = el => el.checked === true || String(el.getAttribute('aria-checked') || el.closest('[role="checkbox"]')?.getAttribute('aria-checked') || '').toLowerCase() === 'true';
        const mark = el => {
          if (!el || isChecked(el)) return false;
          const label = el.closest('label');
          try {
            (label && visible(label) ? label : el).scrollIntoView({block:'center'});
            (label && visible(label) ? label : el).click();
          } catch (_) {}
          if (!isChecked(el)) {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked')?.set;
            if (setter) setter.call(el, true); else el.checked = true;
            el.dispatchEvent(new MouseEvent('click', {bubbles:true}));
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
          }
          return isChecked(el);
        };
        const all = [...document.querySelectorAll('input[type="checkbox"]')]
          .filter(el => visible(el) || visible(el.closest('label')));
        if (!all.length) return {count:0, names:[]};
        const byName = name => all.find(el => String(el.name || '').toLowerCase() === name.toLowerCase());
        const ordered = [];
        const add = el => { if (el && !ordered.includes(el)) ordered.push(el); };
        add(byName('allCheckboxes'));
        for (const name of ['personalInfoConsent', 'thirdPartyConsent', 'overseasTransferConsent']) add(byName(name));
        for (const el of all) {
          const n = String(el.name || '').toLowerCase();
          const id = String(el.id || '').toLowerCase();
          if (/consent|checkbox|agree|required|personal|third|overseas/.test(`${n} ${id}`)) add(el);
        }
        // about-you/profile 页面里的 checkbox 基本都是必选 consent；剩余可见 checkbox 也全部勾选。
        for (const el of all) add(el);
        const clicked = [];
        for (const el of ordered) {
          if (mark(el)) clicked.push(el.name || el.id || 'checkbox');
        }
        return {count: clicked.length, names: clicked};
        """) or {}
        count = int(result.get('count') or 0)
        if count:
            logger.info("%s 已勾选 about-you/profile 同意协议复选框：%s", _log_prefix(driver), result.get('names'))
        return count
    except Exception as exc:
        logger.debug('%s 勾选 profile consent 失败：%s', _log_prefix(driver), exc)
        return 0


def _complete_profile_page(driver, name: str, birthday: str, timeout: int = 45) -> bool:
    """等待并完成姓名/生日页；若已经登录成功则返回 False，不把它当失败。"""
    end = time.time() + timeout
    y, m, d = birthday.split('-')
    from datetime import date
    today = date.today()
    age = today.year - int(y) - ((today.month, today.day) < (int(m), int(d)))
    last_snapshot = {}
    while time.time() < end:
        time.sleep(1)
        if _has_access_token(driver):
            logger.info('%s 已检测到登录态，资料页可能已跳过', _log_prefix(driver))
            return False
        snap = _page_snapshot(driver)
        last_snapshot = snap
        if not _is_profile_like(snap):
            logger.info('%s 等待资料页中：url=%s', _log_prefix(driver), snap.get('url'))
            continue

        logger.info('%s 检测到资料页，开始填写姓名生日：url=%s inputs=%s', _log_prefix(driver), snap.get('url'), snap.get('inputs'))
        name_ok = False
        # 常见单姓名字段
        for selectors in [
            ["input[name='name']", "input[name='fullName']", "input[name='full_name']", "input[autocomplete='name']"],
            ["input[placeholder*='Name']", "input[placeholder*='name']", "input[aria-label*='Name']", "input[aria-label*='name']"],
        ]:
            if _select_or_type(driver, selectors, name, timeout=3):
                logger.info("%s 已填写姓名字段：%s", _log_prefix(driver), name)
                name_ok = True
                break
        # 兼容 first/last 分开
        if not name_ok:
            parts = name.split(' ', 1)
            first = parts[0]
            last = parts[1] if len(parts) > 1 else 'User'
            first_ok = _select_or_type(driver, ["input[name='firstName']", "input[name='first_name']", "input[placeholder*='First']", "input[aria-label*='First']"], first, timeout=2)
            last_ok = _select_or_type(driver, ["input[name='lastName']", "input[name='last_name']", "input[placeholder*='Last']", "input[aria-label*='Last']"], last, timeout=2)
            name_ok = first_ok or last_ok

        birth_mode = _fill_birthday_or_age(driver, birthday, age)
        birth_ok = bool(birth_mode)
        if birth_ok:
            if birth_mode == 'age':
                logger.info("%s 已填写年龄字段：%s", _log_prefix(driver), age)
            else:
                logger.info("%s 已填写生日字段 mode=%s value=%s", _log_prefix(driver), birth_mode, birthday)

        if not name_ok or not birth_ok:
            logger.warning('%s 资料页字段未填完整 name_ok=%s birth_ok=%s snapshot=%s', _log_prefix(driver), name_ok, birth_ok, snap)
            continue

        _accept_profile_consents(driver)
        human_delay('form')
        for _ in range(3):
            if _click_if_enabled_submit(driver):
                logger.info('%s 已点击资料页提交按钮，等待 OAuth 跳转', _log_prefix(driver))
                # 给 callback/跳转一点时间；若仍停 about-you 再补点一次
                leave_end = time.time() + 18
                while time.time() < leave_end:
                    try:
                        cur = str(getattr(driver, "current_url", "") or "").lower()
                    except Exception:
                        cur = ""
                    if _has_access_token(driver):
                        logger.info("%s 资料提交后已检测到登录态", _log_prefix(driver))
                        return True
                    if "about-you" not in cur and "profile" not in cur:
                        logger.info("%s 资料页已跳转：url=%s", _log_prefix(driver), cur or "-")
                        return True
                    if "chatgpt.com" in cur:
                        logger.info("%s 资料提交后已到 ChatGPT：url=%s", _log_prefix(driver), cur)
                        return True
                    time.sleep(0.8)
                # 仍在 about-you：再提交一次
                if _click_if_enabled_submit(driver):
                    logger.info("%s 资料页仍未跳转，已补点提交，继续后续 session 流程", _log_prefix(driver))
                return True
            time.sleep(1)
        logger.warning('%s 找不到可点击的资料页提交按钮 snapshot=%s', _log_prefix(driver), _page_snapshot(driver))
    raise RuntimeError(f'等待/填写资料页超时，最后页面：{last_snapshot}')


def _click_if_enabled_submit(driver) -> bool:
    """提交资料页：优先 form.requestSubmit/button[type=submit]，不依赖按钮文字。"""
    try:
        target = driver.execute_script(r"""
        const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const forms = [...document.querySelectorAll('form')].filter(visible);
        for (const form of forms) {
          const submit = form.querySelector('button[type="submit"], input[type="submit"]');
          if (submit && visible(submit) && !submit.disabled) {
            submit.scrollIntoView({block:'center'});
            return submit;
          }
          if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
            return 'submitted_by_requestSubmit';
          }
        }
        const submitters = [...document.querySelectorAll('button[type="submit"], input[type="submit"]')]
          .filter(el => visible(el) && !el.disabled);
        if (submitters.length) {
          submitters[0].scrollIntoView({block:'center'});
          return submitters[0];
        }
        // 兜底：页面只有一个可点击 button 时点击它，但仍不读文字。
        const buttons = [...document.querySelectorAll('button:not([disabled])')].filter(visible);
        if (buttons.length === 1) {
          buttons[0].scrollIntoView({block:'center'});
          return buttons[0];
        }
        return null;
        """)
        if not target:
            return False
        if isinstance(target, str):
            return True
        _human_click(driver, target, label="profile_submit")
        return True
    except Exception:
        return False


def _collect_chatgpt_cookies(driver) -> dict[str, str]:
    """收集当前浏览器里 chatgpt.com 相关 cookie（含 HttpOnly session-token）。"""
    cookies: dict[str, str] = {}
    try:
        for item in driver.get_cookies() or []:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            cookies[name] = str(item.get("value") or "")
    except Exception:
        pass
    # CDP 可拿到更多分区/HttpOnly cookie，作为 get_cookies 的补充。
    try:
        if hasattr(driver, "execute_cdp_cmd"):
            for source in (
                {"urls": ["https://chatgpt.com", "https://auth.openai.com"]},
                {},
            ):
                try:
                    raw = driver.execute_cdp_cmd("Network.getCookies", source) or {}
                except Exception:
                    continue
                for item in raw.get("cookies") or []:
                    domain = str(item.get("domain") or "").lower()
                    if "chatgpt.com" not in domain and "openai.com" not in domain:
                        continue
                    name = str(item.get("name") or "").strip()
                    if name and name not in cookies:
                        cookies[name] = str(item.get("value") or "")
    except Exception:
        pass
    return cookies


def _cookie_auth_hint(cookies: dict[str, str]) -> dict:
    names = list(cookies.keys())
    lower_names = [n.lower() for n in names]
    return {
        "count": len(names),
        "has_session_token": any("session-token" in n for n in lower_names),
        "has_next_auth": any("next-auth" in n for n in lower_names),
        "names": names[:30],
    }


def _read_chatgpt_session_via_page(driver) -> dict | None:
    """页面内 fetch 读取 session；用绝对 URL + 超时，避免 SPA 过渡期挂死。"""
    script = r"""
    const done = arguments[0];
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort('session-timeout'), 8000);
    fetch('https://chatgpt.com/api/auth/session', {
      credentials: 'include',
      cache: 'no-store',
      headers: {'accept': 'application/json'},
      signal: ctrl.signal,
    })
      .then(async r => {
        let data = null;
        try { data = await r.json(); } catch (e) { data = {text: await r.text()}; }
        if (data && typeof data === 'object') data._http_status = r.status;
        done({ok: true, data});
      })
      .catch(e => done({ok: false, error: String(e)}))
      .finally(() => clearTimeout(timer));
    """
    try:
        try:
            driver.set_script_timeout(12)
        except Exception:
            pass
        result = driver.execute_async_script(script)
    except Exception as exc:
        logger.info("%s 页面内读取 session 异常：%s: %s", _log_prefix(driver), type(exc).__name__, str(exc)[:160])
        return None
    if result and result.get("ok"):
        data = result.get("data") or {}
        if isinstance(data, dict) and data.get("accessToken"):
            logger.info("%s /api/auth/session 已返回 accessToken（via=page）", _log_prefix(driver))
            return data
        logger.info(
            "%s 等待 ChatGPT session 写入 accessToken（via=page），keys=%s status=%s",
            _log_prefix(driver),
            list(data.keys()) if isinstance(data, dict) else type(data),
            (data or {}).get("_http_status") if isinstance(data, dict) else "-",
        )
        return data if isinstance(data, dict) else None
    if result and result.get("error"):
        logger.info("%s 页面内读取 session 失败：%s", _log_prefix(driver), str(result.get("error"))[:160])
    return None


def _read_chatgpt_session_via_cookies(driver) -> dict | None:
    """用浏览器 cookie 在 Python 侧直拉 session，绕过页面 SPA/JS 过渡期。

    实测：You're all set 点 Continue 后，UI 可能已进主站，但页面内 fetch 长时间
    只返回 WARNING_BANNER；此时 cookie 里往往已有 session-token，直拉可立刻拿到 AT。
    """
    cookies = _collect_chatgpt_cookies(driver)
    hint = _cookie_auth_hint(cookies)
    if not cookies:
        logger.info("%s cookie 兜底：当前未收集到 chatgpt cookie", _log_prefix(driver))
        return None
    if not (hint["has_session_token"] or hint["has_next_auth"]):
        logger.info(
            "%s cookie 兜底：尚未出现 session-token，cookie=%s",
            _log_prefix(driver),
            hint["names"],
        )
        return None

    ua = "Mozilla/5.0"
    try:
        ua = str(driver.execute_script("return navigator.userAgent || ''") or "") or ua
    except Exception:
        pass

    headers = {
        "accept": "application/json",
        "referer": "https://chatgpt.com/",
        "user-agent": ua,
        "cache-control": "no-cache",
    }
    try:
        # 优先 curl_cffi（与项目其它协议请求一致），失败再退 requests。
        try:
            from curl_cffi import requests as crequests
            resp = crequests.get(
                "https://chatgpt.com/api/auth/session",
                cookies=cookies,
                headers=headers,
                timeout=15,
                impersonate="chrome",
            )
            data = resp.json()
            status = getattr(resp, "status_code", None)
        except Exception:
            import requests
            resp = requests.get(
                "https://chatgpt.com/api/auth/session",
                cookies=cookies,
                headers=headers,
                timeout=15,
            )
            data = resp.json()
            status = resp.status_code
        if isinstance(data, dict) and data.get("accessToken"):
            logger.info("%s /api/auth/session 已返回 accessToken（via=cookies status=%s）", _log_prefix(driver), status)
            return data
        logger.info(
            "%s cookie 兜底仍无 accessToken：status=%s keys=%s cookie_hint=%s",
            _log_prefix(driver),
            status,
            list(data.keys()) if isinstance(data, dict) else type(data),
            {k: hint[k] for k in ("count", "has_session_token", "has_next_auth", "names")},
        )
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.info("%s cookie 兜底读取 session 失败：%s: %s", _log_prefix(driver), type(exc).__name__, str(exc)[:180])
        return None


def _read_chatgpt_session_once(driver) -> dict | None:
    """读取 /api/auth/session；优先页面 fetch，失败/无 token 时用 cookie 兜底。"""
    page_data = None
    try:
        page_data = _read_chatgpt_session_via_page(driver)
        if isinstance(page_data, dict) and page_data.get("accessToken"):
            return page_data
    except Exception as exc:
        logger.info("%s 页面 session 读取异常：%s: %s", _log_prefix(driver), type(exc).__name__, str(exc)[:160])

    cookie_data = _read_chatgpt_session_via_cookies(driver)
    if isinstance(cookie_data, dict) and cookie_data.get("accessToken"):
        return cookie_data

    # 返回任意一侧的响应，方便上层日志；没有 token 时仍视为未就绪。
    return None


def _force_chatgpt_home_for_session(driver) -> None:
    """点完 all-set Continue 后强制进主站，促发 session cookie 落稳。"""
    try:
        logger.info("%s 已点 Continue，强制打开 chatgpt.com 主站以落稳 session", _log_prefix(driver))
        _safe_get(driver, "https://chatgpt.com/", timeout=25, attempts=2, accept_hosts=("chatgpt.com",))
        time.sleep(1.2)
    except Exception as exc:
        logger.warning("%s 强制打开 chatgpt.com 失败：%s: %s", _log_prefix(driver), type(exc).__name__, str(exc)[:160])


def _detect_all_set_page(driver) -> dict:
    """识别注册完成后的 You're all set 中间页，并定位 Continue 按钮。

    该页上 accessToken 往往还没写进 /api/auth/session，必须点 Continue 后
    才会完成登录态落 cookie；之前只轮询 session 会一直空等，直到人工点按钮。
    """
    try:
        return driver.execute_script(r"""
        const bodyText = (document.body?.innerText || '').replace(/\s+/g, ' ').trim();
        const lower = bodyText.toLowerCase();
        const looksAllSet = (
          lower.includes("you're all set")
          || lower.includes('you are all set')
          || lower.includes('all set')
          || bodyText.includes('一切就绪')
          || bodyText.includes('全部完成')
          || bodyText.includes('已准备就绪')
        );
        const buttons = [...document.querySelectorAll('button, a[role="button"], input[type="submit"]')]
          .map(el => {
            const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
            const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            return {el, text, lower: text.toLowerCase(), visible, disabled: !!el.disabled};
          })
          .filter(x => x.visible && !x.disabled && x.text);

        // 只点独立的 Continue / 继续，避开 Continue with Google/Apple/Microsoft。
        const continueBtn = buttons.find(x => {
          if (x.lower === 'continue' || x.text === '继续' || x.lower === 'get started' || x.text === '开始使用') {
            return true;
          }
          if ((x.lower.startsWith('continue') || x.text.startsWith('继续')) &&
              !/(google|apple|microsoft|sso|github|phone|手机|电话)/i.test(x.text)) {
            return x.lower === 'continue' || /^continue\b/.test(x.lower) && x.lower.split(/\s+/).length <= 2;
          }
          return false;
        });

        return {
          ok: true,
          looksAllSet,
          bodyPreview: bodyText.slice(0, 180),
          buttonTexts: buttons.map(x => x.text).slice(0, 12),
          hasContinue: Boolean(continueBtn),
          continueText: continueBtn ? continueBtn.text : '',
        };
        """) or {"ok": False}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _click_all_set_continue_if_present(driver, *, clicked_once: bool) -> bool:
    """若在 You're all set 页，点击 Continue 以完成登录态写入。

    返回 True 表示本轮实际点过 Continue（调用方应避免重复狂点）。
    """
    if clicked_once:
        return False

    state = _detect_all_set_page(driver)
    if not state.get("ok"):
        return False
    if not (state.get("looksAllSet") or state.get("hasContinue")):
        return False
    # 没有 all-set 文案时，仅当页面按钮几乎只有 Continue 才点，降低误点风险。
    if not state.get("looksAllSet"):
        texts = [str(t or "").strip().lower() for t in (state.get("buttonTexts") or [])]
        if not texts or not all(t in ("continue", "继续", "get started", "开始使用") for t in texts if t):
            return False
    if not state.get("hasContinue"):
        logger.info(
            "%s 检测到疑似 all-set 页但未找到 Continue 按钮 body=%s buttons=%s",
            _log_prefix(driver),
            str(state.get("bodyPreview") or "")[:120],
            state.get("buttonTexts"),
        )
        return False

    try:
        target = driver.execute_script(r"""
        const buttons = [...document.querySelectorAll('button, a[role="button"], input[type="submit"]')];
        for (const el of buttons) {
          const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
          const lower = text.toLowerCase();
          const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          if (!visible || el.disabled) continue;
          if (lower === 'continue' || text === '继续' || lower === 'get started' || text === '开始使用') {
            el.scrollIntoView({block:'center'});
            return el;
          }
          if ((lower.startsWith('continue') || text.startsWith('继续')) &&
              !/(google|apple|microsoft|sso|github|phone|手机|电话)/i.test(text) &&
              lower.split(/\s+/).length <= 2) {
            el.scrollIntoView({block:'center'});
            return el;
          }
        }
        return null;
        """)
        if not target:
            return False
        logger.info(
            "%s 检测到 You're all set 页，自动点击 Continue（text=%s）以写入 session/accessToken",
            _log_prefix(driver),
            state.get("continueText") or "Continue",
        )
        _human_click(driver, target, label="all_set_continue")
        time.sleep(1.5)
        return True
    except Exception as exc:
        logger.warning("%s 自动点击 all-set Continue 失败：%s: %s", _log_prefix(driver), type(exc).__name__, str(exc)[:180])
        return False


def _switch_to_chatgpt_window_if_any(driver) -> bool:
    """有些浏览器/适配层会在新窗口完成 callback；尝试切到已有 chatgpt.com 句柄。"""
    try:
        handles = list(getattr(driver, "window_handles", []) or [])
        current_handle = None
        try:
            current_handle = getattr(driver, "current_window_handle", None)
        except Exception:
            current_handle = None
        for handle in handles:
            try:
                driver.switch_to.window(handle)
                if "chatgpt.com" in str(getattr(driver, "current_url", "") or ""):
                    return True
            except Exception:
                continue
        if current_handle is not None:
            try:
                driver.switch_to.window(current_handle)
            except Exception:
                pass
    except Exception:
        pass
    return False


def _session_diag_snapshot(driver) -> dict:
    """超时诊断：URL + 关键 cookie 是否齐全。"""
    try:
        url = str(getattr(driver, "current_url", "") or "")
    except Exception:
        url = ""
    cookies = {}
    try:
        cookies = _collect_chatgpt_cookies(driver)
    except Exception:
        cookies = {}
    hint = _cookie_auth_hint(cookies)
    return {
        "url": url,
        "cookie_count": hint.get("count"),
        "has_session_token": hint.get("has_session_token"),
        "has_next_auth": hint.get("has_next_auth"),
        "cookie_names": hint.get("names"),
    }


def _fetch_chatgpt_session(driver, timeout: int = 90, auto_jump_wait: int = 15) -> dict:
    """等待页面完成跳转并从 ChatGPT 页面内读取登录 session/accessToken。

    旧逻辑会在 auth.openai.com 上一直等到总超时，Cloak/部分 Chromium 场景下
    实际账号已创建成功但当前句柄 URL 没及时更新，导致白等 120 秒。现在只给
    自动跳转 `auto_jump_wait` 秒；超过后立即主动打开 chatgpt.com 读 session。

    另外：注册成功后常见 You're all set 中间页，该页必须点 Continue 后
    session cookie / accessToken 才会稳定写入；这里会自动点一次，并：
    1) 强制打开 chatgpt.com 主站
    2) 页面 fetch 失败/仅 WARNING_BANNER 时，用浏览器 cookie 直拉 session
    3) 若仍停 about-you，补点提交；周期性打印 cookie 诊断
    """
    end = time.time() + timeout
    auto_jump_end = time.time() + max(3, int(auto_jump_wait or 15))
    last_data = None
    forced_chatgpt_open = False
    all_set_clicked = False
    post_continue_refreshed = False
    empty_rounds = 0
    about_you_resubmits = 0
    signin_bounce_tried = False

    while time.time() < end:
        _check_manual_stop()
        try:
            current = str(driver.current_url or '')
        except Exception:
            current = ''
        current_l = current.lower()

        # 资料页未跳转：补提交，避免过早硬跳 chatgpt.com 丢 callback
        if "about-you" in current_l or ("auth.openai.com" in current_l and "profile" in current_l):
            if about_you_resubmits < 3:
                logger.info(
                    "%s 仍在资料页，补点提交等待 OAuth（%s/3）：url=%s",
                    _log_prefix(driver), about_you_resubmits + 1, current,
                )
                try:
                    _click_if_enabled_submit(driver)
                except Exception:
                    pass
                about_you_resubmits += 1
                time.sleep(2.5)
                continue

        if 'chatgpt.com' not in current_l:
            if _switch_to_chatgpt_window_if_any(driver):
                current = str(getattr(driver, "current_url", "") or "")
                current_l = current.lower()
            elif time.time() >= auto_jump_end and not forced_chatgpt_open:
                try:
                    logger.info(
                        "%s 未在 %ss 内观察到当前窗口跳转 chatgpt.com，主动打开 ChatGPT 读取 session；diag=%s",
                        _log_prefix(driver),
                        int(auto_jump_wait or 15),
                        _session_diag_snapshot(driver),
                    )
                    _safe_get(driver, "https://chatgpt.com/", timeout=35, attempts=2, accept_hosts=("chatgpt.com",))
                    forced_chatgpt_open = True
                    time.sleep(3)
                    current = str(getattr(driver, "current_url", "") or "")
                    current_l = current.lower()
                except Exception as exc:
                    last_data = f"{type(exc).__name__}: {exc}"
            else:
                time.sleep(1)
                continue

        if 'chatgpt.com' in current_l:
            # 先处理 You're all set：否则 /api/auth/session 可能一直没有 accessToken。
            if _click_all_set_continue_if_present(driver, clicked_once=all_set_clicked):
                all_set_clicked = True
                if not post_continue_refreshed:
                    _force_chatgpt_home_for_session(driver)
                    post_continue_refreshed = True
                try:
                    current = str(driver.current_url or '')
                    current_l = current.lower()
                except Exception:
                    pass
            try:
                data = _read_chatgpt_session_once(driver)
                if data and data.get("accessToken"):
                    return data
                keys = list((data or {}).keys()) if isinstance(data, dict) else []
                last_data = f"session 暂无 accessToken keys={keys}"
                empty_rounds += 1
                diag = _session_diag_snapshot(driver)
                if empty_rounds == 1 or empty_rounds % 4 == 0:
                    logger.warning(
                        "%s 仍无 accessToken（round=%s）：keys=%s diag=%s",
                        _log_prefix(driver), empty_rounds, keys, diag,
                    )
                # 点过 Continue 后仍只有 WARNING_BANNER：隔几轮再强制刷一次主站。
                if empty_rounds >= 3 and empty_rounds % 3 == 0:
                    _force_chatgpt_home_for_session(driver)
                    post_continue_refreshed = True
                # 没有 session-token 时，尝试 next-auth signin 入口促发回跳
                if (
                    empty_rounds >= 6
                    and not signin_bounce_tried
                    and not diag.get("has_session_token")
                ):
                    signin_bounce_tried = True
                    try:
                        logger.info(
                            "%s cookie 无 session-token，尝试打开 /api/auth/signin 促发登录回跳",
                            _log_prefix(driver),
                        )
                        _safe_get(
                            driver,
                            "https://chatgpt.com/api/auth/signin",
                            timeout=25,
                            attempts=1,
                            accept_hosts=("chatgpt.com", "auth.openai.com"),
                        )
                        time.sleep(2.5)
                        _safe_get(
                            driver,
                            "https://chatgpt.com/",
                            timeout=25,
                            attempts=1,
                            accept_hosts=("chatgpt.com",),
                        )
                        time.sleep(2)
                    except Exception as bounce_exc:
                        logger.info(
                            "%s signin 回跳尝试失败：%s",
                            _log_prefix(driver),
                            str(bounce_exc)[:160],
                        )
            except Exception as exc:
                last_data = f"{type(exc).__name__}: {exc}"
        time.sleep(1.2 if all_set_clicked else 2)

    diag = _session_diag_snapshot(driver)
    raise RuntimeError(
        f"等待 /api/auth/session accessToken 超时，最后响应: {str(last_data)[:500]}；diag={diag}"
    )


def _check_manual_stop() -> None:
    try:
        from core.registration_service import check_stop_requested
        check_stop_requested()
    except ImportError:
        return


def run_roxy_registration(email: str, name: str, birthday: str, proxy: str = None, otp_code: str = None, batch_dir: Path | None = None) -> dict:
    """Roxy 指纹浏览器自动化注册入口。"""
    client = RoxyBrowserClient()
    opened = client.open_profile()
    driver = None
    create_acknowledged = False
    openai_password: str | None = None
    try:
        driver = _build_driver(opened)
        _center_browser_window(driver)
        driver.set_page_load_timeout(int(_cfg.ROXY_SELENIUM_TIMEOUT))
        try:
            driver.set_script_timeout(12)
        except Exception:
            pass
        logger.info("[Roxy注册] 开始：%s，profile=%s", email, opened.profile_id)

        otp_after_ts = time.time()
        logger.info("[Roxy注册] 打开登录页：https://chatgpt.com/auth/login")
        _safe_get(
            driver,
            "https://chatgpt.com/auth/login",
            timeout=min(45, int(getattr(_cfg, "ROXY_SELENIUM_TIMEOUT", 90) or 90)),
            attempts=2,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
        human_delay("navigate")
        _page_warmup(driver, reason="login_page")
        logger.info("[Roxy注册] 登录页加载完成，准备填写邮箱")
        _maybe_accept(driver)
        _check_manual_stop()

        # 填邮箱。OpenAI UI 会随出口 IP/语言变化；这里只按 DOM 技术属性找邮箱入口，
        # 并排除 Google/Apple/Microsoft 等第三方入口，不依赖按钮可见文字。
        next_state = _submit_email_and_wait_next(driver, email, attempts=3)
        _check_manual_stop()

        # 新版注册流可能先进入 /create-account/password；参考 FlowPilot 的 fill-password 步骤，
        # 先设置密码并提交，然后再等待邮箱验证码页。
        openai_password = None if next_state == "otp" else _fill_password_page_if_present(driver, email, timeout=25)
        _check_manual_stop()

        current_otp = otp_code
        max_otp_attempts = registration_otp_attempts(email)
        for otp_attempt in range(1, max_otp_attempts + 1):
            if current_otp is None:
                logger.info("[Roxy注册][OTP] 等待验证码：%s（第 %s/%s 次）", email, otp_attempt, max_otp_attempts)
                try:
                    current_otp = wait_for_otp(email, after_ts=otp_after_ts)
                except Exception as exc:
                    if otp_attempt >= max_otp_attempts:
                        raise
                    # 兜底：OpenAI 重发验证码时常常是同一封邮件（时间戳不变），
                    # after_ts 过滤会把它当成旧邮件忽略。先宽松取最新一条验证码，
                    # 取到就直接用它重试提交，避免误点“重新发送”后死等。
                    fallback_otp = None
                    try:
                        fallback_otp = wait_for_otp(email, after_ts=0.0, max_wait=15, poll_interval=3)
                    except Exception:
                        fallback_otp = None
                    if fallback_otp:
                        logger.info(
                            "[Roxy注册][OTP] 取码接口超时但宽松取到最新验证码，直接重试提交：%s (fallback)",
                            fallback_otp,
                        )
                        current_otp = fallback_otp
                        continue
                    logger.warning(
                        "[Roxy注册][OTP] 一直未收到验证码，点击“重新发送电子邮件”后继续等待（下一轮 %s/%s）：%s: %s",
                        otp_attempt + 1,
                        max_otp_attempts,
                        type(exc).__name__,
                        str(exc)[:180],
                    )
                    otp_after_ts = time.time()
                    _click_resend_email_otp(driver, timeout=25)
                    human_delay("api")
                    current_otp = None
                    continue
            logger.info("[Roxy注册][OTP] 收到验证码：%s", current_otp)
            _clear_otp_inputs(driver)
            _type_otp(driver, current_otp)
            logger.info("[Roxy注册][OTP] 已填写邮箱验证码")
            _check_manual_stop()
            human_delay("otp_input")
            try:
                _click_continue(driver)
                logger.info("[Roxy注册][OTP] 已提交邮箱验证码，等待资料页或登录态")
            except Exception as exc:
                logger.info("[Roxy注册][OTP] 未找到显式提交按钮，继续等待页面状态：%s", str(exc)[:120])

            outcome = _wait_after_email_otp_submit(driver, timeout=30)
            if outcome == "accepted" or _otp_flow_already_passed(driver):
                if outcome != "accepted":
                    logger.info("[Roxy注册][OTP] 等待结果=%s，但已离开验证码页，按通过处理", outcome)
                break
            if outcome == "account_unusable":
                from core.openai_auth import AccountUnusableError

                info = _inspect_auth_page_failure(driver)
                code = info.get("error_code") or "account_deactivated"
                raise AccountUnusableError(
                    f"OTP 提交后账号已废 error_code={code} url={info.get('url') or getattr(driver, 'current_url', '')}",
                    error_code=code,
                )
            if outcome == "rate_limit":
                from core.openai_auth import RateLimitError

                info = _inspect_auth_page_failure(driver)
                code = info.get("error_code") or "rate_limit_exceeded"
                raise RateLimitError(
                    f"OTP 提交后 Auth 限流 error_code={code} url={info.get('url') or getattr(driver, 'current_url', '')}",
                    error_code=code,
                )
            if outcome == "route_error" or _is_auth_route_error_page(driver):
                logger.warning(
                    "[Roxy注册][OTP] 提交后路由错误（%s/%s），尝试恢复 url=%s",
                    otp_attempt, max_otp_attempts, getattr(driver, "current_url", ""),
                )
                rec = _recover_auth_route_error(driver)
                if rec.get("ok") and _otp_flow_already_passed(driver):
                    break
                if otp_attempt >= max_otp_attempts:
                    raise RuntimeError(
                        f"auth_route_error: OpenAI OTP 路由错误连续失败 url={getattr(driver, 'current_url', '')}"
                    )
                _restart_email_login_flow(driver, email)
                otp_after_ts = time.time()
                current_otp = None
                human_delay("api")
                continue
            if otp_attempt >= max_otp_attempts:
                raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
            logger.warning("[Roxy注册][OTP] 验证码错误/过期，准备重新发送并重新获取验证码（%s/%s）", otp_attempt + 1, max_otp_attempts)
            otp_after_ts = time.time()
            try:
                resend = _click_resend_email_otp(driver, timeout=25)
            except Exception as resend_exc:
                from core.openai_auth import AccountUnusableError, RateLimitError

                if isinstance(resend_exc, (AccountUnusableError, RateLimitError)):
                    raise
                if "auth_route_error" in str(resend_exc) or _is_auth_route_error_page(driver):
                    logger.warning("[Roxy注册][OTP] 重发遇路由错误，重开邮箱登录：%s", str(resend_exc)[:160])
                    _restart_email_login_flow(driver, email)
                    otp_after_ts = time.time()
                    current_otp = None
                    human_delay("api")
                    continue
                raise
            if resend.get("skipped"):
                logger.info("[Roxy注册][OTP] 重发前发现已离开验证码页，按 OTP 通过继续")
                break
            human_delay("api")
            current_otp = None

        # about-you / profile 信息页：必须完成或确认已有登录态，不能静默跳过。
        logger.info("[Roxy注册] 开始等待资料页/登录态")
        _check_manual_stop()
        profile_submitted = _complete_profile_page(driver, name, birthday, timeout=60)
        if profile_submitted:
            create_acknowledged = True
            # 给 OAuth 回调 / session cookie 写入一点时间。
            human_delay("post_auth")

        logger.info("[Roxy注册] 等待 ChatGPT 跳转并写入 session/accessToken")
        _check_manual_stop()
        session_info = _fetch_chatgpt_session(driver, timeout=120)
        access_token = session_info["accessToken"]
        logger.info("[Roxy注册] 已拿到 accessToken：%s", email)
        _check_manual_stop()

        # 2FA：ENABLE_2FA=True 时用浏览器 cookie 走协议 enroll
        totp_secret = None
        try:
            from config import twofa as _twofa_live
            enable_2fa = bool(getattr(_twofa_live, "ENABLE_2FA", False))
        except Exception:
            enable_2fa = bool(getattr(_twofa_cfg, "ENABLE_2FA", False))
        if enable_2fa:
            try:
                from core.account_export import setup_2fa_from_browser_driver
                logger.info("[Roxy注册] ENABLE_2FA=True，开始设置 TOTP 2FA")
                totp_secret = setup_2fa_from_browser_driver(
                    driver,
                    email,
                    proxy=proxy,
                )
                logger.info(
                    "[Roxy注册] 2FA 设置完成 secret=%s...%s",
                    (totp_secret or "")[:4],
                    (totp_secret or "")[-4:],
                )
            except Exception as exc:
                logger.error("[Roxy注册] 2FA 设置失败：%s: %s", type(exc).__name__, exc)
                logger.debug("[Roxy注册] 2FA 失败详情", exc_info=True)
                logger.warning("[Roxy注册] 将继续保存账号（不含 TOTP secret），可后续手动设置")
                totp_secret = None
        else:
            logger.info("[Roxy注册] 已跳过 2FA 设置 (ENABLE_2FA=False)")

        codex_result = {
            "status": "skipped",
            "ok": True,
            "message": "ENABLE_CODEX_AUTO=False，跳过 Codex",
        }
        try:
            from config import codex as _codex_cfg
            if bool(getattr(_codex_cfg, "ENABLE_CODEX_AUTO", False)):
                # 注册流程本身已创建 Roxy 一号一环境。这里不能再新建第二个 Roxy 环境；
                # 复用当前注册窗口，先清理 Cookie/session/localStorage/cache，再开始 Codex 授权。
                from core.roxy_codex_oauth import run_roxy_codex_oauth
                logger.info("[Roxy注册][Codex] ENABLE_CODEX_AUTO=True，复用当前注册 Roxy 窗口执行 Codex 授权，不创建新环境")
                _check_manual_stop()
                codex_result = run_roxy_codex_oauth(
                    email,
                    reuse_existing_profile=True,
                    existing_driver=driver,
                    existing_opened=opened,
                    force=True,
                    clear_existing_state=True,
                )
            else:
                logger.info("[Roxy注册][Codex] ENABLE_CODEX_AUTO=False，注册后跳过 Codex OAuth")
        except Exception as exc:
            codex_result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {str(exc)[:180]}"}

        account_id = save_account_data(
            email=email,
            access_token=access_token,
            totp_secret=totp_secret,
            email_source=resolve_email_source(email),
            proxy_used=proxy or None,
            batch_dir=batch_dir,
            extra={
                "user": session_info.get("user"),
                "account": session_info.get("account"),
                "expires": session_info.get("expires"),
                "roxybrowser": {"profile_id": opened.profile_id, "open_result": opened.raw},
                "registration_password": openai_password,
                "codex": codex_result,
            },
        )
        post_register_dwell(email, label="Roxy注册")
        codex_ok = codex_result.get("ok") or codex_result.get("status") == "skipped"
        return {
            "success": bool(codex_ok),
            "email": email,
            "account_id": account_id,
            "access_token": access_token,
            "totp_secret": totp_secret,
            "codex": codex_result,
            "error": None if codex_ok else f"Codex 未完成: {codex_result.get('message')}",
        }
    except Exception as exc:
        logger.error("[Roxy注册] 失败：%s: %s", type(exc).__name__, exc)
        logger.debug("[Roxy注册] 失败详情", exc_info=True)
        err_text = f"{type(exc).__name__}: {str(exc)[:300]}"
        # 废号立即 disabled；可重试失败保持 used 供任务层重试；已创建则 failed。
        try:
            from core.email_provider import release_email, mark_mailcom_otp_miss
            from core.openai_auth import (
                AccountUnusableError,
                RateLimitError,
                should_disable_registration_email_for_error,
            )

            if isinstance(exc, AccountUnusableError) or should_disable_registration_email_for_error(exc):
                release_email(email, status="disabled", note=f"自动停用: {str(exc)[:180]}")
            elif create_acknowledged:
                release_email(email, status="failed", note=f"Roxy注册失败: {str(exc)[:180]}")
            elif mark_mailcom_otp_miss(email, err_text):
                logger.warning("[Roxy注册] mail.com 收不到验证码，已标失败：%s", email)
            elif isinstance(exc, RateLimitError):
                logger.info("[Roxy注册] 限流失败，保持邮箱 used 供任务重试：%s", email)
            else:
                logger.info("[Roxy注册] 可重试失败，保持邮箱 used：%s (%s)", email, type(exc).__name__)
        except Exception:
            pass
        return {"success": False, "email": email, "error": err_text}
    finally:
        if driver and not bool(_cfg.ROXY_KEEP_BROWSER_OPEN):
            try:
                driver.quit()
            except Exception:
                pass
        if not bool(_cfg.ROXY_KEEP_BROWSER_OPEN):
            client.cleanup_profile(opened)
