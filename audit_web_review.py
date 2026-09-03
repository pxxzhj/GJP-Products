#!/usr/bin/env python3
"""Triage web-evidence leads without fetching the network.

Input is a run directory created by audit_web_evidence.py. The script reads
new_leads.csv plus the local evidence backups, then writes review-only outputs
next to them. It intentionally does not import apps into data/*.js.
"""
import argparse
import csv
import json
import os
import re
import urllib.parse
from collections import Counter, defaultdict

from audit_web_fetcher import itunes_lookup_batch
from audit_web_evidence import (
    hostname,
    is_noise_url,
    is_public_hosting_domain,
    load_apps,
    registered_domain,
)


SERVICE_OR_WEAK_DOMAINS = {
    'appsflyer.com',
    'applovin.com',
    'appointlet.com',
    'auth0.com',
    'cloudflare.com',
    'feishu.cn',
    'freshdesk.com',
    'getui.com',
    'github.com',
    'github.io',
    'glueup.com',
    'helpshift.com',
    'latisglobal.com',
    'lightspeedhq.com',
    'mailchimp.com',
    'myshopline.com',
    'notion.site',
    'notion.so',
    'printify.com',
    'readymag.com',
    'samsung.com',
    'samsung.com.cn',
    'sentry.io',
    'shopline.com',
    'tumblr.com',
    'un.org',
    'unity3d.com',
    'vk.com',
    'vk-portal.net',
    'wix.com',
    'wixpress.com',
    'wixsite.com',
    'zapier.com',
    'zendesk.com',
    'kuaishou.com',
}

PLATFORM_OFFICIAL_APP_IDS = {
    ('iOS', '1450874784'): 'Apple Transporter tutorial/tooling link, not a competitor product',
}


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline='', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def read_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def source_domain(url):
    return registered_domain(hostname(url))


def canonical_gp_developer_id(url_or_id):
    if not url_or_id:
        return ''
    if not url_or_id.startswith(('http://', 'https://')):
        return urllib.parse.unquote_plus(url_or_id).strip()
    parsed = urllib.parse.urlparse(url_or_id)
    qs = urllib.parse.parse_qs(parsed.query)
    values = qs.get('id') or []
    return urllib.parse.unquote_plus(values[0]).strip() if values else ''


def is_malformed_gp_developer(row):
    if row.get('type') != 'gp_developer':
        return False
    dev_id = canonical_gp_developer_id(row.get('store_link') or row.get('id'))
    if not dev_id:
        return True
    if dev_id in {'id', 'dev?id'}:
        return True
    if dev_id.startswith(('http://', 'https://')):
        return True
    return False


def clean_store_title(row):
    if row.get('lookup_track_name'):
        return row.get('lookup_track_name', '').strip()
    title = row.get('title') or ''
    title = title.replace('â\x80\x8e', '').replace('Â·', '·').replace('ï¼\x8c', ',')
    title = re.sub(r'\s+-\s+Apps on Google Play$', '', title)
    title = re.sub(r'\s+on the App Store$', '', title)
    if 'App Store' in title and 'Today' in title:
        return ''
    return title.strip()


def is_service_or_weak_domain(domain):
    if not domain:
        return True
    return domain in SERVICE_OR_WEAK_DOMAINS or is_public_hosting_domain(domain)


def classify(row, store_row, existing_keys):
    lead_type = row.get('type', '')
    platform = row.get('platform', '')
    app_id = row.get('id', '')
    s_kind = row.get('source_kind', '')
    s_domain = source_domain(row.get('source_url', ''))

    if (platform, app_id) in existing_keys:
        return 'defer_noise', 'already exists in library'
    if (platform, app_id) in PLATFORM_OFFICIAL_APP_IDS:
        return 'defer_noise', PLATFORM_OFFICIAL_APP_IDS[(platform, app_id)]
    if is_malformed_gp_developer(row):
        return 'defer_noise', 'malformed Google Play developer link'
    if is_noise_url(row.get('source_url', '')) or is_service_or_weak_domain(s_domain):
        return 'defer_noise', f'weak/service source domain: {s_domain}'
    if s_kind == 'email_domain':
        return 'manual_review', 'found by expanding an email domain only'
    if s_kind == 'same_domain_link':
        root_pages = row.get('_source_domain_pages') or []
        if root_pages and all(page.get('source_kind') == 'email_domain' for page in root_pages):
            return 'manual_review', 'same-domain link reached only through email-domain expansion'
    if row.get('source_company') == '__DISCOVERED__':
        return 'manual_review', 'found recursively from a discovered app'
    if lead_type.endswith('_developer'):
        return 'manual_review', 'developer account needs publisher-level review'
    if lead_type.endswith('_app'):
        status = str(store_row.get('status', '')) if store_row else ''
        if status == '200' and s_kind in {'website', 'privacy', 'support', 'same_domain_link'}:
            return 'priority_verify', 'store app link found on official/contact page and store page fetched'
        if status == '200':
            return 'manual_review', 'store page fetched but source needs review'
        return 'manual_review', 'store page was not fetched successfully in this run'
    return 'manual_review', 'unrecognized lead type'


