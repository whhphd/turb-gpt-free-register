# -*- coding: utf-8 -*-
"""注册 + GCash 提链交互会话（Cloak 保活浏览器）。

Playwright 同步 API 有线程亲和：创建 browser 的线程必须继续操作它。
因此本模块用「浏览器归属线程 + 命令队列」：
  - 注册、打开支付链、抽码、关闭 都在同一线程执行
  - Flask 请求线程只投递命令并等待结果
"""
from __future__ import annotations

import base64
import logging
import queue
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_QR_DIR = _PROJECT_ROOT / "data" / "gcash_qr"
_LOG_DIR = _PROJECT_ROOT / "注册日志"

_LOCK = threading.RLock()
_SESSION: dict[str, Any] | None = None
_CMD_Q: queue.Queue | None = None  # (cmd, args, result_q)
_OWNER_THREAD: threading.Thread | None = None

_ACTIVE_STATUSES = frozenset({
    "registering", "ready_at", "opening_url", "qr_ready",
})

# 支付/GCash 成功落点关键词
_PAY_OK_HINTS = (
    "gcash.com",
    "m.gcash.com",
    "merchant-auth",
    "gcashapp",
    "scan the qr",
    "link gcash",
)
# 这些 URL 不能当成支付成功
_PAY_BAD_SETTLE = (
    "chatgpt.com",
    "chat.openai.com",
    "auth.openai.com",
    "about:blank",
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _append_log(sess: dict[str, Any], line: str) -> None:
    msg = f"{_stamp()} {line}"
    logs = sess.setdefault("logs", [])
    logs.append(msg)
    if len(logs) > 200:
        del logs[:-200]
    sess["updated_at"] = _now()
    try:
        sid = str(sess.get("session_id") or "unknown")
        p = _LOG_DIR / f"gcash-session-{sid}.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass
    logger.info("[GCash会话] %s", line)


def _public_view(sess: dict[str, Any] | None) -> dict[str, Any]:
    if not sess:
        return {"active": False, "status": "idle"}
    qr_b64 = ""
    qr_path = str(sess.get("qr_path") or "")
    if qr_path and Path(qr_path).is_file():
        try:
            qr_b64 = base64.b64encode(Path(qr_path).read_bytes()).decode("ascii")
        except Exception:
            qr_b64 = ""
    return {
        "active": str(sess.get("status") or "") in _ACTIVE_STATUSES,
        "session_id": sess.get("session_id"),
        "status": sess.get("status"),
        "email": sess.get("email") or "",
        "account_id": sess.get("account_id"),
        "has_access_token": bool(sess.get("access_token")),
        "access_token_preview": (str(sess.get("access_token") or "")[:20] + "…")
        if sess.get("access_token") else "",
        "payment_url": sess.get("payment_url") or "",
        "page_url": sess.get("page_url") or "",
        "qr_ready": bool(sess.get("qr_path") and Path(str(sess.get("qr_path"))).is_file()),
        "qr_path": sess.get("qr_path") or "",
        "qr_base64": qr_b64,
        "error": sess.get("error") or "",
        "message": sess.get("message") or "",
        "created_at": sess.get("created_at"),
        "updated_at": sess.get("updated_at"),
        "log_tail": list(sess.get("logs") or [])[-40:],
    }


def get_session() -> dict[str, Any]:
    with _LOCK:
        return _public_view(_SESSION)


def get_access_token() -> dict[str, Any]:
    with _LOCK:
        sess = _SESSION
        if not sess:
            return {"ok": False, "error": "无活跃会话"}
        token = str(sess.get("access_token") or "").strip()
        if not token:
            return {"ok": False, "error": f"当前状态无 AT（status={sess.get('status')}）"}
        return {
            "ok": True,
            "access_token": token,
            "email": sess.get("email") or "",
            "account_id": sess.get("account_id"),
            "status": sess.get("status"),
        }


def _call_owner(cmd: str, args: dict | None = None, *, timeout: float = 180.0) -> dict[str, Any]:
    """向浏览器归属线程投递命令并等待结果。"""
    global _CMD_Q
    with _LOCK:
        q = _CMD_Q
        sess = _SESSION
        if not q or not sess:
            return {"ok": False, "error": "无活跃会话/命令队列"}
        if str(sess.get("status") or "") == "registering" and cmd not in ("close",):
            return {"ok": False, "error": "仍在注册中，请稍候"}
    result_q: queue.Queue = queue.Queue(maxsize=1)
    try:
        q.put((cmd, args or {}, result_q), timeout=5)
    except queue.Full:
        return {"ok": False, "error": "命令队列忙，请稍后重试"}
    try:
        return result_q.get(timeout=timeout)
    except queue.Empty:
        return {"ok": False, "error": f"命令超时（{timeout:.0f}s）：{cmd}"}


def _switch_to_newest_page(driver) -> str:
    try:
        pages = []
        ctx = getattr(driver, "context", None)
        if ctx is not None:
            pages = list(getattr(ctx, "pages", []) or [])
        if not pages and getattr(driver, "browser", None) is not None:
            for c in list(getattr(driver.browser, "contexts", []) or []):
                pages.extend(list(getattr(c, "pages", []) or []))
        if pages:
            driver.page = pages[-1]
            try:
                driver.page.bring_to_front()
            except Exception:
                pass
    except Exception:
        pass
    try:
        return str(getattr(driver, "current_url", "") or getattr(driver.page, "url", "") or "")
    except Exception:
        return ""


def _page_text(driver) -> str:
    try:
        return str(driver.page.inner_text("body", timeout=2500) or "")
    except Exception:
        try:
            return str(driver.page.content() or "")[:8000]
        except Exception:
            return ""


def _looks_pay_ok(url: str, text: str = "") -> bool:
    blob = f"{url} {text}".lower()
    if any(b in blob for b in _PAY_BAD_SETTLE) and not any(h in blob for h in ("gcash", "merchant-auth")):
        # 纯 chatgpt 不算成功
        if "gcash" not in blob and "merchant-auth" not in blob:
            return False
    return any(h in blob for h in _PAY_OK_HINTS)


def _wait_payment_page(driver, *, timeout: float = 90.0) -> str:
    """等待跳到 GCash/扫码页；不要把 chatgpt.com 当成功。"""
    end = time.time() + max(15.0, timeout)
    last_url = ""
    last_log = 0.0
    while time.time() < end:
        url = _switch_to_newest_page(driver)
        if url != last_url:
            last_url = url
            with _LOCK:
                if _SESSION:
                    _append_log(_SESSION, f"导航中 url={url[:220]}")
        text = ""
        low = url.lower()
        # 已离开 chatgpt 再读正文
        if not any(b in low for b in _PAY_BAD_SETTLE) or "gcash" in low:
            text = _page_text(driver)
        if _looks_pay_ok(url, text):
            # 稳定 1.2s
            time.sleep(1.2)
            url2 = _switch_to_newest_page(driver)
            text2 = _page_text(driver)
            if _looks_pay_ok(url2, text2):
                return url2
        # Adyen 中间跳转：继续等
        if time.time() - last_log > 8:
            last_log = time.time()
            with _LOCK:
                if _SESSION:
                    _append_log(_SESSION, f"仍在等待 GCash 页… url={url[:180]}")
        time.sleep(0.5)
    return last_url or _switch_to_newest_page(driver)


def _extract_qr_png(driver, *, session_id: str) -> Path:
    _QR_DIR.mkdir(parents=True, exist_ok=True)
    page = getattr(driver, "page", None)
    if page is None:
        raise RuntimeError("driver 无 page，无法截图")

    out = _QR_DIR / f"{session_id}.png"
    last_err: Exception | None = None
    selectors = [
        "canvas",
        "img[src*='qr' i]",
        "img[alt*='QR' i]",
        "img[alt*='qr' i]",
        "img[src^='data:image']",
        "[class*='qr' i] canvas",
        "[class*='qr' i] img",
        "[id*='qr' i] canvas",
        "[id*='qr' i] img",
        "main canvas",
        "main img",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = int(loc.count())
        except Exception as exc:
            last_err = exc
            continue
        for i in range(min(n, 12)):
            try:
                el = loc.nth(i)
                if not el.is_visible(timeout=800):
                    continue
                box = el.bounding_box()
                if not box:
                    continue
                w, h = float(box.get("width") or 0), float(box.get("height") or 0)
                if w < 120 or h < 120:
                    continue
                ratio = w / h if h else 0
                if ratio < 0.7 or ratio > 1.4:
                    continue
                el.screenshot(path=str(out), type="png")
                if out.is_file() and out.stat().st_size > 500:
                    return out
            except Exception as exc:
                last_err = exc
                continue

    try:
        handle = page.evaluate_handle(
            """() => {
              const nodes = [...document.querySelectorAll('canvas, img')];
              let best = null, bestArea = 0;
              for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (r.width < 120 || r.height < 120) continue;
                const ratio = r.width / r.height;
                if (ratio < 0.7 || ratio > 1.4) continue;
                const style = getComputedStyle(el);
                if (style.visibility === 'hidden' || style.display === 'none') continue;
                const area = r.width * r.height;
                if (area > bestArea) { bestArea = area; best = el; }
              }
              return best;
            }"""
        )
        el_handle = handle.as_element() if handle else None
        if el_handle is not None:
            el_handle.screenshot(path=str(out), type="png")
            if out.is_file() and out.stat().st_size > 500:
                return out
    except Exception as exc:
        last_err = exc

    # 整页截图兜底（至少能给人看）
    try:
        page.screenshot(path=str(out), type="png", full_page=False)
        if out.is_file() and out.stat().st_size > 500:
            return out
    except Exception as exc:
        last_err = exc

    raise RuntimeError(f"未能提取二维码: {last_err or 'no candidate'}")


def _open_url_in_new_tab(driver, url: str, append_log: Callable[[str], None]) -> Any:
    """在同一 browser context 新建第二个标签打开链接，保留原 ChatGPT 页。

    返回新 page 对象；失败时抛错。
    """
    ctx = getattr(driver, "context", None)
    old_page = getattr(driver, "page", None)
    old_url = ""
    try:
        old_url = str(getattr(old_page, "url", "") or "")
    except Exception:
        old_url = ""

    pages_before = []
    try:
        if ctx is not None:
            pages_before = list(ctx.pages)
    except Exception:
        pages_before = [old_page] if old_page is not None else []

    new_page = None
    # 1) 优先 context.new_page()（同上下文，共享 cookie/登录态）
    if ctx is not None and hasattr(ctx, "new_page"):
        try:
            new_page = ctx.new_page()
            append_log(f"已新建标签页（context.new_page），原页保留 url={old_url[:120]}")
        except Exception as exc:
            append_log(f"context.new_page 失败，改试 window.open：{type(exc).__name__}: {str(exc)[:120]}")
            new_page = None

    # 2) 回退：在当前页 window.open，再接管新 page
    if new_page is None and old_page is not None and ctx is not None:
        try:
            with ctx.expect_page(timeout=15000) as page_info:
                old_page.evaluate(
                    """(u) => { window.open(u, '_blank', 'noopener,noreferrer'); }""",
                    url,
                )
            new_page = page_info.value
            append_log("已通过 window.open 打开新窗口/标签")
        except Exception as exc:
            append_log(f"window.open 失败：{type(exc).__name__}: {str(exc)[:140]}")
            # 再轮询是否出现新 page
            time.sleep(1.0)
            try:
                pages_now = list(ctx.pages)
                if len(pages_now) > len(pages_before):
                    new_page = pages_now[-1]
                    append_log(f"轮询到新标签 pages={len(pages_now)}")
            except Exception:
                pass

    if new_page is None:
        # 最后兜底：同页跳转（不推荐，但总比彻底失败好）
        append_log("警告：无法新建标签，回退为当前页打开（可能离开 ChatGPT）")
        driver.get(url)
        return getattr(driver, "page", None)

    # 在新标签导航
    try:
        new_page.bring_to_front()
    except Exception:
        pass
    try:
        timeout_ms = int(getattr(driver, "_page_load_timeout_ms", 90000) or 90000)
        new_page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        append_log(f"新标签 goto 异常（继续等跳转）：{type(exc).__name__}: {str(exc)[:160]}")

    # 操作焦点切到新标签（抽码只看这一页；原 ChatGPT 标签仍在）
    driver.page = new_page
    try:
        n = len(list(ctx.pages)) if ctx is not None else "?"
    except Exception:
        n = "?"
    append_log(f"焦点已切到支付标签，当前共 {n} 个标签；原 ChatGPT 页未关闭")
    return new_page


def _owner_open_url(sess: dict[str, Any], url: str) -> dict[str, Any]:
    driver = sess.get("driver")
    if not driver:
        return {"ok": False, "error": "浏览器不存在"}
    sess["status"] = "opening_url"
    sess["payment_url"] = url
    sess["message"] = "正在新标签打开支付链接…"
    sess["error"] = ""
    _append_log(sess, f"打开支付链接（新标签）：{url[:200]}")

    def _log(line: str) -> None:
        _append_log(sess, line)

    try:
        _open_url_in_new_tab(driver, url, _log)

        page_url = _wait_payment_page(driver, timeout=90.0)
        sess["page_url"] = page_url
        _append_log(sess, f"等待结束 url={page_url[:220]}")

        if not _looks_pay_ok(page_url, _page_text(driver)):
            # 列出所有标签 URL 便于诊断
            try:
                ctx = getattr(driver, "context", None)
                urls = []
                if ctx is not None:
                    for i, p in enumerate(list(ctx.pages)):
                        try:
                            urls.append(f"[{i}]{(p.url or '')[:100]}")
                        except Exception:
                            urls.append(f"[{i}]?")
                _append_log(sess, "各标签: " + " | ".join(urls[:8]))
            except Exception:
                pass
            raise RuntimeError(
                f"支付页未到达 GCash（当前焦点页 {page_url[:160]}）。"
                f"原 ChatGPT 标签应仍保留；可换新链接重试。"
            )

        time.sleep(1.2)
        _switch_to_newest_page(driver)
        sid = str(sess.get("session_id") or "qr")
        qr_path = _extract_qr_png(driver, session_id=sid)
        sess["qr_path"] = str(qr_path)
        sess["status"] = "qr_ready"
        sess["page_url"] = str(getattr(driver, "current_url", "") or page_url)
        sess["message"] = "二维码已提取（新标签）；可下载/复制后扫码；到账后请点「确认关闭浏览器」"
        sess["error"] = ""
        _append_log(sess, f"二维码已保存：{qr_path}")
        return {"ok": True, "session": _public_view(sess)}
    except Exception as exc:
        logger.exception("[GCash会话] 打开链接/抽码失败")
        sess["status"] = "ready_at"
        sess["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
        sess["message"] = "打开链接或提取二维码失败，可修改链接重试"
        _append_log(sess, f"打开/抽码失败：{sess['error']}")
        return {"ok": False, "error": sess["error"], "session": _public_view(sess)}


def _owner_refresh_qr(sess: dict[str, Any]) -> dict[str, Any]:
    driver = sess.get("driver")
    if not driver:
        return {"ok": False, "error": "浏览器不存在"}
    _append_log(sess, "重新提取二维码…")
    try:
        _switch_to_newest_page(driver)
        sid = str(sess.get("session_id") or "qr")
        qr_path = _extract_qr_png(driver, session_id=sid)
        sess["qr_path"] = str(qr_path)
        sess["status"] = "qr_ready"
        sess["page_url"] = str(getattr(driver, "current_url", "") or "")
        sess["error"] = ""
        sess["message"] = "二维码已更新"
        _append_log(sess, f"二维码已更新：{qr_path}")
        return {"ok": True, "session": _public_view(sess)}
    except Exception as exc:
        sess["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        _append_log(sess, f"重抽二维码失败：{sess['error']}")
        return {"ok": False, "error": sess["error"], "session": _public_view(sess)}


def _owner_close(sess: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    driver = sess.get("driver")
    _append_log(sess, f"关闭浏览器 force={force}")
    if driver is not None:
        try:
            driver.quit()
        except Exception as exc:
            logger.warning("[GCash会话] driver.quit 异常：%s", exc)
    sess["driver"] = None
    sess["opened"] = None
    sess["status"] = "closed"
    sess["message"] = "浏览器已关闭，会话结束"
    sess["updated_at"] = _now()
    _append_log(sess, "会话已关闭")
    return {"ok": True, "session": _public_view(sess)}


def _owner_loop(sess: dict[str, Any], cmd_q: queue.Queue) -> None:
    """归属线程：先注册，再处理命令直到 close/exit。"""
    global _SESSION, _CMD_Q, _OWNER_THREAD
    driver = None
    try:
        from config import roxybrowser as _roxy_cfg
        driver_mode = str(getattr(_roxy_cfg, "REGISTRATION_DRIVER", "protocol") or "protocol").strip().lower()
        if driver_mode not in ("cloak", "cloakbrowser"):
            raise RuntimeError(f"GCash 提链需要 REGISTRATION_DRIVER=cloak，当前={driver_mode!r}")

        from core.registration_service import _prepare_registration_args
        from core.profile_utils import generate_random_birthday
        from core.cloakbrowser_registration import run_cloak_registration

        with _LOCK:
            _append_log(sess, "领取邮箱并开始 Cloak 注册（keep_browser + skip_codex）…")
        email, name, _ = _prepare_registration_args()
        birthday = generate_random_birthday()
        with _LOCK:
            sess["email"] = email
            sess["message"] = f"正在注册 {email}"
            _append_log(sess, f"邮箱={email} name={name}")

        result = run_cloak_registration(
            email=email,
            name=name or "User",
            birthday=birthday,
            keep_browser=True,
            skip_codex=True,
        )
        if not isinstance(result, dict) or not result.get("success"):
            err = (result or {}).get("error") if isinstance(result, dict) else "unknown"
            drv = result.get("driver") if isinstance(result, dict) else None
            if drv is not None:
                try:
                    drv.quit()
                except Exception:
                    pass
            raise RuntimeError(str(err or "注册失败"))

        driver = result.get("driver")
        with _LOCK:
            if sess.get("abort"):
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                sess["driver"] = None
                sess["status"] = "closed"
                sess["access_token"] = result.get("access_token") or ""
                sess["account_id"] = result.get("account_id")
                sess["email"] = result.get("email") or email
                sess["message"] = "注册完成但用户已中止，浏览器已关闭"
                _append_log(sess, "abort=True，注册成功后立即关闭浏览器")
                return

            sess["status"] = "ready_at"
            sess["email"] = result.get("email") or email
            sess["account_id"] = result.get("account_id")
            sess["access_token"] = result.get("access_token") or ""
            sess["driver"] = driver
            sess["opened"] = result.get("opened")
            sess["message"] = "注册成功，浏览器已保活。请复制 AT 去第三方提链。"
            sess["error"] = ""
            _append_log(
                sess,
                f"注册成功 account_id={sess.get('account_id')} AT_len={len(str(sess.get('access_token') or ''))}",
            )

        # 命令循环（同一线程操作 Playwright）
        while True:
            try:
                cmd, args, result_q = cmd_q.get(timeout=1.0)
            except queue.Empty:
                with _LOCK:
                    if sess.get("abort") and str(sess.get("status")) != "closed":
                        res = _owner_close(sess, force=True)
                        try:
                            # 没有等待方
                            pass
                        except Exception:
                            pass
                        break
                continue

            try:
                if cmd == "open_url":
                    res = _owner_open_url(sess, str(args.get("url") or ""))
                elif cmd == "refresh_qr":
                    res = _owner_refresh_qr(sess)
                elif cmd == "close":
                    res = _owner_close(sess, force=bool(args.get("force")))
                elif cmd == "ping":
                    res = {"ok": True, "session": _public_view(sess)}
                else:
                    res = {"ok": False, "error": f"未知命令: {cmd}"}
            except Exception as exc:
                logger.exception("[GCash会话] 命令执行异常 cmd=%s", cmd)
                res = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "session": _public_view(sess)}

            try:
                result_q.put(res)
            except Exception:
                pass

            if cmd == "close" or str(sess.get("status")) == "closed":
                break
    except Exception as exc:
        logger.exception("[GCash会话] 归属线程失败")
        with _LOCK:
            try:
                if sess.get("driver"):
                    sess["driver"].quit()
            except Exception:
                pass
            if driver is not None and sess.get("driver") is None:
                try:
                    driver.quit()
                except Exception:
                    pass
            sess["driver"] = None
            sess["status"] = "failed"
            sess["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
            sess["message"] = "注册失败"
            _append_log(sess, f"失败：{sess['error']}")
    finally:
        with _LOCK:
            # 若队列里还有等待方，全部失败返回
            q = _CMD_Q
            if q is not None:
                while True:
                    try:
                        _cmd, _args, rq = q.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        rq.put({"ok": False, "error": "会话已结束", "session": _public_view(sess)})
                    except Exception:
                        pass
            if _OWNER_THREAD is threading.current_thread():
                _CMD_Q = None
                _OWNER_THREAD = None


def start_session() -> dict[str, Any]:
    """启动一次注册+GCash 会话（全局同时只能有一个活跃会话）。"""
    global _SESSION, _CMD_Q, _OWNER_THREAD
    with _LOCK:
        if _SESSION and str(_SESSION.get("status") or "") in _ACTIVE_STATUSES:
            return {
                "ok": False,
                "error": f"已有进行中的会话 status={_SESSION.get('status')} email={_SESSION.get('email')}",
                "session": _public_view(_SESSION),
            }
        try:
            from config import roxybrowser as _roxy_cfg
            mode = str(getattr(_roxy_cfg, "REGISTRATION_DRIVER", "") or "").strip().lower()
        except Exception:
            mode = ""
        if mode not in ("cloak", "cloakbrowser"):
            return {
                "ok": False,
                "error": f"当前 REGISTRATION_DRIVER={mode!r}，GCash 提链仅支持 cloak",
            }

        # 旧 closed 会话可覆盖
        cmd_q: queue.Queue = queue.Queue()
        sess = {
            "session_id": uuid.uuid4().hex[:12],
            "status": "registering",
            "email": "",
            "account_id": None,
            "access_token": "",
            "payment_url": "",
            "page_url": "",
            "qr_path": "",
            "error": "",
            "message": "正在注册…",
            "created_at": _now(),
            "updated_at": _now(),
            "logs": [],
            "driver": None,
            "opened": None,
            "abort": False,
        }
        _append_log(sess, "会话创建")
        _SESSION = sess
        _CMD_Q = cmd_q
        t = threading.Thread(target=_owner_loop, args=(sess, cmd_q), name="gcash-browser-owner", daemon=True)
        _OWNER_THREAD = t
        t.start()
        return {"ok": True, "session": _public_view(sess)}


def open_payment_url(url: str) -> dict[str, Any]:
    url = str(url or "").strip()
    if not url:
        return {"ok": False, "error": "payment_url 为空"}
    if not re.match(r"^https?://", url, re.I):
        return {"ok": False, "error": "链接必须以 http:// 或 https:// 开头"}
    return _call_owner("open_url", {"url": url}, timeout=150.0)


def refresh_qr() -> dict[str, Any]:
    return _call_owner("refresh_qr", {}, timeout=60.0)


def close_session(*, force: bool = False) -> dict[str, Any]:
    with _LOCK:
        sess = _SESSION
        if not sess:
            return {"ok": True, "session": _public_view(None), "message": "无会话"}
        if str(sess.get("status")) == "registering":
            if not force:
                return {
                    "ok": False,
                    "error": "仍在注册中，若要强制结束请 force=true",
                    "session": _public_view(sess),
                }
            sess["abort"] = True
            sess["message"] = "已请求中止，等待注册线程结束后关浏览器…"
            _append_log(sess, "force close：设置 abort")
            # 注册完成后归属循环会处理；若已在命令循环则投 close
    # 尝试投递 close（注册中队列也可能稍后处理）
    res = _call_owner("close", {"force": force}, timeout=60.0)
    if not res.get("ok") and "无活跃" in str(res.get("error") or ""):
        # 注册线程可能已自行关闭
        with _LOCK:
            return {"ok": True, "session": _public_view(_SESSION), "message": res.get("error")}
    return res


def qr_file_path() -> Path | None:
    with _LOCK:
        if not _SESSION:
            return None
        p = Path(str(_SESSION.get("qr_path") or ""))
        return p if p.is_file() else None
