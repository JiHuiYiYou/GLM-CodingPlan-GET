"""
Convert cookie.json → Playwright storage state format, then load via state-load.
"""
import json
from pathlib import Path

data = json.loads(Path('cookie.json').read_text(encoding='utf-8'))
domain = data.get('domain', 'www.bigmodel.cn')

# Cookies: convert to Playwright format
cookies = []
for c in data.get('cookies', []):
    nc = {
        'name': c['name'],
        'value': c['value'],
        'domain': '.' + domain,
        'path': c.get('path', '/'),
        'httpOnly': c.get('httpOnly', False),
        'secure': c.get('secure', False),
        'sameSite': c.get('sameSite', 'Lax'),
    }
    cookies.append(nc)

# localStorage: Playwright uses 'origins' with localStorage array
ls_items = [
    {'name': k, 'value': v}
    for k, v in data.get('localStorage', {}).items()
]

state = {
    'cookies': cookies,
    'origins': [
        {
            'origin': f'https://{domain}',
            'localStorage': ls_items,
        }
    ],
}
Path('.pw-state.json').write_text(
    json.dumps(state, ensure_ascii=False, indent=2),
    encoding='utf-8',
)
print(f'Wrote .pw-state.json: {len(cookies)} cookies, {len(ls_items)} localStorage')
