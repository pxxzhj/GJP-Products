#!/usr/bin/env python3
"""Discover and privately archive app-ads.txt files for the current library."""

import argparse
import csv
import hashlib
import ipaddress
import json
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from itertools import combinations
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from audit_icon_appads_correlation import COMMON_AD_SYSTEMS, parse_app_ads
from audit_web_evidence import extract_gp_contact_urls, extract_ios_official_urls
from audit_web_fetcher import fetch_url, itunes_lookup_batch
from import_confirmed_developers import load_apps


PRIVATE_ROOT = Path(
    '/Users/zhenghongjian/Documents/software/claude/GameJP_GPT/'
    'product_library_private_evidence'
)
IGNORED_HOSTS = {
    'apps.apple.com',
    'itunes.apple.com',
    'play.google.com',
    'support.google.com',
    'facebook.com',
    'www.facebook.com',
    'instagram.com',
    'www.instagram.com',
    'linkedin.com',
    'www.linkedin.com',
    'twitter.com',
    'x.com',
    'youtube.com',
    'www.youtube.com',
    'tiktok.com',
    'www.tiktok.com',
    'discord.com',
    # Social/profile hosts are not developer-owned app-ads.txt origins. A
    # profile URL on one of these hosts must not become /app-ads.txt.
    'weibo.com',
    'weibo.cn',
    'm.weibo.cn',
    'weibo.com.cn',
    'xiaohongshu.com',
    'xhslink.com',
}
TRUSTED_STORE_SOURCE_KINDS = {'app_store', 'google_play'}


def app_key(app):
    return f"{app.get('platform', '')}:{app.get('pkg_or_id', '')}"


def developer_key(app):
    return '|'.join((
        str(app.get('platform', '')),
        str(app.get('developer', '')),
        str(app.get('dev_link', '')),
    ))


def read_jsonl(path, key_field='app_key'):
    rows = {}
    if not path.exists():
        return rows
    with path.open(encoding='utf-8') as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            key = row.get(key_field)
            if key:
                rows[key] = row
    return rows


def append_jsonl(path, row):
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + '\n')


def normalize_candidate(url):
    if not isinstance(url, str):
        return ''
    url = url.strip()
    if not url or url.lower().startswith('mailto:'):
        return ''
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ''
    if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname:
        return ''
    host = parsed.hostname.lower().rstrip('.')
    if (
        host in IGNORED_HOSTS
        or any(host.endswith('.' + ignored) for ignored in IGNORED_HOSTS)
        or host == 'localhost'
        or host.endswith('.localhost')
    ):
        return ''
    try:
        address = ipaddress.ip_address(host)
        if not address.is_global:
            return ''
    except ValueError:
        pass

    direct_match = re.search(r'(?i)(^|/)app-ads\.txt(?:$|[?#])', parsed.path)
    path = parsed.path if direct_match else '/app-ads.txt'
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, '', ''))


def historical_contacts(evidence_root, current_keys):
    # Historical web-crawl contacts are not interchangeable with current store
    # metadata. In particular, old email-domain and recursive discoveries may
    # have been recorded under the seed app's context. Only explicitly tagged
    # store-page records are eligible as a fallback source here; untagged legacy
    # rows and mapping caches are intentionally excluded.
    trusted_source_kinds = {'app_store', 'google_play'}
    contacts = defaultdict(set)
    for path in evidence_root.glob('**/store_contact_pages.jsonl'):
        try:
            handle = path.open(encoding='utf-8')
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                key = f"{row.get('platform', '')}:{row.get('pkg_or_id', '')}"
                if key not in current_keys:
                    continue
                if row.get('source_kind') not in trusted_source_kinds:
                    continue
                for url in row.get('contact_urls') or []:
                    if isinstance(url, str) and url.startswith(('http://', 'https://')):
                        contacts[key].add(url)
    return contacts


