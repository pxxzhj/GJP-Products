#!/usr/bin/env python3
"""Replay an explicitly supplied failed-entry list without changing the library."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
from pathlib import Path

import monitor
from audit_domestic_reconciliation import (
    DEFAULT_EXCLUDED_COMPANIES, DEFAULT_EXCLUDED_PREFIXES, app_key,
    fetch_all_publishers, fetch_known_mappings, is_excluded, match_failed_apps,
    write_csv,
)
from gp_store_verification import UNAVAILABLE, account_key, scan_developer, verify_app


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def apply_verified_identities(apps, checks):
    changes, conflicts = [], []
    by_id = {app['pkg_or_id']: app for app in apps if app['platform'] == 'GP'}
    for row in checks:
        if row['store_status'] != '200':
            continue
        age = (datetime.now() - datetime.fromisoformat(row['checked_at'])).total_seconds()
        app = by_id.get(row['id'])
        if not app or not 0 <= age < 21600 or app['company_cn'] != row['company']:
            conflicts.append({'id': row['id'], 'reason': 'stale evidence or library ownership changed'})
            continue
        if (app.get('developer', '') != row['library_developer'] or
                app.get('dev_link', '') != row['library_dev_link']):
            conflicts.append({'id': row['id'], 'reason': 'library identity changed since verification'})
            continue
        patch = {}
        if app.get('developer') != row['current_developer']:
            patch['developer'] = row['current_developer']
        if account_key(app.get('dev_link', '')) != account_key(row['current_dev_link']):
            patch['dev_link'] = row['current_dev_link']
        if app.get('removed') and app.get('release_date'):
            patch['removed'] = False
        elif app.get('removed'):
            conflicts.append({'id': row['id'], 'reason': 'live again but release date requires metadata backfill'})
        if patch:
            changes.append({'id': row['id'], 'company': app['company_cn'],
                            'before': {key: app.get(key) for key in patch}, 'after': patch})
            app.update(patch)
    return changes, conflicts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--summary', required=True, help='Explicit monitor summary to replay, including historical runs.')
    parser.add_argument('--output-dir', required=True, help='Private directory outside the repository.')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--resume', action='store_true', help='Reuse same-run app checks under six hours old; rescan accounts.')
    parser.add_argument('--apply-identities', action='store_true', help='Write only individually verified native identities and restore live dated apps; never import candidates or mark removals.')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    out = Path(args.output_dir).resolve()
    if out == root or root in out.parents:
        parser.error('Evidence must be outside the repository')
    out.mkdir(parents=True, exist_ok=True)
    errors = json.loads(Path(args.summary).read_text())['gp_scan']['error_details']
    apps = monitor.load_current_library()
    owners = defaultdict(set)
    for app in apps:
        owners[app_key(app['platform'], app['pkg_or_id'])].add(app['company_cn'])
    matched = [match_failed_apps(error, apps) for error in errors]
    targets = {app['pkg_or_id']: app for group in matched for app in group}
    print(f'Failed entries: {len(errors)}; historical apps: {len(targets)}', flush=True)
    checked = {}
    evidence = out / 'app_pages.jsonl'
    if args.resume and evidence.exists():
        for line in evidence.read_text(encoding='utf-8').splitlines():
            row = json.loads(line)
            age = (datetime.now() - datetime.fromisoformat(row['checked_at'])).total_seconds()
            if 0 <= age < 21600 and row['id'] in targets and row['store_status'] in {'200', '404', 'soft_404'}:
                checked[row['id']] = row
    with evidence.open('w', encoding='utf-8') as handle:
        for row in checked.values():
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(verify_app, pkg): pkg for pkg in targets if pkg not in checked}
            for future in as_completed(futures):
                pkg = futures[future]
                app = targets[pkg]
                row = {'id': pkg, 'company': app['company_cn'],
                       'library_developer': app.get('developer', ''),
                       'library_dev_link': app.get('dev_link', ''),
                       'previously_removed': bool(app.get('removed')),
                       'checked_at': datetime.now().isoformat(), **future.result()}
                checked[pkg] = row
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
                handle.flush()
                if len(checked) % 25 == 0 or len(checked) == len(targets):
                    print(f'App pages: {len(checked)}/{len(targets)} {dict(Counter(x["store_status"] for x in checked.values()))}', flush=True)

    accounts = sorted({row['current_dev_link'] for row in checked.values() if row['store_status'] == '200'})
    scans = {}
    with ThreadPoolExecutor(max_workers=min(args.workers, 3)) as pool:
        futures = {pool.submit(scan_developer, url): url for url in accounts}
        for future in as_completed(futures):
            url = futures[future]
            scans[url] = future.result()
            print(f'Current account: {len(scans)}/{len(accounts)} apps={len(scans[url]["packages"])} complete={scans[url]["complete"]}', flush=True)
    dump(out / 'current_accounts.json', scans)

    old_scans = {}
    with ThreadPoolExecutor(max_workers=min(args.workers, 3)) as pool:
        futures = {pool.submit(scan_developer, error['url']): error['url'] for error in errors}
        for future in as_completed(futures):
            old_scans[futures[future]] = future.result()
    dump(out / 'old_accounts.json', old_scans)

    domestic = [app for app in targets.values() if not is_excluded(app['company_cn'], DEFAULT_EXCLUDED_COMPANIES, DEFAULT_EXCLUDED_PREFIXES)]
    mappings = fetch_known_mappings(domestic, str(out / 'known_mapping_cache.json'), refresh_after_hours=6 if args.resume else 0)
    publishers = {row.get('publisher_id') for row in mappings.values() if row.get('publisher_id')}
    inventory = fetch_all_publishers(publishers, str(out / 'publisher_apps_cache.json'), refresh_after_hours=6 if args.resume else 0)
    missing = {}
    for app in domestic:
        pid = mappings.get(app_key('GP', app['pkg_or_id']), {}).get('publisher_id')
        for candidate in inventory.get(pid, {}).get('apps', []):
            if candidate['platform'] != 'GP':
                continue
            key = app_key('GP', candidate['id'])
            if app['company_cn'] not in owners[key]:
                missing[(app['company_cn'], candidate['id'])] = {
                    **candidate, 'company': app['company_cn'], 'publisher_id': pid,
                    'other_library_owners': sorted(owners[key]), 'source': 'AppMagic',
                }
    for row in checked.values():
        if row['store_status'] != '200':
            continue
        for pkg in scans[row['current_dev_link']]['packages']:
            if row['company'] not in owners[app_key('GP', pkg)]:
                missing.setdefault((row['company'], pkg), {
                    'id': pkg, 'company': row['company'], 'source': 'current_native_account',
                    'other_library_owners': sorted(owners[app_key('GP', pkg)]),
                })
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = {pkg for _, pkg in missing if pkg not in checked}
        futures = {pool.submit(verify_app, pkg): pkg for pkg in pending}
        for future in as_completed(futures):
            checked[futures[future]] = future.result()
    for row in missing.values():
        row['verification'] = checked[row['id']]
    dump(out / 'missing_candidates.json', list(missing.values()))

    rows = []
    for error, group in zip(errors, matched):
        states = Counter(checked[app['pkg_or_id']]['store_status'] for app in group)
        urls = {checked[app['pkg_or_id']]['current_dev_link'] for app in group if checked[app['pkg_or_id']]['store_status'] == '200'}
        candidate_rows = [x for x in missing.values() if x['company'] == error['company']]
        mapping_errors = [app['pkg_or_id'] for app in group if app['company_cn'] in {x['company_cn'] for x in domestic} and mappings.get(app_key('GP', app['pkg_or_id']), {}).get('status') != 'mapped']
        rows.append({
            'company': error['company'], 'old_developer': error['developer'], 'old_url': error['url'],
            'old_entry_complete': old_scans[error['url']]['complete'],
            'old_entry_error': old_scans[error['url']]['error'],
            'historical_apps': len(group), 'live': states['200'],
            'us_unavailable': sum(states[x] for x in UNAVAILABLE),
            'verification_errors': sum(n for s, n in states.items() if s != '200' and s not in UNAVAILABLE),
            'current_accounts': len(urls), 'incomplete_accounts': sum(not scans[url]['complete'] for url in urls),
            'live_missing_company_candidates': sum(x['verification']['store_status'] == '200' for x in candidate_rows),
            'mapping_unresolved': len(mapping_errors),
            'appmagic_scope': 'excluded_overseas' if is_excluded(error['company'], DEFAULT_EXCLUDED_COMPANIES, DEFAULT_EXCLUDED_PREFIXES) else 'targeted_publishers_refreshed',
        })
    write_csv(str(out / 'entry_review.csv'), rows, list(rows[0]) if rows else [])
    summary = {'checked_at': datetime.now().isoformat(), 'source_summary': args.summary,
               'entries': len(errors), 'historical_apps': len(targets),
               'app_states': dict(Counter(checked[pkg]['store_status'] for pkg in targets)),
               'current_accounts': len(scans), 'incomplete_accounts': sum(not x['complete'] for x in scans.values()),
               'publisher_errors': {pid: x.get('error') or 'truncated' for pid, x in inventory.items() if x.get('error') or x.get('possibly_truncated')},
               'live_missing_candidates': sum(x['verification']['store_status'] == '200' for x in missing.values()),
               'entries_report': rows}
    dump(out / 'summary.json', summary)
    if args.apply_identities:
        changes, conflicts = apply_verified_identities(apps, [checked[pkg] for pkg in targets])
        backfill_failures = []
        for conflict in conflicts:
            if conflict['reason'] != 'live again but release date requires metadata backfill':
                backfill_failures.append(conflict)
                continue
            from import_verified_appmagic_candidates import fetch_gp
            app = targets[conflict['id']]
            try:
                detail = fetch_gp({'id': app['pkg_or_id'], 'company': app['company_cn']})
                patch = {key: value for key, value in detail.items() if not key.startswith('_')}
                changes.append({'id': app['pkg_or_id'], 'company': app['company_cn'],
                                'before': dict(app), 'after': patch})
                app.update(patch)
            except Exception as exc:
                backfill_failures.append({**conflict, 'error': str(exc)})
        conflicts = backfill_failures
        dump(out / 'identity_changes.json', {'changes': changes, 'conflicts': conflicts})
        monitor.regenerate_files(apps, {row['company'] for row in changes})
        print(f'Applied identity/status changes: {len(changes)}; conflicts: {len(conflicts)}', flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
