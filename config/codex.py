# -*- coding: utf-8 -*-
"""
注册成功后自动跑 Codex OAuth 授权的配置项。
设置 ENABLE_CODEX = False 可完全跳过此步骤。

参数来源：CLIProxyAPI 源码 internal/auth/codex/openai_auth.go + pkce.go，
对照 https://github.com/router-for-me/CLIProxyAPI 逐行确认。
"""
from config.env_loader import env_str, apply_env_overrides


# 是否启用 Codex OAuth 授权（False = 跳过，不影响注册结果）
ENABLE_CODEX: bool = False

# Codex OAuth 客户端 ID（固定值，来自 CLIProxyAPI openai_auth.go:27 ClientID）
CODEX_CLIENT_ID: str = "app_EMoamEEZ73f0CkXaXp7hrann"

# 授权端点（openai_auth.go:25 AuthURL）
CODEX_AUTH_URL: str = "https://auth.openai.com/oauth/authorize"

# 换 token 端点（openai_auth.go:26 TokenURL）
CODEX_TOKEN_URL: str = "https://auth.openai.com/oauth/token"

# 回调地址（openai_auth.go:28 RedirectURI）
# 注意：本地并不真的起这个 server，只用来拦截重定向并从 Location 提取 code。
CODEX_REDIRECT_URI: str = "http://localhost:1455/auth/callback"

# OAuth scopes（openai_auth.go:75 GenerateAuthURL 里的 scope）
CODEX_SCOPE: str = "openid email profile offline_access"

# 输出目录名（仅名字，运行时拼到项目根；与 OUTLOOK_ACCOUNTS_FILE 同级风格）
CODEX_OUTPUT_DIRNAME: str = "codex_accounts"

# 请求超时（秒）
CODEX_REQUEST_TIMEOUT: int = 30


# ============================================================
# Codex 授权方式（2026-06-15 改造）
#
# 旧方案"复用注册的已登录 session"会撞 /choose-an-account 卡死；
# 新方案用全新干净 session 从头登录，走 OpenAI 标准风控路径
# （邮箱 OTP → 手机短信验证 → 选 workspace → 拿 code），
# 手机验证靠接码平台 GrizzlySMS 自动收码。
# ============================================================

# 注册成功后是否自动跑 Codex 授权（True=自动，False=跳过）
ENABLE_CODEX_AUTO: bool = False

# Codex OAuth 授权驱动：
#   "protocol" = 原有 curl_cffi 协议授权
#   "roxy"     = 调用 RoxyBrowser 指纹浏览器完成授权页面/手机验证/回调捕获
#   "cloak"       = 调用 CloakBrowser 完成授权页面/手机验证/回调捕获
#   "browser_use" = 调用 Browser Use Cloud 完成授权页面/手机验证/回调捕获
#   "same_as_registration" = 跟随 REGISTRATION_DRIVER
CODEX_OAUTH_DRIVER: str = "roxy"




# ============================================================
# CPA 管理接口（Codex 授权地址由 CPA 生成，本地只负责跑登录并提交回调）
# ============================================================

# 授权地址来源：
#   "local" = 本程序本地 PKCE 生成授权地址、自行换 token 并保存凭据（推荐；不依赖 CPA/sub2 admin key）
#   "sub2"  = 通过 sub2api 管理接口生成，并把 callback 上传到 sub2api（需 SUB2API_API_BASE + SUB2API_API_KEY）
#   "cpa"   = 通过 CPA 管理接口 /v0/management/codex-auth-url 生成
# 显式配置 cpa/sub2 但密钥缺失时，会自动回退到 local。
CODEX_AUTH_URL_SOURCE: str = "local"

# CPA 管理页面或服务地址，例如 http://localhost:8317/admin/oauth
# 实际请求会取 origin，调用：
#   GET  /v0/management/codex-auth-url
#   POST /v0/management/oauth-callback
CPA_MANAGEMENT_URL: str = "http://127.0.0.1:8317/management.html"#/oauth"

# CPA 管理密钥，同时作为 Authorization: Bearer 和 X-Management-Key
CPA_MANAGEMENT_KEY: str = env_str("CPA_MANAGEMENT_KEY", "")

# CPA 管理接口请求超时（秒）
CPA_REQUEST_TIMEOUT: int = 30

# 提交 OAuth callback 给 CPA 的重试次数/基础间隔。
# 遇到 409 Timeout waiting for OAuth callback、网络超时或 5xx 时，会按同一个 callback URL 重试。
CPA_CALLBACK_SUBMIT_RETRIES: int = 5
CPA_CALLBACK_SUBMIT_RETRY_DELAY: int = 6

