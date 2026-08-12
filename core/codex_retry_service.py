# -*- coding: utf-8 -*-
"""Codex 授权补跑服务，供账号页和注册任务队列共同使用。"""
import ctypes
import logging
import threading
import time
from pathlib import Path

from core import db

logger = logging.getLogger(__name__)

_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"
_RETRYING: set[str] = set()
_RETRYING_LOCK = threading.Lock()
_STOP_REQUESTED: set[str] = set()
_RUNNING_THREADS: dict[str, int] = {}
_RESERVED_AT: dict[str, float] = {}


class CodexRetryStopped(Exception):
    """用户手动停止 Codex 补跑。"""


def _thread_alive(thread_id: int | None) -> bool:
    if not thread_id:
        return False
    try:
        tid = int(thread_id)
    except Exception:
        return False
    return any(getattr(t, "ident", None) == tid and t.is_alive() for t in threading.enumerate())


def _clear_state_locked(key: str) -> None:
    _RETRYING.discard(key)
    _RUNNING_THREADS.pop(key, None)
    _RESERVED_AT.pop(key, None)


def log_path(email: str) -> Path:
    safe = email.replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"codex-retry-{safe}.log"


def reserve(email: str) -> bool:
    """进程内防止同一账号被重复补跑。"""
    key = (email or "").strip().lower()
    if not key:
        return False
    with _RETRYING_LOCK:
        if key in _RETRYING:
            thread_id = _RUNNING_THREADS.get(key)
            alive = _thread_alive(thread_id)
            age = time.time() - float(_RESERVED_AT.get(key) or 0)
            stop_req = key in _STOP_REQUESTED
            try:
                acc = db.get_account_by_email(email)
                status = str((acc or {}).get("codex_status") or "").lower()
            except Exception:
                status = ""
            # 修复“实际已停止/线程已结束，但进程内占位未释放”导致无法再次补跑。
            # 用户点停止后，部分浏览器/短信等待步骤可能不会立刻退出，UI 已是 stopped 但进程占位仍在。
            # 这种场景允许清理占位后重新补跑；旧线程仍保留 stop_requested，会在检查点退出。
            terminal_status = status in {"stopped", "failed", "success", "deactivated", "skipped", "cancelled"}
            if ((not alive) and (status != "retrying" or age > 15 * 60)) or (terminal_status and (stop_req or age > 30)):
                logger.warning(
                    "[Codex 补跑] 清理脏占位：email=%s status=%s thread_id=%s alive=%s stop_requested=%s age=%.1fs",
                    email, status or "-", thread_id or "-", alive, stop_req, age,
                )
                _clear_state_locked(key)
            else:
                return False
        _STOP_REQUESTED.discard(key)
        _RUNNING_THREADS.pop(key, None)
        _RETRYING.add(key)
        _RESERVED_AT[key] = time.time()
        return True


def release(email: str) -> None:
    key = (email or "").strip().lower()
    with _RETRYING_LOCK:
        _clear_state_locked(key)


def is_retrying(email: str) -> bool:
    with _RETRYING_LOCK:
        return (email or "").strip().lower() in _RETRYING


def is_stop_requested(email: str) -> bool:
    with _RETRYING_LOCK:
        return (email or "").strip().lower() in _STOP_REQUESTED


def check_stop_requested(email: str) -> None:
    if is_stop_requested(email):
        raise CodexRetryStopped("用户手动停止 Codex 补跑")


def _async_raise(thread_id: int, exc_type: type[BaseException]) -> bool:
    """向指定 Python 线程注入异常，用于尽快中断阻塞中的补跑流程。"""
    if not thread_id:
        return False
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_long(thread_id),
        ctypes.py_object(exc_type),
    )
    if res == 0:
        return False
    if res != 1:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(thread_id), None)
        return False
    return True


