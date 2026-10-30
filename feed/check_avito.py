"""Verify Avito credentials without printing secrets or changing listings."""
import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://api.avito.ru'


def credentials():
    values = {}
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.removeprefix('export ').split('=', 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    for key in ('AVITO_CLIENT_ID', 'AVITO_CLIENT_SECRET'):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def request(path, token=None, data=None):
    headers = {'Accept': 'application/json', 'User-Agent': 'avito-feed-local-check/1.0'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    req = urllib.request.Request(BASE + path, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--items', action='store_true', help='Also read the first page of listings')
    parser.add_argument('--all-items', action='store_true', help='Read every page of accessible listings')
    args = parser.parse_args()
    config = credentials()
    missing = [key for key in ('AVITO_CLIENT_ID', 'AVITO_CLIENT_SECRET') if not config.get(key)]
    if missing:
        print(json.dumps({'connected': False, 'missing': missing}))
        return 2
    stage = 'token'
    try:
        auth = request('/token/', data={'grant_type': 'client_credentials', 'client_id': config['AVITO_CLIENT_ID'], 'client_secret': config['AVITO_CLIENT_SECRET']})
        token = auth['access_token']
        stage = 'account'
        account = request('/core/v1/accounts/self', token=token)
        report = {'connected': True, 'account_id': account.get('id'), 'token_expires_in_seconds': auth.get('expires_in')}
        if args.items or args.all_items:
            stage = 'items'
            try:
                items = request('/core/v1/items?per_page=100&page=1', token=token)
                report['items_first_page'] = items
                if args.all_items:
                    resources = list(items.get('resources', []))
                    ids = {item['id'] for item in resources}
                    page_items = resources
                    for page in range(2, 101):
                        if len(page_items) < 100:
                            report['pagination_complete'] = True
                            break
                        page_items = request(f'/core/v1/items?per_page=100&page={page}', token=token).get('resources', [])
                        new = [item for item in page_items if item['id'] not in ids]
                        if page_items and not new:
                            report['pagination_complete'] = False
                            break
                        resources.extend(new)
                        ids.update(item['id'] for item in new)
                    else:
                        report['pagination_complete'] = False
                    report['all_items'] = resources
                    report['items_total'] = len(resources)
            except urllib.error.HTTPError as exc:
                report['items_http_status'] = exc.code
        output = ROOT / '.local' / 'avito-status.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        summary = {key: value for key, value in report.items() if key not in {'items_first_page', 'all_items'}}
        if 'items_first_page' in report:
            summary['items_on_first_page'] = len(report['items_first_page'].get('resources', []))
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except urllib.error.HTTPError as exc:
        print(json.dumps({'connected': False, 'stage': stage, 'http_status': exc.code}))
    except (urllib.error.URLError, TimeoutError, OSError):
        print(json.dumps({'connected': False, 'stage': stage, 'error': 'Connection failed or timed out'}))
    except (KeyError, ValueError):
        print(json.dumps({'connected': False, 'stage': stage, 'error': 'Unexpected API response'}))
    return 1


if __name__ == '__main__':
    sys.exit(main())