def scan_ios(apps, output_dir, batch_size=200):
    cache_path = output_dir / 'ios_lookup.jsonl'
    cached = read_jsonl(cache_path)
    pending = [app for app in apps if app_key(app) not in cached]
    total_batches = (len(pending) + batch_size - 1) // batch_size
    for batch_index in range(0, len(pending), batch_size):
        batch = pending[batch_index:batch_index + batch_size]
        lookup = itunes_lookup_batch([app['pkg_or_id'] for app in batch], country='us')
        for app in batch:
            item = lookup.get(app['pkg_or_id'], {})
            row = {
                'app_key': app_key(app),
                'company': app.get('company_cn', ''),
                'developer_key': developer_key(app),
                'developer': app.get('developer', ''),
                'dev_link': app.get('dev_link', ''),
                'lookup_found': bool(item),
                'seller_url': item.get('sellerUrl', ''),
                'artist_id': str(item.get('artistId', '')),
                'artist_name': item.get('artistName', ''),
                'fetched_at': datetime.now().isoformat(timespec='seconds'),
            }
            cached[row['app_key']] = row
            append_jsonl(cache_path, row)
        current = batch_index // batch_size + 1
        print(f'iOS Lookup: {current}/{total_batches}', flush=True)
        time.sleep(0.25)
    return cached


def fetch_gp_contacts(app, timeout):
    result = {}
    for attempt in range(3):
        result = fetch_url(app['store_link'], timeout=timeout)
        if result['status'] not in {0, 429, 500, 502, 503, 504}:
            break
        time.sleep(attempt + 1)
    if result['status'] == 200 and result['text']:
        contacts = extract_gp_contact_urls(result['text'])
    else:
        contacts = []
    return {
        'app_key': app_key(app),
        'company': app.get('company_cn', ''),
        'developer_key': developer_key(app),
        'developer': app.get('developer', ''),
        'dev_link': app.get('dev_link', ''),
        'store_link': app.get('store_link', ''),
        'removed': bool(app.get('removed')),
        'status': result['status'],
        'final_url': result['final_url'],
        'contact_urls': contacts,
        'error': result['error'],
        'elapsed': result['elapsed'],
        'fetched_at': datetime.now().isoformat(timespec='seconds'),
    }


def scan_gp(apps, output_dir, workers, timeout, include_removed=False):
    cache_path = output_dir / 'gp_store_contacts.jsonl'
    cached = read_jsonl(cache_path)
    retry_statuses = {0, 429, 500, 502, 503, 504}
    targets = [
        app for app in apps
        if (include_removed or not app.get('removed'))
        and (
            app_key(app) not in cached
            or cached[app_key(app)].get('status') in retry_statuses
        )
    ]
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_gp_contacts, app, timeout): app
            for app in targets
        }
        for future in as_completed(futures):
            app = futures[future]
            try:
                row = future.result()
            except Exception as exc:
                row = {
                    'app_key': app_key(app),
                    'company': app.get('company_cn', ''),
                    'developer_key': developer_key(app),
                    'developer': app.get('developer', ''),
                    'dev_link': app.get('dev_link', ''),
                    'store_link': app.get('store_link', ''),
                    'removed': bool(app.get('removed')),
                    'status': 0,
                    'final_url': '',
                    'contact_urls': [],
                    'error': str(exc),
                    'elapsed': 0,
                    'fetched_at': datetime.now().isoformat(timespec='seconds'),
                }
            cached[row['app_key']] = row
            append_jsonl(cache_path, row)
            completed += 1
            if completed % 100 == 0 or completed == len(targets):
                ok = sum(item.get('status') == 200 for item in cached.values())
                print(
                    f'GP contacts: {completed}/{len(targets)} new; '
                    f'{len(cached)} cached; {ok} HTTP 200',
                    flush=True,
                )
    return cached


def fetch_ios_store_contacts(app, lookup_row, timeout):
    result = {}
    for attempt in range(3):
        result = fetch_url(app['store_link'], timeout=timeout)
        if result['status'] not in {0, 429, 500, 502, 503, 504}:
            break
        time.sleep(attempt + 1)
    urls = []
    if result['status'] == 200 and result['text']:
        urls = extract_ios_official_urls(
            result['text'], {'sellerUrl': lookup_row.get('seller_url', '')}
        )
    return {
        'app_key': app_key(app),
        'company': app.get('company_cn', ''),
        'developer_key': developer_key(app),
        'developer': app.get('developer', ''),
        'dev_link': app.get('dev_link', ''),
        'store_link': app.get('store_link', ''),
        'status': result['status'],
        'final_url': result['final_url'],
        'contact_urls': urls,
        'error': result['error'],
        'elapsed': result['elapsed'],
        'fetched_at': datetime.now().isoformat(timespec='seconds'),
    }


