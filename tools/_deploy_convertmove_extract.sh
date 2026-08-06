#!/bin/bash
set -e
APP=/opt/turb-gpt-free-register
cp /tmp/cm_extract/extract_link_service.py "$APP/core/"
cp /tmp/cm_extract/extract_link.py "$APP/config/"
cp /tmp/cm_extract/config_editor.py "$APP/webui/"
cp /tmp/cm_extract/test_extract_link_convertmove.py "$APP/tests/"
python3 <<'PY'
from pathlib import Path
p = Path('/opt/turb-gpt-free-register/.env')
text = p.read_text(encoding='utf-8', errors='replace')
lines = text.splitlines()
wanted = {
    'EXTRACT_LINK_API_BASE': 'EXTRACT_LINK_API_BASE="https://convertmove.cc.cd"',
    'EXTRACT_LINK_TYPE': 'EXTRACT_LINK_TYPE="kakao_pay"',
    'EXTRACT_LINK_WORKERS': 'EXTRACT_LINK_WORKERS="3"',
    'EXTRACT_LINK_EVENT_TIMEOUT': 'EXTRACT_LINK_EVENT_TIMEOUT="180"',
    'EXTRACT_LINK_POLL_INTERVAL': 'EXTRACT_LINK_POLL_INTERVAL="3"',
}
found = set()
out = []
for ln in lines:
    key = ln.split('=', 1)[0].strip() if ('=' in ln and not ln.strip().startswith('#')) else None
    if key in wanted:
        out.append(wanted[key])
        found.add(key)
    else:
        out.append(ln)
for k, v in wanted.items():
    if k not in found:
        out.append(v)
p.write_text('\n'.join(out) + '\n', encoding='utf-8')
print('patched')
for k in wanted:
    print([x for x in out if x.startswith(k + '=')][0])
cdk_line = next((x for x in out if x.startswith('EXTRACT_LINK_CDK=')), 'EXTRACT_LINK_CDK=')
val = cdk_line.split('=', 1)[1].strip().strip('"')
print('CDK_configured', bool(val))
PY
cd "$APP"
.venv/bin/python -m unittest tests.test_extract_link_convertmove -v
.venv/bin/python -c "from config import extract_link as c; from core.extract_link_service import _api_mode, queue_settings; print(c.EXTRACT_LINK_API_BASE, c.EXTRACT_LINK_TYPE, queue_settings(), _api_mode('kakao_pay'))"
systemctl restart turb-gpt-webui
sleep 2
systemctl is-active turb-gpt-webui
