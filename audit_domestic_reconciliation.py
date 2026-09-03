#!/usr/bin/env python3
"""Reconcile domestic library apps against AppMagic and private web evidence.

The script is read-only with respect to data/*.js, index.html, and product_index.js.
It writes review artifacts to the private evidence directory and never imports apps.
"""
import argparse
import csv
import html
import json
import os
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from audit_web_detail_review import (
    appmagic_child_for_target,
    fetch_publisher_apps,
    search_appmagic_by_ids,
)
from audit_web_evidence import PRIVATE_BASE, load_apps
from audit_web_fetcher import itunes_lookup_batch
from import_confirmed_developers import fetch_gp_detail_html
import monitor


DEFAULT_EXCLUDED_COMPANIES = {'playvalve', '英国tripledot'}
DEFAULT_EXCLUDED_PREFIXES = ('海外',)


def is_excluded(company, excluded_companies, excluded_prefixes):
    return company in excluded_companies or company.startswith(tuple(excluded_prefixes))


def app_key(platform, app_id):
    return f'{platform}:{app_id}'


def normalize_name(value):
    return re.sub(r'[^a-z0-9]+', '', str(value or '').lower())


def normalize_date(value):
    value = str(value or '')
    return value[:10].replace('-', '/') if len(value) >= 10 else ''


def json_load(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def json_write(path, value):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=True)


def write_csv(path, rows, fields):
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, '') for field in fields})


def summarize_mapping(item, platform, app_id):
    if not item:
        return {'status': 'not_found'}
    if item.get('error'):
        return {'status': 'error', 'error': item.get('error', '')}
    child = appmagic_child_for_target(item, platform, app_id)
    publisher = item.get('unitedPublisher') or child.get('unitedPublisher') or {}
    return {
        'status': 'mapped',
        'publisher_id': str(publisher.get('id') or ''),
        'publisher_name': publisher.get('name') or '',
        'store_publisher_id': str(child.get('store_publisher_id') or ''),
        'store_publisher_name': child.get('publisher_name') or '',
        'appmagic_name': child.get('name') or item.get('name') or '',
        'release_date': normalize_date(child.get('releaseDate') or item.get('releaseDate')),
        'first_detected': normalize_date(child.get('first_detected') or item.get('first_detected')),
        'removed': bool(child.get('removed')),
        'website_url': child.get('website_url') or item.get('website_url') or '',
        'support_url': child.get('support_url') or item.get('support_url') or '',
    }


def fetch_known_mappings(apps, cache_path, batch_size=800):
    cached = json_load(cache_path, {})
    targets = []
    for app in apps:
        key = app_key(app['platform'], app['pkg_or_id'])
        if key not in cached:
            targets.append({'platform': app['platform'], 'id': app['pkg_or_id']})

    for offset in range(0, len(targets), batch_size):
        batch = targets[offset:offset + batch_size]
        result = search_appmagic_by_ids(batch)
        for target in batch:
            key = app_key(target['platform'], target['id'])
            cached[key] = summarize_mapping(result.get(key), target['platform'], target['id'])
        json_write(cache_path, cached)
        print(f'AppMagic known apps: {min(offset + batch_size, len(targets))}/{len(targets)}', flush=True)
    return cached


def simplify_publisher_apps(items):
    rows = []
    seen = set()
    for united_app in items:
        for child in united_app.get('applications') or []:
            stores = set(child.get('store') or [])
            platform = 'GP' if 1 in stores else 'iOS' if stores & {2, 3} else ''
            app_id = str(child.get('store_application_id') or '')
            if not platform or not app_id:
                continue
            key = app_key(platform, app_id)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                'platform': platform,
                'id': app_id,
                'name': child.get('name') or united_app.get('name') or '',
                'store_publisher_id': str(child.get('store_publisher_id') or ''),
                'store_publisher_name': child.get('publisher_name') or '',
                'store_link': child.get('url') or '',
                'release_date': normalize_date(child.get('releaseDate')),
                'first_detected': normalize_date(child.get('first_detected')),
                'removed': bool(child.get('removed')),
                'website_url': child.get('website_url') or '',
                'support_url': child.get('support_url') or '',
            })
    return rows