# CPA 未返回完整 auth json 时，是否仍在本地 codex_accounts/ 记录一份回调提交凭据
CPA_SAVE_CALLBACK_RECEIPT: bool = True

# ============================================================
# 接码平台（手机短信验证用）
# SMS_PROVIDER:
#   "grizzly"  = GrizzlySMS，接口说明见 https://api.grizzlysms.com
#   "herosms"  = HeroSMS（SMS-Activate 兼容），https://hero-sms.com
#   "smsbower" = SMSBower（SMS-Activate 兼容），https://smsbower.page
#   "l"        = 本地 L 取号服务，接口说明见 L_API.md
#   "h"        = 本地 H 取号服务，接口说明见 H_API.md
# ============================================================

SMS_PROVIDER: str = "l"

# GrizzlySMS API 基址（GET handler）
SMS_API_BASE: str = "https://api.grizzlysms.com/stubs/handler_api.php"

# GrizzlySMS API 密钥（在 GrizzlySMS 后台 → 设置 获取）
# 留空时 Codex 授权的手机验证步会失败；如不需要 Codex 自动授权，把 ENABLE_CODEX_AUTO=False。
SMS_API_KEY: str = env_str("SMS_API_KEY", "")

# HeroSMS（独立 key，与 Grizzly/SMSBower 分开）
HEROSMS_API_BASE: str = "https://hero-sms.com/stubs/handler_api.php"
HEROSMS_API_KEY: str = env_str("HEROSMS_API_KEY", "")

# SMSBower（独立 key）
SMSBOWER_API_BASE: str = "https://smsbower.page/stubs/handler_api.php"
SMSBOWER_API_KEY: str = env_str("SMSBOWER_API_KEY", "")

# 服务代码：HeroSMS/SMSBower/Grizzly 上 OpenAI = "dr"；L/H 按各自文档填写
SMS_SERVICE: str = "dr"

# 国家代码：泰国=52（OpenAI 确认走 SMS）/ 美国=187 / 葡萄牙=117
SMS_COUNTRY: str = "52"

# 单个号愿意支付的最高价格（硬上限，留空=不限）。前端可配；选供应商/价位时不会超过此值。
SMS_MAX_PRICE: str = "0.35"

# 最低单价（可选，留空/0=不限）。用于挡极端低价虚拟号，例如 0.004。
SMS_MIN_PRICE: str = ""

# 优先国家列表（逗号分隔）。同国先换供应商/价位，再换其它优先国。
# 当 SMS_COUNTRY_WHITELIST 为空时，本列表同时作为硬白名单（默认不再乱扩到全世界）。
# 52=泰国（OpenAI 确认走 SMS）
SMS_PREFERRED_COUNTRIES: str = "52"

# 接码国家硬白名单（逗号分隔）。非空时只从这些国家取号，绝不扩展到白名单外。
# 例：52,6,16,36。留空时：若开启「按服务 Top 白名单」则用 API TopN；否则用优先国家。
SMS_COUNTRY_WHITELIST: str = ""

# 是否允许在白名单之外扩展到 getPrices 其它有货国（默认 False）
SMS_ALLOW_OUTSIDE_WHITELIST: bool = False

# 用接码平台 getTopCountriesByService 的 TopN 当动态白名单（SMSBower/Hero 推荐）。
# 手动 SMS_COUNTRY_WHITELIST 非空时仍以手动为准。
SMS_USE_TOP_COUNTRIES_WHITELIST: bool = True

# Top 国家数量上限（API 通常返回 10）
SMS_TOP_COUNTRIES_LIMIT: int = 10

# Top 白名单缓存秒数，避免每次取号都打 API
SMS_TOP_COUNTRIES_CACHE_SEC: int = 300

# 自动跨国家选号（herosms/smsbower）：在白名单内按「国家×供应商×价位」轮换；
# 仅当 SMS_ALLOW_OUTSIDE_WHITELIST=True 时才会扩到白名单外其它有货国。
SMS_AUTO_COUNTRY: bool = True

# 供应商最低库存（getPricesV3 的 count）
SMS_PROVIDER_MIN_STOCK: int = 15

# 兼容旧字段：自动选国最低库存（若 >0 可覆盖 SMS_PROVIDER_MIN_STOCK）
SMS_AUTO_COUNTRY_MIN_STOCK: int = 0

# 兼容旧字段：自动选国最高价；>0 时与 SMS_MAX_PRICE 取更严（更小）的那个
SMS_AUTO_COUNTRY_MAX_PRICE: float = 0

# 相对该国中位价的地板比例：price < median*ratio 且库存很大时，首轮跳过（防垃圾虚拟号）
SMS_PRICE_FLOOR_RATIO: float = 0.25

