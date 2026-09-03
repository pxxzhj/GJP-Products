#!/usr/bin/env python3
"""Second-pass review for priority web-evidence leads.

This is a local post-processor. It does not fetch network data and does not
modify product data. It narrows triaged priority leads before import review.
"""
import argparse
import csv
import json
import os
from collections import Counter, defaultdict

from audit_web_evidence import filter_email_domain_tainted_pages


REJECT_IDS = {
    ('GP', 'com.DAA.appchoices'): 'advertising choices compliance link, not a competitor product',
    ('iOS', '894822870'): 'advertising choices compliance link, not a competitor product',
    ('iOS', '1450874784'): 'Apple Transporter tutorial/tooling link, not a competitor product',
}

WEAK_SOURCE_DOMAINS = {
    'app-ads-txt.com',
    'aboutads.info',
    'youradchoices.com',
}

CROSS_COMPANY_SOURCE_DOMAINS = {
    ('广州河马游戏', 'eyewind.com'): (
        'EyeWind official site reached from an existing app contact URL; '
        'do not attribute to 广州河马游戏 without separate ownership proof'
    ),
}


def read_csv(path):
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


def page_refs_count(page):
    return len(page.get('gp_packages') or []) + len(page.get('ios_ids') or [])


def classify(row, page, store):
    key = (row.get('platform'), row.get('id'))
    if key in REJECT_IDS:
        return 'reject', REJECT_IDS[key]
    if row.get('source_domain') in WEAK_SOURCE_DOMAINS:
        return 'reject', 'weak compliance/ads source domain'
    cross_company_reason = CROSS_COMPANY_SOURCE_DOMAINS.get((
        row.get('source_company'),
        row.get('source_domain'),
    ))
    if cross_company_reason:
        return 'manual_appmagic', cross_company_reason
    if row.get('source_kind') == 'privacy' and page_refs_count(page) <= 2:
        return 'manual_appmagic', 'found from privacy page with few app links; verify publisher ownership'
    if row.get('source_kind') in {'website', 'same_domain_link', 'support'}:
        title = (page.get('title') or '').lower()
        if any(word in title for word in ('games', 'game', '产品', 'works', 'work show', 'studio', 'app')):
            return 'next_verify', 'official product/app listing page'
        if page_refs_count(page) >= 3:
            return 'next_verify', 'official page lists multiple store links'
        return 'manual_appmagic', 'official page but ownership needs AppMagic/store developer check'
    return 'manual_appmagic', 'needs manual source review'


def write_csv(path, rows):
    fields = [
        'second_bucket',
        'second_reason',
        'source_company',
        'source_app',
        'platform',
        'id',
        'store_title',
        'source_domain',
        'source_kind',
        'page_title',
        'page_ref_count',
        'store_status',
        'store_developer',
        'store_contact_domains',
        'store_link',
        'source_url',
    ]
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, '') for field in fields})


def build_markdown(rows):
    labels = {
        'next_verify': '建议进入详情核验',
        'manual_appmagic': '需 AppMagic/人工复核',
        'reject': '不建议入库',
    }
    lines = ['# Second Review For Priority Leads', '']
    lines.append(f'- Reviewed priority leads: {len(rows)}')
    for bucket in ('next_verify', 'manual_appmagic', 'reject'):
        subset = [row for row in rows if row['second_bucket'] == bucket]
        lines.append(f'- {labels[bucket]}: {len(subset)} {dict(Counter(row["platform"] for row in subset))}')
    lines.append('')
    lines.append('## 公司分布')
    for bucket in ('next_verify', 'manual_appmagic', 'reject'):
        lines.append(f'### {labels[bucket]}')
        counts = Counter(row['source_company'] for row in rows if row['second_bucket'] == bucket)
        for company, count in counts.most_common():
            lines.append(f'- {company}: {count}')
        lines.append('')
    for bucket in ('next_verify', 'manual_appmagic', 'reject'):
        lines.append(f'## {labels[bucket]}')
        grouped = defaultdict(list)
        for row in rows:
            if row['second_bucket'] == bucket:
                grouped[row['source_company']].append(row)
        for company in sorted(grouped):
            lines.append(f'### {company}')
            for row in grouped[company]:
                title = row.get('store_title') or row.get('id')
                lines.append(
                    f'- {row["platform"]} `{row["id"]}` | {title} | '
                    f'{row["source_domain"]} / {row["source_kind"]} | {row["second_reason"]}'
                )
            lines.append('')
    return '\n'.join(lines) + '\n'


def run(run_dir):
    triage = read_csv(os.path.join(run_dir, 'triaged_new_leads.csv'))
    pages = filter_email_domain_tainted_pages(
        read_jsonl(os.path.join(run_dir, 'page_evidence.jsonl'))
    )
    stores = read_jsonl(os.path.join(run_dir, 'store_contact_pages.jsonl'))

    page_by_url = {}
    for page in pages:
        for key in (page.get('source_url'), page.get('final_url')):
            if key:
                page_by_url[key] = page

    store_by_key = {}
    for store in stores:
        key = (store.get('platform'), store.get('pkg_or_id'))
        if key not in store_by_key or str(store.get('status')) == '200':
            store_by_key[key] = store

    reviewed = []
    for row in triage:
        if row.get('bucket') != 'priority_verify':
            continue
        page = page_by_url.get(row.get('source_url')) or {}
        store = store_by_key.get((row.get('platform'), row.get('id')), {})
        bucket, reason = classify(row, page, store)
        reviewed.append({
            'second_bucket': bucket,
            'second_reason': reason,
                'source_company': row.get('source_company', ''),
                'source_app': row.get('source_app', ''),
                'platform': row.get('platform', ''),
            'id': row.get('id', ''),
            'store_title': row.get('store_title', ''),
            'source_domain': row.get('source_domain', ''),
            'source_kind': row.get('source_kind', ''),
            'page_title': page.get('title', ''),
            'page_ref_count': page_refs_count(page),
            'store_status': row.get('store_status', ''),
            'store_developer': row.get('store_developer', ''),
            'store_contact_domains': row.get('store_contact_domains', ''),
            'store_link': row.get('store_link', ''),
            'source_url': row.get('source_url', ''),
        })

    order = {'next_verify': 0, 'manual_appmagic': 1, 'reject': 2}
    reviewed.sort(key=lambda row: (
        order.get(row['second_bucket'], 9),
        row['source_company'],
        row['platform'],
        row['id'],
    ))
    csv_path = os.path.join(run_dir, 'second_review_priority_leads.csv')
    md_path = os.path.join(run_dir, 'second_review_priority_leads.md')
    write_csv(csv_path, reviewed)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(build_markdown(reviewed))
    return csv_path, md_path, reviewed


def main():
    parser = argparse.ArgumentParser(description='Second-pass review priority web leads.')
    parser.add_argument('run_dir')
    args = parser.parse_args()
    csv_path, md_path, rows = run(args.run_dir)
    print(json.dumps({
        'review_csv': csv_path,
        'review_report': md_path,
        'counts': dict(Counter(row['second_bucket'] for row in rows)),
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