def request_stop(email: str) -> dict:
    """请求停止单个 Codex 补跑。运行中会注入停止异常；排队中会在启动前退出。"""
    key = (email or "").strip().lower()
    if not key:
        return {"ok": False, "error": "email 为空", "status": 400}
    with _RETRYING_LOCK:
        retrying = key in _RETRYING
        thread_id = _RUNNING_THREADS.get(key)
        _STOP_REQUESTED.add(key)
    if not retrying:
        db.update_account_codex_status(email, "stopped", "用户手动停止（未发现运行中的补跑）")
        return {"ok": True, "message": "未发现运行中的补跑，已标记为已停止", "state": "stopped", "running": False}

    injected = bool(thread_id and _async_raise(int(thread_id), CodexRetryStopped))
    db.update_account_codex_status(email, "stopped", "用户手动停止 Codex 补跑")
    # 如果没有可注入的存活线程，立即释放进程内占位，避免 UI 显示已停止但再次补跑仍 409。
    with _RETRYING_LOCK:
        if not _thread_alive(thread_id):
            _clear_state_locked(key)
    if injected:
        # 异常注入通常会很快让线程进入 finally/release；若浏览器/CDP/短信等待阻塞导致线程
        # 短时间内仍未退出，延迟清理占位，避免 UI 已显示“已停止”但再次补跑仍 409。
        def _delayed_release() -> None:
            time.sleep(5)
            with _RETRYING_LOCK:
                if key in _RETRYING and key in _STOP_REQUESTED:
                    try:
                        acc = db.get_account_by_email(email)
                        status = str((acc or {}).get("codex_status") or "").lower()
                    except Exception:
                        status = ""
                    if status == "stopped":
                        logger.warning("[Codex 补跑] 停止后延迟释放占位：email=%s thread_id=%s", email, thread_id or "-")
                        _clear_state_locked(key)

        threading.Thread(target=_delayed_release, name=f"codex-stop-release-{key}", daemon=True).start()
    try:
        p = log_path(email)
        p.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime as _dt
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{_dt.now().strftime('%H:%M:%S')} [WARNING] [Codex 补跑] 用户手动停止，已发送停止信号 injected={injected}\n")
    except Exception:
        logger.exception("写入 Codex 停止日志失败")
    return {"ok": True, "message": "已发送停止信号", "state": "stopped", "running": True, "injected": injected}


def _codex_retry_settings() -> tuple[int, float]:
    """返回 (失败后再试次数, 重试间隔秒)。总尝试 = 1 + 再试次数。"""
    retries = 3
    delay = 2.0
    try:
        from config import codex as codex_cfg
        raw_r = getattr(codex_cfg, "CODEX_RETRY_MAX_RETRIES", None)
        if raw_r is not None and str(raw_r).strip() != "":
            retries = int(raw_r)
        raw_d = getattr(codex_cfg, "CODEX_RETRY_DELAY", None)
        if raw_d is not None and str(raw_d).strip() != "":
            delay = float(raw_d)
    except Exception:
        pass
    retries = max(0, min(10, int(retries)))
    delay = max(0.0, min(60.0, float(delay)))
    return retries, delay


def _is_terminal_codex_failure(result: dict | None) -> bool:
    """不可自动重试的终态：成功 / 停用 / 用户停止 / 废号。"""
    if not isinstance(result, dict):
        return False
    if result.get("ok"):
        return True
    status = str(result.get("status") or "").strip().lower()
    if status in ("success", "stopped", "deactivated", "skipped", "cancelled"):
        return True
    msg = str(result.get("message") or "").lower()
    if any(x in msg for x in (
        "account_deactivated",
        "account_deleted",
        "account_banned",
        "accountunusable",
        "账号已废",
        "用户手动停止",
    )):
        return True
    return False


def _retry_delay_for_codex_error(base_delay: float, result_or_error: object) -> float:
    """限流类错误加长退避；其它用基础间隔。"""
    delay = max(0.0, float(base_delay or 0.0))
    text = ""
    if isinstance(result_or_error, dict):
        text = f"{result_or_error.get('status') or ''} {result_or_error.get('message') or ''}"
    else:
        text = str(result_or_error or "")
    low = text.lower()
    if "rate_limit" in low or "too many" in low or "429" in low:
        return max(delay, 12.0)
    if "err_ssl" in low or "ssl_protocol" in low or "tunnel" in low or "connection" in low:
        return max(delay, 3.0)
    return delay