def store_contact_domains(store_row):
    domains = []
    for url in store_row.get('contact_urls') or []:
        if url.startswith('mailto:') or not url:
            continue
        domain = source_domain(url)
        if domain:
            domains.append(domain)
    return ';'.join(sorted(set(domains)))


def write_csv(path, rows):
    fields = [
        'bucket',
        'reason',
        'type',
        'platform',
        'id',
        'store_status',
        'store_title',
        'store_developer',
        'source_company',
        'source_app',
        'source_kind',
        'source_domain',
        'store_contact_domains',
        'store_link',
        'source_url',
    ]
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, '') for field in fields})


def build_markdown(rows, summary):
    bucket_labels = {
        'priority_verify': '优先核验',
        'manual_review': '需人工确认',
        'defer_noise': '暂缓/噪声',
    }
    lines = ['# Web Evidence Triage', '']
    lines.append(f'- Raw leads: {len(rows)}')
    for bucket in ('priority_verify', 'manual_review', 'defer_noise'):
        subset = [row for row in rows if row['bucket'] == bucket]
        type_counts = Counter(row['type'] for row in subset)
        lines.append(f'- {bucket_labels[bucket]}: {len(subset)} {dict(type_counts)}')
    if summary:
        lines.append(f'- Evidence pages fetched: {summary.get("site_pages_fetched", 0)}')
        lines.append(f'- Private email evidence rows: {summary.get("email_evidence_rows", 0)}')
    lines.append('')

    lines.append('## 按公司分布')
    company_counts = Counter(
        (row['bucket'], row.get('source_company') or '(unknown)')
        for row in rows
    )
    for bucket in ('priority_verify', 'manual_review', 'defer_noise'):
        lines.append(f'### {bucket_labels[bucket]}')
        for (_, company), count in company_counts.most_common():
            if _ == bucket:
                lines.append(f'- {company}: {count}')
        lines.append('')

    lines.append('## 优先核验样例')
    grouped = defaultdict(list)
    for row in rows:
        if row['bucket'] == 'priority_verify':
            grouped[row.get('source_company') or '(unknown)'].append(row)
    for company in sorted(grouped):
        lines.append(f'### {company}')
        for row in grouped[company][:12]:
            title = row.get('store_title') or row.get('id')
            lines.append(
                f'- {row["platform"]} `{row["id"]}` | {title} | '
                f'source: {row.get("source_domain", "")} / {row.get("source_kind", "")}'
            )
        if len(grouped[company]) > 12:
            lines.append(f'- ... and {len(grouped[company]) - 12} more')
        lines.append('')

    lines.append('## 处理建议')
    lines.append('- 优先核验：下一步可逐条走 AppMagic + 商店详情 + 官网/支持/隐私页复核，确认归属后再入库。')
    lines.append('- 需人工确认：主要是开发者账号、递归发现线索、仅邮箱域名扩展出来的线索，不能直接入库。')
    lines.append('- 暂缓/噪声：主要是 malformed 链接、公共托管/SaaS/第三方服务域名带出的弱关联，默认不入库。')
    lines.append('- 邮箱明文仍只保留在私有备份，不写入本报告。')
    return '\n'.join(lines) + '\n'


