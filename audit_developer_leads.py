#!/usr/bin/env python3
"""Expand official-site developer-account leads and reconcile them with AppMagic."""
import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict

import requests

from audit_web_detail_review import (
    appmagic_child_for_target,
    fetch_publisher_apps,
    search_appmagic_by_ids,
)
from audit_web_evidence import load_apps
from audit_web_fetcher import USER_AGENT
import monitor


def read_csv(path):
    with open(path, newline='', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, '') for field in fields})


def key(platform, app_id):
    return f'{platform}:{app_id}'


def fetch_gp_developer(url):
    response = requests.get(
        url,
        headers={'User-Agent': USER_AGENT, 'Accept-Language': 'en-US,en;q=0.9'},
        timeout=40,
    )
    response.raise_for_status()
    packages = []
    for package in re.findall(r'/store/apps/details\?id=([A-Za-z0-9_.]+)', response.text):
        if package not in packages:
            packages.append(package)
    return packages


def fetch_ios_developer(artist_id):
    endpoint = (
        f'https://itunes.apple.com/lookup?id={artist_id}'
        '&entity=software&country=us&limit=200'
    )
    data = monitor.itunes_lookup(endpoint) or {}
    return [row for row in data.get('results', []) if row.get('wrapperType') == 'software']


def appmagic_summary(item, platform, app_id):
    if not item or item.get('error'):
        return {}
    child = appmagic_child_for_target(item, platform, app_id)
    publisher = item.get('unitedPublisher') or child.get('unitedPublisher') or {}
    return {
        'appmagic_name': child.get('name') or item.get('name') or '',
        'store_publisher_name': child.get('publisher_name') or '',
        'store_publisher_id': str(child.get('store_publisher_id') or ''),
        'publisher_id': str(publisher.get('id') or ''),
        'publisher_name': publisher.get('name') or '',
        'release_date': str(child.get('releaseDate') or item.get('releaseDate') or '')[:10].replace('-', '/'),
        'appmagic_removed': bool(child.get('removed')),
    }


