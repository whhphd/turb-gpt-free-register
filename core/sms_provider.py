# -*- coding: utf-8 -*-
"""
接码平台客户端。

用于 Codex OAuth "全新 session" 流程过 OpenAI 的 /phone-verification 手机号验证：
    1. acquire_number()       getNumber 取一个手机号（返回 激活ID + 号码）
    2. wait_for_sms_code()    轮询 getStatus 直到拿到短信验证码
    3. complete() / cancel()  setStatus 标记完成(6) / 取消(8)

当前支持：
    - GrizzlySMS：GET 文本接口，文档 https://api.grizzlysms.com
    - HeroSMS：SMS-Activate 兼容，文档 https://hero-sms.com（OpenAI service=dr）
    - SMSBower：SMS-Activate 兼容，文档 https://smsbower.page（OpenAI service=dr）
    - L：本地 JSON 管理接口，文档 L_API.md
    - H：本地 JSON 管理接口，文档 H_API.md

价格相关：每取一个号、收到短信都会计费，所以：
    - 取号后若收不到短信，必须 cancel(8) 释放，避免白扣钱；
    - 成功拿到码后 complete(6) 正式完成激活。
"""
import json
import logging
import math
import random
import threading
import time
from pathlib import Path
from urllib.parse import urljoin

from curl_cffi.requests import Session as CurlSession

# 注意：用 `from config import codex` 而不是 `from config.codex import X`，
# 这样 WebUI 调 config.reload_all() 后，本模块通过 codex.X 读到的是最新值。
from config import codex as _cfg
from config import IMPERSONATE

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SMS_STATE_DIR = _PROJECT_ROOT / "data" / "sms_weight"
_DELIVERY_STATE_PATH = _SMS_STATE_DIR / "delivery_stats.json"

# SMS-Activate 风格平台：号码取出后约 2 分钟内不允许取消（防薅号）。
# 这里留 5 秒缓冲，时间到了再发 setStatus=8。
_MIN_CANCEL_DELAY = 125

# 记录每个 activation_id 的取号时间，供 cancel() 判断是否要等。
# 用模块级 dict 而不是改 acquire_number 返回值，保持向后兼容。
_ACQUIRED_AT: dict[str, float] = {}

# OpenAI 目前确认走 SMS 的兜底国家（仅在白名单/优先国都未配置时使用）。
# 52 = Thailand（已验证走 SMS）
_OPENAI_SMS_ALLOWED_COUNTRIES = {"52"}

# 极贵区轻微标签（不写死优先名单；分类靠样本库+当前价位分位）
_FALLBACK_COUNTRY_PENALTY = {"187": 6.0, "12": 6.0}

# ---------- 智能调度：样本库驱动 → 平价 / 探索 / 兜底 / 垃圾 ----------
# 平价 value   = 价不贵 + 到码率尚可（或样本少但价在平价带）
# 探索 explore = 白名单内样本不足，偶尔抽中以发现新好档
# 兜底 fallback = 价高 + 到码率高（如美区），第 4 次换号起才开放
# 垃圾 junk    = 样本够且到码差 / 全灭
_DEAD_COUNTRY_PENALTY = 50.0
_DEAD_COUNTRY_MIN_N = 4
_TIER_HOT_MIN_N = 3
_TIER_HOT_MIN_RATE = 0.35
_TIER_WARM_MIN_RATE = 0.12
_RATE_VALUE_WEIGHT = 55.0
_RATE_PRIOR_N = 5.0
# 换号策略：前 N 次只打平价(+探索)，之后开放兜底
_BUDGET_PHASE_ATTEMPTS = 3
# 偶发探索：少样本档 / 极低概率带 1 个垃圾档（垃圾只走探索，常规路径永不排）
_EXPLORE_PROB = 0.18
_JUNK_IN_EXPLORE_PROB = 0.22  # 触发探索时，再以该概率塞入 1 个垃圾档试探
# 平价到码率下限（贝叶斯平滑后）
_VALUE_MIN_RATE_HAT = 0.22
# 兜底：价位 ≥ 候选 75 分位（或绝对偏贵）且到码率尚可
_FALLBACK_MIN_RATE_HAT = 0.30

# 并发占槽：取号中的槽对其它线程大幅加分，避免 N 路齐步同一死槽
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_SLOTS: dict[str, float] = {}  # slot_key -> expire_ts
_INFLIGHT_TTL_SEC = 75
_INFLIGHT_PENALTY = 30.0

# 旧：整国 NO_NUMBERS（仅作软降权时长参考，不再硬排除国家）
_NO_NUMBERS_UNTIL: dict[str, dict[str, float]] = {}
_NO_NUMBERS_COOLDOWN_SEC = 10 * 60

# 槽位「软冷却」：只加分惩罚，不踢出候选。key=channel, value={ "54:3193": {until, reason, sec} }
# 选号统一用权重调度（价格 + 连跪 + 软冷却分），与硬屏蔽脱钩。
_SLOT_COOLDOWN: dict[str, dict[str, dict]] = {}
_SLOT_COOLDOWN_NO_NUMBERS = 5 * 60
_SLOT_COOLDOWN_SEND_REJECT = 30 * 60  # 软降权窗口（非硬锁）；真拒发仍用较长窗口
_SLOT_COOLDOWN_TIMEOUT = 3 * 60
# 软冷却期间的基础加分（越大越差）；随剩余时间线性衰减到 0
_SOFT_COOL_PENALTY = {
    "timeout": 6.0,
    "code_timeout": 6.0,
    "sms_timeout": 6.0,
    "send_reject": 18.0,
    "send_not_accepted": 18.0,
    "invalid_phone": 18.0,
    "whatsapp": 18.0,
    "no_numbers": 12.0,
    "error": 8.0,
    "bad_response": 6.0,
}
# 槽位失败计数（用于排序轻惩罚）
_SLOT_FAILS: dict[str, dict[str, int]] = {}
# activation_id -> {country, provider_id, price, service, channel}
_ACTIVATION_META: dict[str, dict] = {}
# 单次 acquire 最多尝试候选槽
_MAX_COUNTRY_ROTATIONS = 12

# ---------- 全局投递统计（跨并发 + 落盘，按「次数」滑动）----------
# 接码是一批一批跑的：空闲很久不应丢经验，故用「最近 N 次投递事件」而不是时间窗。
# 落盘到 data/sms_weight/delivery_stats.json，webui 频繁重启仍保留权重。
_DELIVERY_LOCK = threading.Lock()
_DELIVERY_LOADED = False
_DELIVERY_PERSIST_ENABLED = True  # 单测可关，避免污染磁盘
_DELIVERY_SAVE_SEQ = 0
# channel -> list[{ts, slot, country, outcome, aid}]
_DELIVERY_EVENTS: dict[str, list[dict]] = {}
# activation_id -> 已记账 outcome（防 timeout + send_reject 双计）
_DELIVERY_BY_AID: dict[str, str] = {}
# 全局最近 N 次发送/投递结果（约 10 账号×10 次；过大则老经验淡出慢）
_DELIVERY_MAX_EVENTS = 100
# 兼容旧名：时间窗已废弃，仅作文档/迁移参考
_DELIVERY_WINDOW_SEC = 0
# 槽位至少 N 次样本才用槽位级「到码率」微调；连跪惩罚 n≥1 即可
_DELIVERY_SLOT_MIN_N = 2
# 国家级传染：需要「多个不同槽」都表现差才连累同国未试供应商（避免英 2442 连跪误伤 3248）
_DELIVERY_COUNTRY_MIN_N = 4
_DELIVERY_COUNTRY_MIN_DEAD_SLOTS = 3  # 至少几个「基本死掉」的不同槽才整国降权
_DELIVERY_GOOD_SLOT_RATE = 0.4       # 槽位成功率达到此线视为「已验证好槽」
# 价格在总分中的权重（越大越偏向便宜号；失败不扣费，故主优化单价）
# 300：价差 $0.05 ≈ 15 分，美 0.13 更难压过墨/印 0.05～0.08
_SCORE_PRICE_WEIGHT = 300.0
# 同层内抖动带宽（分）
_ACQUIRE_SHARD_EPS = 4.0
# 成功 outcome
_DELIVERY_OK = "success"
# 失败 outcomes（到码失败 / 发送拒绝 / 通道不对）
_DELIVERY_FAIL = frozenset({
    "timeout", "reject", "send_reject", "send_not_accepted",
    "invalid_phone", "whatsapp", "fail",
})

# SMS-Activate 兼容通道：共用 getNumber/getStatus/setStatus 文本协议
_ACTIVATE_PROVIDERS = {
    "grizzly": {
        "label": "GrizzlySMS",
        "default_base": "https://api.grizzlysms.com/stubs/handler_api.php",
        "base_attr": "SMS_API_BASE",
        "key_attr": "SMS_API_KEY",
    },
    "herosms": {
        "label": "HeroSMS",
        "default_base": "https://hero-sms.com/stubs/handler_api.php",
        "base_attr": "HEROSMS_API_BASE",
        "key_attr": "HEROSMS_API_KEY",
    },
    "smsbower": {
        "label": "SMSBower",
        "default_base": "https://smsbower.page/stubs/handler_api.php",
        "base_attr": "SMSBOWER_API_BASE",
        "key_attr": "SMSBOWER_API_KEY",
    },
}


class SmsProviderError(RuntimeError):
    """接码平台通用错误。"""


class SmsNoNumbersError(SmsProviderError):
    """暂无可用号码（NO_NUMBERS），可换国家或稍后重试。"""


class SmsNoBalanceError(SmsProviderError):
    """余额不足（NO_BALANCE），必须充值，重试无意义——上层应立即停止。"""


class SmsCodeTimeout(SmsProviderError):
    """单个号等短信超时（OpenAI 没发或没到达）。"""


def _http() -> CurlSession:
    s = CurlSession(impersonate=IMPERSONATE)
    s.timeout = _cfg.SMS_REQUEST_TIMEOUT
    return s


def _provider() -> str:
    return str(getattr(_cfg, "SMS_PROVIDER", "grizzly") or "grizzly").strip().lower()


def _is_activate_provider(name: str | None = None) -> bool:
    return (name or _provider()) in _ACTIVATE_PROVIDERS


def _activate_meta(name: str | None = None) -> dict:
    provider = (name or _provider()).strip().lower()
    meta = _ACTIVATE_PROVIDERS.get(provider)
    if not meta:
        raise SmsProviderError(f"不是 SMS-Activate 兼容通道：{provider}")
    return meta


def _activate_api_base(name: str | None = None) -> str:
    meta = _activate_meta(name)
    raw = str(getattr(_cfg, meta["base_attr"], "") or "").strip()
    return raw or meta["default_base"]


def _activate_api_key(name: str | None = None) -> str:
    meta = _activate_meta(name)
    return str(getattr(_cfg, meta["key_attr"], "") or "").strip()


def _activate_label(name: str | None = None) -> str:
    return str(_activate_meta(name).get("label") or (name or _provider()))