def run_worker(
    email: str,
    *,
    batch_label: str | None = None,
    clear_log: bool = True,
    target_log_path: str | Path | None = None,
    max_retries: int | None = None,
) -> dict:
    """执行 Codex 补跑（含失败自动重试）。调用前必须先 reserve，结束时会自动 release。

    max_retries：失败后再试次数；None 时读配置 CODEX_RETRY_MAX_RETRIES（默认 3）。
    总尝试次数 = 1 + max_retries。废号 / 用户停止 不重试。
    """
    fh: logging.FileHandler | None = None
    root_logger = logging.getLogger()
    result: dict = {"status": "failed", "ok": False, "message": "Codex 补跑未返回结果"}
    key = (email or "").strip().lower()
    try:
        with _RETRYING_LOCK:
            _RUNNING_THREADS[key] = threading.get_ident()
            _RESERVED_AT[key] = time.time()
        check_stop_requested(email)

        from core.codex_oauth import run_codex_oauth

        path = Path(target_log_path) if target_log_path else log_path(email)
        path.parent.mkdir(parents=True, exist_ok=True)
        if clear_log:
            path.write_text("", encoding="utf-8")

        thread_name = threading.current_thread().name
        fh = logging.FileHandler(str(path), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        fh.addFilter(lambda record: record.threadName == thread_name)
        root_logger.addHandler(fh)

        cfg_retries, cfg_delay = _codex_retry_settings()
        retries = cfg_retries if max_retries is None else max(0, min(10, int(max_retries)))
        max_attempts = 1 + retries

        try:
            import config as config_pkg
            config_pkg.reload_all()
            from config import codex as codex_cfg
            from config import roxybrowser as roxy_cfg
            # reload 后重新读重试配置
            cfg_retries, cfg_delay = _codex_retry_settings()
            if max_retries is None:
                retries = cfg_retries
                max_attempts = 1 + retries
            logger.info(
                "[Codex 补跑] 已热加载配置：CODEX_OAUTH_DRIVER=%s ROXY_OPEN_HEADLESS=%s ROXY_KEEP_BROWSER_OPEN=%s "
                "RETRY_MAX=%s RETRY_DELAY=%s",
                getattr(codex_cfg, "CODEX_OAUTH_DRIVER", ""),
                getattr(roxy_cfg, "ROXY_OPEN_HEADLESS", ""),
                getattr(roxy_cfg, "ROXY_KEEP_BROWSER_OPEN", ""),
                retries,
                cfg_delay,
            )
        except Exception as exc:
            logger.warning("[Codex 补跑] 配置热加载失败，将继续使用当前内存配置：%s: %s", type(exc).__name__, exc)

        if batch_label:
            logger.info("[Codex 补跑] 批量任务：%s", batch_label)
        logger.info(
            "[Codex 补跑] 开始：%s（最多 %s 次尝试，失败后自动重试 %s 次）",
            email, max_attempts, retries,
        )
        logger.info("[Codex 补跑] 阶段说明：获取授权地址 → 登录邮箱 → 邮箱 OTP → 手机验证 → 捕获 callback → 提交/保存凭证")

        for attempt in range(1, max_attempts + 1):
            check_stop_requested(email)
            # 首次/重试都写 retrying，账号页「补跑中」可见
            db.update_account_codex_status(
                email,
                "retrying",
                f"第 {attempt}/{max_attempts} 次尝试" if max_attempts > 1 else "补跑中",
            )
            logger.info("[Codex 补跑] 第 %s/%s 次尝试：%s", attempt, max_attempts, email)

            try:
                result = run_codex_oauth(email, force=True)
                check_stop_requested(email)
            except CodexRetryStopped as exc:
                result = {"status": "stopped", "ok": False, "message": str(exc) or "用户手动停止 Codex 补跑"}
                db.update_account_codex_status(email, "stopped", result["message"])
                logger.warning("[Codex 补跑] %s 已停止: %s", email, result["message"])
                result["attempts"] = attempt
                return result
            except Exception as exc:
                if is_stop_requested(email):
                    result = {"status": "stopped", "ok": False, "message": "用户手动停止 Codex 补跑"}
                    db.update_account_codex_status(email, "stopped", result["message"])
                    logger.warning("[Codex 补跑] %s 已停止", email)
                    result["attempts"] = attempt
                    return result
                result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {exc}"}
                logger.exception("[Codex 补跑] %s 第 %s/%s 次异常", email, attempt, max_attempts)

            logger.info(
                "[Codex 补跑] 第 %s/%s 次结果：status=%s ok=%s file=%s callback=%s msg=%s",
                attempt,
                max_attempts,
                result.get("status"),
                result.get("ok"),
                result.get("file_path"),
                result.get("callback_url"),
                str(result.get("message") or "")[:180],
            )
            result_status = str(result.get("status") or "failed")
            if result.get("ok"):
                db.update_account_codex_status(email, "success", None)
                logger.info("[Codex 补跑] %s 成功（第 %s/%s 次）", email, attempt, max_attempts)
                result["attempts"] = attempt
                return result

            if _is_terminal_codex_failure(result):
                # 废号 / 停止等：写终态并结束，不重试
                msg_low = str(result.get("message") or "").lower()
                if (
                    result_status == "deactivated"
                    or "account_deactivated" in msg_low
                    or "account_deleted" in msg_low
                    or "account_banned" in msg_low
                    or "accountunusable" in msg_low
                    or "账号已废" in str(result.get("message") or "")
                ):
                    final_status = "deactivated"
                elif result_status == "stopped" or "用户手动停止" in str(result.get("message") or ""):
                    final_status = "stopped"
                elif result_status in ("skipped", "cancelled"):
                    final_status = result_status
                else:
                    final_status = result_status or "failed"
                db.update_account_codex_status(email, final_status, result.get("message"))
                if final_status == "deactivated":
                    logger.warning("[Codex 补跑] %s 账号已废，不再重试: %s", email, result.get("message"))
                elif final_status == "stopped":
                    logger.warning("[Codex 补跑] %s 已停止，不再重试", email)
                else:
                    logger.warning("[Codex 补跑] %s 终态失败，不再重试: %s", email, result.get("message"))
                result["status"] = final_status
                result["attempts"] = attempt
                return result

            if attempt < max_attempts:
                wait_s = _retry_delay_for_codex_error(cfg_delay, result)
                logger.warning(
                    "[Codex 补跑] 第 %s/%s 次失败，%.1fs 后自动重试（剩余 %s 次）: %s",
                    attempt,
                    max_attempts,
                    wait_s,
                    max_attempts - attempt,
                    str(result.get("message") or result_status)[:220],
                )
                db.update_account_codex_status(
                    email,
                    "retrying",
                    f"第 {attempt}/{max_attempts} 次失败，将重试: {str(result.get('message') or '')[:120]}",
                )
                if wait_s > 0:
                    # 分段 sleep，便于响应停止
                    end = time.time() + wait_s
                    while time.time() < end:
                        check_stop_requested(email)
                        time.sleep(min(0.5, max(0.05, end - time.time())))
                continue

            db.update_account_codex_status(email, result_status or "failed", result.get("message"))
            logger.warning(
                "[Codex 补跑] %s 最终失败（已达最大尝试 %s 次）: %s",
                email, max_attempts, result.get("message"),
            )
            result["attempts"] = attempt
            return result

        result["attempts"] = max_attempts
        return result
    except CodexRetryStopped as exc:
        result = {"status": "stopped", "ok": False, "message": str(exc) or "用户手动停止 Codex 补跑"}
        db.update_account_codex_status(email, "stopped", result["message"])
        logger.warning("[Codex 补跑] %s 已停止: %s", email, result["message"])
        return result
    except Exception as exc:
        if is_stop_requested(email):
            result = {"status": "stopped", "ok": False, "message": "用户手动停止 Codex 补跑"}
            db.update_account_codex_status(email, "stopped", result["message"])
            logger.warning("[Codex 补跑] %s 已停止", email)
            return result
        result = {"status": "failed", "ok": False, "message": f"{type(exc).__name__}: {exc}"}
        db.update_account_codex_status(email, "failed", result["message"])
        logger.exception("[Codex 补跑] %s 异常", email)
        logger.error("[Codex 补跑] 已结束：异常失败")
        return result
    finally:
        try:
            logger.info("[Codex 补跑] 结束：%s", email)
            if fh is not None:
                root_logger.removeHandler(fh)
                fh.close()
        finally:
            release(email)
            with _RETRYING_LOCK:
                if key:
                    _STOP_REQUESTED.discard(key)