# 单次取号最多尝试几个（国家×供应商）候选槽
SMS_ACQUIRE_MAX_SLOTS: int = 12

# 一个号收不到短信/被拒时，换号重试的最大次数
SMS_MAX_RETRIES: int = 10

# 单个号等待短信的最长秒数（超时则取消该号换下一个）
SMS_CODE_WAIT: int = 120

# 轮询接码平台查短信的间隔（秒）
SMS_POLL_INTERVAL: int = 5

# 接码平台 HTTP 请求超时（秒）
SMS_REQUEST_TIMEOUT: int = 30


# ============================================================
# H 取号服务（SMS_PROVIDER="h" 时使用）
# ============================================================

# H API 基址，例如本地后台：http://localhost:8788
H_API_BASE: str = "http://localhost:8788"

# H 后台授权码，对应 H_API.md 里的 Authorization: Bearer <ADMIN_AUTH_CODE>
H_ADMIN_AUTH_CODE: str = env_str("H_ADMIN_AUTH_CODE", "")

# H 返回的号码如果不含国家码，可在这里补前缀；留空则直接使用 H 返回的 item.phone。
H_PHONE_PREFIX: str = ""

# H 取号方式：
#   "reusable" = 优先复用号码，调用 /api/admin/h/take-reusable-phone（默认）
#   "new"      = 每次取新号，调用 /api/admin/h/take-phone
H_PHONE_ACQUIRE_MODE: str = "reusable"


# ============================================================
# L 取号服务（SMS_PROVIDER="l" 时使用）
# ============================================================

# L API 基址，例如本地后台：http://localhost:8788
L_API_BASE: str = "http://localhost:8788"

# L 后台授权码，对应 L_API.md 里的 Authorization: Bearer <ADMIN_AUTH_CODE>
L_ADMIN_AUTH_CODE: str = env_str("L_ADMIN_AUTH_CODE", "")

# L 返回的号码如果不含国家码，可在这里补前缀；例如美国本地 10 位号填 "1"。
# 留空则直接使用 L 返回的 item.phone。
L_PHONE_PREFIX: str = ""

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {
    'ENABLE_CODEX_AUTO': 'bool',
    'CODEX_OAUTH_DRIVER': 'str',
    'CODEX_AUTH_URL_SOURCE': 'str',
    'CPA_MANAGEMENT_URL': 'str',
    'CPA_MANAGEMENT_KEY': 'str',
    'CPA_REQUEST_TIMEOUT': 'int',
    'CPA_CALLBACK_SUBMIT_RETRIES': 'int',
    'CPA_CALLBACK_SUBMIT_RETRY_DELAY': 'int',
    'CPA_SAVE_CALLBACK_RECEIPT': 'bool',
    'SMS_PROVIDER': 'str',
    'SMS_COUNTRY': 'str',
    'SMS_SERVICE': 'str',
    'SMS_MAX_PRICE': 'str',
    'SMS_MIN_PRICE': 'str',
    'SMS_PREFERRED_COUNTRIES': 'str',
    'SMS_COUNTRY_WHITELIST': 'str',
    'SMS_ALLOW_OUTSIDE_WHITELIST': 'bool',
    'SMS_USE_TOP_COUNTRIES_WHITELIST': 'bool',
    'SMS_TOP_COUNTRIES_LIMIT': 'int',
    'SMS_TOP_COUNTRIES_CACHE_SEC': 'int',
    'SMS_AUTO_COUNTRY': 'bool',
    'SMS_PROVIDER_MIN_STOCK': 'int',
    'SMS_AUTO_COUNTRY_MIN_STOCK': 'int',
    'SMS_AUTO_COUNTRY_MAX_PRICE': 'float',
    'SMS_PRICE_FLOOR_RATIO': 'float',
    'SMS_ACQUIRE_MAX_SLOTS': 'int',
    'SMS_MAX_RETRIES': 'int',
    'SMS_CODE_WAIT': 'int',
    'SMS_API_KEY': 'str',
    'HEROSMS_API_BASE': 'str',
    'HEROSMS_API_KEY': 'str',
    'SMSBOWER_API_BASE': 'str',
    'SMSBOWER_API_KEY': 'str',
    'H_API_BASE': 'str',
    'H_ADMIN_AUTH_CODE': 'str',
    'H_PHONE_PREFIX': 'str',
    'H_PHONE_ACQUIRE_MODE': 'str',
    'L_API_BASE': 'str',
    'L_ADMIN_AUTH_CODE': 'str',
    'L_PHONE_PREFIX': 'str',
})
