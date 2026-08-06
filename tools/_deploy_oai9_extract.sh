#!/bin/bash
set -e
APP=/opt/turb-gpt-free-register
cp /tmp/oai9_extract/extract_link_service.py "$APP/core/"
cp /tmp/oai9_extract/extract_link.py "$APP/config/"
cp /tmp/oai9_extract/config_editor.py "$APP/webui/"
cp /tmp/oai9_extract/test_extract_link_oai9.py "$APP/tests/"
cp /tmp/oai9_extract/test_extract_link_convertmove.py "$APP/tests/"
python3 <<'PY'
from pathlib import Path
p = Path('/opt/turb-gpt-free-register/.env')
text = p.read_text(encoding='utf-8', errors='replace')
lines = text.splitlines()
# 切换到 oai9，保留已有 CDK/卡密与 API_BASE（若用户已填）
wanted = {
    'EXTRACT_LINK_PROVIDER': 'EXTRACT_LINK_PROVIDER="oai9"',
    'EXTRACT_LINK_TYPE': 'EXTRACT_LINK_TYPE="kakao_pay"',
    'EXTRACT_LINK_PLAN_TYPE': 'EXTRACT_LINK_PLAN_TYPE="plus"',
    'EXTRACT_LINK_WORKERS': 'EXTRACT_LINK_WORKERS="3"',
    'EXTRACT_LINK_EVENT_TIMEOUT': 'EXTRACT_LINK_EVENT_TIMEOUT="240"',
    'EXTRACT_LINK_POLL_INTERVAL': 'EXTRACT_LINK_POLL_INTERVAL="5"',
}
# 若还是 convertmove 默认地址，清空让用户填 oai9 域名
found = set()
out = []
for ln in lines:
    key = ln.split('=', 1)[0].strip() if ('=' in ln and not ln.strip().startswith('#')) else None
    if key == 'EXTRACT_LINK_API_BASE' and 'convertmove.cc.cd' in ln:
        out.append('EXTRACT_LINK_API_BASE=""')
        found.add(key)
        continue
    if key in wanted:
        out.append(wanted[key])
        found.add(key)
    else:
        out.append(ln)
        if key:
            found.add(key)
for k, v in wanted.items():
    if k not in found:
        out.append(v)
if 'EXTRACT_LINK_API_BASE' not in found:
    out.append('EXTRACT_LINK_API_BASE=""')
if 'EXTRACT_LINK_PROMO_CODE' not in found:
    out.append('EXTRACT_LINK_PROMO_CODE=""')
p.write_text('\n'.join(out) + '\n', encoding='utf-8')
print('env patched')
for k in ['EXTRACT_LINK_PROVIDER', 'EXTRACT_LINK_API_BASE', 'EXTRACT_LINK_TYPE', 'EXTRACT_LINK_WORKERS']:
    print(next(x for x in out if x.startswith(k + '=')))
cdk = next((x for x in out if x.startswith('EXTRACT_LINK_CDK=')), '')
val = cdk.split('=', 1)[1].strip().strip('"') if '=' in cdk else ''
print('card_or_cdk_set', bool(val))
PY
cd "$APP"
.venv/bin/python -m unittest tests.test_extract_link_oai9 tests.test_extract_link_convertmove -v
.venv/bin/python -c "from config import extract_link as c; from core.extract_link_service import queue_settings; print('provider', c.EXTRACT_LINK_PROVIDER); print('base', c.EXTRACT_LINK_API_BASE); print(queue_settings())"
systemctl restart turb-gpt-webui
sleep 2
systemctl is-active turb-gpt-webui