def fetch_all_publishers(publisher_ids, cache_path, max_rows=5000):
    cached = json_load(cache_path, {})
    missing = [
        pid for pid in sorted(publisher_ids)
        if pid and (pid not in cached or cached[pid].get('error'))
    ]
    for offset, publisher_id in enumerate(missing, 1):
        result = fetch_publisher_apps({publisher_id}, max_rows=max_rows)
        info = result.get(publisher_id, {})
        cached[publisher_id] = {
            'error': info.get('error', ''),
            'apps': simplify_publisher_apps(info.get('apps') or []),
            'possibly_truncated': len(info.get('apps') or []) >= max_rows,
        }
        json_write(cache_path, cached)
        state = 'ok' if not cached[publisher_id]['error'] else cached[publisher_id]['error'].split(':', 1)[0]
        print(
            f'AppMagic publishers: {offset}/{len(missing)} '
            f'id={publisher_id} apps={len(cached[publisher_id]["apps"])} status={state}',
            flush=True,
        )
    return cached


def load_web_refs(run_dir):
    refs = defaultdict(list)
    if not run_dir:
        return refs
    path = os.path.join(run_dir, 'page_evidence.jsonl')
    if not os.path.exists(path):
        return refs
    with open(path, encoding='utf-8') as f:
        for line in f:
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            company = row.get('company') or ''
            source = row.get('final_url') or row.get('source_url') or ''
            for package in row.get('gp_packages') or []:
                refs[(company, app_key('GP', str(package)))].append(source)
            for ios_id in row.get('ios_ids') or []:
                refs[(company, app_key('iOS', str(ios_id)))].append(source)
    return refs


def match_failed_apps(error, all_apps):
    company = error.get('company', '')
    if company == '北京kiwi科维智娱':
        return [
            app for app in all_apps
            if app.get('platform') == 'GP' and app.get('company_cn') == company
        ]
    developer_norm = normalize_name(error.get('developer'))
    url = error.get('url', '')
    return [
        app for app in all_apps
        if app.get('platform') == 'GP'
        and app.get('company_cn') == company
        and (
            normalize_name(app.get('developer')) == developer_norm
            or app.get('dev_link') == url
        )
    ]


def verify_failed_gp_store_pages(summary_path, all_apps, excluded_companies,
                                 excluded_prefixes, workers=4):
    payload = json_load(summary_path, {})
    errors = payload.get('gp_scan', {}).get('error_details', [])
    tasks = {}
    for error in errors:
        if is_excluded(error.get('company', ''), excluded_companies, excluded_prefixes):
            continue
        for app in match_failed_apps(error, all_apps):
            if app.get('removed'):
                continue
            tasks.setdefault(app['pkg_or_id'], app)

    def check(app):
        pkg = app['pkg_or_id']
        row = {
            'company': app.get('company_cn', ''),
            'id': pkg,
            'library_developer': app.get('developer', ''),
            'library_dev_link': app.get('dev_link', ''),
            'store_status': '',
            'current_developer': '',
            'current_dev_link': '',
            'error': '',
        }
        try:
            body = fetch_gp_detail_html(
                f'https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=us'
            )
            developer, dev_link = monitor.extract_gp_developer_identity(body)
            title_match = re.search(r'<meta property="og:title" content="([^"]+)', body, re.S)
            if not title_match and not developer:
                row.update({'store_status': 'soft_404', 'error': 'HTTP 200 without app metadata'})
            else:
                if not dev_link:
                    meta = re.search(r'<meta name="appstore:developer_url" content="([^"]+)', body, re.S)
                    if meta:
                        dev_link = html.unescape(meta.group(1)).strip()
                row.update({
                    'store_status': '200',
                    'current_developer': developer,
                    'current_dev_link': dev_link,
                })
        except Exception as exc:
            message = str(exc)
            status = '404' if '404' in message else 'error'
            row.update({'store_status': status, 'error': message})
        return row

    rows = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(check, app): pkg for pkg, app in tasks.items()}
        for index, future in enumerate(as_completed(futures), 1):
            rows.append(future.result())
            if index % 25 == 0:
                print(f'Failed GP seed verification: {index}/{len(tasks)}', flush=True)
    rows.sort(key=lambda row: (row['company'], row['id']))
    return rows


