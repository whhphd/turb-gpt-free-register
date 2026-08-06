# -*- coding: utf-8 -*-
"""Plus 试用提链服务配置（多 Provider 可选）。

支持：
  - oai9：Kakao 多任务 API（promo 预检 + /api/kakao-link/tasks）
  - convertmove：Customer API（/api/v1/submissions）

账号页手动点「提链」触发，不会注册后自动跑。
"""
from config.env_loader import apply_env_overrides

# 提链后端：oai9 | convertmove
EXTRACT_LINK_PROVIDER: str = "oai9"

# 服务根地址（不要带具体 path）
# oai9 例：https://your-oai9-host
# convertmove 例：https://convertmove.cc.cd
EXTRACT_LINK_API_BASE: str = ""

# 卡密 / CDK（oai9 用 card；convertmove 用 cdk；同一配置项）
EXTRACT_LINK_CDK: str = ""

# 提链类型：oai9 目前主推 kakao_pay；convertmove 支持 pix/upi/kakao_pay/ideal
EXTRACT_LINK_TYPE: str = "kakao_pay"

# oai9 批量提交时的 plan_type（文档示例 plus）
EXTRACT_LINK_PLAN_TYPE: str = "plus"

# oai9 promo_code，可空
EXTRACT_LINK_PROMO_CODE: str = ""

# 后台提链并发与超时（账号页可同时点多个号）
EXTRACT_LINK_WORKERS: int = 3
EXTRACT_LINK_QUEUE_LIMIT: int = 500
EXTRACT_LINK_REQUEST_TIMEOUT: int = 30
# 单任务最长等待（秒）
EXTRACT_LINK_EVENT_TIMEOUT: int = 240
EXTRACT_LINK_POLL_INTERVAL: int = 5

apply_env_overrides(globals(), {
    'EXTRACT_LINK_PROVIDER': 'str',
    'EXTRACT_LINK_API_BASE': 'str',
    'EXTRACT_LINK_CDK': 'str',
    'EXTRACT_LINK_TYPE': 'str',
    'EXTRACT_LINK_PLAN_TYPE': 'str',
    'EXTRACT_LINK_PROMO_CODE': 'str',
    'EXTRACT_LINK_WORKERS': 'int',
    'EXTRACT_LINK_QUEUE_LIMIT': 'int',
    'EXTRACT_LINK_REQUEST_TIMEOUT': 'int',
    'EXTRACT_LINK_EVENT_TIMEOUT': 'int',
    'EXTRACT_LINK_POLL_INTERVAL': 'int',
})