def scan_ios_store_pages(apps, ios_rows, target_accounts, output_dir, workers, timeout):
    cache_path = output_dir / 'ios_store_contacts.jsonl'
    cached = read_jsonl(cache_path)
    retry_statuses = {0, 429, 500, 502, 503, 504}
    targets = [
        app for app in apps
        if developer_key(app) in target_accounts
        and not app.get('removed')
        and (
            app_key(app) not in cached
            or cached[app_key(app)].get('status') in retry_statuses
        )
    ]
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                fetch_ios_store_contacts,
                app,
                ios_rows.get(app_key(app), {}),
                timeout,
            ): app
            for app in targets
        }
        for future in as_completed(futures):
            app = futures[future]
            try:
                row = future.result()
            except Exception as exc:
                row = {
                    'app_key': app_key(app),
                    'company': app.get('company_cn', ''),
                    'developer_key': developer_key(app),
                    'developer': app.get('developer', ''),
                    'dev_link': app.get('dev_link', ''),
                    'store_link': app.get('store_link', ''),
                    'status': 0,
                    'final_url': '',
                    'contact_urls': [],
                    'error': str(exc),
                    'elapsed': 0,
                    'fetched_at': datetime.now().isoformat(timespec='seconds'),
                }
            cached[row['app_key']] = row
            append_jsonl(cache_path, row)
            completed += 1
            if completed % 50 == 0 or completed == len(targets):
                print(
                    f'iOS store fallback: {completed}/{len(targets)} new; '
                    f'{sum(item.get("status") == 200 for item in cached.values())} HTTP 200',
                    flush=True,
                )
    return cached


def candidate_evidence(apps, ios_rows, gp_rows, ios_store_rows=None):
    """Build candidates from current store metadata only.

    Historical web-crawl contacts remain in the private evidence backup, but
    are deliberately excluded here. Old crawls may have inherited the seed
    app's company after recursive or email-domain expansion and are unsafe as
    current ownership evidence.
    """
    evidence = defaultdict(list)
    seen = set()
    ios_store_rows = ios_store_rows or {}
    for app in apps:
        key = app_key(app)
        urls = []
        if app['platform'] == 'iOS':
            seller_url = ios_rows.get(key, {}).get('seller_url', '')
            if seller_url:
                urls.append((seller_url, 'current_ios_lookup'))
            for contact_url in ios_store_rows.get(key, {}).get('contact_urls') or []:
                urls.append((contact_url, 'current_ios_store_page'))
            source_kind = 'app_store'
        else:
            urls.extend(
                (url, 'current_google_play_contact')
                for url in gp_rows.get(key, {}).get('contact_urls') or []
            )
            source_kind = 'google_play'
        for source_url, source in urls:
            candidate = normalize_candidate(source_url)
            if not candidate:
                continue
            dedupe_key = (candidate, key, source_url, source)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            evidence[candidate].append({
                'app_key': key,
                'company': app.get('company_cn', ''),
                'developer_key': developer_key(app),
                'developer': app.get('developer', ''),
                'dev_link': app.get('dev_link', ''),
                'source': source,
                'source_kind': source_kind,
                'source_url': source_url,
            })
    return evidence


def validate_candidate_evidence(evidence):
    """Fail closed if unsafe historical/recursive evidence leaks back in."""
    invalid = []
    for url, refs in evidence.items():
        for ref in refs:
            source_kind = ref.get('source_kind', '')
            source = ref.get('source', '')
            if source_kind not in TRUSTED_STORE_SOURCE_KINDS:
                invalid.append((url, ref.get('app_key', ''), source_kind, source))
            if 'email_domain' in source or 'historical' in source:
                invalid.append((url, ref.get('app_key', ''), source_kind, source))
    if invalid:
        preview = '; '.join(':'.join(map(str, item)) for item in invalid[:5])
        raise RuntimeError(
            'Unsafe app-ads evidence source detected; refusing to archive: '
            + preview
        )
    return {
        'passed': True,
        'candidate_refs': sum(len(refs) for refs in evidence.values()),
        'candidate_urls': len(evidence),
        'allowed_source_kinds': sorted(TRUSTED_STORE_SOURCE_KINDS),
        'historical_and_email_domain_refs': 0,
    }