def verify_strong_candidates(candidates, workers=4):
    strong = [row for row in candidates if row.get('review_status') == 'strong_official_and_appmagic']
    gp_rows = [row for row in strong if row.get('platform') == 'GP']
    ios_rows = [row for row in strong if row.get('platform') == 'iOS']
    results = []

    def check_gp(candidate):
        row = dict(candidate)
        row.update({'store_status': '', 'current_developer': '', 'current_dev_link': '', 'identity_matches_appmagic': False, 'store_error': ''})
        try:
            body = fetch_gp_detail_html(
                f'https://play.google.com/store/apps/details?id={candidate["id"]}&hl=en&gl=us'
            )
            developer, dev_link = monitor.extract_gp_developer_identity(body)
            title_match = re.search(r'<meta property="og:title" content="([^"]+)', body, re.S)
            if not title_match and not developer:
                row.update({'store_status': 'soft_404', 'store_error': 'HTTP 200 without app metadata'})
            else:
                row.update({
                    'store_status': '200',
                    'current_developer': developer,
                    'current_dev_link': dev_link,
                    'identity_matches_appmagic': normalize_name(developer) == normalize_name(candidate.get('store_publisher_name')),
                })
        except Exception as exc:
            message = str(exc)
            row.update({'store_status': '404' if '404' in message else 'error', 'store_error': message})
        return row

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(check_gp, row) for row in gp_rows]
        for future in as_completed(futures):
            results.append(future.result())

    lookup = {}
    ios_ids = [row['id'] for row in ios_rows]
    for offset in range(0, len(ios_ids), 200):
        lookup.update(itunes_lookup_batch(ios_ids[offset:offset + 200]))
    for candidate in ios_rows:
        item = lookup.get(candidate['id']) or {}
        developer = item.get('artistName', '')
        row = dict(candidate)
        row.update({
            'store_status': '200' if item else 'not_found',
            'current_developer': developer,
            'current_dev_link': item.get('artistViewUrl', ''),
            'identity_matches_appmagic': normalize_name(developer) == normalize_name(candidate.get('store_publisher_name')),
            'store_error': '',
        })
        results.append(row)
    results.sort(key=lambda row: (row['company'], row['platform'], row['id']))
    return results


def company_and_publisher_rows(apps, mappings):
    rows = []
    publisher_companies = defaultdict(Counter)
    company_publishers = defaultdict(Counter)
    for app in apps:
        key = app_key(app['platform'], app['pkg_or_id'])
        mapping = mappings.get(key, {'status': 'not_found'})
        publisher_id = mapping.get('publisher_id', '')
        if publisher_id:
            publisher_companies[publisher_id][app['company_cn']] += 1
            company_publishers[app['company_cn']][publisher_id] += 1
        rows.append({
            'company': app['company_cn'],
            'platform': app['platform'],
            'id': app['pkg_or_id'],
            'library_name': app.get('name', ''),
            'library_developer': app.get('developer', ''),
            'library_removed': bool(app.get('removed')),
            **mapping,
        })
    return rows, publisher_companies, company_publishers


