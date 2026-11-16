"""Apply an explicitly approved one-day promotion window and restore its snapshot.

Amounts in the plan and API are kopecks. Feed amounts are rubles. Credentials
are read only from the existing local client, never copied into the plan/log.
"""
import argparse
import concurrent.futures
import contextlib
import copy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import check_avito

ROOT = Path(__file__).resolve().parents[1]
MSK = timezone(timedelta(hours=3))
SOURCES = ['feed/ads.json', 'feed/avito.xml', 'feed/placement-policy.json', 'README.md']
GIT_FLAGS = {'creationflags': 0x08000000} if sys.platform == 'win32' else {}


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def atomic(path, raw):
    tmp = path.with_name(path.name + '.temporary-write')
    tmp.write_bytes(raw)
    tmp.replace(path)


def write(path, value):
    atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


def now():
    return datetime.now(MSK).isoformat(timespec='seconds')


def emit(out, value):
    line = json.dumps({'time_moscow': now(), **value}, ensure_ascii=False)
    with (out / 'runtime.log').open('a', encoding='utf-8') as f:
        f.write(line + '\n')
    if sys.stdout is not None:
        print(line, flush=True)


class Api:
    def __init__(self):
        cfg = check_avito.credentials()
        self.token = check_avito.request('/token/', data={
            'grant_type': 'client_credentials', 'client_id': cfg['AVITO_CLIENT_ID'],
            'client_secret': cfg['AVITO_CLIENT_SECRET'],
        })['access_token']
        self.locks = {'bids': threading.Lock(), 'manual': threading.Lock()}
        self.last = {}

    def request(self, path, body=None):
        bucket = 'bids' if '/getBids/' in path else 'manual' if path.endswith('/setManual') else None
        for attempt in range(3):
            if bucket:
                with self.locks[bucket]:
                    delay = 3.55 - (time.monotonic() - self.last.get(bucket, 0))
                    if delay > 0:
                        time.sleep(delay)
                    self.last[bucket] = time.monotonic()
            req = urllib.request.Request(
                'https://api.avito.ru' + path,
                data=None if body is None else json.dumps(body).encode('utf-8'),
                headers={'Authorization': 'Bearer ' + self.token,
                         'Content-Type': 'application/json', 'X-Source': 'avito-feed-project'},
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    raw = response.read()
                return json.loads(raw) if raw else {'ok': True}
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise RuntimeError(f'Avito HTTP {exc.code}: {path}') from None
            except (TimeoutError, urllib.error.URLError):
                if attempt == 2:
                    raise RuntimeError(f'Avito connection timeout: {path}') from None
            time.sleep(10 * (attempt + 1))

    def promotions(self, ids):
        rows = self.request('/cpxpromo/1/getPromotionsByItemIds', {'itemIDs': ids})['items']
        return {x['itemID']: x for x in rows}


def desired_promotion(item, restore):
    if restore:
        return item['before_promotion']
    return {'itemID': item['avito_id'], 'actionTypeID': 5,
            'manualPromotion': item['temporary_manual']}


def matches(actual, expected):
    if expected is None:
        return actual is None
    return (actual is not None and actual.get('actionTypeID') == expected['actionTypeID']
            and actual.get('manualPromotion') == expected.get('manualPromotion')
            and actual.get('autoPromotion') == expected.get('autoPromotion'))


def source_promo(item, restore):
    if restore:
        return item['before_source_promo']
    m = item['temporary_manual']
    assert m['bidPenny'] % 100 == m['limitPenny'] % 100 == 0
    return {'bid': m['bidPenny'] // 100, 'daily_limit': m['limitPenny'] // 100}


def without_promo(block):
    return re.sub(r'^    <Promo(?:ManualOptions)?>.*?</Promo(?:ManualOptions)?>\r?\n', '', block, flags=re.M)


def patch_sources(rows, raw, plan, restore):
    """Edit promotion fields only, with baseline/temporary conflict guards."""
    rows = copy.deepcopy(rows)
    selected = {x['autoload_id']: x for x in plan['items']}
    seen = set()
    skipped = set()
    for row in rows:
        if row['id'] not in selected:
            continue
        item = selected[row['id']]
        unchanged_identity = (row['avito_id'], row['direction'], row['city']) == (item['avito_id'], item['direction'], item['city'])
        known_promotion = row.get('promo') in [source_promo(item, False), source_promo(item, True)]
        if restore and not (unchanged_identity and known_promotion):
            skipped.add(row['id'])
            continue
        assert unchanged_identity and known_promotion, ('Unrecognized source promotion', row['id'])
        row['promo'] = source_promo(item, restore)
        seen.add(row['id'])
    if restore:
        skipped.update(set(selected) - seen)
        selected = {k: v for k, v in selected.items() if k not in skipped}
    assert seen == set(selected)
    newline = '\r\n' if '\r\n' in raw else '\n'
    xml_seen = set()

    def patch(match):
        block = match.group()
        element = ET.fromstring(block)
        aid = element.findtext('Id')
        if aid not in selected:
            return block
        item = selected[aid]
        assert element.findtext('AvitoId') == str(item['avito_id'])
        base = without_promo(block)
        target = source_promo(item, restore)
        if target:
            insertion = (f'    <Promo>Manual</Promo>{newline}'
                         f'    <PromoManualOptions><Item><Bid>{target["bid"]}</Bid>'
                         f'<DailyLimit>{target["daily_limit"]}</DailyLimit></Item></PromoManualOptions>{newline}')
            assert base.count('    <Description>') == 1
            result = base.replace('    <Description>', insertion + '    <Description>', 1)
        else:
            result = base
        assert without_promo(result) == without_promo(block)
        xml_seen.add(aid)
        return result

    new_raw = re.sub(r'  <Ad>.*?</Ad>', patch, raw, flags=re.S)
    assert xml_seen == set(selected)
    assert len(ET.fromstring(new_raw)) == len(rows)
    return rows, new_raw


def save_sources(plan, out, restore, phase):
    rows, raw = patch_sources(read(ROOT / 'feed/ads.json'), (ROOT / 'feed/avito.xml').read_bytes().decode('utf-8'), plan, restore)
    policy = read(ROOT / 'feed/placement-policy.json')
    byid = {x['avito_id']: x for x in policy['listings']}
    by_source = {x['id']: x for x in rows}
    for item in plan['items']:
        if restore and (item['avito_id'] not in byid or
                        by_source.get(item['autoload_id'], {}).get('promo') != source_promo(item, True)):
            continue
        row = byid[item['avito_id']]
        if restore:
            row.update(item['before_policy_fields'])
        else:
            row.update(bid_rub=item['temporary_manual']['bidPenny'] / 100,
                       daily_limit_rub=item['temporary_manual']['limitPenny'] / 100,
                       pricing='temporary_20261115')
    policy['temporary_promotion_window'] = {
        'window_id': plan['window_id'], 'plan_file': f'feed/promotion-windows/{plan["window_id"]}.json',
        'restore_at_moscow': plan['restore_at'], 'affected_count': len(plan['items']),
        'phase': phase, 'updated_at_moscow': now(),
        'tutoring_increment_rub': 20, 'ml_programming_increment_rub': 10,
        'excluded': 'AI directions and tutoring in Makhachkala',
        'moscow_ml_programming_temporary_bid_rub': 31,
    }
    if phase in ('active_verified', 'restored_verified'):
        policy.update(manual_pricing_count=plan['baseline_manual_count'] if restore else 135,
                      base_minimum_exceptions_count=3 if restore else 1, verified_at_moscow=now())
    begin = '<!-- temporary-promotion-20261115 -->'
    end = '<!-- /temporary-promotion-20261115 -->'
    note = ('Временное повышение 15.11.2026 завершено: прежние ставки и лимиты восстановлены и проверены.'
            if phase == 'restored_verified' else
            '15.11.2026 действует временное повышение: 74 репетиторских объявления +20 ₽, ML и помощь студентам +10 ₽; '
            'московские ML и помощь студентам — 31 ₽ / 93 ₽. Репетиторство в Махачкале и направления ИИ без изменений. '
            'Возврат точных прежних настроек запускается 16.11.2026 в 00:00 по Москве. '
            'Указанные ниже постоянные ставки временно перекрываются этим правилом.')
    if phase == 'restoring':
        note = '16.11.2026: выполняется возврат прежних ставок после однодневного повышения; файл автозагрузки уже возвращён к исходным значениям.'
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    fragment = begin + '\n' + note + '\n' + end
    if begin in readme:
        readme = re.sub(re.escape(begin) + r'.*?' + re.escape(end), lambda _: fragment, readme, flags=re.S)
    else:
        readme = readme.replace('# Объявления Авито\n', '# Объявления Авито\n\n' + fragment + '\n', 1)
    write(ROOT / 'feed/ads.json', rows)
    atomic(ROOT / 'feed/avito.xml', raw.encode('utf-8'))
    write(ROOT / 'feed/placement-policy.json', policy)
    atomic(ROOT / 'README.md', readme.encode('utf-8'))
    write(out / 'source-state.json', {'phase': phase, 'updated_at_moscow': now()})


def git_sync(out, message):
    git_exe = shutil.which('git')
    if git_exe is None:
        # Task Scheduler does not inherit the Codex app's augmented PATH.
        candidates = [
            Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/native/git/cmd/git.exe',
            Path('C:/Program Files/Git/cmd/git.exe'),
        ]
        git_exe = next((str(path) for path in candidates if path.is_file()), None)
    if git_exe is None:
        raise FileNotFoundError('Git executable unavailable; live restoration can still proceed')
    def git(*args):
        return subprocess.check_output([git_exe, *args], cwd=ROOT, stderr=subprocess.STDOUT, **GIT_FLAGS)
    staged = git('diff', '--cached', '--name-only').decode().splitlines()
    assert set(staged) <= set(SOURCES), 'Other staged work must not be committed'
    git('add', '--', *SOURCES)
    cfg = check_avito.credentials()
    for name in git('diff', '--cached', '--name-only').decode().splitlines():
        blob = git('show', ':' + name)
        for key in ['AVITO_CLIENT_ID', 'AVITO_CLIENT_SECRET']:
            assert cfg[key].encode() not in blob, 'Credential found in staged content'
    git('diff', '--cached', '--check')
    if git('diff', '--cached', '--name-only').strip():
        git('commit', '-m', message)
    git('push', 'origin', 'HEAD:main')
    emit(out, {'github_synced': True, 'commit': git('rev-parse', 'HEAD').decode().strip()})


def validate_manual(detail, manual):
    assert detail['actionTypeID'] == 5
    m = detail['manual']
    assert manual['bidPenny'] % 100 == 0
    assert m['minBidPenny'] < manual['bidPenny'] <= m['maxBidPenny'], 'Bid outside current allowed range'
    assert m['minLimitPenny'] <= manual['limitPenny'] <= m['maxLimitPenny'], 'Limit outside current allowed range'


def change_api(plan, out, restore):
    api = Api()
    ids = plan['baseline_active_ids']
    current = api.promotions(ids)
    cache = read(out / 'minimums.json') if not restore else {}
    pending = []
    conflicts = []
    for item in plan['items']:
        iid = item['avito_id']
        if matches(current.get(iid), desired_promotion(item, restore)):
            continue
        if not matches(current.get(iid), desired_promotion(item, not restore)):
            conflicts.append(iid)
            continue
        pending.append(item)
    results = {}
    lock = threading.Lock()

    def change(item):
        iid = item['avito_id']
        try:
            detail = api.request('/cpxpromo/1/getBids/' + str(iid)) if restore else cache[str(iid)]['data']
            target = desired_promotion(item, restore)
            if target is None:
                assert restore and item['before_promotion'] is None
                api.request('/cpxpromo/1/remove', {'itemID': iid})
            else:
                manual = target['manualPromotion']
                validate_manual(detail, manual)
                api.request('/cpxpromo/1/setManual', {'itemID': iid, 'actionTypeID': 5, **manual})
            result = {'ok': True}
        except Exception as exc:
            result = {'error': type(exc).__name__ + ': ' + str(exc)[:200]}
        with lock:
            results[str(iid)] = result
            write(out / ('restore-results.json' if restore else 'apply-results.json'), results)
            if len(results) % 20 == 0 or 'error' in result:
                emit(out, {'processed': len(results), 'pending_total': len(pending), 'errors': sum('error' in x for x in results.values())})

    emit(out, {'mode': 'restore' if restore else 'apply', 'pending': len(pending), 'conflicts': conflicts})
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(change, pending))
    time.sleep(5)
    after = api.promotions(ids)
    missing = [x['avito_id'] for x in plan['items'] if not matches(after.get(x['avito_id']), desired_promotion(x, restore))]
    for item in plan['untouched']:
        assert matches(after.get(item['avito_id']), item['before_promotion']), 'Excluded promotion changed'
    proof = {'complete': not missing, 'mode': 'restore' if restore else 'apply', 'target_count': len(plan['items']),
             'missing_ids': missing, 'excluded_unchanged': True, 'verified_at_moscow': now()}
    write(out / ('restore-verified.json' if restore else 'apply-verified.json'), proof)
    emit(out, proof)
    assert not missing, 'Some promotions remain unverified; retry is safe and does not add again'


def offline_check(plan, out):
    old = read(out / 'before-ads.json')
    raw = (out / 'before-avito.xml').read_bytes().decode('utf-8')
    boosted, boosted_raw = patch_sources(old, raw, plan, False)
    restored, restored_raw = patch_sources(boosted, boosted_raw, plan, True)
    assert restored == old and restored_raw == raw, 'Snapshot round trip failed'
    twice, twice_raw = patch_sources(boosted, boosted_raw, plan, False)
    assert twice == boosted and twice_raw == boosted_raw, 'Replay increased twice'
    assert len(plan['items']) == 134 and len(plan['untouched']) == 2
    assert all(not (x['direction'] == 'tutoring' and x['city'] == 'Махачкала') for x in plan['items'])
    assert all(x['direction'] not in ['ai-lessons', 'ai-course'] for x in plan['items'])
    assert all(x['temporary_manual']['limitPenny'] == x['temporary_manual']['bidPenny'] * 3 for x in plan['items'])
    write(out / 'offline-verified.json', {'complete': True, 'exact_source_and_xml_round_trip': True, 'repeat_does_not_add_again': True, 'excluded_preserved': True})
    emit(out, {'offline_checks_passed': True})


@contextlib.contextmanager
def single_run(out):
    with (out / 'run.lock').open('a+b') as handle:
        handle.seek(0)
        handle.write(b'0')
        handle.flush()
        handle.seek(0)
        if sys.platform == 'win32':
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                emit(out, {'another_run_in_progress': True})
                raise SystemExit(0)
        try:
            yield
        finally:
            if sys.platform == 'win32':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['check', 'source-apply', 'apply', 'restore'])
    parser.add_argument('--window', default='2026-11-15')
    args = parser.parse_args()
    plan = read(ROOT / 'feed/promotion-windows' / (args.window + '.json'))
    out = ROOT / '.local' / ('day-boost-' + args.window.replace('-', ''))
    out.mkdir(exist_ok=True)
    if args.mode == 'check':
        offline_check(plan, out)
        return
    restore = args.mode == 'restore'
    deadline = datetime.fromisoformat(plan['restore_at'])
    assert (datetime.now(MSK) >= deadline) if restore else (datetime.fromisoformat(plan['valid_from']) <= datetime.now(MSK) < deadline), 'Outside authorized time window'
    with single_run(out):
        if restore:
            # Publish baseline first so an autoload run cannot reapply the boost.
            try:
                save_sources(plan, out, True, 'restoring')
                git_sync(out, 'Restore baseline feed after November 15 promotion window')
            except Exception:
                emit(out, {'source_or_github_sync_failed': True, 'continuing_live_restoration': True})
            change_api(plan, out, True)
            save_sources(plan, out, True, 'restored_verified')
            git_sync(out, 'Record verified restoration of November 15 promotion window')
            write(out / 'completed.json', {'complete': True, 'restored_at_moscow': now(), 'github_synced': True})
        elif args.mode == 'source-apply':
            assert read(out / 'offline-verified.json')['complete']
            assert read(out / 'minimums-verified.json')['complete']
            assert read(out / 'rollback-job.json')['registered']
            assert read(out / 'automation.json')['active']
            save_sources(plan, out, False, 'prepared')
            emit(out, {'temporary_source_prepared': True})
        else:
            assert read(out / 'minimums-verified.json')['complete']
            assert read(out / 'rollback-job.json')['registered'] and read(out / 'automation.json')['active']
            assert read(out / 'source-published.json')['complete']
            change_api(plan, out, False)
            save_sources(plan, out, False, 'active_verified')
            git_sync(out, 'Verify temporary November 15 promotion rates')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        window = sys.argv[sys.argv.index('--window') + 1] if '--window' in sys.argv else '2026-11-15'
        out = ROOT / '.local' / ('day-boost-' + window.replace('-', ''))
        out.mkdir(exist_ok=True)
        error = {'fatal_error': type(exc).__name__ + ': ' + str(exc)[:240]}
        emit(out, error)
        write(out / 'failure.json', {**error, 'time_moscow': now()})
        raise