def fetch_app_ads_candidate(url, timeout, raw_dir):
    result = {}
    for attempt in range(3):
        result = fetch_url(url, timeout=timeout)
        if result['status'] not in {0, 429, 500, 502, 503, 504}:
            break
        time.sleep(attempt + 1)
    text = result['text'] or ''
    rows, variables = parse_app_ads(text)
    valid = result['status'] == 200 and bool(
        rows or variables.get('OWNERDOMAIN') or variables.get('MANAGERDOMAIN')
    )
    content = text.encode('utf-8')
    raw_path = ''
    if result['status'] == 200 and text:
        digest = hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]
        host = re.sub(r'[^A-Za-z0-9.-]+', '_', urlsplit(url).netloc)[:100]
        path = raw_dir / f'{host}_{digest}.txt'
        path.write_text(text, encoding='utf-8')
        raw_path = str(path)
    return {
        'url': url,
        'final_url': result['final_url'],
        'status': result['status'],
        'content_type': result['content_type'],
        'error': result['error'],
        'elapsed': result['elapsed'],
        'bytes': len(content),
        'sha256': hashlib.sha256(content).hexdigest() if content else '',
        'parsed_rows': len(rows),
        'direct_rows': sum(row[2] == 'DIRECT' for row in rows),
        'owner_domain': variables.get('OWNERDOMAIN', ''),
        'manager_domain': variables.get('MANAGERDOMAIN', ''),
        'valid_app_ads': valid,
        'raw_path': raw_path,
        'fetched_at': datetime.now().isoformat(timespec='seconds'),
    }


def archive_candidates(evidence, output_dir, workers, timeout):
    cache_path = output_dir / 'app_ads_fetches.jsonl'
    excluded_cache_path = output_dir / 'historical_excluded_app_ads_fetches.jsonl'
    cached = read_jsonl(excluded_cache_path, key_field='url')
    cached.update(read_jsonl(cache_path, key_field='url'))
    for row in cached.values():
        if row.get('status') != 200:
            row['valid_app_ads'] = False
    retry_statuses = {0, 429, 500, 502, 503, 504}
    pending = [
        url for url in evidence
        if url not in cached or cached[url].get('status') in retry_statuses
    ]
    raw_dir = output_dir / 'raw'
    raw_dir.mkdir(exist_ok=True)
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_app_ads_candidate, url, timeout, raw_dir): url
            for url in pending
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                row = future.result()
            except Exception as exc:
                row = {
                    'url': url,
                    'final_url': '',
                    'status': 0,
                    'content_type': '',
                    'error': str(exc),
                    'elapsed': 0,
                    'bytes': 0,
                    'sha256': '',
                    'parsed_rows': 0,
                    'direct_rows': 0,
                    'owner_domain': '',
                    'manager_domain': '',
                    'valid_app_ads': False,
                    'raw_path': '',
                    'fetched_at': datetime.now().isoformat(timespec='seconds'),
                }
            cached[url] = row
            append_jsonl(cache_path, row)
            completed += 1
            if completed % 50 == 0 or completed == len(pending):
                valid = sum(item.get('valid_app_ads') for item in cached.values())
                print(
                    f'app-ads.txt: {completed}/{len(pending)} new; '
                    f'{len(cached)} cached; {valid} valid',
                    flush=True,
                )
    current = {url: cached[url] for url in evidence if url in cached}
    excluded = {url: row for url, row in cached.items() if url not in evidence}
    compact_path = cache_path.with_suffix('.jsonl.tmp')
    with compact_path.open('w', encoding='utf-8') as handle:
        for url in sorted(current):
            handle.write(json.dumps(current[url], ensure_ascii=False) + '\n')
    compact_path.replace(cache_path)
    excluded_tmp = excluded_cache_path.with_suffix('.jsonl.tmp')
    with excluded_tmp.open('w', encoding='utf-8') as handle:
        for url in sorted(excluded):
            handle.write(json.dumps(excluded[url], ensure_ascii=False) + '\n')
    excluded_tmp.replace(excluded_cache_path)
    return current


def write_csv(path, rows, fields):
    with path.open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, '') for field in fields})