def fetch_ios_names(rows):
    ids = sorted({
        row.get('id', '')
        for row in rows
        if row.get('platform') == 'iOS' and row.get('id', '').isdigit()
    })
    lookup = {}
    for country in ('us', 'cn', 'hk', 'tw', 'jp', 'sg', 'gb', 'au'):
        missing = [app_id for app_id in ids if app_id not in lookup]
        if not missing:
            break
        for start in range(0, len(missing), 200):
            lookup.update(itunes_lookup_batch(missing[start:start + 200], country=country))
    return lookup


def triage(run_dir, fetch_ios_names_enabled=False):
    leads = read_csv(os.path.join(run_dir, 'new_leads.csv'))
    store_rows = read_jsonl(os.path.join(run_dir, 'store_contact_pages.jsonl'))
    page_rows = read_jsonl(os.path.join(run_dir, 'page_evidence.jsonl'))
    summary_path = os.path.join(run_dir, 'summary.json')
    summary = json.load(open(summary_path, encoding='utf-8')) if os.path.exists(summary_path) else {}

    store_by_key = {}
    for row in store_rows:
        key = (row.get('platform', ''), row.get('pkg_or_id', ''))
        if key not in store_by_key or str(row.get('status')) == '200':
            store_by_key[key] = row

    pages_by_domain = defaultdict(list)
    for row in page_rows:
        domain = source_domain(row.get('final_url') or row.get('source_url') or '')
        if domain:
            pages_by_domain[domain].append(row)

    ios_lookup = fetch_ios_names(leads) if fetch_ios_names_enabled else {}

    apps = load_apps()
    existing_keys = {(app.get('platform', ''), app.get('pkg_or_id', '')) for app in apps}

    triaged = []
    for lead in leads:
        key = (lead.get('platform', ''), lead.get('id', ''))
        store_row = store_by_key.get(key, {})
        if lead.get('platform') == 'iOS' and lead.get('id') in ios_lookup:
            lookup_row = ios_lookup[lead.get('id')]
            store_row = {
                **store_row,
                'lookup_track_name': lookup_row.get('trackName', ''),
                'lookup_artist_name': lookup_row.get('artistName', ''),
                'lookup_seller_name': lookup_row.get('sellerName', ''),
                'lookup_release_date': lookup_row.get('releaseDate', ''),
                'lookup_current_version_release_date': lookup_row.get('currentVersionReleaseDate', ''),
            }
        lead['_source_domain_pages'] = pages_by_domain.get(source_domain(lead.get('source_url', '')), [])
        bucket, reason = classify(lead, store_row, existing_keys)
        triaged.append({
            'bucket': bucket,
            'reason': reason,
            'type': lead.get('type', ''),
            'platform': lead.get('platform', ''),
            'id': lead.get('id', ''),
            'store_status': store_row.get('status', ''),
            'store_title': clean_store_title(store_row),
            'store_developer': store_row.get('developer', ''),
            'source_company': lead.get('source_company', ''),
            'source_app': lead.get('source_app', ''),
            'source_kind': lead.get('source_kind', ''),
            'source_domain': source_domain(lead.get('source_url', '')),
            'store_contact_domains': store_contact_domains(store_row),
            'store_link': lead.get('store_link', ''),
            'source_url': lead.get('source_url', ''),
        })

    order = {'priority_verify': 0, 'manual_review': 1, 'defer_noise': 2}
    triaged.sort(key=lambda row: (
        order.get(row['bucket'], 9),
        row.get('source_company', ''),
        row.get('type', ''),
        row.get('id', ''),
    ))

    csv_path = os.path.join(run_dir, 'triaged_new_leads.csv')
    md_path = os.path.join(run_dir, 'triaged_new_leads.md')
    write_csv(csv_path, triaged)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(build_markdown(triaged, summary))
    return csv_path, md_path, triaged


def main():
    parser = argparse.ArgumentParser(description='Triage web evidence leads locally.')
    parser.add_argument('run_dir', help='Audit run directory containing new_leads.csv.')
    parser.add_argument('--fetch-ios-names', action='store_true', help='Use Apple Lookup to fill readable iOS app names in the report.')
    args = parser.parse_args()
    csv_path, md_path, rows = triage(args.run_dir, args.fetch_ios_names)
    counts = Counter(row['bucket'] for row in rows)
    print(json.dumps({
        'triaged_csv': csv_path,
        'triaged_report': md_path,
        'counts': dict(counts),
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