def main():
    parser = argparse.ArgumentParser(description='Expand developer links discovered on official sites.')
    parser.add_argument('web_run_dir')
    parser.add_argument('--reconciliation-dir', required=True)
    args = parser.parse_args()

    triaged = read_csv(os.path.join(args.web_run_dir, 'triaged_new_leads.csv'))
    leads = [
        row for row in triaged
        if row.get('bucket') == 'manual_review' and row.get('type', '').endswith('_developer')
        and row.get('source_kind') != 'email_domain'
        and 'email-domain expansion' not in row.get('reason', '')
    ]
    apps = load_apps()
    existing = defaultdict(list)
    for app in apps:
        existing[key(app['platform'], app['pkg_or_id'])].append(app['company_cn'])

    known_mapping_path = os.path.join(args.reconciliation_dir, 'known_mapping_cache.json')
    known_mapping = json.load(open(known_mapping_path, encoding='utf-8'))
    company_publishers = defaultdict(set)
    for app in apps:
        mapping = known_mapping.get(key(app['platform'], app['pkg_or_id']), {})
        if mapping.get('publisher_id'):
            company_publishers[app['company_cn']].add(mapping['publisher_id'])

    expanded = []
    account_rows = []
    for index, lead in enumerate(leads, 1):
        platform = lead['platform']
        company = lead['source_company']
        account = lead['id']
        store_link = lead['store_link']
        error = ''
        found = []
        try:
            if platform == 'GP':
                packages = fetch_gp_developer(store_link)
                found = [{'id': package, 'name': '', 'developer': account} for package in packages]
            else:
                items = fetch_ios_developer(account)
                found = [
                    {
                        'id': str(item.get('trackId', '')),
                        'name': item.get('trackName', ''),
                        'developer': item.get('artistName', ''),
                    }
                    for item in items if item.get('trackId')
                ]
        except Exception as exc:
            error = str(exc)
        account_rows.append({
            'source_company': company,
            'platform': platform,
            'developer_account': account,
            'developer_link': store_link,
            'source_url': lead.get('source_url', ''),
            'status': 'ok' if found else 'empty_or_error',
            'apps_found': len(found),
            'error': error,
        })
        for item in found:
            expanded.append({
                'source_company': company,
                'source_developer_account': account,
                'source_developer_link': store_link,
                'source_url': lead.get('source_url', ''),
                'platform': platform,
                **item,
            })
        print(f'Developer accounts: {index}/{len(leads)} apps={len(found)}', flush=True)

    deduped = {}
    for row in expanded:
        dedupe_key = (row['source_company'], row['platform'], row['id'])
        deduped.setdefault(dedupe_key, row)
    expanded = list(deduped.values())
    missing = [row for row in expanded if key(row['platform'], row['id']) not in existing]
    targets = [{'platform': row['platform'], 'id': row['id']} for row in missing]
    appmagic = search_appmagic_by_ids(targets)

    publisher_ids = set()
    rows = []
    for row in expanded:
        app_key = key(row['platform'], row['id'])
        mapping = appmagic_summary(appmagic.get(app_key), row['platform'], row['id'])
        if mapping.get('publisher_id'):
            publisher_ids.add(mapping['publisher_id'])
        owners = sorted(existing.get(app_key, []))
        if owners:
            status = 'already_in_library'
        elif row['source_company'] == '__DISCOVERED__':
            status = 'manual_recursive_context'
        elif not mapping:
            status = 'manual_no_appmagic_mapping'
        elif mapping.get('publisher_id') in company_publishers[row['source_company']]:
            status = 'candidate_official_developer_and_known_appmagic_publisher'
        else:
            status = 'candidate_official_developer_new_appmagic_publisher'
        rows.append({
            'review_status': status,
            **row,
            'existing_library_companies': ';'.join(owners),
            **mapping,
        })

    publisher_expansions = fetch_publisher_apps(publisher_ids, max_rows=5000)
    publisher_rows = []
    for publisher_id, info in publisher_expansions.items():
        linked_companies = sorted({
            row['source_company'] for row in rows if row.get('publisher_id') == publisher_id
        })
        publisher_rows.append({
            'publisher_id': publisher_id,
            'source_companies': ';'.join(linked_companies),
            'expanded_united_apps': len(info.get('apps') or []),
            'error': info.get('error', ''),
        })

    rows.sort(key=lambda row: (row['review_status'], row['source_company'], row['platform'], row['id']))
    write_csv(os.path.join(args.reconciliation_dir, 'official_developer_account_review.csv'), rows, [
        'review_status', 'source_company', 'source_developer_account',
        'source_developer_link', 'source_url', 'platform', 'id', 'name', 'developer',
        'existing_library_companies', 'appmagic_name', 'store_publisher_name',
        'store_publisher_id', 'publisher_id', 'publisher_name', 'release_date',
        'appmagic_removed',
    ])
    write_csv(os.path.join(args.reconciliation_dir, 'official_developer_accounts.csv'), account_rows, [
        'source_company', 'platform', 'developer_account', 'developer_link',
        'source_url', 'status', 'apps_found', 'error',
    ])
    write_csv(os.path.join(args.reconciliation_dir, 'official_developer_new_publishers.csv'), publisher_rows, [
        'publisher_id', 'source_companies', 'expanded_united_apps', 'error',
    ])
    summary = {
        'developer_accounts': len(leads),
        'account_statuses': dict(Counter(row['status'] for row in account_rows)),
        'apps_from_accounts': len(expanded),
        'unique_missing_apps': len(missing),
        'review_statuses': dict(Counter(row['review_status'] for row in rows)),
        'appmagic_publishers': len(publisher_ids),
        'publisher_errors': sum(bool(row.get('error')) for row in publisher_rows),
    }
    with open(os.path.join(args.reconciliation_dir, 'official_developer_account_summary.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