def build_candidates(apps, publisher_apps, publisher_companies, company_publishers, web_refs):
    global_owner = defaultdict(set)
    company_keys = defaultdict(set)
    for app in apps:
        key = app_key(app['platform'], app['pkg_or_id'])
        global_owner[key].add(app['company_cn'])
        company_keys[app['company_cn']].add(key)

    rows = []
    seen = set()
    for company, publishers in company_publishers.items():
        for publisher_id, overlap_count in publishers.items():
            pub_info = publisher_apps.get(publisher_id, {})
            linked_companies = sorted(publisher_companies[publisher_id])
            for candidate in pub_info.get('apps') or []:
                key = app_key(candidate['platform'], candidate['id'])
                if key in company_keys[company]:
                    continue
                dedupe = (company, publisher_id, key)
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                owners = sorted(global_owner.get(key, []))
                refs = sorted(set(web_refs.get((company, key), [])))
                if owners:
                    status = 'cross_company_library_collision'
                elif len(linked_companies) > 1:
                    status = 'appmagic_shared_publisher_manual'
                elif refs:
                    status = 'strong_official_and_appmagic'
                elif overlap_count >= 2:
                    status = 'appmagic_multi_overlap_manual'
                else:
                    status = 'appmagic_single_overlap_manual'
                rows.append({
                    'review_status': status,
                    'company': company,
                    'platform': candidate['platform'],
                    'id': candidate['id'],
                    'name': candidate.get('name', ''),
                    'store_publisher_name': candidate.get('store_publisher_name', ''),
                    'store_publisher_id': candidate.get('store_publisher_id', ''),
                    'store_link': candidate.get('store_link', ''),
                    'release_date': candidate.get('release_date', ''),
                    'first_detected': candidate.get('first_detected', ''),
                    'appmagic_removed': candidate.get('removed', False),
                    'publisher_id': publisher_id,
                    'known_company_overlap': overlap_count,
                    'publisher_linked_companies': ';'.join(linked_companies),
                    'existing_library_companies': ';'.join(owners),
                    'official_web_ref_count': len(refs),
                    'official_web_refs': ';'.join(refs[:5]),
                })
    return rows


def build_failed_gp_review(summary_path, all_apps, domestic_apps, mappings, publisher_apps, web_refs,
                           excluded_companies, excluded_prefixes, store_verification=None):
    payload = json_load(summary_path, {})
    errors = payload.get('gp_scan', {}).get('error_details', [])
    rows = []
    for error in errors:
        company = error.get('company', '')
        excluded = is_excluded(company, excluded_companies, excluded_prefixes)
        url = error.get('url', '')
        matches = match_failed_apps(error, all_apps)
        publisher_ids = sorted({
            mappings.get(app_key(app['platform'], app['pkg_or_id']), {}).get('publisher_id', '')
            for app in matches
            if mappings.get(app_key(app['platform'], app['pkg_or_id']), {}).get('publisher_id')
        })
        current_publishers = set()
        expanded_keys = set()
        for publisher_id in publisher_ids:
            for item in publisher_apps.get(publisher_id, {}).get('apps') or []:
                if item.get('platform') == 'GP':
                    current_publishers.add(item.get('store_publisher_name', ''))
                    expanded_keys.add(app_key('GP', item.get('id', '')))
        refs = set()
        for app in matches:
            refs.update(web_refs.get((company, app_key('GP', app['pkg_or_id'])), []))
        verification = [
            row for row in (store_verification or [])
            if row.get('company') == company
            and row.get('id') in {app.get('pkg_or_id') for app in matches}
        ]
        live_rows = [row for row in verification if row.get('store_status') == '200']
        current_store_developers = sorted({
            row.get('current_developer', '') for row in live_rows if row.get('current_developer')
        })

        if excluded:
            status = 'deferred_overseas'
        elif company == '北京kiwi科维智娱':
            status = 'resolved_name_split_same_company'
        elif not matches:
            status = 'unresolved_no_matching_library_apps'
        elif not publisher_ids:
            status = 'unresolved_no_appmagic_mapping'
        elif not any(not app.get('removed') for app in matches):
            status = 'retired_or_removed_apps_appmagic_checked'
        elif verification and not live_rows:
            status = 'resolved_all_active_flags_now_store_inaccessible'
        elif current_store_developers:
            status = 'resolved_old_developer_page_apps_migrated_or_split'
        else:
            status = 'appmagic_checked_store_identity_unresolved'

        library_keys = {app_key(app['platform'], app['pkg_or_id']) for app in domestic_apps}
        rows.append({
            'scope_status': status,
            'company': company,
            'developer': error.get('developer', ''),
            'developer_url': url,
            'original_error': error.get('error', ''),
            'matched_library_apps': len(matches),
            'active_matched_apps': sum(not app.get('removed') for app in matches),
            'matched_app_ids': ';'.join(app.get('pkg_or_id', '') for app in matches),
            'appmagic_publisher_ids': ';'.join(publisher_ids),
            'current_appmagic_gp_publishers': ';'.join(sorted(x for x in current_publishers if x)),
            'appmagic_expanded_gp_apps': len(expanded_keys),
            'appmagic_missing_gp_candidates': len(expanded_keys - library_keys),
            'official_web_ref_count': len(refs),
            'store_seed_pages_checked': len(verification),
            'store_seed_pages_live': len(live_rows),
            'current_store_developers': ';'.join(current_store_developers),
        })
    return rows


