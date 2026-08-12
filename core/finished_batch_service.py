# -*- coding: utf-8 -*-
"""成品号入池：按批导入已注册账号 → 补跑 Codex → 推送号池。"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from core import db
from core.timeutil import beijing_now_iso

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BATCH_JSON = _PROJECT_ROOT / "data" / "finished_account_batches.json"
_LOG_DIR = _PROJECT_ROOT / "注册日志"
_LOCK = threading.RLock()
_RUN_LOCK = threading.RLock()
_RUNNING: set[str] = set()


def _ensure_dirs() -> None:
    _BATCH_JSON.parent.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def _load_batches() -> list[dict]:
    _ensure_dirs()
    if not _BATCH_JSON.exists():
        return []
    try:
        import json
        data = json.loads(_BATCH_JSON.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_batches(rows: list[dict]) -> None:
    _ensure_dirs()
    import json
    tmp = _BATCH_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_BATCH_JSON)


def _empty_summary() -> dict:
    return {
        "imported": 0,
        "skipped": 0,
        "plus": 0,
        "codex_success": 0,
        "codex_failed": 0,
        "codex_deactivated": 0,
        "codex_skipped": 0,
        "pool_pushed": 0,
        "pool_failed": 0,
        "pool_skipped": 0,
        "total_lines": 0,
        "parse_errors": 0,
    }


def _new_batch_id() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"fab-{ts}-{uuid.uuid4().hex[:6]}"


def _log_path(batch_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in batch_id)
    return _LOG_DIR / f"finished-batch-{safe}.log"


def _append_batch_log(batch: dict, line: str) -> None:
    msg = str(line or "").rstrip()
    if not msg:
        return
    stamp = datetime.now().strftime("%H:%M:%S")
    full = f"{stamp} {msg}"
    logs = batch.setdefault("logs", [])
    if isinstance(logs, list):
        logs.append(full)
        if len(logs) > 800:
            del logs[:-800]
    path = Path(batch.get("log_file") or _log_path(batch.get("id") or "unknown"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(full + "\n")
    except Exception:
        pass
    logger.info("[成品号批次 %s] %s", batch.get("id"), msg)


def list_batches(limit: int = 100) -> list[dict]:
    with _LOCK:
        rows = sorted(_load_batches(), key=lambda x: str(x.get("created_at") or ""), reverse=True)
        out = []
        for r in rows[: max(1, min(500, int(limit or 100)))]:
            item = dict(r)
            # 列表接口不带全量 logs，减负
            item["log_tail"] = list(item.get("logs") or [])[-30:]
            item.pop("logs", None)
            out.append(item)
        return out


def get_batch(batch_id: str) -> dict | None:
    bid = str(batch_id or "").strip()
    with _LOCK:
        for r in _load_batches():
            if str(r.get("id") or "") == bid:
                return dict(r)
    return None


def create_batch(
    *,
    name: str,
    text: str,
    note: str = "",
    auto_run: bool = True,
    auto_push: bool = True,
    codex_workers: int = 3,
) -> dict:
    """创建批次并可选立即后台跑流水线。

    auto_push=True：Codex 成功后自动推 sub2api 号池（默认）。
    auto_push=False：只导入 + 补跑 Codex，不入池；需用户在 Codex 授权页自行导出/推送。
    """
    name_s = str(name or "").strip() or f"批次-{datetime.now().strftime('%m%d%H%M')}"
    note_s = str(note or "").strip()
    raw_text = str(text or "")
    workers = max(1, min(16, int(codex_workers or 3)))
    do_push = bool(auto_push)
    batch_id = _new_batch_id()
    log_file = str(_log_path(batch_id))
    row = {
        "id": batch_id,
        "name": name_s,
        "note": note_s,
        "created_at": beijing_now_iso(),
        "updated_at": beijing_now_iso(),
        "status": "pending",
        "phase": "created",
        "account_ids": [],
        "emails": [],
        "items": [],  # per-email progress
        "summary": _empty_summary(),
        "log_file": log_file,
        "logs": [],
        "error": "",
        "codex_workers": workers,
        "auto_push": do_push,
        "raw_line_count": len([ln for ln in raw_text.splitlines() if ln.strip() and not ln.strip().startswith("#")]),
    }
    with _LOCK:
        rows = _load_batches()
        rows.append(row)
        _save_batches(rows)
    _append_batch_log(
        row,
        f"批次已创建 name={name_s} workers={workers} auto_push={'是' if do_push else '否（仅补跑Codex）'}",
    )
    with _LOCK:
        rows = _load_batches()
        for i, r in enumerate(rows):
            if r.get("id") == batch_id:
                rows[i] = row
                break
        _save_batches(rows)

    if auto_run:
        start_batch_pipeline(batch_id, text=raw_text)
    else:
        # 仅保存原文到临时字段，等手动 run
        with _LOCK:
            rows = _load_batches()
            for r in rows:
                if r.get("id") == batch_id:
                    r["pending_text"] = raw_text
                    r["updated_at"] = beijing_now_iso()
                    break
            _save_batches(rows)
    return get_batch(batch_id) or row


def _update_batch(batch_id: str, mutator: Callable[[dict], None]) -> dict | None:
    with _LOCK:
        rows = _load_batches()
        for i, r in enumerate(rows):
            if str(r.get("id") or "") != str(batch_id):
                continue
            mutator(r)
            r["updated_at"] = beijing_now_iso()
            rows[i] = r
            _save_batches(rows)
            return dict(r)
    return None


def start_batch_pipeline(batch_id: str, *, text: str | None = None) -> dict:
    """后台启动/续跑批次流水线。"""
    bid = str(batch_id or "").strip()
    with _RUN_LOCK:
        if bid in _RUNNING:
            return {"ok": False, "error": "该批次正在运行中", "status": 409}
        _RUNNING.add(bid)

    batch = get_batch(bid)
    if not batch:
        with _RUN_LOCK:
            _RUNNING.discard(bid)
        return {"ok": False, "error": "批次不存在", "status": 404}

    raw_text = text if text is not None else str(batch.get("pending_text") or "")

    def _runner() -> None:
        try:
            _run_pipeline(bid, raw_text)
        finally:
            with _RUN_LOCK:
                _RUNNING.discard(bid)

    t = threading.Thread(target=_runner, name=f"finished-batch-{bid}", daemon=True)
    t.start()
    return {"ok": True, "batch_id": bid, "running": True}


def _is_plus_account(acc: dict | None, codex_plan: str = "") -> bool:
    if not acc and not codex_plan:
        return False
    blob = " ".join([
        str((acc or {}).get("plan_type") or ""),
        str((acc or {}).get("plan_check_plan") or ""),
        str((acc or {}).get("plan") or ""),
        str(codex_plan or ""),
    ]).lower()
    return any(x in blob for x in ("plus", "team", "pro", "enterprise", "business"))


def _find_codex_files_for_email(email: str) -> list[str]:
    email_l = (email or "").strip().lower()
    if not email_l:
        return []
    files = []
    try:
        for row in db.list_codex_accounts():
            if str(row.get("email") or "").strip().lower() == email_l:
                fn = str(row.get("filename") or row.get("file") or "").strip()
                if fn:
                    files.append(fn)
    except Exception:
        pass
    return files


def _run_pipeline(batch_id: str, raw_text: str) -> None:
    from core.codex_retry_service import reserve as codex_reserve, release as codex_release, run_worker as codex_run
    from core.sub2api_pool_push import push_codex_files_to_pool

    def log(msg: str) -> None:
        def mut(b: dict) -> None:
            _append_batch_log(b, msg)
        _update_batch(batch_id, mut)

    batch = get_batch(batch_id)
    if not batch:
        return
    workers = max(1, min(16, int(batch.get("codex_workers") or 3)))
    summary = _empty_summary()

    # ---- phase 1: parse + import ----
    def _mark_import(b: dict) -> None:
        b["status"] = "running"
        b["phase"] = "import"
        b["error"] = ""
        _append_batch_log(b, "阶段1：解析并导入为已注册账号…")

    _update_batch(batch_id, _mark_import)

    records: list[dict] = []
    parse_errors: list[dict] = []
    try:
        from core.account_import import parse_import_text
        recs, errs = parse_import_text(raw_text)
        records = list(recs or [])
        for e in (errs or []):
            parse_errors.append({"error": str(e)[:160]})
    except Exception as exc:
        log(f"解析异常：{type(exc).__name__}: {exc}")
        _update_batch(batch_id, lambda b: b.update({
            "status": "failed", "phase": "import", "error": f"解析失败: {exc}",
        }))
        return

    summary["total_lines"] = len(records) + len(parse_errors)
    summary["parse_errors"] = len(parse_errors)
    log(f"解析完成：有效 {len(records)} 行，格式错误 {len(parse_errors)} 行")

    if not records:
        _update_batch(batch_id, lambda b: b.update({
            "status": "failed",
            "phase": "import",
            "error": "没有可导入的有效账号行",
            "summary": summary,
            "parse_errors": parse_errors[:50],
        }) or _append_batch_log(b, "导入中止：无有效行"))
        return

    batch_name = str(batch.get("name") or batch_id)
    try:
        inserted, skipped, details = db.import_registered_email_accounts(
            records,
            source=None,
            batch_id=batch_id,
            batch_name=batch_name,
            return_details=True,
        )
    except Exception as exc:
        log(f"导入失败：{type(exc).__name__}: {exc}")
        _update_batch(batch_id, lambda b: b.update({
            "status": "failed", "phase": "import", "error": f"导入失败: {exc}", "summary": summary,
        }))
        return

    summary["imported"] = int(inserted)
    summary["skipped"] = int(skipped)
    account_ids = [d.get("account_id") for d in details if d.get("account_id") is not None]
    emails = [str(d.get("email") or "") for d in details if d.get("email")]
    # include skipped existing for codex if they have account_id
    items = []
    for d in details:
        items.append({
            "email": d.get("email"),
            "account_id": d.get("account_id"),
            "import_status": d.get("status"),
            "import_reason": d.get("reason"),
            "codex_status": "",
            "codex_message": "",
            "pool_status": "",
            "pool_message": "",
            "plan": "",
            "is_plus": False,
        })
    log(f"导入完成：新增 {inserted}，跳过 {skipped}")

    _update_batch(batch_id, lambda b: b.update({
        "account_ids": account_ids,
        "emails": emails,
        "items": items,
        "summary": dict(summary),
        "parse_errors": parse_errors[:50],
        "pending_text": "",
        "phase": "codex",
    }) or _append_batch_log(b, "阶段2：批量补跑 Codex…"))

    # targets for codex: all with account_id (new + already existed)
    targets = [it for it in items if it.get("account_id") and it.get("email")]
    if not targets:
        log("没有可补跑 Codex 的账号，流水线结束")
        _finalize_summary(batch_id, summary, items, status="partial")
        return

    # ---- phase 2(+3): Codex；若 auto_push 则成功一个立刻推号池 ----
    auto_push = bool(batch.get("auto_push", True))
    if auto_push:
        log("阶段2：Codex 补跑；每成功 1 个立刻推号池（不等整批结束）")
    else:
        log("阶段2：Codex 补跑；已关闭自动入池，成功后请到 Codex 授权页自行导出/推送")
    items_lock = threading.Lock()
    summary_lock = threading.Lock()

    def _pick_codex_file(email: str) -> str | None:
        files = _find_codex_files_for_email(email)
        if not files:
            return None
        for f in files:
            if "callback" not in f.lower():
                return f
        return files[0]

    def _fill_plan_plus(out: dict, email: str, acc: dict | None = None) -> None:
        try:
            for row in db.list_codex_accounts():
                if str(row.get("email") or "").lower() == email.lower():
                    out["plan"] = str(row.get("plan") or row.get("plan_type") or "")
                    break
        except Exception:
            pass
        acc2 = acc or db.get_account_by_email(email)
        out["is_plus"] = _is_plus_account(acc2, out.get("plan") or "")

    def _push_one_now(out: dict) -> None:
        """Codex 成功后立刻推号池（单账号）。auto_push=False 时跳过。"""
        email = str(out.get("email") or "")
        if str(out.get("codex_status") or "") != "success":
            return
        if not auto_push:
            out["pool_status"] = "skipped"
            out["pool_message"] = "未开启自动入池"
            with summary_lock:
                summary["pool_skipped"] += 1
            return
        if str(out.get("pool_status") or "") == "success":
            return
        fname = _pick_codex_file(email)
        if not fname:
            out["pool_status"] = "skipped"
            out["pool_message"] = "无本地 Codex 凭证文件"
            with summary_lock:
                summary["pool_skipped"] += 1
            return
        try:
            result = push_codex_files_to_pool([fname])
        except Exception as exc:
            out["pool_status"] = "failed"
            out["pool_message"] = f"{type(exc).__name__}: {exc}"[:200]
            with summary_lock:
                summary["pool_failed"] += 1
            return
        rows = list(result.get("results") or [])
        hit = next((r for r in rows if r.get("ok")), None)
        if hit is not None:
            out["pool_status"] = "success"
            out["pool_message"] = f"pool_id={hit.get('account_id') or ''}"
            with summary_lock:
                summary["pool_pushed"] += 1
            return
        row = rows[0] if rows else {}
        if row.get("skipped") or (
            int(result.get("skipped") or 0) > 0 and int(result.get("success") or 0) == 0
        ):
            out["pool_status"] = "skipped"
            out["pool_message"] = str(row.get("error") or "skipped")[:200]
            with summary_lock:
                summary["pool_skipped"] += 1
            return
        if int(result.get("success") or 0) > 0:
            out["pool_status"] = "success"
            out["pool_message"] = "pushed"
            with summary_lock:
                summary["pool_pushed"] += 1
            return
        out["pool_status"] = "failed"
        out["pool_message"] = str(row.get("error") or "push failed")[:200]
        with summary_lock:
            summary["pool_failed"] += 1

    def _merge_item_result(res: dict) -> None:
        """把单个账号结果写回 items，并刷新批次 JSON（UI 可实时看到）。"""
        email_l = str(res.get("email") or "").lower()
        with items_lock:
            for it in items:
                if str(it.get("email") or "").lower() != email_l:
                    continue
                for k in (
                    "codex_status", "codex_message", "plan", "is_plus",
                    "pool_status", "pool_message",
                ):
                    if k in res:
                        it[k] = res.get(k)
                break
            snap_items = [dict(x) for x in items]
            snap_summary = dict(summary)

        def mut(b: dict) -> None:
            b["items"] = snap_items
            b["summary"] = snap_summary
            b["phase"] = "codex_push"
            b["status"] = "running"

        _update_batch(batch_id, mut)

    def _one_codex_and_push(item: dict) -> dict:
        email = str(item.get("email") or "")
        out = dict(item)

        # 先判断终态，再标 retrying，避免把已成功账号改掉
        acc = db.get_account_by_email(email)
        if acc and str(acc.get("codex_status") or "") == "success":
            out["codex_status"] = "success"
            out["codex_message"] = "已有成功 Codex，跳过补跑"
            _fill_plan_plus(out, email, acc)
            with summary_lock:
                summary["codex_success"] += 1
                if out.get("is_plus"):
                    summary["plus"] += 1
            _push_one_now(out)
            return out
        if acc and str(acc.get("codex_status") or "") == "deactivated":
            out["codex_status"] = "deactivated"
            out["codex_message"] = str(acc.get("codex_error") or "deactivated")
            with summary_lock:
                summary["codex_deactivated"] += 1
            return out

        # 账号页可见「补跑中」
        try:
            db.update_account_codex_status(email, "retrying", f"成品批 {batch_id} 补跑中")
        except Exception:
            pass
        out["codex_status"] = "retrying"
        out["codex_message"] = "补跑中"
        _merge_item_result(out)

        reserved = False
        try:
            if not codex_reserve(email):
                time.sleep(1.0)
                if not codex_reserve(email):
                    out["codex_status"] = "failed"
                    out["codex_message"] = "无法占用补跑锁（可能正在补跑）"
                    with summary_lock:
                        summary["codex_failed"] += 1
                    return out
            reserved = True
            result = codex_run(email, batch_label=f"成品批 {batch_id}", clear_log=False)
            if result.get("ok"):
                out["codex_status"] = "success"
                out["codex_message"] = str(result.get("message") or "ok")[:240]
            else:
                st = str(result.get("status") or "failed")
                out["codex_status"] = st if st else "failed"
                out["codex_message"] = str(result.get("message") or "")[:240]
            _fill_plan_plus(out, email)
            with summary_lock:
                st2 = str(out.get("codex_status") or "")
                if st2 == "success":
                    summary["codex_success"] += 1
                    if out.get("is_plus"):
                        summary["plus"] += 1
                elif st2 == "deactivated":
                    summary["codex_deactivated"] += 1
                else:
                    summary["codex_failed"] += 1
            # 成功一个立刻推一个
            if str(out.get("codex_status") or "") == "success":
                _push_one_now(out)
            return out
        except Exception as exc:
            out["codex_status"] = "failed"
            out["codex_message"] = f"{type(exc).__name__}: {exc}"[:240]
            with summary_lock:
                summary["codex_failed"] += 1
            return out
        finally:
            if reserved:
                try:
                    codex_release(email)
                except Exception:
                    pass

    total_n = len(targets)
    done_n = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fab-codex") as ex:
        futs = {ex.submit(_one_codex_and_push, it): it.get("email") for it in targets}
        for fut in as_completed(futs):
            done_n += 1
            try:
                res = fut.result()
            except Exception as exc:
                res = {
                    "email": futs[fut],
                    "codex_status": "failed",
                    "codex_message": f"{type(exc).__name__}: {exc}",
                }
                with summary_lock:
                    summary["codex_failed"] += 1
            _merge_item_result(res)
            st = str(res.get("codex_status") or "")
            pool_st = str(res.get("pool_status") or "")
            log(
                f"进度 {done_n}/{total_n} {res.get('email')} "
                f"codex={st} pool={pool_st or '-'} "
                f"{(res.get('codex_message') or res.get('pool_message') or '')[:80]}"
            )

    if auto_push:
        log(
            f"Codex/入池完成：Codex成功 {summary['codex_success']} 失败 {summary['codex_failed']} "
            f"废号 {summary['codex_deactivated']}；入池成功 {summary['pool_pushed']} "
            f"失败 {summary['pool_failed']} 跳过 {summary['pool_skipped']}；Plus {summary['plus']}"
        )
    else:
        log(
            f"Codex 完成（未自动入池）：成功 {summary['codex_success']} 失败 {summary['codex_failed']} "
            f"废号 {summary['codex_deactivated']}；Plus {summary['plus']}"
        )

    final_status = "done"
    if summary["imported"] == 0 and summary["codex_success"] == 0:
        final_status = "failed"
    elif summary["codex_failed"] or summary["codex_deactivated"] or (auto_push and summary["pool_failed"]):
        final_status = "partial"

    if auto_push:
        summary_line = (
            f"本批共导入 {summary['imported']} 个（跳过 {summary['skipped']}），"
            f"Plus {summary['plus']} 个，"
            f"Codex 授权成功 {summary['codex_success']} 个（失败 {summary['codex_failed']}，废号 {summary['codex_deactivated']}），"
            f"入池成功 {summary['pool_pushed']} 个（失败 {summary['pool_failed']}，跳过 {summary['pool_skipped']}）"
        )
    else:
        summary_line = (
            f"本批共导入 {summary['imported']} 个（跳过 {summary['skipped']}），"
            f"Plus {summary['plus']} 个，"
            f"Codex 授权成功 {summary['codex_success']} 个（失败 {summary['codex_failed']}，废号 {summary['codex_deactivated']}），"
            f"未自动入池（请到 Codex 授权页自行导出/推送）"
        )
    log(f"汇总：{summary_line}")

    def _finish(b: dict) -> None:
        b["status"] = final_status
        b["phase"] = "done"
        b["items"] = [dict(x) for x in items]
        b["summary"] = dict(summary)
        b["summary_text"] = summary_line
        b["error"] = "" if final_status != "failed" else summary_line
        _append_batch_log(b, f"流水线结束 status={final_status}")

    _update_batch(batch_id, _finish)


def _finalize_summary(batch_id: str, summary: dict, items: list, *, status: str) -> None:
    summary_line = (
        f"本批共导入 {summary.get('imported', 0)} 个（跳过 {summary.get('skipped', 0)}），"
        f"Plus {summary.get('plus', 0)} 个，"
        f"Codex 授权成功 {summary.get('codex_success', 0)} 个，"
        f"入池成功 {summary.get('pool_pushed', 0)} 个"
    )

    def mut(b: dict) -> None:
        b["status"] = status
        b["phase"] = "done"
        b["items"] = items
        b["summary"] = dict(summary)
        b["summary_text"] = summary_line
        _append_batch_log(b, f"汇总：{summary_line}")

    _update_batch(batch_id, mut)