def build_company_overlap_report(evidence, fetches, output_dir):
    """Compare seller rows across companies without inferring ownership."""
    company_rows = defaultdict(set)
    company_urls = defaultdict(set)
    for url, refs in evidence.items():
        referenced_companies = {
            ref.get('company', '') for ref in refs if ref.get('company')
        }
        # A file already mapped to multiple companies makes every row a
        # tautological match. Keep it in the cross-company URL audit instead.
        if len(referenced_companies) != 1:
            continue
        fetch = fetches.get(url, {})
        raw_path = fetch.get('raw_path')
        if not fetch.get('valid_app_ads') or not raw_path or not Path(raw_path).is_file():
            continue
        try:
            rows, _variables = parse_app_ads(Path(raw_path).read_text(encoding='utf-8'))
        except (OSError, UnicodeError):
            continue
        for ref in refs:
            company = ref.get('company', '')
            if company:
                company_rows[company].update(rows)
                company_urls[company].add(url)

    overlap_rows = []
    row_company_frequency = Counter(
        row for rows in company_rows.values() for row in rows
    )
    for company_a, company_b in combinations(sorted(company_rows), 2):
        shared = company_rows[company_a] & company_rows[company_b]
        if not shared:
            continue
        shared_direct = {row for row in shared if row[2] == 'DIRECT'}
        specific_direct = {
            row for row in shared_direct if row[0] not in COMMON_AD_SYSTEMS
        }
        rare_specific_direct = {
            row for row in specific_direct if row_company_frequency[row] <= 2
        }
        overlap_rows.append({
            'company_a': company_a,
            'company_b': company_b,
            'shared_rows': len(shared),
            'shared_direct_rows': len(shared_direct),
            'shared_specific_direct_rows': len(specific_direct),
            'shared_rare_specific_direct_rows': len(rare_specific_direct),
            'shared_common_network_direct_rows': len(shared_direct - specific_direct),
            'company_a_app_ads_urls': len(company_urls[company_a]),
            'company_b_app_ads_urls': len(company_urls[company_b]),
            'rare_specific_direct_samples': ';'.join(
                ','.join(row) for row in sorted(rare_specific_direct)[:10]
            ),
            'classification': (
                'review_auxiliary_rare_direct'
                if rare_specific_direct
                else 'common_monetization_overlap_only'
            ),
        })
    write_csv(output_dir / 'app_ads_company_overlap.csv', overlap_rows, [
        'company_a', 'company_b', 'shared_rows', 'shared_direct_rows',
        'shared_specific_direct_rows', 'shared_rare_specific_direct_rows',
        'shared_common_network_direct_rows', 'company_a_app_ads_urls',
        'company_b_app_ads_urls', 'rare_specific_direct_samples', 'classification',
    ])
    return overlap_rows


