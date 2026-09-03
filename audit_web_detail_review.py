#!/usr/bin/env python3
"""Build a pre-import detail review for web-discovered product leads.

This script writes review artifacts into an existing private evidence run
directory. It does not modify data/*.js or index.html.
"""
import argparse
import csv
import html
import json
import os
import re
import time
import urllib.parse
from collections import Counter, defaultdict

import requests

import monitor
from audit_web_fetcher import USER_AGENT, fetch_url, itunes_lookup_batch
from audit_web_evidence import hostname, registered_domain
from import_confirmed_developers import (
    clean_text,
    fetch_gp_detail_html,
    parse_downloads_from_html,
    parse_gp_detail_value,
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AM_BASE = 'https://appmagic.rocks'
AM_SEARCH_BY_IDS = AM_BASE + '/api/v2/united-applications/search-by-ids'
AM_PUBLISHER_APPS = AM_BASE + '/api/v2/search/publisher-applications'

REJECT_DETAIL_IDS = {
    ('iOS', '1450874784'): 'Apple Transporter 是 Apple 官方开发/上传工具，来自教程页链接，非竞品产品',
}

PLATFORM_OFFICIAL_IOS_DEVELOPERS = {
    'apple',
    'apple inc.',
}

CROSS_COMPANY_SOURCE_DOMAINS = {
    ('广州河马游戏', 'eyewind.com'): (
        '跨公司官网线索：EyeWind/深圳市风眼官网是从库内 Used Car Tycoon 的联系网址顺出来的，'
        '只能证明存在跳转/联系关系，不能直接归到广州河马游戏'
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


def load_apps():
    apps = []
    data_dir = os.path.join(BASE_DIR, 'data')
    for filename in sorted(os.listdir(data_dir)):
        if not filename.endswith('.js'):
            continue
        with open(os.path.join(data_dir, filename), encoding='utf-8') as f:
            content = f.read()
        m = re.match(r'window\._loadCompany\("(.*?)",\s*(\[.*\])\);\s*$', content, re.S)
        if not m:
            raise ValueError(f'Cannot parse {filename}')
        apps.extend(json.loads(m.group(2)))
    return apps


def normalize_iso_date(value):
    if not value:
        return ''
    value = str(value)
    if 'T' in value:
        value = value.split('T', 1)[0]
    return monitor.normalize_date(value[:10])


def make_appmagic_session():
    session = requests.Session()
    session.headers.update({
        'User-Agent': USER_AGENT,
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Referer': AM_BASE + '/',
    })
    return session


def appmagic_store_id(platform):
    if platform == 'GP':
        return 1
    if platform == 'iOS':
        return 2
    return 0


def target_key(platform, app_id):
    return f'{platform}:{app_id}'


def appmagic_app_url(platform, app_id):
    if platform == 'GP':
        return f'{AM_BASE}/google-play/x/{urllib.parse.quote(app_id, safe="")}/?hl=en'
    if platform == 'iOS':
        return f'{AM_BASE}/iphone/x/{urllib.parse.quote(app_id, safe="")}/?hl=en'
    return ''


def appmagic_publisher_url(pub):
    if not pub:
        return ''
    pub_id = pub.get('id')
    name = pub.get('name') or pub_id
    if not pub_id:
        return ''
    slug = re.sub(r'[^A-Za-z0-9]+', '-', str(name)).strip('-') or 'publisher'
    return f'{AM_BASE}/publisher/{slug}/{pub_id}_{urllib.parse.quote(str(name))}'


def search_appmagic_by_ids(targets, retries=3):
    session = make_appmagic_session()
    out = {}

    def request_batch(batch):
        body = {
            'ids': [
                {'store': appmagic_store_id(row['platform']), 'store_application_id': row['id']}
                for row in batch
                if appmagic_store_id(row['platform'])
            ]
        }
        if not body['ids']:
            return []
        last_error = ''
        for attempt in range(retries):
            try:
                resp = session.post(AM_SEARCH_BY_IDS, json=body, timeout=30)
                if resp.status_code == 200:
                    return resp.json().get('data', [])
                last_error = f'{resp.status_code}: {resp.text[:160]}'
            except Exception as e:
                last_error = str(e)
            time.sleep(min(2 * (attempt + 1), 8))
        return [{'__error__': last_error}]

    for i in range(0, len(targets), 80):
        batch = targets[i:i + 80]
        for item in request_batch(batch):
            if item.get('__error__'):
                for row in batch:
                    out[target_key(row['platform'], row['id'])] = {'error': item['__error__']}
                continue
            for app in item.get('applications') or [item]:
                store_ids = app.get('store_ids') or item.get('store_ids') or []
                for sid in store_ids:
                    try:
                        store, app_id = sid.split('_', 1)
                    except ValueError:
                        continue
                    platform = 'GP' if store == '1' else 'iOS' if store in {'2', '3'} else ''
                    if platform:
                        out[target_key(platform, app_id)] = item

    missing_ios = [
        row for row in targets
        if row['platform'] == 'iOS' and target_key(row['platform'], row['id']) not in out
    ]
    if missing_ios:
        body = {'ids': [{'store': 3, 'store_application_id': row['id']} for row in missing_ios]}
        try:
            resp = session.post(AM_SEARCH_BY_IDS, json=body, timeout=30)
            if resp.status_code == 200:
                for item in resp.json().get('data', []):
                    for app in item.get('applications') or [item]:
                        if str(app.get('store_application_id')) in {row['id'] for row in missing_ios}:
                            out[target_key('iOS', str(app.get('store_application_id')))] = item
        except Exception:
            pass
    return out


def fetch_publisher_apps(publisher_ids, max_rows=500):
    session = make_appmagic_session()
    result = {}
    request_delay = float(os.environ.get('APPMAGIC_REQUEST_DELAY', '0.4'))
    rate_limit_wait = float(os.environ.get('APPMAGIC_429_WAIT', '60'))
    for pub_id in sorted(pid for pid in publisher_ids if pid):
        apps = []
        for offset in range(0, max_rows, 100):
            chunk = None
            last_error = ''
            for attempt in range(7):
                try:
                    resp = session.get(
                        AM_PUBLISHER_APPS,
                        params={'united_publisher_id': pub_id, 'from': offset, 'limit': 100},
                        timeout=30,
                    )
                    if resp.status_code == 200:
                        chunk = resp.json()
                        time.sleep(request_delay)
                        break
                    last_error = f'{resp.status_code}: {resp.text[:160]}'
                    if resp.status_code != 429:
                        break
                    retry_after = resp.headers.get('Retry-After', '')
                    wait_seconds = float(retry_after) if retry_after.replace('.', '', 1).isdigit() else rate_limit_wait
                    time.sleep(min(max(wait_seconds, 2), 120))
                except Exception as e:
                    last_error = str(e)
                    time.sleep(min(2 * (attempt + 1), 12))
            if chunk is None:
                result[pub_id] = {'error': last_error or 'empty response', 'apps': apps}
                break
            if not chunk:
                break
            apps.extend(chunk)
            if len(chunk) < 100:
                break
        result.setdefault(pub_id, {'error': '', 'apps': apps})
    return result


def extract_gp_static_detail(pkg):
    url = f'https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=us'
    detail = {
        'store_status': '',
        'name': '',
        'icon': '',
        'downloads': '',
        'last_update': '',
        'release_date_static': '',
        'developer': '',
        'dev_link': '',
        'tags': '',
        'error': '',
    }
    try:
        body = fetch_gp_detail_html(url)
        detail['store_status'] = '200'
    except Exception as e:
        detail['store_status'] = 'error'
        detail['error'] = str(e)
        return detail

    m = re.search(r'<meta property="og:title" content="(.*?) - Apps on Google Play"', body, re.S)
    if m:
        detail['name'] = clean_text(m.group(1))
    else:
        m = re.search(r'<h1><span[^>]*itemprop="name"[^>]*>(.*?)</span></h1>', body, re.S)
        if m:
            detail['name'] = clean_text(m.group(1))

    m = re.search(r'<meta property="og:image" content="(.*?)"', body, re.S)
    if m:
        detail['icon'] = html.unescape(m.group(1)).strip()

    detail['downloads'] = parse_downloads_from_html(body)
    detail['last_update'] = monitor.normalize_date(parse_gp_detail_value(body, 'Updated on'))
    detail['release_date_static'] = monitor.normalize_date(parse_gp_detail_value(body, 'Released on'))

    developer, dev_link = monitor.extract_gp_developer_identity(body)
    detail['developer'] = developer
    detail['dev_link'] = dev_link

    tags = []
    for tag in re.findall(r'itemprop="genre".*?<span[^>]*aria-hidden="true">(.*?)</span>', body, re.S):
        tag = clean_text(tag)
        if tag and tag not in tags:
            tags.append(tag)
    detail['tags'] = ', '.join(tags)
    return detail


def appmagic_child_for_target(appmagic_item, platform, app_id):
    if not appmagic_item or appmagic_item.get('error'):
        return {}
    store = appmagic_store_id(platform)
    for app in appmagic_item.get('applications') or []:
        if app.get('store_application_id') == app_id and store in (app.get('store') or []):
            return app
    return appmagic_item


def appmagic_release_fields(appmagic_item, platform, app_id):
    child = appmagic_child_for_target(appmagic_item, platform, app_id)
    release_date = normalize_iso_date(child.get('releaseDate') or appmagic_item.get('releaseDate'))
    first_detected = normalize_iso_date(child.get('first_detected') or appmagic_item.get('first_detected'))
    last_release = normalize_iso_date(child.get('last_release_date') or appmagic_item.get('last_release_date'))
    return release_date, first_detected, last_release


def gp_release_with_fallbacks(gp_detail, appmagic_item, pkg, name):
    if gp_detail.get('release_date_static'):
        return gp_detail['release_date_static'], 'Google Play Released on'

    appmagic_release, first_detected, _ = appmagic_release_fields(appmagic_item, 'GP', pkg)
    if appmagic_release:
        return appmagic_release, 'AppMagic Release Date'
    if first_detected:
        return first_detected, 'AppMagic First Detected'

    apkcombo_date = monitor.fetch_apkcombo_release_date(pkg, name)
    if apkcombo_date:
        return apkcombo_date, 'APKCombo'

    if gp_detail.get('last_update'):
        return gp_detail['last_update'], 'Google Play Updated on fallback'
    return '', ''


def ios_detail_from_lookup(row):
    release_date = monitor.normalize_ios_release_date(row)
    last_update = monitor.normalize_past_or_today_date(str(row.get('currentVersionReleaseDate', ''))[:10])
    return {
        'name': row.get('trackName', ''),
        'icon': row.get('artworkUrl512', row.get('artworkUrl100', '')),
        'store_link': row.get('trackViewUrl', ''),
        'dev_link': row.get('artistViewUrl') or (f"https://apps.apple.com/developer/id{row.get('artistId')}" if row.get('artistId') else ''),
        'developer': row.get('artistName', ''),
        'seller_name': row.get('sellerName', ''),
        'rating_count': row.get('userRatingCount', 0),
        'tags': ', '.join(row.get('genres', [])),
        'release_date': release_date,
        'last_update': last_update,
        'website_url': row.get('sellerUrl', ''),
    }


def collect_page_refs(pages, platform, app_id):
    refs = []
    for page in pages:
        if platform == 'GP':
            found = app_id in (page.get('gp_packages') or [])
        else:
            found = app_id in (page.get('ios_ids') or [])
        if found:
            refs.append(page)
    return refs


def summarize_refs(refs, limit=4):
    values = []
    for page in refs[:limit]:
        url = page.get('final_url') or page.get('source_url') or ''
        title = page.get('title') or ''
        values.append(f'{page.get("source_kind", "")}:{registered_domain(url)}:{title[:80]}')
    if len(refs) > limit:
        values.append(f'...+{len(refs) - limit}')
    return ' | '.join(values)


def collect_store_domains(store_row, appmagic_item, ios_detail):
    urls = []
    urls.extend(store_row.get('contact_urls') or [])
    child = appmagic_child_for_target(appmagic_item, store_row.get('platform'), store_row.get('pkg_or_id'))
    for key in ('website_url', 'support_url'):
        for value in (child.get(key), appmagic_item.get(key) if appmagic_item else None):
            if isinstance(value, list):
                urls.extend(value)
            elif value:
                urls.append(value)
    if ios_detail.get('website_url'):
        urls.append(ios_detail['website_url'])
    domains = []
    for url in urls:
        if not isinstance(url, str):
            continue
        if str(url).startswith('mailto:'):
            continue
        domain = registered_domain(url)
        if domain and domain not in domains:
            domains.append(domain)
    return domains


def publisher_summary(apps):
    gp_ids = set()
    ios_ids = set()
    names = []
    tags = Counter()
    for item in apps:
        if item.get('name') and item['name'] not in names:
            names.append(item['name'])
        for tag in item.get('tags') or []:
            if tag.get('type') in {'domain', 'games', 'apps'}:
                tags[tag.get('name')] += 1
        for app in item.get('applications') or [item]:
            for sid in app.get('store_ids') or []:
                try:
                    store, app_id = sid.split('_', 1)
                except ValueError:
                    continue
                if store == '1':
                    gp_ids.add(app_id)
                elif store in {'2', '3'}:
                    ios_ids.add(app_id)
    return {
        'app_count': len(apps),
        'gp_count': len(gp_ids),
        'ios_count': len(ios_ids),
        'top_names': names[:8],
        'top_tags': [name for name, _ in tags.most_common(8)],
    }


def classify_detail(row, appmagic_item, refs, pub_info, existing_key, developer='', seller_name=''):
    key = (row.get('platform'), row.get('id'))
    if key in REJECT_DETAIL_IDS:
        return 'reject', REJECT_DETAIL_IDS[key]
    if row.get('platform') == 'iOS':
        identities = {str(developer or '').strip().lower(), str(seller_name or '').strip().lower()}
        if identities & PLATFORM_OFFICIAL_IOS_DEVELOPERS:
            return 'reject', '平台官方开发者应用，非竞品产品'
    cross_company_reason = CROSS_COMPANY_SOURCE_DOMAINS.get((
        row.get('source_company'),
        row.get('source_domain'),
    ))
    if cross_company_reason:
        return 'manual_review', cross_company_reason
    if existing_key:
        return 'already_in_library', '当前库中已存在相同 platform/id'
    if not refs:
        return 'manual_review', '缺少官网/支持/隐私页反向链接证据'
    if not appmagic_item or appmagic_item.get('error'):
        return 'manual_review', 'AppMagic 未返回可用应用记录'
    if row.get('second_bucket') == 'manual_appmagic':
        return 'manual_review', '上一轮标记为需要 AppMagic/人工复核'
    if pub_info.get('app_count', 0) > 0:
        return 'candidate_import', '商店/AppMagic/官网证据齐全，待人工确认入库'
    return 'manual_review', 'publisher 应用列表为空或未取到'


def build_rows(run_dir, use_gp_network=True):
    second = read_csv(os.path.join(run_dir, 'second_review_priority_leads.csv'))
    targets = [r for r in second if r.get('second_bucket') in {'next_verify', 'manual_appmagic'}]
    pages = read_jsonl(os.path.join(run_dir, 'page_evidence.jsonl'))
    stores = read_jsonl(os.path.join(run_dir, 'store_contact_pages.jsonl'))
    apps = load_apps()
    existing = {(a.get('platform'), a.get('pkg_or_id')): a for a in apps}

    store_by_key = {}
    for store in stores:
        key = (store.get('platform'), store.get('pkg_or_id'))
        if key not in store_by_key or str(store.get('status')) == '200':
            store_by_key[key] = store

    ios_ids = [r['id'] for r in targets if r['platform'] == 'iOS']
    ios_lookup = {}
    for i in range(0, len(ios_ids), 200):
        ios_lookup.update(itunes_lookup_batch(ios_ids[i:i + 200]))
        time.sleep(0.2)

    appmagic_by_key = search_appmagic_by_ids(targets)
    publisher_ids = set()
    for item in appmagic_by_key.values():
        if item and not item.get('error'):
            pub = item.get('unitedPublisher') or {}
            if pub.get('id'):
                publisher_ids.add(pub['id'])
    publisher_apps = fetch_publisher_apps(publisher_ids)
    publisher_summaries = {
        pub_id: publisher_summary(info.get('apps') or [])
        for pub_id, info in publisher_apps.items()
    }

    email_counts = Counter()
    private_email_path = os.path.join(run_dir, 'private_emails.csv')
    if os.path.exists(private_email_path):
        with open(private_email_path, newline='', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                key = (row.get('platform'), row.get('pkg_or_id'))
                email_counts[key] += 1

    rows = []
    for idx, row in enumerate(targets, 1):
        platform = row['platform']
        app_id = row['id']
        key = (platform, app_id)
        store_row = store_by_key.get(key, {})
        appmagic_item = appmagic_by_key.get(target_key(platform, app_id), {})
        am_child = appmagic_child_for_target(appmagic_item, platform, app_id)
        pub = (appmagic_item or {}).get('unitedPublisher') or am_child.get('unitedPublisher') or {}
        pub_id = pub.get('id')
        pub_info = publisher_summaries.get(pub_id, {})
        refs = collect_page_refs(pages, platform, app_id)
        ios_detail = {}
        gp_detail = {}
        release_date = ''
        release_source = ''
        last_update = ''
        name = row.get('store_title') or app_id
        icon = ''
        downloads = ''
        rating_count = ''
        developer = row.get('store_developer', '')
        dev_link = ''
        tags = ''
        store_link = row.get('store_link', '')

        if platform == 'iOS':
            ios_detail = ios_detail_from_lookup(ios_lookup.get(app_id, {}))
            name = ios_detail.get('name') or name
            icon = ios_detail.get('icon', '')
            store_link = ios_detail.get('store_link') or store_link or f'https://apps.apple.com/app/id{app_id}'
            dev_link = ios_detail.get('dev_link', '')
            developer = ios_detail.get('developer', '') or developer
            rating_count = ios_detail.get('rating_count', '')
            tags = ios_detail.get('tags', '')
            release_date = ios_detail.get('release_date', '')
            release_source = 'Apple iTunes Lookup'
            last_update = ios_detail.get('last_update', '')
        else:
            if use_gp_network:
                gp_detail = extract_gp_static_detail(app_id)
            else:
                gp_detail = {}
            name = gp_detail.get('name') or am_child.get('name') or appmagic_item.get('name') or name
            icon = gp_detail.get('icon') or am_child.get('icon_url') or appmagic_item.get('icon_url') or ''
            downloads = gp_detail.get('downloads') or ''
            developer = gp_detail.get('developer') or am_child.get('publisher_name') or appmagic_item.get('publisher_name') or developer
            if not developer and pub.get('name'):
                developer = pub['name']
            dev_link = gp_detail.get('dev_link') or appmagic_publisher_url(pub)
            tags = gp_detail.get('tags') or ', '.join(tag.get('name') for tag in (appmagic_item.get('tags') or [])[:8] if tag.get('name'))
            last_update = gp_detail.get('last_update', '')
            release_date, release_source = gp_release_with_fallbacks(gp_detail, appmagic_item, app_id, name)

        am_release, am_first_detected, am_last_release = appmagic_release_fields(appmagic_item, platform, app_id)
        store_domains = collect_store_domains({**store_row, 'platform': platform, 'pkg_or_id': app_id}, appmagic_item, ios_detail)
        status, status_reason = classify_detail(
            row,
            appmagic_item,
            refs,
            pub_info,
            key in existing,
            developer=developer,
            seller_name=ios_detail.get('seller_name', ''),
        )

        rows.append({
            'review_status': status,
            'status_reason': status_reason,
            'source_company': row.get('source_company', ''),
            'source_app': row.get('source_app', ''),
            'second_bucket': row.get('second_bucket', ''),
            'platform': platform,
            'id': app_id,
            'name': name,
            'developer': developer,
            'seller_name': ios_detail.get('seller_name', ''),
            'store_link': store_link,
            'dev_link': dev_link,
            'icon': icon,
            'downloads': downloads,
            'rating_count': rating_count,
            'release_date': release_date,
            'release_date_source': release_source,
            'last_update': last_update,
            'tags': tags,
            'appmagic_name': appmagic_item.get('name', ''),
            'appmagic_publisher': pub.get('name', ''),
            'appmagic_publisher_id': pub_id or '',
            'appmagic_publisher_url': appmagic_publisher_url(pub),
            'appmagic_app_url': appmagic_app_url(platform, app_id),
            'appmagic_release_date': am_release,
            'appmagic_first_detected': am_first_detected,
            'appmagic_last_release_date': am_last_release,
            'publisher_app_count': pub_info.get('app_count', ''),
            'publisher_gp_count': pub_info.get('gp_count', ''),
            'publisher_ios_count': pub_info.get('ios_count', ''),
            'publisher_top_apps': '; '.join(pub_info.get('top_names', [])),
            'publisher_top_tags': '; '.join(pub_info.get('top_tags', [])),
            'store_status': store_row.get('status', row.get('store_status', '')),
            'store_contact_domains': row.get('store_contact_domains', ''),
            'verified_domains': ';'.join(store_domains),
            'source_domain': row.get('source_domain', ''),
            'source_kind': row.get('source_kind', ''),
            'source_url': row.get('source_url', ''),
            'official_page_refs': len(refs),
            'official_page_summary': summarize_refs(refs),
            'private_email_evidence_rows': email_counts[key],
        })
        if idx % 20 == 0:
            print(f'Detail progress {idx}/{len(targets)}', flush=True)
    return rows


def write_csv(path, rows):
    fields = [
        'review_status', 'status_reason', 'source_company', 'source_app',
        'second_bucket', 'platform', 'id', 'name', 'developer', 'seller_name', 'store_link',
        'dev_link', 'icon', 'downloads', 'rating_count', 'release_date',
        'release_date_source', 'last_update', 'tags', 'appmagic_name',
        'appmagic_publisher', 'appmagic_publisher_id', 'appmagic_publisher_url',
        'appmagic_app_url', 'appmagic_release_date', 'appmagic_first_detected',
        'appmagic_last_release_date', 'publisher_app_count', 'publisher_gp_count',
        'publisher_ios_count', 'publisher_top_apps', 'publisher_top_tags',
        'store_status', 'store_contact_domains', 'verified_domains',
        'source_domain', 'source_kind', 'source_url', 'official_page_refs',
        'official_page_summary', 'private_email_evidence_rows',
    ]
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, '') for field in fields})


def build_markdown(rows):
    labels = {
        'candidate_import': '建议入库候选',
        'manual_review': '需要人工确认',
        'reject': '剔除/噪音',
        'already_in_library': '库中已存在',
    }
    lines = ['# Detail Review For New Leads', '']
    lines.append(f'- Reviewed leads: {len(rows)}')
    for status, count in Counter(row['review_status'] for row in rows).most_common():
        lines.append(f'- {labels.get(status, status)}: {count}')
    lines.append(f'- Platforms: {dict(Counter(row["platform"] for row in rows))}')
    lines.append('')
    lines.append('## 日期来源')
    for source, count in Counter(row.get('release_date_source') or 'missing' for row in rows).most_common():
        lines.append(f'- {source}: {count}')
    missing_dates = [row for row in rows if not row.get('release_date')]
    lines.append(f'- 缺 release_date: {len(missing_dates)}')
    lines.append('')
    lines.append('## 公司分布')
    by_company = defaultdict(list)
    for row in rows:
        by_company[row['source_company']].append(row)
    for company, items in sorted(by_company.items()):
        counts = Counter(row['review_status'] for row in items)
        lines.append(f'- {company}: {len(items)} {dict(counts)}')
    lines.append('')

    for status in ('candidate_import', 'manual_review', 'reject', 'already_in_library'):
        subset = [row for row in rows if row['review_status'] == status]
        if not subset:
            continue
        lines.append(f'## {labels.get(status, status)}')
        grouped = defaultdict(list)
        for row in subset:
            grouped[row['source_company']].append(row)
        for company in sorted(grouped):
            lines.append(f'### {company}')
            for row in grouped[company]:
                date = row.get('release_date') or '缺日期'
                pub = row.get('appmagic_publisher') or row.get('developer') or '-'
                source_app = row.get('source_app') or '-'
                lines.append(
                    f'- {row["platform"]} `{row["id"]}` | {row["name"]} | '
                    f'pub: {pub} | date: {date} ({row.get("release_date_source") or "-"}) | '
                    f'source app: {source_app} | {row["status_reason"]}'
                )
            lines.append('')
    lines.append('## 说明')
    lines.append('- 原始邮箱只保留在 private_emails.csv，本报告只写证据行数。')
    lines.append('- 本报告不修改产品库；确认后再按候选清单入库。')
    return '\n'.join(lines) + '\n'


def run(run_dir, use_gp_network=True):
    rows = build_rows(run_dir, use_gp_network=use_gp_network)
    order = {'candidate_import': 0, 'manual_review': 1, 'already_in_library': 2}
    rows.sort(key=lambda row: (
        order.get(row['review_status'], 9),
        row['source_company'],
        row['platform'],
        row['id'],
    ))
    csv_path = os.path.join(run_dir, 'detail_review_candidates.csv')
    md_path = os.path.join(run_dir, 'detail_review_candidates.md')
    write_csv(csv_path, rows)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(build_markdown(rows))
    return csv_path, md_path, rows


def main():
    parser = argparse.ArgumentParser(description='Build detail review for web-discovered leads.')
    parser.add_argument('run_dir')
    parser.add_argument('--skip-gp-network', action='store_true', help='Do not refetch Google Play static pages.')
    args = parser.parse_args()
    csv_path, md_path, rows = run(args.run_dir, use_gp_network=not args.skip_gp_network)
    print(json.dumps({
        'detail_csv': csv_path,
        'detail_report': md_path,
        'counts': dict(Counter(row['review_status'] for row in rows)),
        'missing_release_date': len([row for row in rows if not row.get('release_date')]),
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
