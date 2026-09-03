#!/usr/bin/env python3
"""Import store-live candidates produced by audit_domestic_reconciliation.py."""

import argparse
import csv
import html
import json
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import monitor
from import_confirmed_developers import (
    clean_text,
    fetch_gp_detail_html,
    load_apps,
    parse_gp_detail_value,
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRIVATE_BASE = os.path.join(
    os.path.dirname(BASE_DIR), 'product_library_private_evidence'
)
DEFAULT_CSV = os.path.join(
    PRIVATE_BASE,
    'domestic_reconciliation_latest',
    'strong_candidate_store_verification.csv',
)


class StaticPage:
    def __init__(self, body):
        self.page_source = body

    def find_element(self, *_args, **_kwargs):
        raise RuntimeError('Static page has no Selenium elements')


def gp_name(body, package):
    patterns = (
        r'<meta property="og:title" content="(.*?) - Apps on Google Play"',
        r'<h1><span[^>]*itemprop="name"[^>]*>(.*?)</span></h1>',
    )
    for pattern in patterns:
        match = re.search(pattern, body, re.S)
        if match:
            return clean_text(match.group(1))
    return package


def gp_icon(body):
    match = re.search(r'<meta property="og:image" content="(.*?)"', body, re.S)
    return html.unescape(match.group(1)).strip() if match else ''


def gp_tags(body):
    values = []
    for raw in re.findall(
        r'itemprop="genre".*?<span[^>]*aria-hidden="true">(.*?)</span>',
        body,
        re.S,
    ):
        value = clean_text(raw)
        if value and value not in values:
            values.append(value)
    return ', '.join(values)


def fetch_gp(candidate):
    package = candidate['id']
    url = (
        'https://play.google.com/store/apps/details?'
        f'id={package}&hl=en&gl=us'
    )
    body = fetch_gp_detail_html(url)
    name = gp_name(body, package)
    developer, dev_link = monitor.extract_gp_developer_identity(body)
    if name == package or not developer or not dev_link:
        raise RuntimeError('current Google Play product/developer identity unavailable')

    downloads, rating_count = monitor.extract_gp_metrics(StaticPage(body))
    downloads = downloads or monitor.extract_gp_downloads_from_html(body)
    last_update = monitor.normalize_date(
        parse_gp_detail_value(body, 'Updated on')
    )
    release_date = monitor.normalize_date(
        parse_gp_detail_value(body, 'Released on')
    )
    release_source = 'Google Play Released on'
    if not release_date:
        release_date = monitor.normalize_date(candidate.get('release_date', ''))
        release_source = 'AppMagic Release Date'
    if not release_date:
        release_date = monitor.normalize_date(candidate.get('first_detected', ''))
        release_source = 'AppMagic First Detected'
    if not release_date:
        release_date = monitor.fetch_apkcombo_release_date(package, name)
        release_source = 'APKCombo' if release_date else ''
    if not release_date and last_update:
        release_date = last_update
        release_source = 'Google Play Updated on fallback'
    if not release_date:
        raise RuntimeError('release date unavailable from configured fallback chain')

    return {
        'name': name,
        'company_cn': candidate['company'],
        'icon': gp_icon(body),
        'platform': 'GP',
        'pkg_or_id': package,
        'store_link': url,
        'dev_link': dev_link,
        'developer': developer,
        'downloads': downloads,
        'rating_count': rating_count,
        'last_update': last_update,
        'tags': gp_tags(body),
        'removed': False,
        'release_date': release_date,
        '_release_source': release_source,
    }


def lookup_ios(ids):
    found = {}
    for offset in range(0, len(ids), 200):
        batch = ids[offset:offset + 200]
        endpoint = (
            'https://itunes.apple.com/lookup?'
            f'id={",".join(batch)}&country=us'
        )
        payload = monitor.itunes_lookup(endpoint, retries=4) or {}
        for row in payload.get('results', []):
            if row.get('wrapperType') == 'software' and row.get('trackId'):
                found[str(row['trackId'])] = row
        time.sleep(0.3)
    return found


def ios_app(row, company):
    app_id = str(row['trackId'])
    artist_url = row.get('artistViewUrl', '')
    if not artist_url and row.get('artistId'):
        artist_url = f'https://apps.apple.com/developer/id{row["artistId"]}'
    if not row.get('artistName') or not artist_url:
        raise RuntimeError('current Apple developer identity unavailable')
    return {
        'name': row.get('trackName', app_id),
        'company_cn': company,
        'icon': row.get('artworkUrl512', row.get('artworkUrl100', '')),
        'platform': 'iOS',
        'pkg_or_id': app_id,
        'store_link': row.get('trackViewUrl') or f'https://apps.apple.com/app/id{app_id}',
        'dev_link': artist_url,
        'developer': row.get('artistName', ''),
        'downloads': '',
        'rating_count': row.get('userRatingCount', 0),
        'last_update': monitor.normalize_past_or_today_date(
            str(row.get('currentVersionReleaseDate', ''))[:10]
        ),
        'tags': ', '.join(row.get('genres', [])),
        'removed': False,
        'release_date': monitor.normalize_ios_release_date(row),
    }


def load_verified_candidates(path):
    with open(path, newline='', encoding='utf-8-sig') as handle:
        rows = list(csv.DictReader(handle))
    candidates = [row for row in rows if row.get('store_status') == '200']
    seen = set()
    for row in candidates:
        key = (row.get('platform'), row.get('id'))
        if key in seen:
            raise RuntimeError(f'duplicate verified candidate: {key[0]}:{key[1]}')
        seen.add(key)
        if not row.get('company'):
            raise RuntimeError(f'candidate has no company: {key[0]}:{key[1]}')
    return candidates


def clean_for_storage(app):
    return {key: value for key, value in app.items() if not key.startswith('_')}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', default=DEFAULT_CSV)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--report-dir')
    args = parser.parse_args()

    candidates = load_verified_candidates(args.input)
    gp_candidates = [row for row in candidates if row['platform'] == 'GP']
    ios_candidates = [row for row in candidates if row['platform'] == 'iOS']
    fetched = []
    failed = []

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(fetch_gp, row): row for row in gp_candidates
        }
        for index, future in enumerate(as_completed(futures), 1):
            candidate = futures[future]
            try:
                app = future.result()
                fetched.append(app)
                print(
                    f'GP {index}/{len(gp_candidates)} OK '
                    f'{candidate["company"]}: {candidate["id"]}',
                    flush=True,
                )
            except Exception as exc:
                failed.append({
                    'platform': 'GP',
                    'id': candidate['id'],
                    'company': candidate['company'],
                    'error': str(exc),
                })
                print(
                    f'GP {index}/{len(gp_candidates)} FAIL '
                    f'{candidate["company"]}: {candidate["id"]}: {exc}',
                    flush=True,
                )

    ios_lookup = lookup_ios([row['id'] for row in ios_candidates])
    for candidate in ios_candidates:
        row = ios_lookup.get(candidate['id'])
        if not row:
            failed.append({
                'platform': 'iOS',
                'id': candidate['id'],
                'company': candidate['company'],
                'error': 'US Lookup returned no current item',
            })
            continue
        try:
            fetched.append(ios_app(row, candidate['company']))
        except Exception as exc:
            failed.append({
                'platform': 'iOS',
                'id': candidate['id'],
                'company': candidate['company'],
                'error': str(exc),
            })

    apps = load_apps()
    positions = {
        (app['platform'], app['pkg_or_id']): index
        for index, app in enumerate(apps)
    }
    added = []
    already_present = []
    affected = set()
    for raw_app in fetched:
        app = clean_for_storage(raw_app)
        key = (app['platform'], app['pkg_or_id'])
        if key in positions:
            existing = apps[positions[key]]
            if existing.get('company_cn') != app['company_cn']:
                raise RuntimeError(
                    f'company collision for {key[0]}:{key[1]}: '
                    f'{existing.get("company_cn")} != {app["company_cn"]}'
                )
            already_present.append({
                'platform': key[0],
                'id': key[1],
                'company': app['company_cn'],
            })
            continue
        positions[key] = len(apps)
        apps.append(app)
        affected.add(app['company_cn'])
        added.append({
            'platform': key[0],
            'id': key[1],
            'company': app['company_cn'],
            'name': app['name'],
            'developer': app['developer'],
            'release_source': raw_app.get('_release_source', ''),
        })

    keys = [(app['platform'], app['pkg_or_id']) for app in apps]
    if len(keys) != len(set(keys)):
        raise RuntimeError('duplicate platform:id keys before regeneration')
    if affected:
        monitor.regenerate_files(apps, affected)

    report_dir = args.report_dir or os.path.join(
        PRIVATE_BASE,
        datetime.now().strftime('appmagic_verified_import_%Y%m%d_%H%M%S'),
    )
    os.makedirs(report_dir, exist_ok=True)
    report = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'source': os.path.abspath(args.input),
        'verified_candidates': len(candidates),
        'fetched': len(fetched),
        'added': sorted(
            added,
            key=lambda row: (row['company'], row['platform'], row['id']),
        ),
        'already_present': already_present,
        'failed': sorted(
            failed,
            key=lambda row: (row['company'], row['platform'], row['id']),
        ),
        'added_by_company': dict(sorted(Counter(
            row['company'] for row in added
        ).items())),
        'added_by_platform': dict(Counter(
            row['platform'] for row in added
        )),
        'total_after_import': len(apps),
    }
    report_path = os.path.join(report_dir, 'import_report.json')
    with open(report_path, 'w', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({
        key: value for key, value in report.items()
        if key not in {'added', 'already_present', 'failed'}
    } | {
        'already_present': len(already_present),
        'failed': len(failed),
        'report': report_path,
    }, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