def build_report(summary, company_rows, candidates, failures):
    lines = ['# 国内厂商 AppMagic 与官网深度对账', '']
    lines.extend([
        f'- 审计公司：{summary["companies"]}',
        f'- 审计应用：{summary["apps"]}（GP {summary["gp_apps"]} / iOS {summary["ios_apps"]}）',
        f'- AppMagic 已映射：{summary["mapped_apps"]}，未映射/错误：{summary["unmapped_apps"]}',
        f'- AppMagic publisher：{summary["publishers"]}',
        f'- 缺失候选：{summary["candidates"]}（只供审查，未入库）',
        f'- 官网/支持/隐私页：抓取 {summary.get("web_pages", 0)}，失败 {summary.get("web_page_errors", 0)}，原始新线索 {summary.get("web_leads", 0)}',
        f'- 当前商店可访问审查项：证据完整 {summary.get("review_candidate_import", 0)}，仍需人工确认 {summary.get("review_manual", 0)}',
        f'- AppMagic+官网历史命中但当前已下架：{summary.get("historical_store_unavailable", 0)}',
        f'- 官网开发者账号：核查 {summary.get("official_developer_accounts", 0)}，可展开 {summary.get("official_developer_accounts_live", 0)}，展开应用 {summary.get("official_developer_account_apps", 0)}，库外 {summary.get("official_developer_account_missing_apps", 0)}',
        f'- 17 个 GP 失败项：国内 {summary["domestic_gp_failures"]}，海外暂缓 {summary["overseas_gp_failures"]}',
        '',
        '## 逐公司概览',
    ])
    by_company_mapping = defaultdict(list)
    by_company_candidates = defaultdict(list)
    for row in company_rows:
        by_company_mapping[row['company']].append(row)
    for row in candidates:
        by_company_candidates[row['company']].append(row)
    for company in sorted(by_company_mapping):
        mapped = by_company_mapping[company]
        publisher_names = sorted({row.get('publisher_name', '') for row in mapped if row.get('publisher_name')})
        candidate_counts = Counter(row['review_status'] for row in by_company_candidates[company])
        lines.append(
            f'- {company}: 库内 {len(mapped)}，AppMagic 映射 '
            f'{sum(row.get("status") == "mapped" for row in mapped)}，publisher {len(publisher_names)}，'
            f'候选 {len(by_company_candidates[company])} {dict(candidate_counts)}'
        )
    lines.extend(['', '## GP 失败开发者处理结果'])
    for row in failures:
        lines.append(
            f'- {row["company"]} / {row["developer"]}: {row["scope_status"]}；'
            f'库内匹配 {row["matched_library_apps"]}，AppMagic publisher '
            f'{row["appmagic_publisher_ids"] or "无"}，扩展 GP {row["appmagic_expanded_gp_apps"]}，'
            f'种子复核 {row.get("store_seed_pages_live", 0)}/{row.get("store_seed_pages_checked", 0)} 在线，'
            f'当前开发者：{row.get("current_store_developers") or "无可用页面"}'
        )
    lines.extend([
        '',
        '## 口径',
        '- AppMagic 同一 united publisher 可能跨主体聚合，因此 AppMagic 单独命中不自动入库。',
        '- 只有 AppMagic 与官网/支持页/隐私页同时反向命中的候选才标为 strong_official_and_appmagic。',
        '- 海外厂商本轮不做 publisher 深度追溯；17 个失败项中的海外 3 项仅保留为 deferred_overseas。',
        '- 本轮不会修改产品库、提交或推送。',
    ])
    return '\n'.join(lines) + '\n'


