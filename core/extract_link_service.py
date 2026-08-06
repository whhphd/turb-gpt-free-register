# -*- coding: utf-8 -*-
"""Plus 试用提链后台队列（多 Provider）。

Providers:
  - oai9：promo 预检 + Kakao 多任务 API
  - convertmove：Customer API submissions

触发：账号页手动提链 / 批量提链。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None

from config import extract_link as cfg
from core import db

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = {"oai9", "convertmove"}
SUPPORTED_LINK_TYPES = {"pix", "upi", "kakao_pay", "kakao", "ideal"}
_CM_MODE_MAP = {
    "pix": "pix",
    "upi": "upi",
    "ideal": "ideal",
    "kakao": "kakao",
    "kakao_pay": "kakao",
}


def _runtime_setting(name: str, default=None):
    try:
        from config.env_loader import load_env
        load_env(override=True)
    except Exception:
        pass
    raw = os.getenv(name)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    return getattr(cfg, name, default)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(_runtime_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _provider() -> str:
    p = str(_runtime_setting("EXTRACT_LINK_PROVIDER", "oai9") or "oai9").strip().lower()
    if p not in SUPPORTED_PROVIDERS:
        raise ValueError(f"EXTRACT_LINK_PROVIDER 无效：{p}，可选 oai9 / convertmove")
    return p


def _link_type(value: str | None = None) -> str:
    t = str(value or _runtime_setting("EXTRACT_LINK_TYPE", "kakao_pay") or "kakao_pay").strip().lower()
    if t not in SUPPORTED_LINK_TYPES:
        raise ValueError("提链类型无效，仅支持 pix / upi / kakao_pay / ideal")
    return "kakao_pay" if t == "kakao" else t


def _api_base() -> str:
    base = str(_runtime_setting("EXTRACT_LINK_API_BASE", "") or "").strip().rstrip("/")
    if not base:
        raise ValueError("EXTRACT_LINK_API_BASE 为空（oai9/convertmove 的站点根地址）")
    return base


def _card_or_cdk(value: str | None = None) -> str:
    """oai9 的 card 与 convertmove 的 cdk 共用 EXTRACT_LINK_CDK 配置项。"""
    code = str(value or _runtime_setting("EXTRACT_LINK_CDK", "") or "").strip()
    if not code:
        raise ValueError("EXTRACT_LINK_CDK 为空（oai9=卡密 card；convertmove=CDK）")
    return code


_WORKERS = _int_setting("EXTRACT_LINK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("EXTRACT_LINK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="extract-link")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def queue_settings() -> dict:
    try:
        provider = _provider()
    except Exception:
        provider = str(_runtime_setting("EXTRACT_LINK_PROVIDER", "oai9") or "oai9")
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT, "provider": provider}


def _session():
    if curl_requests is None:
        return None
    return curl_requests.Session()


def _http_json(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    headers: dict | None = None,
    timeout: int = 30,
) -> tuple[int, dict]:
    hdrs = {"Accept": "application/json", "User-Agent": "turb-gpt-extract-link/2.0"}
    if headers:
        hdrs.update(headers)
    s = _session()
    try:
        if s is None:
            data = None
            if body is not None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                hdrs.setdefault("Content-Type", "application/json")
            req = Request(url, data=data, headers=hdrs, method=method.upper())
            try:
                with urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace") or "{}"
                    code = int(getattr(resp, "status", 200) or 200)
            except Exception as exc:
                code = int(getattr(exc, "code", 0) or 0)
                raw = ""
                try:
                    raw = exc.read().decode("utf-8", "replace")  # type: ignore[attr-defined]
                except Exception:
                    raw = str(exc)
                if not code:
                    raise
            try:
                payload = json.loads(raw) if raw.strip()[:1] in "{[" else {"error": raw[:500]}
            except Exception:
                payload = {"error": raw[:500]}
            if not isinstance(payload, dict):
                payload = {"data": payload}
            return code, payload

        method_u = method.upper()
        if method_u == "GET":
            resp = s.get(url, headers=hdrs, timeout=timeout)
        elif method_u == "POST":
            resp = s.post(url, headers=hdrs, json=body or {}, timeout=timeout)
        else:
            resp = s.request(method_u, url, headers=hdrs, json=body, timeout=timeout)
        try:
            payload = resp.json()
        except Exception:
            payload = {"error": (resp.text or "")[:500]}
        if not isinstance(payload, dict):
            payload = {"data": payload}
        return int(resp.status_code or 0), payload
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass


def _extract_error_message(data) -> str:
    """提取短错误文案，禁止把整段任务 JSON 塞进 UI。"""
    if data is None:
        return ""
    if isinstance(data, str):
        text = data.strip()
        # 若误把 JSON 当字符串存了，只取前一小段
        if text.startswith("{") and len(text) > 160:
            return text[:120] + "…"
        return text[:300]
    if not isinstance(data, dict):
        return str(data)[:200]
    parts = []
    for key in ("error_code", "error", "suggestion", "message", "detail", "reason", "msg"):
        value = data.get(key)
        if value not in (None, ""):
            parts.append(str(value).strip())
    if parts:
        out, seen = [], set()
        for p in parts:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return " | ".join(out)[:240]
    # 无明确 error 时给状态摘要，不要 dump 全字段
    return _task_status_summary(data)


def _task_status_summary(task: dict | None) -> str:
    """轮询超时/无 error 时的短摘要。"""
    if not isinstance(task, dict):
        return ""
    st = str(task.get("status") or "").strip() or "-"
    label = str(task.get("status_label") or task.get("progress_message") or task.get("stage") or "").strip()
    step = task.get("progress_step")
    total = task.get("progress_total")
    attempt = task.get("attempt")
    max_attempts = task.get("max_attempts")
    bits = [f"status={st}"]
    if label:
        bits.append(label[:80])
    if step not in (None, "") and total not in (None, ""):
        bits.append(f"进度 {step}/{total}")
    if attempt not in (None, "") and max_attempts not in (None, ""):
        bits.append(f"attempt {attempt}/{max_attempts}")
    job_id = str(task.get("job_id") or "").strip()
    if job_id:
        bits.append(f"job={job_id[:18]}…") if len(job_id) > 20 else bits.append(f"job={job_id}")
    return " · ".join(bits)[:200]


def _pick_link_url(task: dict) -> str:
    """按文档优先级取支付链接。"""
    if not isinstance(task, dict):
        return ""
    # 文档：nicepay_checkout_url -> kakao_pay_url -> provider_redirect_url -> long_url
    # 以及可选 kakao_intermediate_url / kakao_qr_url
    for key in (
        "nicepay_checkout_url",
        "kakao_pay_url",
        "provider_redirect_url",
        "long_url",
        "link",
        "hosted_instructions_url",
        "kakao_intermediate_url",
        "kakao_qr_url",
    ):
        val = str(task.get(key) or "").strip()
        if val:
            return val
    # nested link object
    link_obj = task.get("link")
    if isinstance(link_obj, dict):
        for key in (
            "nicepay_checkout_url",
            "kakao_pay_url",
            "provider_redirect_url",
            "long_url",
            "url",
        ):
            val = str(link_obj.get(key) or "").strip()
            if val:
                return val
    return ""


def _pick_qr_url(task: dict) -> str:
    if not isinstance(task, dict):
        return ""
    for key in ("kakao_qr_image_url", "kakao_qr_url", "image_url_png", "image_url_svg", "qr_url"):
        val = str(task.get(key) or "").strip()
        if val:
            return val
    return ""


# ---------- convertmove ----------

def _cm_mode(link_type: str) -> str:
    return _CM_MODE_MAP[_link_type(link_type)]


def _cm_create(*, token: str, link_type: str, cdk: str) -> dict:
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    body = {"cdk": cdk, "mode": _cm_mode(link_type), "at": token}
    headers = {"Idempotency-Key": f"turb-{_cm_mode(link_type)}-{uuid.uuid4().hex[:16]}"}
    status, data = _http_json("POST", f"{base}/api/v1/submissions", body=body, headers=headers, timeout=timeout)
    if status not in (200, 202):
        raise RuntimeError(
            f"convertmove 提交失败: {_extract_error_message(data) or f'HTTP {status}'}"
        )
    if not str(data.get("task_id") or "").strip():
        raise RuntimeError(f"convertmove 未返回 task_id: {data}")
    return data


def _cm_get(*, task_id: str) -> dict:
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    status, data = _http_json(
        "GET",
        f"{base}/api/v1/submissions/{quote(task_id, safe='')}",
        timeout=timeout,
    )
    if status not in (200, 202):
        raise RuntimeError(f"convertmove 查询失败: {_extract_error_message(data) or f'HTTP {status}'}")
    return data


def _cm_poll(*, task_id: str, account_id: int, link_type: str) -> dict:
    timeout = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 240, 30, 900)
    interval = max(2.0, float(_runtime_setting("EXTRACT_LINK_POLL_INTERVAL", 5) or 5))
    end = time.time() + timeout
    last: dict = {}
    while time.time() < end:
        last = _cm_get(task_id=task_id)
        st = str(last.get("status") or "").lower()
        if st in ("queued", "running", "pending", "accepted"):
            db.update_account_extract(account_id, {
                "ok": False,
                "status": "running",
                "job_id": task_id,
                "link_type": link_type,
                "message": f"convertmove status={st}",
            })
            time.sleep(interval)
            continue
        return last
    summary = _task_status_summary(last) or _extract_error_message(last) or "unknown"
    raise RuntimeError(f"convertmove 轮询超时（{timeout}s）task={task_id[:24]} {summary}")


def _run_convertmove(*, account_id: int, email: str, access_token: str, link_type: str, cdk: str) -> dict:
    submitted = _cm_create(token=access_token, link_type=link_type, cdk=cdk)
    task_id = str(submitted.get("task_id") or "")
    st0 = str(submitted.get("status") or "queued").lower()
    db.update_account_extract(account_id, {
        "ok": False,
        "status": "running",
        "job_id": task_id,
        "link_type": link_type,
        "message": f"convertmove 已提交 status={st0}",
    })
    final = submitted if st0 in ("completed", "failed", "cancelled") else _cm_poll(
        task_id=task_id, account_id=account_id, link_type=link_type
    )
    st = str(final.get("status") or "").lower()
    if st == "completed":
        url = str(final.get("hosted_instructions_url") or "").strip()
        if not url:
            raise RuntimeError("convertmove completed 但无 hosted_instructions_url")
        return {
            "ok": True,
            "job_id": task_id,
            "url": url,
            "qr": "",
            "cdk_remaining": final.get("remaining_cdk_uses"),
            "card_charged": True,
            "raw": final,
            "message": f"convertmove 成功 remaining={final.get('remaining_cdk_uses')}",
        }
    if st == "cancelled":
        raise RuntimeError("convertmove 任务已取消")
    raise RuntimeError(_extract_error_message(final) or f"convertmove 失败 status={st}")


# ---------- oai9 ----------

def _oai9_check_eligible(access_token: str) -> dict:
    """POST /api/promo-coupon/check；返回该项 result dict。"""
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    status, data = _http_json(
        "POST",
        f"{base}/api/promo-coupon/check",
        body={"accessTokens": [access_token]},
        timeout=timeout,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"oai9 资格预检失败: {_extract_error_message(data) or f'HTTP {status}'}")
    results = data.get("results")
    if not isinstance(results, list) or not results:
        # 兼容直接返回单对象
        if isinstance(data.get("eligible"), bool) or data.get("state"):
            return data
        raise RuntimeError(f"oai9 资格预检无 results: {data}")
    # 用 index 映射；单 token 时取 index=0 或第一项
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("index") in (0, "0", None):
            # 优先 index=0；若没有 index 字段则用第一项
            if item.get("index") in (0, "0") or "index" not in item:
                return item
    return results[0] if isinstance(results[0], dict) else {"error": "invalid result"}


def _oai9_submit(*, token: str, card: str, plan_type: str, promo_code: str) -> dict:
    """POST /api/kakao-link/tasks；返回单任务 dict（含 job_id）。"""
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    body = {
        "card": card,
        "accessTokens": [token],
        "plan_type": plan_type or "plus",
        "promo_code": promo_code or "",
    }
    status, data = _http_json(
        "POST",
        f"{base}/api/kakao-link/tasks",
        body=body,
        timeout=timeout,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"oai9 提交失败: {_extract_error_message(data) or f'HTTP {status}'}")

    tasks = data.get("tasks") if isinstance(data, dict) else None
    active = data.get("active_duplicates") if isinstance(data, dict) else None
    job = None
    if isinstance(tasks, list) and tasks:
        job = tasks[0] if isinstance(tasks[0], dict) else None
    if not job and isinstance(active, list) and active:
        # 已有排队/执行中的重复任务，复用其 job_id
        job = active[0] if isinstance(active[0], dict) else None
        if job:
            logger.info("[提链] oai9 命中 active_duplicates，复用 job_id=%s", job.get("job_id"))
    if not job or not str(job.get("job_id") or "").strip():
        # 有些实现可能顶层直接返回 job_id
        if data.get("job_id"):
            job = data
        else:
            raise RuntimeError(f"oai9 未返回 job_id: {data}")
    return job


def _oai9_get_task(*, job_id: str) -> dict:
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    status, data = _http_json(
        "GET",
        f"{base}/api/kakao-link/tasks/{quote(job_id, safe='')}",
        timeout=timeout,
    )
    if status < 200 or status >= 300:
        raise RuntimeError(f"oai9 查询失败: {_extract_error_message(data) or f'HTTP {status}'}")
    # 兼容 {task: {...}} 包装
    if isinstance(data.get("task"), dict):
        return data["task"]
    return data


def _oai9_poll(*, job_id: str, account_id: int, link_type: str) -> dict:
    timeout = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 240, 30, 900)
    interval = max(3.0, float(_runtime_setting("EXTRACT_LINK_POLL_INTERVAL", 5) or 5))
    end = time.time() + timeout
    last: dict = {}
    while time.time() < end:
        last = _oai9_get_task(job_id=job_id)
        st = str(last.get("status") or "").lower()
        if st in ("queued", "extracting", "running", "pending"):
            db.update_account_extract(account_id, {
                "ok": False,
                "status": "running",
                "job_id": job_id,
                "link_type": link_type,
                "message": f"oai9 status={st}",
            })
            time.sleep(interval)
            continue
        return last
    summary = _task_status_summary(last) or _extract_error_message(last) or "unknown"
    raise RuntimeError(f"oai9 轮询超时（{timeout}s）{summary}")


def _run_oai9(*, account_id: int, email: str, access_token: str, link_type: str, card: str) -> dict:
    if _link_type(link_type) not in ("kakao_pay", "kakao"):
        # oai9 文档仅 Kakao；其他类型明确报错
        raise ValueError("oai9 provider 目前仅支持 kakao_pay")

    check = _oai9_check_eligible(access_token)
    eligible = bool(check.get("eligible") is True and str(check.get("state") or "").lower() == "eligible")
    if not eligible:
        err = _extract_error_message(check) or f"state={check.get('state')} eligible={check.get('eligible')}"
        raise RuntimeError(f"oai9 资格预检未通过，已跳过: {err}")

    plan_type = str(_runtime_setting("EXTRACT_LINK_PLAN_TYPE", "plus") or "plus").strip() or "plus"
    promo_code = str(_runtime_setting("EXTRACT_LINK_PROMO_CODE", "") or "").strip()
    job = _oai9_submit(token=access_token, card=card, plan_type=plan_type, promo_code=promo_code)
    job_id = str(job.get("job_id") or "").strip()
    st0 = str(job.get("status") or "queued").lower()
    db.update_account_extract(account_id, {
        "ok": False,
        "status": "running",
        "job_id": job_id,
        "link_type": link_type,
        "message": f"oai9 已提交 status={st0}",
    })

    if st0 in ("done", "failed", "canceled", "cancelled"):
        final = job
    else:
        final = _oai9_poll(job_id=job_id, account_id=account_id, link_type=link_type)

    st = str(final.get("status") or "").lower()
    if st == "done":
        url = _pick_link_url(final)
        if not url:
            raise RuntimeError("oai9 done 但未解析到支付链接字段")
        qr = _pick_qr_url(final)
        charged = final.get("card_charged")
        return {
            "ok": True,
            "job_id": job_id,
            "url": url,
            "qr": qr,
            "cdk_remaining": final.get("card_remaining") or final.get("remaining") or final.get("remaining_uses"),
            "card_charged": charged,
            "kakao_status": final.get("kakao_status"),
            "raw": final,
            "message": f"oai9 成功 charged={charged} kakao_status={final.get('kakao_status') or '-'}",
        }
    if st in ("canceled", "cancelled"):
        raise RuntimeError("oai9 任务已取消")
    raise RuntimeError(_extract_error_message(final) or f"oai9 失败 status={st}")


# ---------- public ----------

def query_cdk(*, cdk: str | None = None) -> dict:
    """查询卡密/CDK信息（能力探测，不强依赖服务端查次接口）。"""
    provider = _provider()
    code = _card_or_cdk(cdk)
    base = _api_base()
    masked = code if len(code) <= 8 else f"{code[:6]}…{code[-4:]}"
    return {
        "provider": provider,
        "api_base": base,
        "cdk_masked": masked,
        "card_masked": masked,
        "message": (
            "oai9：卡密次数以任务结果 card_charged / remaining 为准"
            if provider == "oai9"
            else "convertmove：成功任务响应 remaining_cdk_uses"
        ),
        "supported": list(SUPPORTED_PROVIDERS),
    }


def _run_extract(*, account_id: int, email: str, access_token: str, link_type: str, cdk: str, trigger: str) -> dict:
    provider = _provider()
    job_id = ""
    try:
        if not db.mark_account_extract_running(account_id):
            return {"ok": False, "error": "账号已删除或提链状态已被重置"}

        logger.info(
            "[提链] 开始 provider=%s email=%s type=%s trigger=%s token_len=%s",
            provider,
            email,
            link_type,
            trigger,
            len(access_token or ""),
        )

        if provider == "oai9":
            out = _run_oai9(
                account_id=account_id,
                email=email,
                access_token=access_token,
                link_type=link_type,
                card=cdk,
            )
        else:
            out = _run_convertmove(
                account_id=account_id,
                email=email,
                access_token=access_token,
                link_type=link_type,
                cdk=cdk,
            )

        job_id = str(out.get("job_id") or "")
        url = str(out.get("url") or "")
        qr = str(out.get("qr") or "")
        result_payload = {
            "long_url": url,
            "copy_paste": url,
            "image_url_png": qr if qr.lower().endswith(".png") or "png" in qr.lower() else (qr or ""),
            "image_url_svg": qr if qr.lower().endswith(".svg") else "",
            "payment_method": "kakao" if "kakao" in link_type else link_type,
            "payment_link_type": link_type,
            "expires_at": "",
            "cdk_remaining": out.get("cdk_remaining"),
            "card_charged": out.get("card_charged"),
            "kakao_status": out.get("kakao_status"),
            "provider": provider,
        }
        # 若 png/svg 分不清，至少把 qr 放 png 字段方便 UI 打开
        if qr and not result_payload["image_url_png"] and not result_payload["image_url_svg"]:
            result_payload["image_url_png"] = qr

        final = {
            "ok": True,
            "status": "success",
            "job_id": job_id,
            "link_type": link_type,
            "cdk_remaining": out.get("cdk_remaining"),
            "result": result_payload,
            "message": out.get("message") or "提链成功",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        db.update_account_extract(account_id, final)
        logger.info("[提链] 成功 provider=%s email=%s job=%s", provider, email, job_id)
        return final
    except Exception as exc:
        # UI 只展示简短原因，不带异常类型前缀/超长 JSON
        raw = str(exc).strip() or type(exc).__name__
        if raw.startswith("RuntimeError: "):
            raw = raw[len("RuntimeError: "):]
        if raw.startswith("ValueError: "):
            raw = raw[len("ValueError: "):]
        reason = raw[:220]
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "job_id": job_id or None,
            "link_type": link_type,
            "error": reason,
            "message": reason,
        }
        try:
            db.update_account_extract(account_id, result)
        except Exception:
            logger.exception("[提链] 写入失败状态异常: account_id=%s", account_id)
        logger.warning("[提链] 失败 provider=%s email=%s %s", provider, email, reason)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_extract(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str = "manual",
    link_type: str | None = None,
    cdk: str | None = None,
) -> dict:
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "提链队列已满"}
    try:
        provider = _provider()
        lt = _link_type(link_type)
        if provider == "oai9" and lt not in ("kakao_pay", "kakao"):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": False, "error": "oai9 目前仅支持 kakao_pay"}
        code = _card_or_cdk(cdk)
        _api_base()
        if not db.claim_account_extract(account_id, trigger=trigger, link_type=lt):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在提链中"}
        fut = _EXECUTOR.submit(
            _run_extract,
            account_id=account_id,
            email=email,
            access_token=access_token,
            link_type=lt,
            cdk=code,
            trigger=trigger,
        )
        return {
            "accepted": True,
            "busy": False,
            "future": fut,
            "link_type": lt,
            "provider": provider,
        }
    except Exception:
        _QUEUE_SLOTS.release()
        raise
