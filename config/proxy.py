# -*- coding: utf-8 -*-
"""
代理池配置

每次注册随机抽取一个代理，保证不同 sid 之间彼此独立，避免风控关联。

协议说明：
    - http:// / https://   HTTP(S) 代理
    - socks5://            SOCKS5（DNS 本地解析，可能泄漏）
    - socks5h://           SOCKS5（DNS 在代理端解析，推荐，避免 DNS-IP 错配）
"""
from config.env_loader import apply_env_overrides
import random
import time


# 本地代理入口；实际出口地区以代理/分流规则为准。
# 推荐使用 socks5h://（DNS 在代理端解析），避免本地 DNS 与出口 IP 地区错配。
PROXY_POOL = [
    "socks5://127.0.0.1:7897",
]

# sticky 代理被 CF 403/429 后的冷却，避免查活并发反复抽到同一坏 IP
_PROXY_COOLDOWN_UNTIL: dict[str, float] = {}
_PROXY_COOLDOWN_SEC = 900

# 套餐/Plus 试用资格查询与 Codex Agent Token 生成共用这组独立网络策略，
# 避免批量请求被注册代理池中的临时本地代理拖垮，也避免无条件直连造成出口策略失控。
#   auto   = 优先使用 PLAN_CHECK_PROXY 或代理池；本地代理端口未监听时回退直连
#   proxy  = 强制使用 PLAN_CHECK_PROXY 或代理池，失败直接报错
#   direct = 始终直连
PLAN_CHECK_PROXY_MODE = "auto"

# 套餐查询 / Codex Agent Token 生成专用代理。留空时 auto/proxy 模式从 PROXY_POOL 选择。
# 代理可能包含账号密码，因此 WebUI 会把它保存到 .env。
PLAN_CHECK_PROXY = ""

# 查套餐 / 生成 Codex Agent Token 使用独立的短超时和有限重试，避免后台任务长时间卡住。
PLAN_CHECK_TIMEOUT = 15.0
PLAN_CHECK_MAX_ATTEMPTS = 2
PLAN_CHECK_RETRY_DELAY = 1.5

# 新注册账号的权益可能存在短暂同步延迟。首次查询失败，或返回 free 且暂未发现
# Plus 试用资格时，等待该秒数后再复查一次；设为 0 可关闭复查。
PLAN_CHECK_REGISTRATION_RECHECK_DELAY = 2.0

# 自动、手动和批量套餐查询共用同一个后台队列；Codex Agent Token 使用独立队列，
# 但复用这里的网络模式、请求启动间隔与随机抖动，避免批量后台请求过于集中。
PLAN_CHECK_WORKERS = 3
PLAN_CHECK_QUEUE_LIMIT = 500
PLAN_CHECK_MIN_INTERVAL = 0.4
PLAN_CHECK_JITTER = 0.3

# 账号查活后台并发（与查套餐分开的线程池）。
LIVE_CHECK_WORKERS = 3
LIVE_CHECK_QUEUE_LIMIT = 500


def mark_proxy_cooldown(proxy: str, *, reason: str = "403", cooldown: int | None = None) -> None:
    """标记代理暂时不可用（查活/预检 403 后调用）。"""
    p = str(proxy or "").strip()
    if not p:
        return
    sec = int(cooldown if cooldown is not None else _PROXY_COOLDOWN_SEC)
    _PROXY_COOLDOWN_UNTIL[p] = time.time() + max(30, sec)


def pick_proxy(exclude: set[str] | list[str] | None = None) -> str:
    """从代理池中随机抽取一个代理 URL；池为空时返回空串（即不使用代理）。

    exclude: 本次调用额外排除的代理（例如本轮已 403 的 sticky）。
    优先跳过冷却中的代理；若全部冷却则回退到未排除全集。
    """
    if not PROXY_POOL:
        return ""
    excluded = {str(x).strip() for x in (exclude or []) if str(x).strip()}
    now = time.time()
    # 清理过期冷却
    for k, exp in list(_PROXY_COOLDOWN_UNTIL.items()):
        if exp <= now:
            _PROXY_COOLDOWN_UNTIL.pop(k, None)

    candidates = [
        p for p in PROXY_POOL
        if p and p not in excluded and float(_PROXY_COOLDOWN_UNTIL.get(p) or 0) <= now
    ]
    if not candidates:
        candidates = [p for p in PROXY_POOL if p and p not in excluded]
    if not candidates:
        return ""
    return random.choice(candidates)


# 兼容入口：默认每次进程启动随机选一个，作为本次注册全程的固定代理
PROXY = pick_proxy()

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'PROXY_POOL': 'list_str_multiline',
    'PLAN_CHECK_PROXY_MODE': 'str',
    'PLAN_CHECK_PROXY': 'str',
    'PLAN_CHECK_TIMEOUT': 'float',
    'PLAN_CHECK_MAX_ATTEMPTS': 'int',
    'PLAN_CHECK_RETRY_DELAY': 'float',
    'PLAN_CHECK_REGISTRATION_RECHECK_DELAY': 'float',
    'PLAN_CHECK_WORKERS': 'int',
    'PLAN_CHECK_QUEUE_LIMIT': 'int',
    'PLAN_CHECK_MIN_INTERVAL': 'float',
    'PLAN_CHECK_JITTER': 'float',
    'LIVE_CHECK_WORKERS': 'int',
    'LIVE_CHECK_QUEUE_LIMIT': 'int',
})
PROXY = pick_proxy()