def write_current_candidate_report(run_dir, detail_rows, current_developer_extras):
    rows = []
    for row in detail_rows:
        rows.append({
            'review_status': row.get('review_status', ''),
            'company': row.get('source_company', ''),
            'platform': row.get('platform', ''),
            'id': row.get('id', ''),
            'name': row.get('name', ''),
            'developer': row.get('developer', ''),
            'release_date': row.get('release_date', ''),
            'store_link': row.get('store_link', ''),
            'evidence_source': 'official_app_link+store+AppMagic',
            'reason': row.get('status_reason', ''),
        })
    for row in current_developer_extras:
        rows.append({
            'review_status': 'candidate_import',
            'company': row.get('source_company', ''),
            'platform': row.get('platform', ''),
            'id': row.get('id', ''),
            'name': row.get('name') or row.get('appmagic_name', ''),
            'developer': row.get('developer') or row.get('store_publisher_name', ''),
            'release_date': row.get('release_date', ''),
            'store_link': (
                f'https://apps.apple.com/app/id{row.get("id")}'
                if row.get('platform') == 'iOS'
                else f'https://play.google.com/store/apps/details?id={row.get("id")}&hl=en&gl=us'
            ),
            'evidence_source': 'official_developer_page+store+AppMagic',
            'reason': '官网开发者页展开，当前商店可访问，AppMagic publisher 命中',
        })
    rows.sort(key=lambda row: (row['review_status'], row['company'], row['platform'], row['id']))
    write_csv(os.path.join(run_dir, 'current_candidate_review.csv'), rows, [
        'review_status', 'company', 'platform', 'id', 'name', 'developer',
        'release_date', 'store_link', 'evidence_source', 'reason',
    ])
    by_company = defaultdict(list)
    for row in rows:
        by_company[row['company']].append(row)
    lines = ['# 当前可访问应用审查清单', '']
    lines.append(f'- 总计：{len(rows)}')
    lines.append(f'- 证据完整候选：{sum(row["review_status"] == "candidate_import" for row in rows)}')
    lines.append(f'- 需要人工确认：{sum(row["review_status"] == "manual_review" for row in rows)}')
    lines.append('- 日期口径：本次均为历史遗漏；无当天或近 7 天新上架。')
    lines.append('')
    for company in sorted(by_company):
        lines.append(f'## {company}')
        for row in by_company[company]:
            lines.append(
                f'- [{row["review_status"]}] {row["platform"]} `{row["id"]}` | '
                f'{row["name"]} | {row["developer"]} | {row["release_date"]}'
            )
        lines.append('')
    lines.append('未确认前不写入产品库。')
    with open(os.path.join(run_dir, 'current_candidate_review.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return rows


def main():
    parser = argparse.ArgumentParser(description='Full domestic AppMagic and web reconciliation.')
    parser.add_argument('--run-dir', help='Private output/cache directory. Created when omitted.')
    parser.add_argument('--web-run-dir', help='Evidence run from audit_web_evidence.py.')
    parser.add_argument('--gp-error-summary', default='/tmp/monitor_20260901_summary.json')
    parser.add_argument('--exclude-company', action='append', default=[])
    parser.add_argument('--exclude-company-prefix', action='append', default=[])
    parser.add_argument('--max-publisher-rows', type=int, default=5000)
    parser.add_argument('--verify-failed-gp-store', action='store_true', help='Fetch active seed app pages for inaccessible GP developers.')
    parser.add_argument('--workers-gp-verify', type=int, default=4)
    parser.add_argument('--verify-strong-candidates', action='store_true', help='Verify store availability and current identity for strong web/AppMagic candidates.')
    args = parser.parse_args()

    excluded_companies = DEFAULT_EXCLUDED_COMPANIES | set(args.exclude_company)
    excluded_prefixes = DEFAULT_EXCLUDED_PREFIXES + tuple(args.exclude_company_prefix)
    all_apps = load_apps()
    apps = [
        app for app in all_apps
        if not is_excluded(app.get('company_cn', ''), excluded_companies, excluded_prefixes)
    ]
    run_dir = args.run_dir or os.path.join(
        PRIVATE_BASE, datetime.now().strftime('domestic_reconciliation_%Y%m%d_%H%M%S')
    )
    os.makedirs(run_dir, exist_ok=True)
    print(f'Run dir: {run_dir}', flush=True)
    print(f'Scope: {len(set(a["company_cn"] for a in apps))} companies, {len(apps)} apps', flush=True)

    mappings = fetch_known_mappings(apps, os.path.join(run_dir, 'known_mapping_cache.json'))
    company_rows, publisher_companies, company_publishers = company_and_publisher_rows(apps, mappings)
    publisher_apps = fetch_all_publishers(
        set(publisher_companies),
        os.path.join(run_dir, 'publisher_apps_cache.json'),
        max_rows=args.max_publisher_rows,
    )
    web_refs = load_web_refs(args.web_run_dir)
    candidates = build_candidates(apps, publisher_apps, publisher_companies, company_publishers, web_refs)
    if args.verify_strong_candidates:
        strong_verification = verify_strong_candidates(candidates, workers=args.workers_gp_verify)
        write_csv(os.path.join(run_dir, 'strong_candidate_store_verification.csv'), strong_verification, [
            'review_status', 'company', 'platform', 'id', 'name', 'store_publisher_name',
            'store_publisher_id', 'store_link', 'release_date', 'first_detected',
            'appmagic_removed', 'publisher_id', 'known_company_overlap',
            'publisher_linked_companies', 'existing_library_companies',
            'official_web_ref_count', 'official_web_refs', 'store_status',
            'current_developer', 'current_dev_link', 'identity_matches_appmagic', 'store_error',
        ])
    store_verification = []
    store_verification_path = os.path.join(run_dir, 'gp_inaccessible_seed_store_verification.csv')
    if args.verify_failed_gp_store:
        store_verification = verify_failed_gp_store_pages(
            args.gp_error_summary, all_apps, excluded_companies, excluded_prefixes,
            workers=args.workers_gp_verify,
        )
        write_csv(store_verification_path, store_verification, [
            'company', 'id', 'library_developer', 'library_dev_link', 'store_status',
            'current_developer', 'current_dev_link', 'error',
        ])
    elif os.path.exists(store_verification_path):
        with open(store_verification_path, newline='', encoding='utf-8-sig') as f:
            store_verification = list(csv.DictReader(f))
    failures = build_failed_gp_review(
        args.gp_error_summary, all_apps, apps, mappings, publisher_apps, web_refs,
        excluded_companies, excluded_prefixes, store_verification=store_verification,
    )

    company_rows.sort(key=lambda row: (row['company'], row['platform'], row['id']))
    candidates.sort(key=lambda row: (row['review_status'], row['company'], row['platform'], row['id']))
    failures.sort(key=lambda row: (row['scope_status'], row['company'], row['developer']))
    write_csv(os.path.join(run_dir, 'known_appmagic_mapping.csv'), company_rows, [
        'company', 'platform', 'id', 'library_name', 'library_developer', 'library_removed',
        'status', 'error', 'publisher_id', 'publisher_name', 'store_publisher_id',
        'store_publisher_name', 'appmagic_name', 'release_date', 'first_detected',
        'removed', 'website_url', 'support_url',
    ])
    write_csv(os.path.join(run_dir, 'appmagic_missing_candidates.csv'), candidates, [
        'review_status', 'company', 'platform', 'id', 'name', 'store_publisher_name',
        'store_publisher_id', 'store_link', 'release_date', 'first_detected',
        'appmagic_removed', 'publisher_id', 'known_company_overlap',
        'publisher_linked_companies', 'existing_library_companies',
        'official_web_ref_count', 'official_web_refs',
    ])
    write_csv(os.path.join(run_dir, 'gp_inaccessible_developer_review.csv'), failures, [
        'scope_status', 'company', 'developer', 'developer_url', 'original_error',
        'matched_library_apps', 'active_matched_apps', 'matched_app_ids',
        'appmagic_publisher_ids', 'current_appmagic_gp_publishers',
        'appmagic_expanded_gp_apps', 'appmagic_missing_gp_candidates',
        'official_web_ref_count',
        'store_seed_pages_checked', 'store_seed_pages_live', 'current_store_developers',
    ])

    mapped = sum(row.get('status') == 'mapped' for row in company_rows)
    web_summary = json_load(os.path.join(args.web_run_dir or '', 'summary.json'), {})
    detail_rows = []
    detail_path = os.path.join(args.web_run_dir or '', 'detail_review_candidates.csv')
    if os.path.exists(detail_path):
        with open(detail_path, newline='', encoding='utf-8-sig') as f:
            detail_rows = list(csv.DictReader(f))
    strong_rows = []
    strong_path = os.path.join(run_dir, 'strong_candidate_store_verification.csv')
    if os.path.exists(strong_path):
        with open(strong_path, newline='', encoding='utf-8-sig') as f:
            strong_rows = list(csv.DictReader(f))
    detail_keys = {app_key(row.get('platform'), row.get('id')) for row in detail_rows}
    developer_account_rows = []
    developer_account_path = os.path.join(run_dir, 'official_developer_account_review.csv')
    if os.path.exists(developer_account_path):
        with open(developer_account_path, newline='', encoding='utf-8-sig') as f:
            developer_account_rows = list(csv.DictReader(f))
    developer_summary = json_load(
        os.path.join(run_dir, 'official_developer_account_summary.json'), {}
    )
    strong_by_key = {
        app_key(row.get('platform'), row.get('id')): row for row in strong_rows
    }
    developer_extras = [
        row for row in developer_account_rows
        if row.get('review_status', '').startswith('candidate_')
        and app_key(row.get('platform'), row.get('id')) not in detail_keys
    ]
    current_developer_extras = [
        row for row in developer_extras
        if row.get('platform') == 'iOS'
        or strong_by_key.get(app_key(row.get('platform'), row.get('id')), {}).get('store_status') == '200'
    ]
    current_review_rows = write_current_candidate_report(
        run_dir, detail_rows, current_developer_extras
    )
    summary = {
        'run_dir': run_dir,
        'web_run_dir': args.web_run_dir or '',
        'companies': len({app['company_cn'] for app in apps}),
        'apps': len(apps),
        'gp_apps': sum(app['platform'] == 'GP' for app in apps),
        'ios_apps': sum(app['platform'] == 'iOS' for app in apps),
        'mapped_apps': mapped,
        'unmapped_apps': len(company_rows) - mapped,
        'publishers': len(publisher_companies),
        'publisher_fetch_errors': sum(bool(row.get('error')) for row in publisher_apps.values()),
        'publisher_fetch_truncated': sum(bool(row.get('possibly_truncated')) for row in publisher_apps.values()),
        'candidates': len(candidates),
        'candidate_statuses': dict(Counter(row['review_status'] for row in candidates)),
        'web_pages': web_summary.get('site_pages_fetched', 0),
        'web_page_errors': web_summary.get('stats', {}).get('site_page_errors', 0),
        'web_leads': web_summary.get('new_leads_total', 0),
        'private_email_evidence_rows': web_summary.get('email_evidence_rows', 0),
        'companies_with_web_evidence': len(web_summary.get('companies_with_evidence', [])),
        'review_candidate_import': sum(row.get('review_status') == 'candidate_import' for row in current_review_rows),
        'review_manual': sum(row.get('review_status') == 'manual_review' for row in current_review_rows),
        'historical_store_unavailable': sum(row.get('store_status') != '200' for row in strong_rows) + len(developer_extras) - len(current_developer_extras),
        'official_developer_accounts': developer_summary.get('developer_accounts', 0),
        'official_developer_accounts_live': developer_summary.get('account_statuses', {}).get('ok', 0),
        'official_developer_account_apps': developer_summary.get('apps_from_accounts', 0),
        'official_developer_account_missing_apps': developer_summary.get('unique_missing_apps', 0),
        'developer_account_extra_current_candidates': len(current_developer_extras),
        'domestic_gp_failures': sum(row['scope_status'] != 'deferred_overseas' for row in failures),
        'overseas_gp_failures': sum(row['scope_status'] == 'deferred_overseas' for row in failures),
    }
    json_write(os.path.join(run_dir, 'reconciliation_summary.json'), summary)
    with open(os.path.join(run_dir, 'domestic_reconciliation.md'), 'w', encoding='utf-8') as f:
        f.write(build_report(summary, company_rows, candidates, failures))
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