def build_reports(
    apps, ios_rows, ios_store_rows, gp_rows, evidence, fetches, output_dir,
    candidate_guard,
):
    accounts = defaultdict(list)
    for app in apps:
        accounts[developer_key(app)].append(app)

    candidates_by_account = defaultdict(set)
    apps_by_candidate = defaultdict(set)
    companies_by_candidate = defaultdict(set)
    developers_by_candidate = defaultdict(set)
    for url, refs in evidence.items():
        for ref in refs:
            candidates_by_account[ref['developer_key']].add(url)
            apps_by_candidate[url].add(ref['app_key'])
            companies_by_candidate[url].add(ref['company'])
            developers_by_candidate[url].add(ref['developer_key'])

    coverage = []
    for account, account_apps in sorted(accounts.items()):
        urls = sorted(candidates_by_account.get(account, set()))
        valid_urls = [url for url in urls if fetches.get(url, {}).get('valid_app_ads')]
        statuses = Counter(str(fetches.get(url, {}).get('status', 0)) for url in urls)
        platform, developer, dev_link = account.split('|', 2)
        if valid_urls:
            coverage_status = 'saved'
        elif urls:
            coverage_status = 'candidates_without_valid_app_ads'
        else:
            coverage_status = 'no_candidate_url'
        if platform == 'iOS':
            scanned = sum(
                app_key(app) in ios_rows or app_key(app) in ios_store_rows
                for app in account_apps
            )
            successful_store = sum(
                bool(ios_rows.get(app_key(app), {}).get('lookup_found'))
                or ios_store_rows.get(app_key(app), {}).get('status') == 200
                for app in account_apps
            )
        else:
            scanned = sum(app_key(app) in gp_rows for app in account_apps)
            successful_store = sum(
                gp_rows.get(app_key(app), {}).get('status') == 200
                for app in account_apps
            )
        coverage.append({
            'coverage_status': coverage_status,
            'platform': platform,
            'company': ';'.join(sorted({app.get('company_cn', '') for app in account_apps})),
            'developer': developer,
            'developer_link': dev_link,
            'apps_in_library': len(account_apps),
            'store_records': scanned,
            'successful_store_records': successful_store,
            'candidate_urls': len(urls),
            'valid_saved_urls': len(valid_urls),
            'valid_urls': ';'.join(valid_urls),
            'http_statuses': json.dumps(dict(statuses), ensure_ascii=False, sort_keys=True),
        })

    url_rows = []
    for url in sorted(evidence):
        fetch = fetches.get(url, {})
        url_rows.append({
            **fetch,
            'companies': ';'.join(sorted(companies_by_candidate[url])),
            'developer_count': len(developers_by_candidate[url]),
            'developers': ';'.join(sorted(developers_by_candidate[url])),
            'app_count': len(apps_by_candidate[url]),
            'apps': ';'.join(sorted(apps_by_candidate[url])),
        })

    write_csv(output_dir / 'developer_coverage.csv', coverage, [
        'coverage_status', 'platform', 'company', 'developer', 'developer_link',
        'apps_in_library', 'store_records', 'successful_store_records',
        'candidate_urls', 'valid_saved_urls', 'valid_urls', 'http_statuses',
    ])
    write_csv(output_dir / 'app_ads_urls.csv', url_rows, [
        'url', 'final_url', 'status', 'valid_app_ads', 'content_type', 'bytes',
        'sha256', 'parsed_rows', 'direct_rows', 'owner_domain', 'manager_domain',
        'raw_path', 'error', 'elapsed', 'fetched_at', 'companies',
        'developer_count', 'developers', 'app_count', 'apps',
    ])

    cross_company_rows = []
    for row in url_rows:
        companies = [item for item in row['companies'].split(';') if item]
        if len(companies) <= 1:
            continue
        cross_company_rows.append({
            'url': row['url'],
            'valid_app_ads': row['valid_app_ads'],
            'owner_domain': row['owner_domain'],
            'manager_domain': row['manager_domain'],
            'companies': row['companies'],
            'developer_count': row['developer_count'],
            'developers': row['developers'],
            'app_count': row['app_count'],
            'apps': row['apps'],
            'source_kinds': ';'.join(sorted({
                ref.get('source_kind', '')
                for ref in evidence[row['url']]
                if ref.get('source_kind')
            })),
            'classification': 'requires_review_not_ownership_proof',
        })
    write_csv(output_dir / 'app_ads_cross_company_candidates.csv', cross_company_rows, [
        'url', 'valid_app_ads', 'owner_domain', 'manager_domain', 'companies',
        'developer_count', 'developers', 'app_count', 'apps', 'source_kinds',
        'classification',
    ])
    overlap_rows = build_company_overlap_report(evidence, fetches, output_dir)

    summary = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'apps_in_library': len(apps),
        'developer_identities': len(accounts),
        'platform_developers': dict(Counter(key.split('|', 1)[0] for key in accounts)),
        'ios_lookup_records': len(ios_rows),
        'ios_lookup_found': sum(row.get('lookup_found') for row in ios_rows.values()),
        'ios_store_fallback_records': len(ios_store_rows),
        'ios_store_fallback_http_200': sum(
            row.get('status') == 200 for row in ios_store_rows.values()
        ),
        'gp_store_records': len(gp_rows),
        'gp_store_http_200': sum(row.get('status') == 200 for row in gp_rows.values()),
        'candidate_urls': len(evidence),
        'fetched_candidate_urls': len(fetches),
        'valid_app_ads_urls': sum(row.get('valid_app_ads') for row in fetches.values()),
        'saved_raw_responses': sum(bool(row.get('raw_path')) for row in fetches.values()),
        'coverage_statuses': dict(Counter(row['coverage_status'] for row in coverage)),
        'companies_with_valid_app_ads': len({
            company
            for row in url_rows if row.get('valid_app_ads')
            for company in row['companies'].split(';') if company
        }),
        'candidate_source_guard': candidate_guard,
        'historical_contacts_excluded_from_candidates': True,
        'cross_company_candidate_urls': len(cross_company_rows),
        'company_overlap_pairs': len(overlap_rows),
        'company_overlap_review_pairs': sum(
            row['classification'] == 'review_auxiliary_rare_direct'
            for row in overlap_rows
        ),
    }
    (output_dir / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    lines = [
        '# app-ads.txt archive',
        '',
        f"- Generated: {summary['generated_at']}",
        f"- Library apps: {summary['apps_in_library']}",
        f"- Developer identities: {summary['developer_identities']}",
        f"- Candidate URLs fetched: {summary['fetched_candidate_urls']}",
        f"- Valid app-ads.txt files: {summary['valid_app_ads_urls']}",
        f"- Raw responses saved: {summary['saved_raw_responses']}",
        f"- Cross-company candidate URLs: {summary['cross_company_candidate_urls']}",
        f"- Company overlap pairs: {summary['company_overlap_pairs']}",
        f"- Auxiliary overlap pairs to review: {summary['company_overlap_review_pairs']}",
        f"- Developer coverage: {summary['coverage_statuses']}",
        '',
        'A valid file contains at least one structured app-ads row or an '
        '`OWNERDOMAIN`/`MANAGERDOMAIN` variable. HTTP 200 HTML fallbacks are '
        'saved for audit but are not counted as valid app-ads.txt files.',
        'Historical web-crawl contacts and email-domain expansion records are '
        'retained privately but excluded from candidate generation.',
        'Cross-company candidates and common-network seller overlaps are audit '
        'findings, not automatic ownership assignments.',
    ]
    (output_dir / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', default='')
    parser.add_argument('--workers-store', type=int, default=10)
    parser.add_argument('--workers-app-ads', type=int, default=10)
    parser.add_argument('--timeout', type=int, default=20)
    parser.add_argument('--include-removed', action='store_true')
    parser.add_argument('--limit-gp', type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()

    output_dir = Path(args.output_dir).expanduser() if args.output_dir else (
        PRIVATE_ROOT / ('app_ads_archive_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    apps = load_apps()
    ios_apps = [app for app in apps if app.get('platform') == 'iOS']
    gp_apps = [app for app in apps if app.get('platform') == 'GP']
    if args.limit_gp:
        gp_apps = gp_apps[:args.limit_gp]
        included = {app_key(app) for app in ios_apps + gp_apps}
        apps = [app for app in apps if app_key(app) in included]

    print(
        f'Archive target: {output_dir}\n'
        f'Apps: {len(apps)} ({len(ios_apps)} iOS, {len(gp_apps)} GP)',
        flush=True,
    )
    current_keys = {app_key(app) for app in apps}
    historical_count = len(historical_contacts(PRIVATE_ROOT, current_keys))
    print(
        f'Historical contact evidence retained for audit only: '
        f'{historical_count} apps',
        flush=True,
    )
    ios_rows = scan_ios(ios_apps, output_dir)
    gp_rows = scan_gp(
        gp_apps,
        output_dir,
        workers=max(1, args.workers_store),
        timeout=args.timeout,
        include_removed=args.include_removed,
    )
    evidence = candidate_evidence(apps, ios_rows, gp_rows)
    candidate_guard = validate_candidate_evidence(evidence)
    (output_dir / 'candidate_evidence.json').write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print(f'Unique app-ads.txt candidates: {len(evidence)}', flush=True)
    fetches = archive_candidates(
        evidence,
        output_dir,
        workers=max(1, args.workers_app_ads),
        timeout=args.timeout,
    )
    all_accounts = {developer_key(app) for app in apps}
    saved_accounts = {
        ref['developer_key']
        for url, refs in evidence.items()
        if fetches.get(url, {}).get('valid_app_ads')
        for ref in refs
    }
    unresolved_ios_accounts = {
        account for account in all_accounts - saved_accounts
        if account.startswith('iOS|')
    }
    print(
        f'iOS developer accounts needing store-page fallback: '
        f'{len(unresolved_ios_accounts)}',
        flush=True,
    )
    ios_store_rows = scan_ios_store_pages(
        ios_apps,
        ios_rows,
        unresolved_ios_accounts,
        output_dir,
        workers=max(1, args.workers_store),
        timeout=args.timeout,
    )
    evidence = candidate_evidence(apps, ios_rows, gp_rows, ios_store_rows)
    candidate_guard = validate_candidate_evidence(evidence)
    (output_dir / 'candidate_evidence.json').write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print(f'Unique candidates after iOS fallback: {len(evidence)}', flush=True)
    fetches = archive_candidates(
        evidence,
        output_dir,
        workers=max(1, args.workers_app_ads),
        timeout=args.timeout,
    )
    summary = build_reports(
        apps, ios_rows, ios_store_rows, gp_rows, evidence, fetches, output_dir,
        candidate_guard,
    )
    print(json.dumps({'output_dir': str(output_dir), **summary}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