def _request_activate(http: CurlSession, params: dict, *, provider: str | None = None) -> str:
    """
    发一个 SMS-Activate 兼容 API 请求，返回去空白的响应文本。
    统一识别公共错误码并抛对应异常。
    兼容 GrizzlySMS / HeroSMS / SMSBower。
    """
    provider = (provider or _provider()).strip().lower()
    label = _activate_label(provider)
    api_key = _activate_api_key(provider)
    if not api_key and str(params.get("action") or "") not in ("getCountries", "getServicesList"):
        # 查询类接口部分可不带 key；取号/收码必须有 key
        if str(params.get("action") or "") in ("getNumber", "getNumberV2", "getStatus", "setStatus", "getBalance", "getPrices"):
            raise SmsProviderError(f"{label} API key 未配置")
    base_params = {}
    if api_key:
        base_params["api_key"] = api_key
    base_params.update(params)
    resp = http.get(_activate_api_base(provider), params=base_params)
    if resp.status_code != 200:
        raise SmsProviderError(
            f"{label} HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        )
    text = (resp.text or "").strip()

    # 公共错误码（任何 action 都可能返回）
    if text == "BAD_KEY":
        raise SmsProviderError(f"{label} API key 无效（BAD_KEY）")
    if text == "NO_BALANCE":
        raise SmsNoBalanceError(f"{label} 余额不足（NO_BALANCE），请充值")
    if text == "NO_NUMBERS":
        raise SmsNoNumbersError(f"{label} 暂无可用号码（NO_NUMBERS）")
    if text == "SERVICE_UNAVAILABLE_REGION":
        raise SmsProviderError(f"{label} 地区受限（SERVICE_UNAVAILABLE_REGION），请换 IP")
    if text in ("BAD_ACTION", "BAD_SERVICE", "BAD_STATUS"):
        raise SmsProviderError(f"{label} 请求参数错误：{text}")
    if text == "NO_ACTIVATION":
        raise SmsProviderError(f"{label} 激活 ID 不存在（NO_ACTIVATION）")
    if text.startswith("The service is prohibited"):
        raise SmsProviderError(f"{label} 该服务被平台禁售：{text}")

    return text


def _request_grizzly(http: CurlSession, params: dict) -> str:
    """兼容旧调用名，实际走当前 SMS_PROVIDER 的 Activate 通道。"""
    return _request_activate(http, params)


def _request_activate_json(http: CurlSession, params: dict, *, provider: str | None = None):
    """请求并尽量解析 JSON；失败时返回原始文本。"""
    text = _request_activate(http, params, provider=provider)
    try:
        return json.loads(text)
    except Exception:
        return text


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _auto_country_enabled() -> bool:
    raw = getattr(_cfg, "SMS_AUTO_COUNTRY", False)
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def get_balance(http: CurlSession | None = None, *, provider: str | None = None) -> float:
    """查询 SMS-Activate 兼容通道余额。"""
    provider = (provider or _provider()).strip().lower()
    if not _is_activate_provider(provider):
        raise SmsProviderError(f"{provider} 不支持 getBalance")
    own_http = http is None
    http = http or _http()
    try:
        text = _request_activate(http, {"action": "getBalance"}, provider=provider)
        if text.startswith("ACCESS_BALANCE:"):
            return _safe_float(text.split(":", 1)[1], 0.0)
        raise SmsProviderError(f"{_activate_label(provider)} getBalance 非预期响应：{text[:200]}")
    finally:
        if own_http:
            http.close()


def get_prices(
    http: CurlSession | None = None,
    *,
    provider: str | None = None,
    service: str | None = None,
    country: str | None = None,
) -> dict:
    """查询 SMS-Activate 兼容通道价格。返回原始 dict。"""
    provider = (provider or _provider()).strip().lower()
    if not _is_activate_provider(provider):
        raise SmsProviderError(f"{provider} 不支持 getPrices")
    own_http = http is None
    http = http or _http()
    try:
        params = {"action": "getPrices"}
        svc = str(service if service is not None else getattr(_cfg, "SMS_SERVICE", "") or "").strip()
        cty = str(country if country is not None else getattr(_cfg, "SMS_COUNTRY", "") or "").strip()
        if svc:
            params["service"] = svc
        if cty:
            params["country"] = cty
        data = _request_activate_json(http, params, provider=provider)
        if isinstance(data, dict):
            return data
        raise SmsProviderError(f"{_activate_label(provider)} getPrices 非预期响应：{str(data)[:200]}")
    finally:
        if own_http:
            http.close()


# getTopCountriesByService / getCountries 缓存：{channel:service -> (expire_ts, rows)}
_TOP_SERVICE_CACHE: dict[str, tuple[float, list[dict]]] = {}
_COUNTRY_NAME_MAP_CACHE: dict[str, tuple[float, dict[str, str]]] = {}


def _slug_country_name(value: str | None) -> str:
    import re as _re
    s = str(value or "").strip().lower()
    s = s.replace("&", " and ")
    s = _re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _parse_top_countries_response(data) -> list[dict]:
    """解析 getTopCountriesByService / Rank。

    兼容：
    1) SMSBower 文档格式：
       {"chile": {"3419": {"price": 0.05, "count": 100}, ...}, "usa": {...}}
       国名 slug → Gold 供应商 map（按销售量优先，API 已排）
    2) 数字国码 → {price,count}
    3) list[{country,price,count}]
    """
    rows: list[dict] = []
    items = data
    if isinstance(data, dict):
        items = data.get("data") or data.get("result") or data.get("response") or data
    if isinstance(items, dict):
        for key, value in items.items():
            if not isinstance(value, dict):
                continue
            # 嵌套 partner 格式：value = {partnerId: {price,count}}
            partner_like = all(
                isinstance(v, dict) and (
                    "price" in v or "cost" in v or "count" in v or "qty" in v
                )
                for v in value.values()
            ) if value else False
            # 排除本身就是扁平 price 对象
            flat_price = any(k in value for k in ("price", "cost", "retail_price", "count", "qty"))
            partners: list[dict] = []
            if partner_like and not (flat_price and any(str(k).isdigit() is False and k in ("price", "cost", "count") for k in value.keys())):
                # 若所有 key 都像 partner id（数字串）→ partners
                if all(str(k).replace("-", "").isalnum() for k in value.keys()):
                    for pid, info in value.items():
                        if not isinstance(info, dict):
                            continue
                        price_f = _safe_float(info.get("price") if info.get("price") is not None else info.get("cost"), -1.0)
                        count_i = _safe_int(info.get("count") if info.get("count") is not None else info.get("qty"), 0)
                        if price_f < 0 or count_i <= 0:
                            continue
                        partners.append({
                            "provider_id": str(pid).strip(),
                            "price": price_f,
                            "count": count_i,
                        })
            if partners:
                # 保持 API 顺序（销售量优先），同时给 best price/count 汇总
                best_price = min(p["price"] for p in partners)
                total_count = sum(p["count"] for p in partners)
                try:
                    country_id = str(int(key))
                    name = ""
                except (TypeError, ValueError):
                    country_id = ""
                    name = str(key)
                rows.append({
                    "country": country_id,
                    "name": name or str(key),
                    "slug": _slug_country_name(name or key),
                    "price": best_price,
                    "count": total_count,
                    "partners": partners,
                    "source": "top_service",
                })
                continue

            try:
                country_id = str(int(key))
            except (TypeError, ValueError):
                country_id = str(value.get("country") or value.get("id") or "")
            price = value.get("price") or value.get("cost") or value.get("retail_price")
            count = value.get("count") or value.get("qty") or value.get("available") or value.get("stock")
            name = value.get("name") or value.get("countryName") or value.get("country_name") or (key if not country_id else "")
            price_f = _safe_float(price, -1.0)
            count_i = _safe_int(count, 0)
            if price_f >= 0:
                rows.append({
                    "country": str(country_id or key),
                    "name": str(name or ""),
                    "slug": _slug_country_name(name or key),
                    "price": price_f,
                    "count": count_i,
                    "partners": [],
                    "source": "top_flat",
                })
    elif isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            country_id = item.get("country") or item.get("countryId") or item.get("country_id") or item.get("id")
            if country_id is None:
                continue
            price = item.get("price") or item.get("cost") or item.get("retail_price") or item.get("retailPrice")
            count = item.get("count") or item.get("qty") or item.get("available") or item.get("stock") or item.get("total")
            name = item.get("name") or item.get("countryName") or item.get("country_name") or item.get("title") or ""
            price_f = _safe_float(price, -1.0)
            count_i = _safe_int(count, 0)
            if price_f >= 0:
                rows.append({
                    "country": str(country_id),
                    "name": str(name),
                    "slug": _slug_country_name(name),
                    "price": price_f,
                    "count": count_i,
                    "partners": [],
                    "source": "top_list",
                })
    return rows


def get_countries_name_map(
    http: CurlSession | None = None,
    *,
    provider: str | None = None,
    force_refresh: bool = False,
) -> dict[str, str]:
    """国名/slug → 数字国码。来自 getCountries。"""
    channel = (provider or _provider()).strip().lower()
    cache_sec = max(60, _safe_int(getattr(_cfg, "SMS_TOP_COUNTRIES_CACHE_SEC", 300), 300))
    cached = _COUNTRY_NAME_MAP_CACHE.get(channel)
    if not force_refresh and cached and cached[0] > time.time():
        return dict(cached[1])

    own_http = http is None
    http = http or _http()
    mapping: dict[str, str] = {}
    try:
        data = _request_activate_json(http, {"action": "getCountries"}, provider=channel)
        if not isinstance(data, dict):
            return {}
        for key, info in data.items():
            if not isinstance(info, dict):
                continue
            cid = str(info.get("id") or key).strip()
            if not cid:
                continue
            names = [
                info.get("eng"), info.get("rus"), info.get("chn"),
                info.get("name"), info.get("en"), info.get("title"),
            ]
            for n in names:
                if not n:
                    continue
                mapping[str(n).strip().lower()] = cid
                mapping[_slug_country_name(n)] = cid
            mapping[cid] = cid
            mapping[_slug_country_name(cid)] = cid
        # 常见别名
        aliases = {
            "usa": "united-states",
            "us": "united-states",
            "u-s-a": "united-states",
            "uk": "united-kingdom",
            "great-britain": "united-kingdom",
            "viet-nam": "vietnam",
            "russian-federation": "russia",
            "korea-south": "south-korea",
            "czechia": "czech-republic",
        }
        for a, b in aliases.items():
            if b in mapping and a not in mapping:
                mapping[a] = mapping[b]
            if a in mapping and b not in mapping:
                mapping[b] = mapping[a]
        _COUNTRY_NAME_MAP_CACHE[channel] = (time.time() + cache_sec, mapping)
        return dict(mapping)
    except Exception as exc:
        logger.warning("[SMS:%s] getCountries 映射失败：%s", _activate_label(channel), exc)
        if cached:
            return dict(cached[1])
        return {}
    finally:
        if own_http:
            http.close()


def _resolve_country_id(raw: str, name_map: dict[str, str]) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    if s.isdigit():
        return s
    low = s.lower()
    slug = _slug_country_name(s)
    return (
        name_map.get(s)
        or name_map.get(low)
        or name_map.get(slug)
        or name_map.get(slug.replace("-", ""))
        or ""
    )


def get_top_countries_by_service(
    http: CurlSession | None = None,
    *,
    provider: str | None = None,
    service: str | None = None,
    limit: int | None = None,
    force_refresh: bool = False,
) -> list[dict]:
    """getTopCountriesByService：服务维度 TopN 国 + Gold 供应商。

    返回按 API 内部优先级排序的列表：
      [{country, name, slug, price, count, partners:[{provider_id,price,count}]}]
    """
    channel = (provider or _provider()).strip().lower()
    if not _is_activate_provider(channel):
        raise SmsProviderError(f"{channel} 不支持 getTopCountriesByService")
    service_code = str(service if service is not None else getattr(_cfg, "SMS_SERVICE", "") or "").strip() or "dr"
    lim = max(1, int(limit if limit is not None else _safe_int(getattr(_cfg, "SMS_TOP_COUNTRIES_LIMIT", 10), 10)))
    cache_sec = max(30, _safe_int(getattr(_cfg, "SMS_TOP_COUNTRIES_CACHE_SEC", 300), 300))
    cache_key = f"{channel}:{service_code}:{lim}"
    cached = _TOP_SERVICE_CACHE.get(cache_key)
    if not force_refresh and cached and cached[0] > time.time():
        return list(cached[1])

    own_http = http is None
    http = http or _http()
    try:
        name_map = get_countries_name_map(http, provider=channel)
        rows: list[dict] = []
        for action in ("getTopCountriesByService", "getTopCountriesByServiceRank"):
            try:
                data = _request_activate_json(
                    http, {"action": action, "service": service_code}, provider=channel
                )
                parsed = _parse_top_countries_response(data)
                if not parsed:
                    continue
                # 保持 API 返回顺序（internal priority），不要按价格重排破坏 Top 语义
                for item in parsed:
                    cid = str(item.get("country") or "").strip()
                    if not cid.isdigit():
                        cid = _resolve_country_id(item.get("slug") or item.get("name") or "", name_map)
                    if not cid:
                        logger.debug(
                            "[SMS:%s] Top 国无法映射数字码：%s",
                            _activate_label(channel), item.get("name") or item.get("slug"),
                        )
                        continue
                    item = dict(item)
                    item["country"] = cid
                    rows.append(item)
                if rows:
                    break
            except Exception as exc:
                logger.debug("[SMS:%s] %s 失败：%s", _activate_label(channel), action, exc)
                continue

        rows = rows[:lim]
        if rows:
            _TOP_SERVICE_CACHE[cache_key] = (time.time() + cache_sec, list(rows))
            logger.info(
                "[SMS:%s] 服务Top白名单 service=%s → %s",
                _activate_label(channel),
                service_code,
                ",".join(
                    f"{r.get('country')}:{r.get('name') or r.get('slug') or '-'}"
                    for r in rows
                ),
            )
        return rows
    finally:
        if own_http:
            http.close()


def get_top_countries(
    http: CurlSession | None = None,
    *,
    provider: str | None = None,
    service: str | None = None,
) -> list[dict]:
    """按服务 Top / 价格返回可用国家（含库存）。优先 getTopCountriesByService。"""
    provider = (provider or _provider()).strip().lower()
    if not _is_activate_provider(provider):
        raise SmsProviderError(f"{provider} 不支持 top-countries")
    service_code = str(service if service is not None else getattr(_cfg, "SMS_SERVICE", "") or "").strip() or "dr"
    own_http = http is None
    http = http or _http()
    try:
        if provider in ("herosms", "smsbower"):
            try:
                rows = get_top_countries_by_service(http, provider=provider, service=service_code)
                if rows:
                    # 兼容旧调用方字段
                    return [
                        {
                            "country": r.get("country"),
                            "name": r.get("name") or r.get("slug") or "",
                            "price": r.get("price"),
                            "count": r.get("count"),
                            "partners": r.get("partners") or [],
                        }
                        for r in rows
                    ]
            except Exception as exc:
                logger.debug("[SMS] get_top_countries_by_service 失败：%s", exc)

        prices = get_prices(http, provider=provider, service=service_code, country="")
        rows = []
        for country_id, services in (prices or {}).items():
            if not isinstance(services, dict):
                continue
            svc_data = services.get(service_code)
            if not isinstance(svc_data, dict):
                # 有时返回 {service: {...}} 已按 service 过滤，services 本身就是价格
                if "cost" in services or "price" in services or "count" in services:
                    svc_data = services
                else:
                    continue
            price = svc_data.get("cost") or svc_data.get("price")
            count = svc_data.get("count") or svc_data.get("qty") or svc_data.get("available")
            price_f = _safe_float(price, -1.0)
            count_i = _safe_int(count, 0)
            if price_f >= 0 and count_i > 0:
                rows.append({"country": str(country_id), "price": price_f, "count": count_i})
        rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
        return rows
    finally:
        if own_http:
            http.close()


def _cfg_max_price() -> float:
    """用户硬上限：SMS_MAX_PRICE 与旧 SMS_AUTO_COUNTRY_MAX_PRICE 取更严。0=不限。"""
    raw = str(getattr(_cfg, "SMS_MAX_PRICE", "") or "").strip()
    cap = _safe_float(raw, 0.0) if raw else 0.0
    legacy = _safe_float(getattr(_cfg, "SMS_AUTO_COUNTRY_MAX_PRICE", 0), 0.0)
    if cap > 0 and legacy > 0:
        return min(cap, legacy)
    return cap if cap > 0 else legacy


def _cfg_min_price() -> float:
    raw = str(getattr(_cfg, "SMS_MIN_PRICE", "") or "").strip()
    return _safe_float(raw, 0.0) if raw else 0.0


def _cfg_min_stock() -> int:
    legacy = _safe_int(getattr(_cfg, "SMS_AUTO_COUNTRY_MIN_STOCK", 0), 0)
    base = _safe_int(getattr(_cfg, "SMS_PROVIDER_MIN_STOCK", 15), 15)
    return legacy if legacy > 0 else max(1, base)


def _parse_country_list(raw: str | None) -> list[str]:
    out: list[str] = []
    for part in str(raw or "").replace(";", ",").split(","):
        c = part.strip()
        if c and c not in out:
            out.append(c)
    return out


def _cfg_preferred_countries() -> list[str]:
    raw = str(getattr(_cfg, "SMS_PREFERRED_COUNTRIES", "") or "").strip()
    if raw:
        return _parse_country_list(raw)
    # 兼容：优先配置国 + 内置兜底
    fixed = str(getattr(_cfg, "SMS_COUNTRY", "") or "").strip()
    out = []
    if fixed:
        out.append(fixed)
    for c in sorted(_OPENAI_SMS_ALLOWED_COUNTRIES):
        if c not in out:
            out.append(c)
    return out


def _cfg_country_whitelist() -> list[str]:
    """静态硬白名单（不含动态 Top API）。

    优先级：SMS_COUNTRY_WHITELIST > PREFERRED > SMS_COUNTRY > 内置兜底。
    动态 Top 见 resolve_country_whitelist()。
    """
    wl = _parse_country_list(getattr(_cfg, "SMS_COUNTRY_WHITELIST", "") or "")
    if wl:
        return wl
    pref = _parse_country_list(getattr(_cfg, "SMS_PREFERRED_COUNTRIES", "") or "")
    if pref:
        return pref
    fixed = str(getattr(_cfg, "SMS_COUNTRY", "") or "").strip()
    if fixed:
        return [fixed]
    return sorted(_OPENAI_SMS_ALLOWED_COUNTRIES)


def _allow_outside_whitelist() -> bool:
    raw = getattr(_cfg, "SMS_ALLOW_OUTSIDE_WHITELIST", False)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _use_top_countries_whitelist() -> bool:
    raw = getattr(_cfg, "SMS_USE_TOP_COUNTRIES_WHITELIST", True)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def resolve_country_whitelist(
    http: CurlSession | None = None,
    *,
    provider: str | None = None,
    service: str | None = None,
) -> tuple[list[str], list[dict]]:
    """解析最终国家白名单。

    返回 (country_ids, top_rows)。
    - 手动 SMS_COUNTRY_WHITELIST 非空：只用手动，top_rows=[]
    - 开启 SMS_USE_TOP_COUNTRIES_WHITELIST：getTopCountriesByService TopN
    - 否则：PREFERRED / SMS_COUNTRY 静态名单
    """
    manual = _parse_country_list(getattr(_cfg, "SMS_COUNTRY_WHITELIST", "") or "")
    if manual:
        return manual, []

    channel = (provider or _provider()).strip().lower()
    svc = str(service or getattr(_cfg, "SMS_SERVICE", "") or "dr").strip() or "dr"
    if _use_top_countries_whitelist() and channel in ("smsbower", "herosms"):
        try:
            top_rows = get_top_countries_by_service(http, provider=channel, service=svc)
            ids = []
            for r in top_rows:
                c = str(r.get("country") or "").strip()
                if c and c not in ids:
                    ids.append(c)
            if ids:
                return ids, top_rows
        except Exception as exc:
            logger.warning(
                "[SMS:%s] 拉取服务Top白名单失败，回退静态名单：%s",
                _activate_label(channel), exc,
            )
    return _cfg_country_whitelist(), []


def _slot_key(country: str, provider_id: str = "", *, for_delivery: bool = False) -> str:
    """槽位键。

    - 冷却/失败计数：provider 空 → `country:-`（整国槽）
    - 投递统计：provider 空 → 空串（只记国家级，禁止 `187:-` 幽灵成功槽）
    """
    cid = str(country or "").strip()
    pid = str(provider_id or "").strip()
    if not cid:
        return ""
    if not pid:
        return "" if for_delivery else f"{cid}:-"
    return f"{cid}:{pid}"


def _delivery_normalize_outcome(outcome: str) -> str:
    o = str(outcome or "").strip().lower()
    if o in ("ok", "code", "got_code", "success"):
        return _DELIVERY_OK
    if o in ("timeout", "sms_timeout", "code_timeout"):
        return "timeout"
    if o in ("send_not_accepted", "send_reject", "reject", "invalid_phone"):
        return o if o in _DELIVERY_FAIL else "reject"
    if o == "whatsapp" or o == "whatsapp_channel":
        return "whatsapp"
    if o in _DELIVERY_FAIL or o == _DELIVERY_OK:
        return o
    return "fail"


def _delivery_is_success(outcome: str) -> bool:
    return _delivery_normalize_outcome(outcome) == _DELIVERY_OK


def _rebuild_delivery_aid_index_locked() -> None:
    """持锁：按当前事件重建 aid→outcome 索引。"""
    alive: dict[str, str] = {}
    for ch_events in _DELIVERY_EVENTS.values():
        for ev in ch_events or []:
            aid = str(ev.get("aid") or "").strip()
            if not aid:
                continue
            # 同 aid 以列表中较新（更靠后）为准
            alive[aid] = str(ev.get("outcome") or "")
    _DELIVERY_BY_AID.clear()
    _DELIVERY_BY_AID.update(alive)


def _delivery_prune_locked(channel: str, now: float | None = None) -> list[dict]:
    """持锁：按「总次数」滑动窗口裁剪（保留最近 N 次），不按时间过期。"""
    del now  # 次数窗不再使用时间
    events = _DELIVERY_EVENTS.setdefault(channel, [])
    max_n = max(1, int(_DELIVERY_MAX_EVENTS or 100))
    if len(events) > max_n:
        _DELIVERY_EVENTS[channel] = list(events[-max_n:])
        events = _DELIVERY_EVENTS[channel]
    _rebuild_delivery_aid_index_locked()
    return events


def _ensure_delivery_loaded_locked() -> None:
    """持锁：首次使用时从磁盘加载。"""
    global _DELIVERY_LOADED
    if _DELIVERY_LOADED:
        return
    _DELIVERY_LOADED = True
    path = _DELIVERY_STATE_PATH
    try:
        if not path.is_file():
            return
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return
        data = json.loads(raw)
        if not isinstance(data, dict):
            return
        events = data.get("events") or {}
        if isinstance(events, dict):
            loaded: dict[str, list[dict]] = {}
            for ch, rows in events.items():
                if not isinstance(rows, list):
                    continue
                cleaned = []
                for ev in rows:
                    if isinstance(ev, dict):
                        cleaned.append(ev)
                loaded[str(ch).strip().lower()] = cleaned
            _DELIVERY_EVENTS.clear()
            _DELIVERY_EVENTS.update(loaded)
        fails = data.get("slot_fails") or {}
        if isinstance(fails, dict):
            _SLOT_FAILS.clear()
            for ch, m in fails.items():
                if isinstance(m, dict):
                    _SLOT_FAILS[str(ch).strip().lower()] = {
                        str(k): int(v or 0) for k, v in m.items()
                    }
        soft = data.get("slot_soft_cool") or {}
        if isinstance(soft, dict):
            now = time.time()
            _SLOT_COOLDOWN.clear()
            for ch, m in soft.items():
                if not isinstance(m, dict):
                    continue
                bucket: dict[str, dict] = {}
                for k, ent in m.items():
                    if not isinstance(ent, dict):
                        continue
                    try:
                        until = float(ent.get("until") or 0)
                    except Exception:
                        continue
                    if until <= now:
                        continue
                    bucket[str(k)] = {
                        "until": until,
                        "reason": str(ent.get("reason") or "no_numbers"),
                        "sec": int(ent.get("sec") or 0),
                        "at": float(ent.get("at") or 0),
                    }
                if bucket:
                    _SLOT_COOLDOWN[str(ch).strip().lower()] = bucket
        # 各 channel 按次数裁剪
        for ch in list(_DELIVERY_EVENTS.keys()):
            _delivery_prune_locked(ch)
        n_ev = sum(len(v) for v in _DELIVERY_EVENTS.values())
        logger.info(
            "[SMS] 已加载投递权重落盘 events=%s fails_ch=%s soft_ch=%s path=%s",
            n_ev, len(_SLOT_FAILS), len(_SLOT_COOLDOWN), path,
        )
    except Exception as exc:
        logger.warning("[SMS] 加载投递权重失败 path=%s: %s", path, exc)


def _save_delivery_state_locked() -> None:
    """持锁：原子写入落盘（事件 + 槽失败计数 + 未过期软降权）。"""
    global _DELIVERY_SAVE_SEQ
    if not _DELIVERY_PERSIST_ENABLED:
        return
    try:
        _SMS_STATE_DIR.mkdir(parents=True, exist_ok=True)
        # 保存前再裁剪一次
        for ch in list(_DELIVERY_EVENTS.keys()):
            evs = _DELIVERY_EVENTS.get(ch) or []
            max_n = max(1, int(_DELIVERY_MAX_EVENTS or 100))
            if len(evs) > max_n:
                _DELIVERY_EVENTS[ch] = list(evs[-max_n:])
        now = time.time()
        soft_out: dict[str, dict] = {}
        for ch, bucket in _SLOT_COOLDOWN.items():
            if not isinstance(bucket, dict):
                continue
            kept = {}
            for k, ent in list(bucket.items()):
                if not isinstance(ent, dict):
                    continue
                try:
                    until = float(ent.get("until") or 0)
                except Exception:
                    continue
                if until <= now:
                    continue
                kept[str(k)] = {
                    "until": until,
                    "reason": str(ent.get("reason") or ""),
                    "sec": int(ent.get("sec") or 0),
                    "at": float(ent.get("at") or 0),
                }
            if kept:
                soft_out[str(ch)] = kept
        payload = {
            "version": 2,
            "window": "count",
            "max_events": int(_DELIVERY_MAX_EVENTS),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "events": {
                ch: list(rows or []) for ch, rows in _DELIVERY_EVENTS.items()
            },
            "slot_fails": {
                ch: dict(m or {}) for ch, m in _SLOT_FAILS.items()
            },
            "slot_soft_cool": soft_out,
        }
        tmp = _DELIVERY_STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(_DELIVERY_STATE_PATH)
        _DELIVERY_SAVE_SEQ += 1
    except Exception as exc:
        logger.warning("[SMS] 保存投递权重失败 path=%s: %s", _DELIVERY_STATE_PATH, exc)


def ensure_delivery_stats_loaded() -> None:
    """公开：确保已从磁盘加载（create_app / 首次选号前可调）。"""
    with _DELIVERY_LOCK:
        _ensure_delivery_loaded_locked()


def clear_delivery_stats(*, provider: str | None = None, persist: bool | None = None) -> None:
    """清空全局投递统计。persist 默认跟随 _DELIVERY_PERSIST_ENABLED。"""
    do_persist = _DELIVERY_PERSIST_ENABLED if persist is None else bool(persist)
    with _DELIVERY_LOCK:
        _ensure_delivery_loaded_locked()
        if provider in (None, ""):
            _DELIVERY_EVENTS.clear()
            _DELIVERY_BY_AID.clear()
            _SLOT_FAILS.clear()
            _SLOT_COOLDOWN.clear()
        else:
            name = str(provider).strip().lower()
            _DELIVERY_EVENTS.pop(name, None)
            _SLOT_FAILS.pop(name, None)
            _SLOT_COOLDOWN.pop(name, None)
            _rebuild_delivery_aid_index_locked()
        if do_persist:
            _save_delivery_state_locked()


def record_delivery_outcome(
    *,
    country: str,
    provider_id: str = "",
    channel: str | None = None,
    outcome: str,
    activation_id: str | None = None,
) -> bool:
    """
    记录一次投递结果到全局统计（进程内所有并发任务共享）。

    outcome:
      - success：收到验证码
      - timeout：等码超时
      - send_reject / send_not_accepted / invalid_phone / whatsapp：发码侧失败

    同一 activation_id 只计一次；若先记失败后记 success，会把失败改成成功。
    返回是否写入/更新了统计。按最近 N 次事件滑动，并落盘。
    """
    cid = str(country or "").strip()
    if not cid:
        return False
    name = (channel or _provider()).strip().lower()
    norm = _delivery_normalize_outcome(outcome)
    aid = str(activation_id or "").strip()
    pid = str(provider_id or "").strip()
    slot = _slot_key(cid, pid, for_delivery=True)
    # 无 provider_id 时只记国家级，避免 success 记成 187:- 幽灵槽拆散真实 187:2266 统计
    now = time.time()
    with _DELIVERY_LOCK:
        _ensure_delivery_loaded_locked()
        _delivery_prune_locked(name, now)
        if aid:
            prev = _DELIVERY_BY_AID.get(aid)
            if prev == _DELIVERY_OK:
                return False
            if prev and norm != _DELIVERY_OK:
                # 已记过失败（timeout 后再 send_reject）不双计
                return False
            if prev and norm == _DELIVERY_OK:
                # 升级：删掉该 aid 的旧失败事件
                events = _DELIVERY_EVENTS.setdefault(name, [])
                _DELIVERY_EVENTS[name] = [ev for ev in events if str(ev.get("aid") or "") != aid]
            _DELIVERY_BY_AID[aid] = norm
        ev = {
            "ts": now,
            "slot": slot,  # 空串 = 仅国家级
            "country": cid,
            "provider_id": pid,
            "outcome": norm,
            "aid": aid,
            "ok": 1 if norm == _DELIVERY_OK else 0,
        }
        _DELIVERY_EVENTS.setdefault(name, []).append(ev)
        _delivery_prune_locked(name, now)
        # 摘要日志（便于并发时观察全局加权是否生效）
        snap = _delivery_stats_for_slot_locked(name, cid, pid)
        win_n = len(_DELIVERY_EVENTS.get(name) or [])
        logger.info(
            "[SMS:%s] 全局投递记账 outcome=%s slot=%s country=%s "
            "slot_ok=%s/%s country_ok=%s/%s dead_slots=%s good_slots=%s "
            "window=%s/%s aid=%s",
            _activate_label(name),
            norm,
            slot or f"{cid}:*",
            cid,
            snap["slot_ok"],
            snap["slot_n"],
            snap["country_ok"],
            snap["country_n"],
            snap.get("country_dead_slots", 0),
            snap.get("country_good_slots", 0),
            win_n,
            int(_DELIVERY_MAX_EVENTS),
            (aid[:16] + "...") if len(aid) > 16 else (aid or "-"),
        )
        _save_delivery_state_locked()
        return True


def record_activation_delivery(activation_id: str, outcome: str) -> bool:
    """按 activation_id 的 meta 记账；无 meta 时跳过。"""
    aid = str(activation_id or "").strip()
    if not aid:
        return False
    meta = _ACTIVATION_META.get(aid) or {}
    country = str(meta.get("country") or "").strip()
    if not country:
        logger.debug("[SMS] record_activation_delivery 无 meta activation_id=%s", aid)
        return False
    return record_delivery_outcome(
        country=country,
        provider_id=str(meta.get("provider_id") or ""),
        channel=str(meta.get("channel") or _provider()),
        outcome=outcome,
        activation_id=aid,
    )


def _delivery_stats_for_slot_locked(channel: str, country: str, provider_id: str = "") -> dict:
    """持锁：统计槽位 + 国家在窗口内的成功/失败，以及同国「死槽/好槽」个数。"""
    events = _DELIVERY_EVENTS.get(channel) or []
    slot = _slot_key(country, provider_id, for_delivery=True)
    cid = str(country or "").strip()
    slot_ok = slot_n = 0
    country_ok = country_n = 0
    # 同国各槽聚合（忽略 slot 为空的仅国家级事件）
    per_slot: dict[str, list[int]] = {}  # slot -> [ok, n]
    for ev in events:
        ev_slot = str(ev.get("slot") or "")
        ok = int(ev.get("ok") or 0)
        if slot and ev_slot == slot:
            slot_n += 1
            slot_ok += ok
        if str(ev.get("country") or "") == cid:
            country_n += 1
            country_ok += ok
            if ev_slot:
                st = per_slot.setdefault(ev_slot, [0, 0])
                st[1] += 1
                st[0] += ok
    slot_consec_fail = 0
    if slot:
        for ev in reversed(events):
            if str(ev.get("slot") or "") != slot:
                continue
            if int(ev.get("ok") or 0) == 1:
                break
            slot_consec_fail += 1
    country_consec_fail = 0
    for ev in reversed(events):
        if str(ev.get("country") or "") != cid:
            continue
        if int(ev.get("ok") or 0) == 1:
            break
        country_consec_fail += 1

    dead_slots = 0
    good_slots = 0
    for s_ok, s_n in per_slot.values():
        if s_n < _DELIVERY_SLOT_MIN_N:
            continue
        rate = float(s_ok) / float(s_n)
        if s_ok == 0:
            dead_slots += 1
        if rate >= _DELIVERY_GOOD_SLOT_RATE:
            good_slots += 1

    return {
        "slot": slot,
        "country": cid,
        "slot_ok": slot_ok,
        "slot_n": slot_n,
        "country_ok": country_ok,
        "country_n": country_n,
        "slot_consec_fail": slot_consec_fail,
        "country_consec_fail": country_consec_fail,
        "country_dead_slots": dead_slots,
        "country_good_slots": good_slots,
        "country_distinct_slots": len(per_slot),
    }


def _consec_fail_penalty(cf: int) -> float:
    """连续收不到码的阶梯大罚（越大越差）。成功一次后 cf 归零，槽可重新被低价选中。"""
    cf = int(cf or 0)
    if cf <= 1:
        return 0.0
    if cf == 2:
        return 6.0
    if cf == 3:
        return 14.0
    # ≥4：继续加重，封顶 40，避免永久数学锁死（窗口过期/成功仍可恢复）
    return min(40.0, 14.0 + (cf - 3) * 6.0)


def delivery_score_delta(
    country: str,
    provider_id: str = "",
    *,
    channel: str | None = None,
) -> tuple[float, dict]:
    """
    返回加到 score 上的增量（越大越差）以及调试信息。

    规则（与「失败不扣费」对齐）：
      - 价格权重在 list_provider_candidates 里加大（price * 160）
      - 槽位主惩罚 = 连续收不到码阶梯大罚（2→+6，3→+14，≥4 更高）
      - 到码率只做轻微调；成功一次 → 连跪清零 → 便宜槽可再次排前
      - 国家级传染：多死槽才连累未试商；有好槽不误伤同国
    """
    cid = str(country or "").strip()
    if not cid:
        return 0.0, {}
    name = (channel or _provider()).strip().lower()
    with _DELIVERY_LOCK:
        _ensure_delivery_loaded_locked()
        _delivery_prune_locked(name)
        snap = _delivery_stats_for_slot_locked(name, cid, provider_id)

    delta = 0.0
    slot_n = int(snap["slot_n"])
    country_n = int(snap["country_n"])
    dead_slots = int(snap.get("country_dead_slots") or 0)
    good_slots = int(snap.get("country_good_slots") or 0)
    cf = int(snap.get("slot_consec_fail") or 0)
    info = dict(snap)

    # —— 槽位：连跪大罚为主，到码率轻调 ——
    # cf≥2 即使样本还少也罚（1 次失败不罚，给抖动机会）
    consec_pen = _consec_fail_penalty(cf)
    if consec_pen > 0:
        delta += consec_pen
        info["consec_penalty"] = round(consec_pen, 3)

    # —— 槽位档位（同国不同 provider）优先 ——
    if slot_n >= _DELIVERY_SLOT_MIN_N:
        rate = float(snap["slot_ok"]) / float(slot_n)
        rate_adj = (0.5 - rate) * 8.0
        if cf == 0 and snap["slot_ok"] > 0 and rate_adj > 0:
            rate_adj *= 0.4
        # 本档全灭：沉底（不影响同国其它 provider）
        if snap["slot_ok"] == 0 and slot_n >= _DEAD_COUNTRY_MIN_N:
            rate_adj += float(_DEAD_COUNTRY_PENALTY)
            info["slot_mode"] = "dead_slot_zero_ok"
        elif snap["slot_ok"] == 0 and slot_n >= 3:
            rate_adj += 8.0
            info["slot_mode"] = "slot_weak"
        else:
            info["slot_mode"] = "slot_rated"
        delta += rate_adj
        info["slot_rate_adj"] = round(rate_adj, 3)
        info["slot_rate"] = round(rate, 3)
        info["slot_adj"] = round(consec_pen + rate_adj, 3)

    # —— 国家级：仅当本档样本不足时作先验；整国全灭才传染未试档 ——
    cty_adj = 0.0
    cty_mode = "none"
    if country_n > 0:
        info["country_rate"] = round(float(snap["country_ok"]) / float(country_n), 3)

    if slot_n < _DELIVERY_SLOT_MIN_N:
        # 本档还没试够：看同国其它档
        if country_n >= _DEAD_COUNTRY_MIN_N and int(snap["country_ok"] or 0) == 0:
            # 同国所有已试档都 0 成功 → 未试档也沉底（巴西）
            cty_adj = float(_DEAD_COUNTRY_PENALTY)
            cty_mode = "dead_country_zero_ok"
        elif good_slots >= 1:
            # 同国已有好档：未试档只轻探索，不连坐
            cty_adj = 0.0
            cty_mode = "sibling_good_explore"
        elif country_n >= _DELIVERY_COUNTRY_MIN_N:
            rate = float(snap["country_ok"]) / float(country_n)
            if dead_slots >= _DELIVERY_COUNTRY_MIN_DEAD_SLOTS and rate < 0.12:
                cty_adj = (0.5 - rate) * 14.0 * 0.55 + 6.0
                cty_mode = "multi_dead_contagion"
            else:
                cty_adj = (0.5 - rate) * 14.0 * 0.25
                cty_mode = "country_prior_for_untried"
        else:
            cty_mode = "insufficient_country"
    else:
        # 本档已有样本：国家只极弱校正（档位为主）
        if country_n >= _DELIVERY_COUNTRY_MIN_N:
            rate = float(snap["country_ok"]) / float(country_n)
            cty_adj = (0.5 - rate) * 14.0 * 0.08
            cty_mode = "slot_primary_country_weak"

    if abs(cty_adj) >= 0.05:
        delta += cty_adj
    info["country_adj"] = round(cty_adj, 3)
    info["country_mode"] = cty_mode
    info["country_weight"] = cty_mode
    info["delta"] = round(delta, 3)
    return float(delta), info


def _inflight_prune_locked(now: float | None = None) -> None:
    now = time.time() if now is None else now
    for k, exp in list(_INFLIGHT_SLOTS.items()):
        if float(exp) <= now:
            _INFLIGHT_SLOTS.pop(k, None)


def claim_slot_inflight(slot_key: str, *, ttl: float | None = None) -> None:
    """标记槽位正在被某线程取号，其它线程排序时大幅降权。"""
    key = str(slot_key or "").strip()
    if not key:
        return
    with _INFLIGHT_LOCK:
        _inflight_prune_locked()
        _INFLIGHT_SLOTS[key] = time.time() + float(ttl if ttl is not None else _INFLIGHT_TTL_SEC)


def release_slot_inflight(slot_key: str) -> None:
    key = str(slot_key or "").strip()
    if not key:
        return
    with _INFLIGHT_LOCK:
        _INFLIGHT_SLOTS.pop(key, None)


def inflight_score_penalty(slot_key: str) -> float:
    key = str(slot_key or "").strip()
    if not key:
        return 0.0
    with _INFLIGHT_LOCK:
        _inflight_prune_locked()
        if key in _INFLIGHT_SLOTS:
            return float(_INFLIGHT_PENALTY)
    return 0.0


def _global_delivery_rate_locked(channel: str) -> float:
    """持锁：窗口内全局到码率，作贝叶斯先验。"""
    events = _DELIVERY_EVENTS.get(channel) or []
    if not events:
        return 0.28
    ok = sum(int(ev.get("ok") or 0) for ev in events)
    return float(ok) / float(len(events))


def _rate_hat(ok: int, n: int, prior_rate: float, prior_n: float | None = None) -> float:
    """贝叶斯平滑到码率：样本少时贴近全局均值，样本多时贴近真实。"""
    pn = float(prior_n if prior_n is not None else _RATE_PRIOR_N)
    pr = max(0.0, min(1.0, float(prior_rate)))
    return (float(ok) + pr * pn) / (float(n) + pn)


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    p = max(0.0, min(100.0, float(p)))
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(math.floor(k))
    c = min(len(sorted_vals) - 1, f + 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def _slot_quality_label(delivery_info: dict | None = None) -> str:
    """样本质量粗标：dead / low / ok / thin（样本薄）。"""
    info = delivery_info or {}
    slot_n = int(info.get("slot_n") or 0)
    slot_ok = int(info.get("slot_ok") or 0)
    cty_n = int(info.get("country_n") or 0)
    cty_ok = int(info.get("country_ok") or 0)
    if info.get("slot_mode") == "dead_slot_zero_ok":
        return "dead"
    if slot_n >= _DEAD_COUNTRY_MIN_N and slot_ok == 0:
        return "dead"
    if info.get("country_mode") == "dead_country_zero_ok" and slot_n < _DELIVERY_SLOT_MIN_N:
        return "dead"
    if cty_n >= _DEAD_COUNTRY_MIN_N and cty_ok == 0 and slot_n < _DELIVERY_SLOT_MIN_N:
        return "dead"
    if slot_n >= _TIER_HOT_MIN_N:
        rate = float(slot_ok) / float(slot_n)
        if rate >= _TIER_HOT_MIN_RATE:
            return "ok"
        if rate >= _TIER_WARM_MIN_RATE:
            return "ok"
        return "low"
    if slot_n > 0 and slot_ok == 0 and slot_n >= 2:
        return "low"
    return "thin"


def _slot_tier(delivery_info: dict | None = None) -> str:
    """兼容旧测试：映射到 hot/explore/warm/cold/dead。"""
    q = _slot_quality_label(delivery_info)
    info = delivery_info or {}
    slot_n = int(info.get("slot_n") or 0)
    slot_ok = int(info.get("slot_ok") or 0)
    if q == "dead":
        return "dead"
    if q == "thin":
        return "explore"
    if q == "low":
        return "cold"
    if slot_n >= _TIER_HOT_MIN_N:
        rate = float(slot_ok) / float(slot_n) if slot_n else 0.0
        if rate >= _TIER_HOT_MIN_RATE:
            return "hot"
        return "warm"
    return "explore"


def _country_tier(country: str, delivery_info: dict | None = None) -> str:
    return _slot_tier(delivery_info)


def classify_slot_bucket(
    row: dict,
    *,
    price_p50: float,
    price_p75: float,
) -> str:
    """
    由当前候选价位分布 + 样本库到码率，把档位分成：
      value(平价) | explore(少样本探索) | fallback(高价兜底) | junk(垃圾)
    不写死国家/供应商 ID。
    """
    price = _safe_float(row.get("price"), -1.0)
    info = row.get("delivery_info") or {}
    rh = _safe_float(row.get("rate_hat"), -1.0)
    if rh < 0:
        # 兜底：从 info 粗算
        sn = int(info.get("slot_n") or 0)
        so = int(info.get("slot_ok") or 0)
        cn = int(info.get("country_n") or 0)
        co = int(info.get("country_ok") or 0)
        if sn > 0:
            rh = float(so) / float(sn)
        elif cn > 0:
            rh = float(co) / float(cn)
        else:
            rh = 0.28
    q = _slot_quality_label(info)
    slot_n = int(info.get("slot_n") or 0)

    if q == "dead" or (q == "low" and slot_n >= _TIER_HOT_MIN_N and rh < _TIER_WARM_MIN_RATE):
        return "junk"

    # 高价 + 到码尚可 → 兜底（典型美区，但不写死 ID）
    expensive = price >= max(float(price_p75), float(price_p50) * 1.35, 0.10) - 1e-9
    if expensive and rh >= _FALLBACK_MIN_RATE_HAT:
        return "fallback"
    if expensive and q == "ok" and rh >= 0.25:
        return "fallback"
    if expensive and slot_n < _TIER_HOT_MIN_N:
        # 贵且没样本：不当平价，也不当探索主力
        return "fallback"

    # 少样本 → 探索（发现新好档）
    if q == "thin" or slot_n < _TIER_HOT_MIN_N:
        if price <= float(price_p75) + 1e-9:
            return "explore"
        return "fallback"

    # 平价：价在中低位 + 到码率尚可
    affordable = price <= float(price_p75) + 1e-9
    if affordable and rh >= _VALUE_MIN_RATE_HAT:
        return "value"
    if affordable and q == "ok":
        return "value"
    if affordable and rh >= _TIER_WARM_MIN_RATE:
        return "value"

    if rh < _TIER_WARM_MIN_RATE and slot_n >= _TIER_HOT_MIN_N:
        return "junk"
    # 偏贵但不够兜底门槛
    if expensive:
        return "fallback"
    return "value"


def _jitter_bucket(group: list[dict]) -> list[dict]:
    if not group:
        return []

    def _sort_key(row: dict) -> tuple:
        return (
            float(row.get("score", 999) or 999),
            float(row.get("price", 999) or 999),
            -int(row.get("count") or 0),
        )

    group = list(group)
    free: list[dict] = []
    busy: list[dict] = []
    for g in group:
        sk = str(g.get("slot_key") or _slot_key(str(g.get("country") or ""), str(g.get("provider_id") or "")))
        pen = inflight_score_penalty(sk)
        g = dict(g)
        g["inflight_pen"] = pen
        g["score"] = float(g.get("score", 999) or 999) + pen
        if pen > 0:
            busy.append(g)
        else:
            free.append(g)
    free.sort(key=_sort_key)
    busy.sort(key=_sort_key)
    free = shard_provider_candidates(free, eps=_ACQUIRE_SHARD_EPS)
    # 额外轻量乱序前 2 名，增加探索/并发打散
    if len(free) >= 2 and random.random() < 0.35:
        free[0], free[1] = free[1], free[0]
    return free + busy


def order_candidates_for_acquire(
    candidates: list[dict],
    *,
    attempt_index: int = 1,
    explore_prob: float | None = None,
    junk_in_explore_prob: float | None = None,
) -> list[dict]:
    """
    样本库驱动分桶 + 换号阶段门控：
      - 第 1～3 次：只打「平价」；按概率插入「探索」少样本档
      - 第 4 次起：平价 → 探索 → 兜底
      - 垃圾档：不进常规队列，只在「触发随机探索」时以低概率塞入 1 个
    分桶完全由当前候选价格分位 + 落盘到码率决定，不写死国家。
    """
    if not candidates:
        return []
    prices = sorted(
        _safe_float(r.get("price"), -1.0)
        for r in candidates
        if _safe_float(r.get("price"), -1.0) >= 0
    )
    prices = [p for p in prices if p >= 0]
    p50 = _percentile(prices, 50) if prices else 0.08
    p75 = _percentile(prices, 75) if prices else 0.12

    buckets: dict[str, list[dict]] = {
        "value": [], "explore": [], "fallback": [], "junk": [],
    }
    for r in candidates:
        row = dict(r)
        bucket = classify_slot_bucket(row, price_p50=p50, price_p75=p75)
        row["bucket"] = bucket
        # 兼容日志字段
        row["tier_name"] = bucket
        buckets.setdefault(bucket, []).append(row)

    attempt = max(1, int(attempt_index or 1))
    allow_fallback = attempt > int(_BUDGET_PHASE_ATTEMPTS)
    exp_p = float(_EXPLORE_PROB if explore_prob is None else explore_prob)
    junk_p = float(
        _JUNK_IN_EXPLORE_PROB if junk_in_explore_prob is None else junk_in_explore_prob
    )
    has_explore_pool = bool(buckets.get("explore") or buckets.get("junk"))
    do_explore = random.random() < exp_p and has_explore_pool

    value = _jitter_bucket(buckets.get("value") or [])
    explore = _jitter_bucket(buckets.get("explore") or [])
    fallback = _jitter_bucket(buckets.get("fallback") or [])
    junk = _jitter_bucket(buckets.get("junk") or [])

    def _build_explore_inject() -> list[dict]:
        """随机探索头：少样本档 + 偶尔 1 个垃圾档（仅此路径可用垃圾）。"""
        inj: list[dict] = []
        if explore:
            inj.extend(explore[: max(1, min(2, len(explore)))])
        # 垃圾只在探索里：触发探索后再掷一次骰子
        if junk and random.random() < junk_p:
            pick = junk[0]  # 已 jitter，取队首 1 个
            pick = dict(pick)
            pick["explore_junk"] = True
            inj.append(pick)
        return inj

    out: list[dict] = []
    if not allow_fallback:
        # 前 3 次：平价为主；探索（含偶发垃圾）插前
        if do_explore:
            out.extend(_build_explore_inject())
        out.extend(value)
        # 剩余探索档接在平价后（不含垃圾全量）
        if explore:
            used = {str(x.get("slot_key") or "") for x in out}
            out.extend([e for e in explore if str(e.get("slot_key") or "") not in used])
        # 平价/探索都空才被迫兜底，避免彻底无号；仍不带垃圾
        if not out:
            out.extend(fallback)
    else:
        if do_explore:
            out.extend(_build_explore_inject())
        out.extend(value)
        if explore:
            used = {str(x.get("slot_key") or "") for x in out}
            out.extend([e for e in explore if str(e.get("slot_key") or "") not in used])
        out.extend(fallback)
        # 垃圾不进常规队列

    # 去重保序（探索插入可能重复）
    seen = set()
    uniq = []
    for r in out:
        sk = str(r.get("slot_key") or f"{r.get('country')}:{r.get('provider_id')}")
        if sk in seen:
            continue
        seen.add(sk)
        uniq.append(r)
    return uniq


def shard_provider_candidates(
    candidates: list[dict],
    *,
    eps: float | None = None,
    salt: int | None = None,
) -> list[dict]:
    """
    并发选号分片：在「分数接近最优」的第一梯队内旋转顺序，避免多线程齐步选同一槽。
    更差的候选相对顺序不变，仍接在后面。
    """
    if not candidates or len(candidates) <= 1:
        return list(candidates or [])
    rows = list(candidates)
    try:
        best = float(rows[0].get("score", 999) or 999)
    except Exception:
        best = 999.0
    band = float(eps if eps is not None else _ACQUIRE_SHARD_EPS)
    tier: list[dict] = []
    rest: list[dict] = []
    for r in rows:
        try:
            sc = float(r.get("score", 999) or 999)
        except Exception:
            sc = 999.0
        if sc <= best + band:
            tier.append(r)
        else:
            rest.append(r)
    if len(tier) <= 1:
        return rows
    if salt is None:
        # 线程 + 时间片：同秒内不同线程错开，跨秒也变化
        salt = (threading.get_ident() ^ int(time.time() * 3)) & 0x7FFFFFFF
    off = int(salt) % len(tier)
    if off:
        tier = tier[off:] + tier[:off]
    # 第一梯队前 3 名再轻量洗牌，进一步打散
    head_n = min(3, len(tier))
    if head_n > 1:
        head = tier[:head_n]
        # 稳定伪随机：按 salt 旋转即可，避免 import random 全局状态干扰测试
        h_off = (int(salt) // 7) % head_n
        if h_off:
            head = head[h_off:] + head[:h_off]
        tier = head + tier[head_n:]
    return tier + rest


def get_delivery_stats_snapshot(*, provider: str | None = None, limit: int = 20) -> list[dict]:
    """运维/测试：按槽位汇总最近次数窗口内投递表现。"""
    name = (provider or _provider()).strip().lower()
    with _DELIVERY_LOCK:
        _ensure_delivery_loaded_locked()
        events = _delivery_prune_locked(name)
        by_slot: dict[str, dict] = {}
        for ev in events:
            slot = str(ev.get("slot") or "")
            if not slot:
                # 仅国家级事件不进槽位快照
                continue
            row = by_slot.setdefault(
                slot,
                {"slot": slot, "country": str(ev.get("country") or ""), "ok": 0, "n": 0},
            )
            row["n"] += 1
            row["ok"] += int(ev.get("ok") or 0)
        rows = list(by_slot.values())
    for r in rows:
        n = max(1, int(r["n"]))
        r["rate"] = round(float(r["ok"]) / float(n), 3)
        r["fail"] = int(r["n"]) - int(r["ok"])
    rows.sort(key=lambda x: (-int(x["n"]), float(x["rate"])))
    return rows[: max(1, int(limit))]


def _slot_bucket(channel: str | None = None) -> dict[str, dict]:
    """软冷却表：value={until, reason, sec}；过期自动剔除。"""
    name = (channel or _provider()).strip().lower()
    now = time.time()
    bucket = _SLOT_COOLDOWN.setdefault(name, {})
    for k, ent in list(bucket.items()):
        try:
            until = float((ent or {}).get("until") or 0)
        except Exception:
            until = 0.0
        if until <= now:
            bucket.pop(k, None)
    return bucket


def _slot_fail_bucket(channel: str | None = None) -> dict[str, int]:
    name = (channel or _provider()).strip().lower()
    return _SLOT_FAILS.setdefault(name, {})


def mark_slot_cooldown(
    country: str,
    provider_id: str = "",
    *,
    channel: str | None = None,
    reason: str = "no_numbers",
    cooldown: int | None = None,
) -> None:
    """
    记录槽位「软冷却」——只影响权重，不踢出候选列表。

    时长仅表示软降权窗口；选号始终靠 score（价格 + 连跪 + 本软分）。
    """
    cid = str(country or "").strip()
    if not cid:
        return
    name = (channel or _provider()).strip().lower()
    reason_l = str(reason or "").strip().lower()
    if cooldown is not None:
        sec = int(cooldown)
    elif reason_l in ("timeout", "code_timeout", "sms_timeout"):
        sec = int(_SLOT_COOLDOWN_TIMEOUT)
    elif reason_l in ("send_reject", "send_not_accepted", "invalid_phone", "whatsapp"):
        sec = int(_SLOT_COOLDOWN_SEND_REJECT)
    else:
        sec = int(_SLOT_COOLDOWN_NO_NUMBERS)
    sec = max(30, sec)
    key = _slot_key(cid, provider_id)
    now = time.time()
    with _DELIVERY_LOCK:
        _ensure_delivery_loaded_locked()
        _slot_bucket(name)[key] = {
            "until": now + sec,
            "reason": reason_l or "no_numbers",
            "sec": sec,
            "at": now,
        }
        fails = _slot_fail_bucket(name)
        fails[key] = int(fails.get(key) or 0) + 1
        fail_n = fails[key]
        _save_delivery_state_locked()
    base_pen = float(_SOFT_COOL_PENALTY.get(reason_l, 5.0))
    logger.info(
        f"[SMS:{_activate_label(name)}] 槽位软降权 {key} reason={reason_l or reason} "
        f"window={sec}s base_pen=+{base_pen:.0f} (fails={fail_n}，不踢候选，已落盘)"
    )


def soft_cooldown_score_delta(
    country: str,
    provider_id: str = "",
    *,
    channel: str | None = None,
) -> tuple[float, dict]:
    """
    软冷却加分（越大越差）。窗口内按剩余时间线性衰减；到期为 0。
    不排除候选，只参与排序。
    """
    cid = str(country or "").strip()
    if not cid:
        return 0.0, {}
    name = (channel or _provider()).strip().lower()
    key = _slot_key(cid, provider_id)
    with _DELIVERY_LOCK:
        _ensure_delivery_loaded_locked()
        bucket = _slot_bucket(name)
        ent = bucket.get(key)
        if not isinstance(ent, dict):
            return 0.0, {}
        now = time.time()
        try:
            until = float(ent.get("until") or 0)
            total = float(ent.get("sec") or 0)
        except Exception:
            return 0.0, {}
        if until <= now:
            return 0.0, {}
        remaining = until - now
        if total <= 0:
            total = remaining
        reason = str(ent.get("reason") or "no_numbers")
        base = float(_SOFT_COOL_PENALTY.get(reason, 5.0))
        # 线性衰减：刚触发满分，到期归零 → 抖动槽会平滑回到价格竞争
        frac = max(0.0, min(1.0, remaining / max(total, 1.0)))
        pen = base * frac
        return pen, {
            "soft_cool_reason": reason,
            "soft_cool_remaining": round(remaining, 1),
            "soft_cool_base": base,
            "soft_cool_penalty": round(pen, 3),
        }


def mark_country_no_numbers(country: str, *, provider: str | None = None, cooldown: int | None = None) -> None:
    """兼容旧接口：整国无号时对 country:- 记软降权（不再硬排除国家）。"""
    mark_slot_cooldown(country, "", channel=provider, reason="no_numbers", cooldown=cooldown)
    cid = str(country or "").strip()
    if not cid:
        return
    name = (provider or _provider()).strip().lower()
    sec = int(cooldown if cooldown is not None else _NO_NUMBERS_COOLDOWN_SEC)
    bucket = _NO_NUMBERS_UNTIL.setdefault(name, {})
    bucket[cid] = time.time() + max(30, sec)


def clear_country_no_numbers(country: str | None = None, *, provider: str | None = None) -> None:
    """清除软冷却标记。"""
    name = (provider or _provider()).strip().lower()
    if country in (None, ""):
        _NO_NUMBERS_UNTIL.setdefault(name, {}).clear()
        _SLOT_COOLDOWN.setdefault(name, {}).clear()
        return
    cid = str(country).strip()
    _NO_NUMBERS_UNTIL.setdefault(name, {}).pop(cid, None)
    bucket = _SLOT_COOLDOWN.setdefault(name, {})
    for k in list(bucket.keys()):
        if k == cid or k.startswith(cid + ":"):
            bucket.pop(k, None)


def get_excluded_countries(*, provider: str | None = None, extra: set[str] | None = None) -> set[str]:
    """不再因 NO_NUMBERS 硬排除国家；仅合并调用方 extra（权重调度）。"""
    excluded: set[str] = set()
    if extra:
        excluded |= {str(x).strip() for x in extra if str(x).strip()}
    return excluded


def remember_activation_meta(activation_id: str, meta: dict) -> None:
    aid = str(activation_id or "").strip()
    if aid:
        _ACTIVATION_META[aid] = dict(meta or {})


def mark_activation_send_rejected(activation_id: str, reason: str = "send_not_accepted") -> None:
    """OpenAI 发码拒绝后调用：冷却该 activation 对应的供应商槽，并记入全局投递失败。"""
    aid = str(activation_id or "").strip()
    meta = _ACTIVATION_META.get(aid) or {}
    country = str(meta.get("country") or "").strip()
    provider_id = str(meta.get("provider_id") or "").strip()
    channel = str(meta.get("channel") or _provider()).strip()
    if country:
        reason_l = str(reason or "").strip().lower()
        # wait_for_sms_code 已先记 timeout 时：按超时短冷却，避免 1h 把抖动中的墨/英锁死
        prev_delivery = _DELIVERY_BY_AID.get(aid) if aid else None
        if prev_delivery == "timeout" or reason_l in ("timeout", "code_timeout", "sms_timeout"):
            cool_reason = "timeout"
        else:
            cool_reason = reason or "send_not_accepted"
        mark_slot_cooldown(country, provider_id, channel=channel, reason=cool_reason)
        # 全局降权：与 timeout 共用 aid 去重，避免双计
        try:
            record_activation_delivery(activation_id, reason or "reject")
        except Exception:
            pass
    else:
        logger.debug("[SMS] mark_activation_send_rejected 无 meta activation_id=%s", activation_id)


def get_prices_v3(
    http: CurlSession | None = None,
    *,
    provider: str | None = None,
    service: str | None = None,
    country: str | None = None,
) -> dict:
    """SMSBower/Hero 风格 getPricesV3：{country:{service:{provider_id:{price,count}}}}"""
    provider = (provider or _provider()).strip().lower()
    if not _is_activate_provider(provider):
        raise SmsProviderError(f"{provider} 不支持 getPricesV3")
    own_http = http is None
    http = http or _http()
    try:
        params = {"action": "getPricesV3"}
        svc = str(service if service is not None else getattr(_cfg, "SMS_SERVICE", "") or "").strip()
        cty = str(country if country is not None else "").strip()
        if svc:
            params["service"] = svc
        if cty:
            params["country"] = cty
        data = _request_activate_json(http, params, provider=provider)
        if isinstance(data, dict):
            return data
        raise SmsProviderError(f"{_activate_label(provider)} getPricesV3 非预期响应：{str(data)[:200]}")
    finally:
        if own_http:
            http.close()


def _parse_v3_providers(data: dict, country: str, service: str) -> list[dict]:
    """从 getPricesV3 响应解析某国某服务的供应商列表。"""
    country = str(country)
    service = str(service)
    root = data.get(country) or data.get(str(int(country)) if country.isdigit() else country) or {}
    if not isinstance(root, dict):
        return []
    svc_map = root.get(service) if isinstance(root.get(service), dict) else None
    # 有时直接就是 provider map
    if svc_map is None and any(isinstance(v, dict) and ("price" in v or "count" in v) for v in root.values()):
        svc_map = root
    if not isinstance(svc_map, dict):
        return []
    rows = []
    for pid, info in svc_map.items():
        if not isinstance(info, dict):
            continue
        price = _safe_float(info.get("price") if info.get("price") is not None else info.get("cost"), -1.0)
        count = _safe_int(info.get("count"), 0)
        provider_id = str(info.get("provider_id") or pid).strip()
        if price < 0 or count <= 0 or not provider_id:
            continue
        rows.append({
            "country": country,
            "provider_id": provider_id,
            "price": price,
            "count": count,
        })
    return rows


def list_provider_candidates(
    http: CurlSession | None = None,
    *,
    provider: str | None = None,
    service: str | None = None,
    countries: list[str] | None = None,
    max_price: float | None = None,
    min_price: float | None = None,
    min_stock: int | None = None,
    include_low_quality: bool = False,
) -> list[dict]:
    """
    构建（国家×供应商×价位）候选列表，按「上限内尽量便宜 + 成功率惩罚」排序。

    - 不默认 ban 某个国家；只冷却差供应商槽位
    - 首轮会过滤「相对该国中位异常低价」的高库存档（虚拟号）
    """
    channel = (provider or _provider()).strip().lower()
    svc = str(service or getattr(_cfg, "SMS_SERVICE", "") or "dr").strip() or "dr"
    max_p = _cfg_max_price() if max_price is None else float(max_price or 0)
    min_p = _cfg_min_price() if min_price is None else float(min_price or 0)
    min_stock_i = _cfg_min_stock() if min_stock is None else int(min_stock)
    floor_ratio = _safe_float(getattr(_cfg, "SMS_PRICE_FLOOR_RATIO", 0.25), 0.25)
    preferred = _cfg_preferred_countries()

    own_http = http is None
    http = http or _http()
    try:
        manual_wl = _parse_country_list(getattr(_cfg, "SMS_COUNTRY_WHITELIST", "") or "")
        # 仅在未指定 countries 时解析默认白名单（可能打 Top API）
        top_rows: list[dict] = []
        if countries:
            country_list = [str(c).strip() for c in countries if str(c).strip()]
            # 仅「手动硬白名单」过滤显式国家；动态 Top 不拦截显式 country=
            if manual_wl and not _allow_outside_whitelist():
                filtered = [c for c in country_list if c in set(manual_wl)]
                if filtered:
                    country_list = filtered
                else:
                    logger.warning(
                        "[SMS] 请求国家 %s 均不在手动白名单 %s，回退手动白名单",
                        country_list, manual_wl,
                    )
                    country_list = list(manual_wl)
            whitelist_set = set(manual_wl) if manual_wl else set(country_list)
        else:
            whitelist, top_rows = resolve_country_whitelist(http, provider=channel, service=svc)
            whitelist_set = set(whitelist)
            country_list = [c for c in preferred if c in whitelist_set]
            for c in whitelist:
                if c not in country_list:
                    country_list.append(c)
            if _auto_country_enabled() and _allow_outside_whitelist():
                try:
                    top = get_top_countries(http, provider=channel, service=svc)
                    for row in top:
                        c = str(row.get("country") or "").strip()
                        if c and c not in country_list:
                            country_list.append(c)
                except Exception:
                    pass
        # 去重保序
        seen = set()
        ordered = []
        for c in country_list:
            if c and c not in seen:
                seen.add(c)
                ordered.append(c)
        if whitelist_set and not _allow_outside_whitelist():
            src = "top_service" if top_rows else "static"
            logger.info(
                "[SMS:%s] 国家白名单硬限制 source=%s：%s（允许白名单外=%s）",
                _activate_label(channel),
                src,
                ",".join(ordered) if ordered else "-",
                _allow_outside_whitelist(),
            )

        fails = _slot_fail_bucket(channel)
        all_rows: list[dict] = []

        # Top 接口的 partners 通常是 Gold 精选；但 Gold 常被 maxPrice 滤光导致 0 槽。
        # 因此：Gold（Top partners）+ Silver/全部（getPricesV3）合并，Gold 略优先。
        top_by_country: dict[str, dict] = {
            str(r.get("country")): r for r in (top_rows or []) if r.get("country")
        }
        gold_n = 0
        silver_n = 0
        for rank, cty in enumerate(ordered[:30]):
            by_pid: dict[str, dict] = {}
            top_item = top_by_country.get(str(cty))
            if top_item and top_item.get("partners"):
                for p in top_item["partners"]:
                    pid = str(p.get("provider_id") or "").strip()
                    if not pid:
                        continue
                    by_pid[pid] = {
                        "country": cty,
                        "provider_id": pid,
                        "price": _safe_float(p.get("price"), -1.0),
                        "count": _safe_int(p.get("count"), 0),
                        "from_top": True,
                        "tier": "gold",
                        "top_rank": rank,
                    }
            # 始终补 getPricesV3：补上 Silver 及 Top 未列出的供应商
            try:
                data = get_prices_v3(http, provider=channel, service=svc, country=cty)
                for p in _parse_v3_providers(data, cty, svc):
                    pid = str(p.get("provider_id") or "").strip()
                    if not pid:
                        # 无 provider 的聚合价，仅当该国完全没有槽时保留
                        if by_pid:
                            continue
                        pid = ""
                    if pid in by_pid:
                        # 已有 Gold：刷新库存/价格，保留 gold 标记
                        cur = by_pid[pid]
                        if _safe_float(p.get("price"), -1) >= 0:
                            cur["price"] = _safe_float(p.get("price"), cur["price"])
                        if _safe_int(p.get("count"), 0) > 0:
                            cur["count"] = max(int(cur.get("count") or 0), _safe_int(p.get("count"), 0))
                        continue
                    by_pid[pid or f"v3-{len(by_pid)}"] = {
                        "country": cty,
                        "provider_id": pid,
                        "price": _safe_float(p.get("price"), -1.0),
                        "count": _safe_int(p.get("count"), 0),
                        "from_top": False,
                        "tier": "silver",
                        "top_rank": rank,
                    }
            except Exception as exc:
                logger.debug("[SMS] getPricesV3 country=%s 失败：%s", cty, exc)
                if not by_pid:
                    try:
                        prices = get_prices(http, provider=channel, service=svc, country=cty)
                        cdata = prices.get(cty) or prices.get(str(cty)) or {}
                        svc_data = cdata.get(svc) if isinstance(cdata, dict) else {}
                        if isinstance(svc_data, dict):
                            price = _safe_float(svc_data.get("cost") or svc_data.get("price"), -1)
                            count = _safe_int(svc_data.get("count"), 0)
                            if price >= 0 and count > 0:
                                by_pid[""] = {
                                    "country": cty, "provider_id": "", "price": price, "count": count,
                                    "from_top": False, "tier": "silver", "top_rank": rank,
                                }
                    except Exception:
                        pass

            rows = list(by_pid.values())
            if not rows:
                continue
            eligible = []
            for r in rows:
                if r.get("price", -1) < 0:
                    continue
                if r["count"] < min_stock_i:
                    continue
                if max_p > 0 and r["price"] > max_p + 1e-9:
                    continue
                if min_p > 0 and r["price"] < min_p - 1e-9:
                    continue
                eligible.append(r)
            if not eligible:
                continue
            prices_only = sorted(r["price"] for r in eligible)
            median = prices_only[len(prices_only) // 2] if prices_only else 0.0
            for r in eligible:
                key = _slot_key(r["country"], r.get("provider_id") or "")
                # 不再硬踢冷却槽：统一权重调度（价格 + 连跪 + 软冷却分）
                # Gold 精选不做地板价误杀；Silver 仍防异常低价虚拟号
                if (
                    not r.get("from_top")
                    and str(r.get("tier") or "") != "gold"
                    and not include_low_quality
                    and median > 0
                    and floor_ratio > 0
                    and r["price"] < median * floor_ratio
                    and r["count"] >= max(min_stock_i * 10, 500)
                ):
                    continue
                r = dict(r)
                r["slot_key"] = key
                r["fail_count"] = int(fails.get(key) or 0)
                # 智能分：价格×240 - 贝叶斯到码率×55 + 连跪/软降权/死国；国家不写死。
                cid = str(r["country"])
                in_cfg_preferred = 0 if cid in preferred else 1
                top_rank = int(r.get("top_rank") if r.get("top_rank") is not None else rank)
                is_gold = bool(r.get("from_top")) or str(r.get("tier") or "") == "gold"
                gold_pen = 0.0 if is_gold else 0.4
                fallback_penalty = float(_FALLBACK_COUNTRY_PENALTY.get(cid, 0.0))
                delivery_adj, delivery_info = delivery_score_delta(
                    cid,
                    str(r.get("provider_id") or ""),
                    channel=channel,
                )
                soft_adj, soft_info = soft_cooldown_score_delta(
                    cid,
                    str(r.get("provider_id") or ""),
                    channel=channel,
                )
                # 到码率：同国不同档位各自算；档位样本优先，否则同国先验，再全局
                with _DELIVERY_LOCK:
                    _ensure_delivery_loaded_locked()
                    prior = _global_delivery_rate_locked(channel)
                slot_n = int((delivery_info or {}).get("slot_n") or 0)
                slot_ok = int((delivery_info or {}).get("slot_ok") or 0)
                cty_n = int((delivery_info or {}).get("country_n") or 0)
                cty_ok = int((delivery_info or {}).get("country_ok") or 0)
                if slot_n >= 1:
                    # 即使 1 次也用档位 + 先验，避免同国好档带飞差档
                    rh = _rate_hat(slot_ok, slot_n, prior, prior_n=_RATE_PRIOR_N)
                    rate_src = "slot"
                elif cty_n > 0:
                    # 未试档：用同国率作弱先验（同国有好档时不至于当死档）
                    rh = _rate_hat(cty_ok, cty_n, prior, prior_n=_RATE_PRIOR_N + 2)
                    rate_src = "country_prior"
                else:
                    rh = float(prior)
                    rate_src = "global_prior"
                # 高到码率减分（更优）；与价格权衡：rate 从 0→1 ≈ 省 55 分 ≈ $0.23
                rate_value_adj = -float(rh) * float(_RATE_VALUE_WEIGHT)
                tier_name = _slot_tier(delivery_info)
                r["preferred"] = in_cfg_preferred
                r["tier_name"] = tier_name
                r["rate_hat"] = round(rh, 4)
                r["rate_src"] = rate_src
                r["delivery_adj"] = float(delivery_adj)
                r["delivery_info"] = delivery_info
                r["soft_cool_adj"] = float(soft_adj)
                r["soft_cool_info"] = soft_info
                r["strategy_adj"] = float(rate_value_adj)
                r["score"] = (
                    float(r["price"]) * float(_SCORE_PRICE_WEIGHT)
                    + float(top_rank) * 0.8
                    + float(in_cfg_preferred) * 1.0
                    + float(gold_pen)
                    + float(fallback_penalty)
                    + float(rate_value_adj)
                    + 0.25 * float(r["fail_count"])
                    + float(delivery_adj)
                    + float(soft_adj)
                    - 0.02 * math.log1p(float(r["count"]))
                )
                all_rows.append(r)
                if is_gold:
                    gold_n += 1
                else:
                    silver_n += 1
        if all_rows:
            logger.info(
                "[SMS:%s] 候选来自 Top国 Gold+Silver 供应商：%s 槽 (gold=%s silver=%s)",
                _activate_label(channel), len(all_rows), gold_n, silver_n,
            )
            # 有投递信号时打一行摘要，方便看并发全局加权是否在起作用
            weighted = [
                r for r in all_rows
                if abs(float(r.get("delivery_adj") or 0.0)) >= 0.5
            ]
            if weighted:
                weighted.sort(key=lambda x: float(x.get("delivery_adj") or 0.0), reverse=True)
                bits = []
                for r in weighted[:8]:
                    info = r.get("delivery_info") or {}
                    bits.append(
                        f"{r.get('slot_key')}:adj={float(r.get('delivery_adj') or 0):+.1f}"
                        f"(slot={info.get('slot_ok', 0)}/{info.get('slot_n', 0)}"
                        f",cty={info.get('country_ok', 0)}/{info.get('country_n', 0)})"
                    )
                logger.info(
                    "[SMS:%s] 全局投递加权生效 top：%s",
                    _activate_label(channel),
                    " | ".join(bits),
                )
    finally:
        if own_http:
            http.close()

    all_rows.sort(key=lambda x: (x.get("score", 999), x.get("price", 999), -x.get("count", 0)))
    return all_rows


def list_best_countries(
    http: CurlSession | None = None,
    *,
    provider: str | None = None,
    service: str | None = None,
    min_stock: int | None = None,
    max_price: float | None = None,
    exclude: set[str] | None = None,
    limit: int = 20,
) -> list[dict]:
    """
    返回排序后的候选国家列表（含 price/count）。

    排序策略：
      1) 优先 OpenAI SMS 白名单国家
      2) 同组内按 价格升序、库存降序
      3) 白名单耗尽后，附带其它有货国家（避免一直卡死在无号国）
    """
    min_stock_i = _cfg_min_stock() if min_stock is None else int(min_stock)
    max_price_f = _cfg_max_price() if max_price is None else float(max_price or 0)
    preferred = set(_cfg_country_whitelist()) | set(_cfg_preferred_countries())
    if not preferred:
        preferred = set(_OPENAI_SMS_ALLOWED_COUNTRIES)
    excluded = get_excluded_countries(provider=provider, extra=exclude)
    try:
        rows = get_top_countries(http, provider=provider, service=service)
    except Exception as exc:
        logger.warning(f"[SMS] list_best_countries 查询失败：{exc}")
        return []
    if not rows:
        return []

    def _ok(row: dict, *, require_min_stock: bool, preferred_only: bool) -> bool:
        country_id = str(row.get("country") or "")
        if not country_id or country_id in excluded:
            return False
        if preferred_only and country_id not in preferred:
            return False
        price = _safe_float(row.get("price"), 0.0)
        count = _safe_int(row.get("count"), 0)
        if require_min_stock and count < min_stock_i:
            return False
        if not require_min_stock and count <= 0:
            return False
        if max_price_f > 0 and price > max_price_f:
            return False
        return True

    def _rank(row: dict) -> tuple:
        country_id = str(row.get("country") or "")
        pref = 0 if country_id in preferred else 1
        return (pref, _safe_float(row.get("price"), 999.0), -_safe_int(row.get("count"), 0))

    stages = ((True, True), (True, False))
    if _allow_outside_whitelist():
        stages = ((True, True), (True, False), (False, True), (False, False))
    for preferred_only, require_min in stages:
        picked = [r for r in rows if _ok(r, require_min_stock=require_min, preferred_only=preferred_only)]
        if picked:
            picked.sort(key=_rank)
            return picked[: max(1, int(limit or 20))]
    return []


def get_best_country(
    http: CurlSession | None = None,
    *,
    provider: str | None = None,
    service: str | None = None,
    min_stock: int | None = None,
    max_price: float | None = None,
    exclude: set[str] | None = None,
) -> str | None:
    """自动选择最优国家：优先配置国/白名单 + 价格低 + 库存足。"""
    # 优先用供应商候选的国家
    try:
        cands = list_provider_candidates(
            http,
            provider=provider,
            service=service,
            max_price=max_price,
            min_stock=min_stock,
            include_low_quality=False,
        )
        if exclude:
            cands = [c for c in cands if str(c.get("country")) not in exclude]
        if cands:
            return str(cands[0].get("country") or "") or None
    except Exception:
        pass
    rows = list_best_countries(
        http,
        provider=provider,
        service=service,
        min_stock=min_stock,
        max_price=max_price,
        exclude=exclude,
        limit=1,
    )
    if not rows:
        return None
    return str(rows[0].get("country") or "") or None


def resolve_acquire_country(
    http: CurlSession | None = None,
    country: str | None = None,
    *,
    exclude: set[str] | None = None,
) -> str:
    """取号用国家：显式参数 > 供应商候选最优国 > 配置 SMS_COUNTRY。"""
    if country not in (None, ""):
        return str(country).strip()
    provider = _provider()
    excluded = get_excluded_countries(provider=provider, extra=exclude)
    if _is_activate_provider(provider) and provider != "grizzly":
        best = get_best_country(http, provider=provider, exclude=excluded)
        if best:
            logger.info(
                f"[SMS:{_activate_label(provider)}] 自动选国：{best}"
                + (f"（已跳过: {','.join(sorted(excluded))}）" if excluded else "")
            )
            return best
        logger.warning(f"[SMS:{_activate_label(provider)}] 自动选国失败，回退 SMS_COUNTRY")
    return str(getattr(_cfg, "SMS_COUNTRY", "") or "").strip()


def _l_url(path: str) -> str:
    base = str(getattr(_cfg, "L_API_BASE", "") or "").strip()
    if not base:
        raise SmsProviderError("L_API_BASE 不能为空")
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _l_headers() -> dict:
    token = str(getattr(_cfg, "L_ADMIN_AUTH_CODE", "") or "").strip()
    if not token:
        raise SmsProviderError("L_ADMIN_AUTH_CODE 不能为空")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post_l_json(http: CurlSession, path: str, payload: dict) -> dict:
    resp = http.post(_l_url(path), headers=_l_headers(), data=json.dumps(payload))
    text = (resp.text or "").strip()
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code != 200:
        msg = data.get("error") if isinstance(data, dict) else ""
        raise SmsProviderError(f"L HTTP {resp.status_code}: {(msg or text)[:200]}")
    if isinstance(data, dict) and data.get("error"):
        error = str(data.get("error") or "")
        raw = str(data.get("raw") or "")
        combined = f"{error} {raw}".strip()
        if "NO_BALANCE" in combined or "余额不足" in combined:
            raise SmsNoBalanceError(f"L 余额不足：{combined}")
        if "NO_NUMBERS" in combined or "暂无号码" in combined:
            raise SmsNoNumbersError(f"L 暂无可用号码：{combined}")
        raise SmsProviderError(f"L 请求失败：{combined}")
    if not isinstance(data, dict):
        raise SmsProviderError(f"L 响应不是 JSON 对象：{text[:200]}")
    return data


def _h_url(path: str) -> str:
    base = str(getattr(_cfg, "H_API_BASE", "") or "").strip()
    if not base:
        raise SmsProviderError("H_API_BASE 不能为空")
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _h_headers() -> dict:
    token = str(getattr(_cfg, "H_ADMIN_AUTH_CODE", "") or "").strip()
    if not token:
        raise SmsProviderError("H_ADMIN_AUTH_CODE 不能为空")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post_h_json(http: CurlSession, path: str, payload: dict) -> dict:
    resp = http.post(_h_url(path), headers=_h_headers(), data=json.dumps(payload))
    text = (resp.text or "").strip()
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code != 200:
        msg = data.get("error") if isinstance(data, dict) else ""
        raise SmsProviderError(f"H HTTP {resp.status_code}: {(msg or text)[:200]}")
    if isinstance(data, dict) and data.get("error"):
        error = str(data.get("error") or "")
        raw = str(data.get("raw") or "")
        combined = f"{error} {raw}".strip()
        if "NO_BALANCE" in combined or "余额不足" in combined:
            raise SmsNoBalanceError(f"H 余额不足：{combined}")
        if "NO_NUMBERS" in combined or "暂无号码" in combined:
            raise SmsNoNumbersError(f"H 暂无可用号码：{combined}")
        raise SmsProviderError(f"H 请求失败：{combined}")
    if not isinstance(data, dict):
        raise SmsProviderError(f"H 响应不是 JSON 对象：{text[:200]}")
    return data


def _release_h_number(activation_id: str, http: CurlSession | None = None) -> dict:
    """调用 H_API /api/admin/h/release 释放单个号码。"""
    activation_id = str(activation_id or "").strip()
    if not activation_id:
        raise SmsProviderError("H release 缺少 id")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_h_json(http, "/api/admin/h/release", {"id": activation_id})
        failed = data.get("failed") if isinstance(data, dict) else None
        if isinstance(failed, list) and failed:
            detail = json.dumps(failed, ensure_ascii=False)[:300]
            raise SmsProviderError(f"H release 失败 id={activation_id}: {detail}")
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        logger.info(f"[SMS:H] 已释放号码 id={activation_id}, released={released}")
        _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def release_h_numbers(ids: list[str], http: CurlSession | None = None) -> dict:
    """批量释放 H 号码。"""
    ids = [str(x or "").strip() for x in (ids or []) if str(x or "").strip()]
    if not ids:
        raise SmsProviderError("H release 缺少 ids")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_h_json(http, "/api/admin/h/release", {"ids": ids})
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        failed = data.get("failed") if isinstance(data, dict) else []
        logger.info(f"[SMS:H] 批量释放号码完成 released={released}, failed={len(failed) if isinstance(failed, list) else 0}")
        for activation_id in ids:
            _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def _release_l_number(activation_id: str, http: CurlSession | None = None) -> dict:
    """调用 L_API /api/admin/l/release 释放单个号码。"""
    activation_id = str(activation_id or "").strip()
    if not activation_id:
        raise SmsProviderError("L release 缺少 id")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_l_json(http, "/api/admin/l/release", {"id": activation_id})
        failed = data.get("failed") if isinstance(data, dict) else None
        if isinstance(failed, list) and failed:
            # 接口允许部分失败。单个释放时 failed 非空基本代表这个 id 释放失败。
            detail = json.dumps(failed, ensure_ascii=False)[:300]
            raise SmsProviderError(f"L release 失败 id={activation_id}: {detail}")
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        logger.info(f"[SMS:L] 已释放号码 id={activation_id}, released={released}")
        _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def release_l_numbers(ids: list[str], http: CurlSession | None = None) -> dict:
    """批量释放 L 号码，供工具/后续批处理复用。"""
    ids = [str(x or "").strip() for x in (ids or []) if str(x or "").strip()]
    if not ids:
        raise SmsProviderError("L release 缺少 ids")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_l_json(http, "/api/admin/l/release", {"ids": ids})
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        failed = data.get("failed") if isinstance(data, dict) else []
        logger.info(f"[SMS:L] 批量释放号码完成 released={released}, failed={len(failed) if isinstance(failed, list) else 0}")
        for activation_id in ids:
            _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def _normalize_phone_digits(value: str) -> str:
    """把平台返回/配置的号码片段规范化为纯数字，避免 +-849... 这类非法 E.164。"""
    return "".join(ch for ch in str(value or "").strip() if ch.isdigit())


def _normalize_l_phone(phone: str) -> str:
    phone = _normalize_phone_digits(phone)
    prefix = _normalize_phone_digits(getattr(_cfg, "L_PHONE_PREFIX", ""))
    if prefix and phone and not phone.startswith(prefix):
        return f"{prefix}{phone}"
    return phone


def _normalize_h_phone(phone: str) -> str:
    phone = _normalize_phone_digits(phone)
    prefix = _normalize_phone_digits(getattr(_cfg, "H_PHONE_PREFIX", ""))
    if prefix and phone and not phone.startswith(prefix):
        return f"{prefix}{phone}"
    return phone


def _h_phone_acquire_mode() -> str:
    """
    H 取号模式：
      - reusable/reuse/prefer_reuse：优先复用，调用 /api/admin/h/take-reusable-phone
      - new/fresh/always_new：每次取新号，调用 /api/admin/h/take-phone
    """
    raw = str(getattr(_cfg, "H_PHONE_ACQUIRE_MODE", "reusable") or "reusable").strip().lower()
    if raw in ("new", "fresh", "always_new", "take_phone", "take-phone", "每次取新号", "新号"):
        return "new"
    return "reusable"


# ============================================================
# 取号
# ============================================================

def acquire_number(
    http: CurlSession | None = None,
    service: str | None = None,
    country: str | None = None,
    *,
    attempt_index: int | None = None,
) -> tuple[str, str]:
    """
    取一个手机号（getNumber）。

    attempt_index: 外层手机验证换号次数（1-based）。
      1～3 只选样本库判定的「平价」档（偶发探索少样本）；
      ≥4 才开放「高价兜底」档。

    Returns:
        (activation_id, phone_number) —— phone_number 不带 + 前缀（如 16195366483）

    Raises:
        SmsNoNumbersError / SmsNoBalanceError / SmsProviderError
    """
    own_http = http is None
    http = http or _http()
    attempt_i = max(1, int(attempt_index or 1))
    try:
        if _provider() == "l":
            payload = {
                "service": service or _cfg.SMS_SERVICE,
                "country": country or _cfg.SMS_COUNTRY,
            }
            if _cfg.SMS_MAX_PRICE:
                payload["maxPrice"] = _cfg.SMS_MAX_PRICE

            data = _post_l_json(http, "/api/admin/l/take-phone", payload)
            item = data.get("item") or {}
            activation_id = str(item.get("id") or "").strip()
            raw_phone = str(item.get("phone") or "")
            raw_prefix = str(getattr(_cfg, "L_PHONE_PREFIX", "") or "")
            phone = _normalize_l_phone(raw_phone)
            if raw_phone.strip() != phone or raw_prefix.strip():
                logger.info(
                    f"[SMS:L] 号码规范化：raw_phone={raw_phone!r}, "
                    f"prefix={raw_prefix!r}, normalized=+{phone}"
                )
            if not activation_id or not phone:
                raise SmsProviderError(f"L take-phone 响应缺少 item.id/item.phone：{str(data)[:200]}")
            _ACQUIRED_AT[activation_id] = time.time()
            logger.info(f"[SMS:L] 取号成功：id={activation_id}, phone=+{phone}")
            return activation_id, phone

        if _provider() == "h":
            # H_API 使用 projectId + country；统一复用 SMS_SERVICE / SMS_COUNTRY，
            # 避免接码平台之间出现重复的“服务/国家”配置。
            project_id = str(service or _cfg.SMS_SERVICE).strip()
            h_country = str(country or _cfg.SMS_COUNTRY).strip()
            if not project_id:
                raise SmsProviderError("H projectId 不能为空：请填写 SMS_SERVICE")
            if not h_country:
                raise SmsProviderError("H country 不能为空：请填写 SMS_COUNTRY")
            payload = {
                "projectId": project_id,
                "country": h_country,
            }
            mode = _h_phone_acquire_mode()
            api_path = "/api/admin/h/take-phone" if mode == "new" else "/api/admin/h/take-reusable-phone"
            data = _post_h_json(http, api_path, payload)
            item = data.get("item") or {}
            activation_id = str(item.get("id") or "").strip()
            raw_phone = str(item.get("phone") or "")
            raw_prefix = str(getattr(_cfg, "H_PHONE_PREFIX", "") or "")
            phone = _normalize_h_phone(raw_phone)
            if raw_phone.strip() != phone or raw_prefix.strip():
                logger.info(
                    f"[SMS:H] 号码规范化：raw_phone={raw_phone!r}, "
                    f"prefix={raw_prefix!r}, normalized=+{phone}"
                )
            if not activation_id or not phone:
                raise SmsProviderError(f"H {api_path.rsplit('/', 1)[-1]} 响应缺少 item.id/item.phone：{str(data)[:200]}")
            _ACQUIRED_AT[activation_id] = time.time()
            logger.info(
                f"[SMS:H] 取号成功：mode={mode}, api={api_path}, id={activation_id}, phone=+{phone}, "
                f"reused={bool(data.get('reused'))}, duplicate={bool(data.get('duplicate'))}"
            )
            return activation_id, phone

        if not _is_activate_provider():
            raise SmsProviderError(
                f"未知接码通道 SMS_PROVIDER={_provider()!r}，"
                f"支持：grizzly / herosms / smsbower / l / h"
            )

        label = _activate_label()
        provider = _provider()
        svc = str(service or _cfg.SMS_SERVICE or "").strip()
        if not svc:
            raise SmsProviderError(f"{label} service 不能为空：请填写 SMS_SERVICE（OpenAI 用 dr）")

        # 国家×供应商×价位候选：上限内尽量省钱，失败只冷却槽位不 ban 整国
        countries = None
        if country not in (None, ""):
            c0 = str(country).strip()
            manual_wl = _parse_country_list(getattr(_cfg, "SMS_COUNTRY_WHITELIST", "") or "")
            # 显式 country 优先；仅手动硬白名单可拦。动态 Top 作为后续扩展候选。
            if manual_wl and c0 not in manual_wl and not _allow_outside_whitelist():
                logger.warning(
                    "[SMS:%s] 请求国 %s 不在手动白名单 %s，改用手动白名单",
                    _activate_label(), c0, manual_wl,
                )
                countries = list(manual_wl)
            else:
                countries = [c0]
                # 显式国时只静态扩展，避免额外 API 打乱调用方 http mock；
                # 默认无 country 时 countries=None，由 list_provider_candidates 走服务 Top 白名单。
                if _auto_country_enabled():
                    for c in _cfg_country_whitelist():
                        if c != c0 and c not in countries:
                            countries.append(c)

        max_slots = max(1, _safe_int(getattr(_cfg, "SMS_ACQUIRE_MAX_SLOTS", _MAX_COUNTRY_ROTATIONS), _MAX_COUNTRY_ROTATIONS))
        last_no_numbers: Exception | None = None
        tried_slots: list[str] = []

        # 两轮：先排除异常低价档，再放开低价档（仍受 SMS_MAX_PRICE 约束）
        for quality_round, include_low in enumerate((False, True), start=1):
            try:
                candidates = list_provider_candidates(
                    http,
                    provider=provider,
                    service=svc,
                    countries=countries,
                    include_low_quality=include_low,
                )
            except Exception as exc:
                logger.warning(f"[SMS:{label}] 构建供应商候选失败：{exc}")
                candidates = []

            if not candidates and quality_round == 1:
                continue
            if not candidates:
                break

            # 样本库分桶：平价/探索/兜底/垃圾；前 3 次锁平价，第 4 次起开兜底
            candidates = order_candidates_for_acquire(
                candidates, attempt_index=attempt_i,
            )
            bucket_counts: dict[str, int] = {}
            for c in candidates:
                t = str(c.get("bucket") or c.get("tier_name") or "?")
                bucket_counts[t] = bucket_counts.get(t, 0) + 1
            logger.info(
                f"[SMS:{label}] 供应商候选 {len(candidates)} 个 "
                f"(round={quality_round}, attempt={attempt_i}, "
                f"budget_phase={attempt_i <= int(_BUDGET_PHASE_ATTEMPTS)}, "
                f"maxPrice={_cfg_max_price() or '∞'}, "
                f"minPrice={_cfg_min_price() or 0}, low_quality={include_low}, "
                f"buckets={bucket_counts})"
            )
            if candidates:
                head = candidates[0]
                logger.info(
                    f"[SMS:{label}] 本线程首选槽 bucket={head.get('bucket') or head.get('tier_name')} "
                    f"country={head.get('country')} provider={head.get('provider_id') or '-'} "
                    f"score={head.get('score')} rate_hat={head.get('rate_hat')} "
                    f"rate_src={head.get('rate_src')} price={head.get('price')} "
                    f"inflight={head.get('inflight_pen')}"
                )
            for idx, cand in enumerate(candidates[:max_slots], start=1):
                cty = str(cand.get("country") or "").strip()
                pid = str(cand.get("provider_id") or "").strip()
                price = _safe_float(cand.get("price"), 0.0)
                slot = _slot_key(cty, pid)
                if slot in tried_slots:
                    continue
                tried_slots.append(slot)
                claim_slot_inflight(slot)
                try:
                    user_cap = _cfg_max_price()
                    # 锁在该档附近，防止平台降级塞更脏的超低价号
                    slot_max = price * 1.15 if price > 0 else user_cap
                    if user_cap > 0:
                        slot_max = min(slot_max, user_cap) if slot_max > 0 else user_cap
                    slot_min = max(_cfg_min_price(), price * 0.85) if price > 0 else _cfg_min_price()
                    if user_cap > 0 and slot_min > user_cap:
                        slot_min = 0.0

                    params = {
                        "action": "getNumber",
                        "service": svc,
                        "country": cty,
                    }
                    if pid:
                        params["providerIds"] = pid
                    if slot_max > 0:
                        params["maxPrice"] = f"{slot_max:.4f}".rstrip("0").rstrip(".")
                    if slot_min > 0:
                        params["minPrice"] = f"{slot_min:.4f}".rstrip("0").rstrip(".")

                    logger.info(
                        f"[SMS:{label}] 尝试取号 {idx}/{min(len(candidates), max_slots)} "
                        f"bucket={cand.get('bucket') or cand.get('tier_name') or '-'} "
                        f"country={cty} provider={pid or '-'} "
                        f"price={price} maxPrice={params.get('maxPrice', '-')} "
                        f"minPrice={params.get('minPrice', '-')}"
                    )
                    try:
                        text = _request_activate(http, params)
                    except SmsNoNumbersError as exc:
                        last_no_numbers = exc
                        mark_slot_cooldown(cty, pid, channel=provider, reason="no_numbers")
                        continue
                    except SmsNoBalanceError:
                        raise
                    except SmsProviderError as exc:
                        logger.warning(f"[SMS:{label}] 取号失败槽位 {slot}：{exc}")
                        mark_slot_cooldown(cty, pid, channel=provider, reason="error", cooldown=180)
                        last_no_numbers = exc if "NO_NUMBERS" in str(exc) else last_no_numbers
                        continue

                    if not text.startswith("ACCESS_NUMBER:"):
                        logger.warning(f"[SMS:{label}] getNumber 非预期：{text[:160]}")
                        mark_slot_cooldown(cty, pid, channel=provider, reason="bad_response", cooldown=120)
                        continue
                    parts = text.split(":")
                    if len(parts) < 3:
                        continue
                    activation_id = parts[1].strip()
                    phone = parts[2].strip()
                    _ACQUIRED_AT[activation_id] = time.time()
                    remember_activation_meta(activation_id, {
                        "country": cty,
                        "provider_id": pid,
                        "price": price,
                        "service": svc,
                        "channel": provider,
                        "max_price": slot_max,
                    })
                    logger.info(
                        f"[SMS:{label}] 取号成功：activation_id={activation_id}, phone=+{phone}, "
                        f"service={svc}, country={cty}, provider={pid or '-'}, price={price}, "
                        f"bucket={cand.get('bucket') or cand.get('tier_name') or '-'}"
                    )
                    return activation_id, phone
                finally:
                    # 取号请求结束即释放占槽，避免长时间锁死；并发齐步靠 claim 窗口错开
                    release_slot_inflight(slot)

        # 候选构建失败（旧平台无 V3 / 测试桩）时回退：固定国 + maxPrice 裸取号
        if not tried_slots:
            cty = str(country or "").strip() or str(getattr(_cfg, "SMS_COUNTRY", "") or "").strip()
            if not cty:
                prefs = _cfg_preferred_countries()
                cty = prefs[0] if prefs else ""
            if not cty:
                raise SmsProviderError(f"{label} country 不能为空：请填写 SMS_COUNTRY 或优先国家")
            params = {"action": "getNumber", "service": svc, "country": cty}
            cap = _cfg_max_price()
            if cap > 0:
                params["maxPrice"] = f"{cap:.4f}".rstrip("0").rstrip(".")
            min_p = _cfg_min_price()
            if min_p > 0:
                params["minPrice"] = f"{min_p:.4f}".rstrip("0").rstrip(".")
            logger.info(f"[SMS:{label}] 无供应商候选，回退裸取号 country={cty} maxPrice={params.get('maxPrice', '-')}")
            text = _request_activate(http, params)
            if text.startswith("ACCESS_NUMBER:"):
                parts = text.split(":")
                if len(parts) >= 3:
                    activation_id = parts[1].strip()
                    phone = parts[2].strip()
                    _ACQUIRED_AT[activation_id] = time.time()
                    remember_activation_meta(activation_id, {
                        "country": cty, "provider_id": "", "price": 0,
                        "service": svc, "channel": provider,
                    })
                    logger.info(
                        f"[SMS:{label}] 取号成功：activation_id={activation_id}, phone=+{phone}, "
                        f"service={svc}, country={cty}"
                    )
                    return activation_id, phone
            if text == "NO_NUMBERS" or "NO_NUMBERS" in text:
                raise SmsNoNumbersError(f"{label} 暂无可用号码（NO_NUMBERS）country={cty}")
            raise SmsProviderError(f"{label} getNumber 非预期响应：{text[:200]}")

        if last_no_numbers is not None:
            raise SmsNoNumbersError(
                f"{label} 多个供应商槽位均无号（已试 {len(tried_slots)} 个："
                f"{','.join(tried_slots[:8])}{'…' if len(tried_slots) > 8 else ''}）"
            ) from last_no_numbers
        raise SmsNoNumbersError(
            f"{label} 无可用供应商候选：请检查 SMS_MAX_PRICE/优先国家/库存，"
            f"或稍后重试"
        )
    finally:
        if own_http:
            http.close()


# ============================================================
# 取短信验证码
# ============================================================

def wait_for_sms_code(
    activation_id: str,
    http: CurlSession | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
) -> str:
    """
    轮询 getStatus 直到拿到短信验证码。

    Returns:
        验证码字符串

    Raises:
        SmsCodeTimeout —— 超时没收到（上层可换号重试）
        SmsProviderError —— 激活被取消等
    """
    own_http = http is None
    http = http or _http()
    deadline = time.time() + (max_wait or _cfg.SMS_CODE_WAIT)
    interval = poll_interval or _cfg.SMS_POLL_INTERVAL
    try:
        provider = _provider()
        total_wait = max_wait or _cfg.SMS_CODE_WAIT
        logger.info(f"[SMS] 等待短信验证码 activation_id={activation_id}，最长 {total_wait}s...")
        round_no = 0
        while time.time() < deadline:
            try:
                from core.registration_service import check_stop_requested
                check_stop_requested()
            except ImportError:
                pass
            round_no += 1
            elapsed = max(0, int(total_wait - max(0, deadline - time.time())))
            remaining_before = max(0, int(deadline - time.time()))
            logger.info(
                f"[SMS] 第 {round_no} 轮获取验证码 activation_id={activation_id}，"
                f"已等 {elapsed}s，剩余约 {remaining_before}s"
            )
            if provider == "l":
                data = _post_l_json(http, "/api/admin/l/fetch-code", {"id": activation_id})
                code = str(data.get("code") or "").strip()
                raw = str(data.get("raw") or "").strip()
                status = str((data.get("item") or {}).get("status") or "").strip()
                if code:
                    logger.info(f"[SMS:L] 第 {round_no} 轮收到验证码：{code}")
                    try:
                        record_activation_delivery(activation_id, _DELIVERY_OK)
                    except Exception:
                        pass
                    return code
                remaining = max(0, int(deadline - time.time()))
                logger.info(
                    f"[SMS:L] 第 {round_no} 轮未收到验证码，状态={status or raw or 'WAIT'}，"
                    f"{interval}s 后重试（剩余 {remaining}s）"
                )
                time.sleep(interval)
                continue

            if provider == "h":
                data = _post_h_json(http, "/api/admin/h/fetch-code", {"id": activation_id})
                code = str(data.get("code") or "").strip()
                raw = str(data.get("raw") or "").strip()
                status = str((data.get("item") or {}).get("status") or "").strip()
                if code:
                    logger.info(f"[SMS:H] 第 {round_no} 轮收到验证码：{code}")
                    try:
                        record_activation_delivery(activation_id, _DELIVERY_OK)
                    except Exception:
                        pass
                    return code
                remaining = max(0, int(deadline - time.time()))
                logger.info(
                    f"[SMS:H] 第 {round_no} 轮未收到验证码，状态={status or raw or 'WAIT'}，"
                    f"{interval}s 后重试（剩余 {remaining}s）"
                )
                time.sleep(interval)
                continue

            if not _is_activate_provider(provider):
                raise SmsProviderError(f"未知接码通道 SMS_PROVIDER={provider!r}")

            label = _activate_label(provider)
            text = _request_activate(http, {"action": "getStatus", "id": activation_id}, provider=provider)

            if text.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip()
                logger.info(f"[SMS:{label}] 第 {round_no} 轮收到验证码：{code}")
                try:
                    record_activation_delivery(activation_id, _DELIVERY_OK)
                except Exception:
                    pass
                return code
            if text == "STATUS_CANCEL":
                raise SmsProviderError(f"{label} 激活已被取消（STATUS_CANCEL）")
            # STATUS_WAIT_CODE / STATUS_WAIT_RETRY:* / STATUS_WAIT_RESEND → 继续等
            remaining = max(0, int(deadline - time.time()))
            logger.info(
                f"[SMS:{label}] 第 {round_no} 轮未收到验证码，状态={text}，"
                f"{interval}s 后重试（剩余 {remaining}s）"
            )
            time.sleep(interval)

        try:
            record_activation_delivery(activation_id, "timeout")
        except Exception:
            pass
        raise SmsCodeTimeout(f"等待短信超时（>{total_wait}s），activation_id={activation_id}")
    finally:
        if own_http:
            http.close()


# ============================================================
# 改状态
# ============================================================

def set_status(activation_id: str, status: int, http: CurlSession | None = None) -> str:
    """
    设置激活状态（setStatus）。
        1 = 号码已就绪（短信已发出）
        3 = 等下一条短信（重发）
        6 = 完成激活
        8 = 取消激活

    注意：status=1/3 是「通知平台开始等短信」的辅助动作。
    部分供应商会回 BAD_STATUS（已就绪/不支持该状态），绝不能因此中断主流程——
    否则会出现平台已收码、程序却换号丢掉验证码的情况。
    """
    own_http = http is None
    http = http or _http()
    status_i = int(status)
    try:
        if _provider() == "l":
            logger.debug(f"[SMS:L] 忽略状态设置 id={activation_id}, status={status_i}")
            return "OK"
        if _provider() == "h":
            logger.debug(f"[SMS:H] 忽略状态设置 id={activation_id}, status={status_i}")
            return "OK"
        if not _is_activate_provider():
            raise SmsProviderError(f"未知接码通道 SMS_PROVIDER={_provider()!r}")
        try:
            text = _request_activate(
                http,
                {"action": "setStatus", "status": str(status_i), "id": activation_id},
            )
            return text
        except Exception as exc:
            # 1/3 仅 best-effort：失败只告警，继续 getStatus 轮询
            if status_i in (1, 3):
                logger.warning(
                    f"[SMS:{_activate_label()}] setStatus={status_i} 失败（忽略，继续等码）"
                    f" activation_id={activation_id}：{exc}"
                )
                return f"IGNORED:{exc}"
            raise
    finally:
        if own_http:
            http.close()


def mark_sms_sent(activation_id: str, http: CurlSession | None = None) -> None:
    """进入验证码页后通知平台「已发短信/开始等码」。失败不抛。"""
    try:
        set_status(activation_id, 1, http=http)
    except Exception as exc:
        logger.warning(f"[SMS] mark_sms_sent 失败（忽略）：activation_id={activation_id} {exc}")


def complete(activation_id: str, http: CurlSession | None = None) -> None:
    """标记激活完成（status=6）。失败只告警不抛，避免影响主流程。"""
    if _provider() == "l":
        logger.info(f"[SMS:L] 已完成 id={activation_id}")
        _ACQUIRED_AT.pop(activation_id, None)
        return
    if _provider() == "h":
        # H 成功 fetch-code 后后台会自动按多次收码策略重取；这里不 release。
        logger.info(f"[SMS:H] 已完成 id={activation_id}")
        _ACQUIRED_AT.pop(activation_id, None)
        return
    try:
        set_status(activation_id, 6, http=http)
        logger.info(f"[SMS:{_activate_label()}] 已标记完成 activation_id={activation_id}")
        _ACQUIRED_AT.pop(activation_id, None)
    except Exception as exc:
        logger.warning(f"[SMS] 标记完成失败（不影响结果）：{exc}")


def _do_cancel_sync(activation_id: str, http_factory) -> None:
    """实际的同步取消逻辑：等够 2 分钟限制 → 发请求 → 失败重试一次。"""
    label = _activate_label() if _is_activate_provider() else "SMS"
    acquired_at = _ACQUIRED_AT.get(activation_id)
    if acquired_at is not None:
        elapsed = time.time() - acquired_at
        if elapsed < _MIN_CANCEL_DELAY:
            wait = _MIN_CANCEL_DELAY - elapsed
            logger.info(
                f"[SMS:{label}] 取消等待 2 分钟限制：activation_id={activation_id}，"
                f"还需等 {wait:.0f}s..."
            )
            time.sleep(wait)

    # 后台线程不能复用外部 http session（curl_cffi 非线程安全），自己建一个
    http = http_factory()
    try:
        for attempt in range(1, 3):
            try:
                set_status(activation_id, 8, http=http)
                logger.info(f"[SMS:{label}] 已取消 activation_id={activation_id}")
                _ACQUIRED_AT.pop(activation_id, None)
                return
            except Exception as exc:
                if attempt == 1:
                    logger.warning(f"[SMS:{label}] 取消失败（{exc}），5s 后重试...")
                    time.sleep(5)
                else:
                    logger.warning(
                        f"[SMS:{label}] 取消最终失败（不影响结果，需到平台手动取消）："
                        f"activation_id={activation_id}, {exc}"
                    )
    finally:
        try:
            http.close()
        except Exception:
            pass


def cancel(activation_id: str, http: CurlSession | None = None, background: bool = True) -> None:
    """
    取消激活（status=8），释放号码避免白扣费。

    GrizzlySMS 规则：号码取出后约 2 分钟内不允许取消。本函数默认 background=True，
    把"等 2 分钟+取消"放到后台守护线程里执行，主流程立刻返回继续走（如换下一个号），
    避免被这 2 分钟阻塞。

    background=False 时同步等够时间再返回（少数场景需要确认取消完成时用）。

    失败只告警不抛，不影响主流程。
    """
    if _provider() == "l":
        try:
            _release_l_number(activation_id, http=http)
        except Exception as exc:
            logger.warning(f"[SMS:L] 释放号码失败（不影响主流程）：id={activation_id}, {type(exc).__name__}: {exc}")
            _ACQUIRED_AT.pop(activation_id, None)
        return
    if _provider() == "h":
        try:
            _release_h_number(activation_id, http=http)
        except Exception as exc:
            logger.warning(f"[SMS:H] 释放号码失败（不影响主流程）：id={activation_id}, {type(exc).__name__}: {exc}")
            _ACQUIRED_AT.pop(activation_id, None)
        return

    if not background:
        _do_cancel_sync(activation_id, _http)
        return

    t = threading.Thread(
        target=_do_cancel_sync,
        args=(activation_id, _http),
        name=f"sms-cancel-{activation_id}",
        daemon=True,
    )
    t.start()
    logger.debug(f"[SMS] 取消任务已派后台：activation_id={activation_id}")
