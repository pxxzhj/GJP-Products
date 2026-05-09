#!/usr/bin/env python3
"""Import candidate developers from the audit CSV into the local product data."""
import csv
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

import monitor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIT_CSV = os.path.join(BASE_DIR, 'developer_audit_20260507.csv')
LOOKUP_CACHE = '/tmp/dev_supplement_lookup_results.json'
IMPORT_REPORT = []
FAILED_ITEMS = []
LOOKUP_BY_DEV = {}
GP_RELEASE_DRIVER = None


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    IMPORT_REPORT.append(line)


def fetch_text(url, timeout=25, retries=4):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    last_error = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode('utf-8', 'ignore')
        except Exception as e:
            last_error = e
            time.sleep(min(5 * (attempt + 1), 20))
    raise last_error


def fetch_text_with_selenium(url, wait=3):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY')
    opts = Options()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--lang=en-US')
    opts.add_argument('--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    if proxy:
        opts.add_argument(f'--proxy-server={proxy}')
    driver = webdriver.Chrome(options=opts)
    try:
        driver.set_page_load_timeout(35)
        driver.get(url)
        time.sleep(wait)
        return driver.page_source
    finally:
        driver.quit()


def fetch_gp_detail_html(url):
    try:
        return fetch_text(url, timeout=20, retries=2)
    except Exception:
        return fetch_text_with_selenium(url)


def make_gp_release_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY')
    opts = Options()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--lang=en-US')
    opts.add_argument('--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    if proxy:
        opts.add_argument(f'--proxy-server={proxy}')

    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(35)
    return driver


def fetch_gp_release_date(pkg):
    global GP_RELEASE_DRIVER
    if GP_RELEASE_DRIVER is None:
        GP_RELEASE_DRIVER = make_gp_release_driver()

    try:
        GP_RELEASE_DRIVER.get(f'https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=us')
        time.sleep(2)
        monitor.open_gp_about_panel(GP_RELEASE_DRIVER)
        return monitor.normalize_date(monitor.extract_gp_detail_value(GP_RELEASE_DRIVER, 'Released on'))
    except Exception:
        try:
            GP_RELEASE_DRIVER.quit()
        except Exception:
            pass
        GP_RELEASE_DRIVER = None
        raise


def close_gp_release_driver():
    global GP_RELEASE_DRIVER
    if GP_RELEASE_DRIVER is not None:
        try:
            GP_RELEASE_DRIVER.quit()
        except Exception:
            pass
        GP_RELEASE_DRIVER = None


def clean_text(value):
    value = re.sub(r'<[^>]+>', '', value or '')
    return html.unescape(value).strip()


def parse_downloads_from_html(body):
    m = re.search(r'<div class="ClM7O">([^<]+)</div><div class="g1rdde">Downloads</div>', body)
    return clean_text(m.group(1)) if m else ''


def parse_gp_detail_value(body, label):
    m = re.search(rf'{re.escape(label)}\s*</div>.*?<div[^>]*>(.*?)</div>', body, re.S)
    return clean_text(m.group(1)) if m else ''


def parse_gp_app_detail(pkg, dev_name, dev_url, company):
    url = f'https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=us'
    body = fetch_gp_detail_html(url)
    name = pkg
    m = re.search(r'<h1><span[^>]*itemprop="name"[^>]*>(.*?)</span></h1>', body, re.S)
    if m:
        name = clean_text(m.group(1))
    else:
        m = re.search(r'<meta property="og:title" content="(.*?) - Apps on Google Play"', body, re.S)
        if m:
            name = clean_text(m.group(1))

    icon = ''
    m = re.search(r'<meta property="og:image" content="(.*?)"', body, re.S)
    if m:
        icon = html.unescape(m.group(1)).strip()

    developer = dev_name
    m = re.search(r'<div class="Vbfug[^"]*"><a href="[^"]+"><span>(.*?)</span></a>', body, re.S)
    if m:
        developer = clean_text(m.group(1))

    tags = []
    for tag in re.findall(r'itemprop="genre".*?<span[^>]*aria-hidden="true">(.*?)</span>', body, re.S):
        tag = clean_text(tag)
        if tag and tag not in tags:
            tags.append(tag)

    release_date = monitor.normalize_date(parse_gp_detail_value(body, 'Released on'))
    if not release_date:
        release_date = fetch_gp_release_date(pkg)

    return {
        'name': name,
        'company_cn': company,
        'icon': icon,
        'platform': 'GP',
        'pkg_or_id': pkg,
        'store_link': url,
        'dev_link': dev_url,
        'developer': developer,
        'downloads': parse_downloads_from_html(body),
        'rating_count': 0,
        'last_update': monitor.normalize_date(parse_gp_detail_value(body, 'Updated on')),
        'tags': ', '.join(tags),
        'removed': False,
        'release_date': release_date,
    }


def fetch_gp_developer_apps(name, company):
    cached = LOOKUP_BY_DEV.get((company, name), {}).get('gp', {})
    dev_url = cached.get('url') or 'https://play.google.com/store/apps/developer?id=' + urllib.parse.quote_plus(name) + '&hl=en&gl=us'
    pkgs = cached.get('apps') or []
    if not pkgs:
        try:
            body = fetch_text(dev_url)
        except Exception as e:
            log(f'GP developer failed: {company} / {name}: {e}')
            FAILED_ITEMS.append({'type': 'gp_developer', 'company': company, 'name': name, 'error': str(e)})
            return []

        seen = set()
        pkgs = []
        for pkg in re.findall(r'/store/apps/details\?id=([A-Za-z0-9_\.]+)', body):
            if pkg not in seen:
                seen.add(pkg)
                pkgs.append(pkg)

    apps = []
    for pkg in pkgs:
        try:
            apps.append(parse_gp_app_detail(pkg, name, dev_url, company))
            time.sleep(0.25)
        except Exception as e:
            log(f'  GP app failed: {pkg}: {e}')
            FAILED_ITEMS.append({'type': 'gp_app', 'company': company, 'developer': name, 'pkg': pkg, 'error': str(e)})
    return apps


def fetch_ios_developer_apps(artist_id, company):
    url = f'https://itunes.apple.com/lookup?id={artist_id}&entity=software&country=us&limit=200'
    data = None
    for attempt in range(4):
        data = monitor.itunes_lookup(url)
        if data:
            break
        time.sleep(min(5 * (attempt + 1), 20))
    if not data:
        FAILED_ITEMS.append({'type': 'ios_developer', 'company': company, 'id': artist_id, 'error': 'iTunes lookup failed'})
        return []

    apps = []
    for r in data.get('results', []):
        if r.get('wrapperType') != 'software':
            continue
        aid = str(r.get('trackId', ''))
        apps.append({
            'name': r.get('trackName', aid),
            'company_cn': company,
            'icon': r.get('artworkUrl512', r.get('artworkUrl100', '')),
            'platform': 'iOS',
            'pkg_or_id': aid,
            'store_link': f'https://apps.apple.com/app/id{aid}',
            'dev_link': f'https://apps.apple.com/developer/id{artist_id}',
            'developer': r.get('artistName', ''),
            'downloads': '',
            'rating_count': r.get('userRatingCount', 0),
            'last_update': monitor.normalize_date(str(r.get('currentVersionReleaseDate', ''))[:10]),
            'tags': ', '.join(r.get('genres', [])),
            'removed': False,
            'release_date': monitor.normalize_date(str(r.get('releaseDate', ''))[:10]),
        })
    return apps


def load_apps():
    apps = []
    for filename in sorted(os.listdir(os.path.join(BASE_DIR, 'data'))):
        if not filename.endswith('.js'):
            continue
        with open(os.path.join(BASE_DIR, 'data', filename), encoding='utf-8') as f:
            content = f.read()
        m = re.match(r'window\._loadCompany\("(.*?)",\s*(\[.*\])\);\s*$', content, re.S)
        if not m:
            raise ValueError(f'Cannot parse {filename}')
        apps.extend(json.loads(m.group(2)))
    return apps


def load_candidates():
    candidates = []
    with open(AUDIT_CSV, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            if row['category'] == 'candidate_found_no_overlap':
                candidates.append(row)
    return candidates


def load_lookup_cache():
    if not os.path.exists(LOOKUP_CACHE):
        return
    with open(LOOKUP_CACHE, encoding='utf-8') as f:
        for row in json.load(f):
            LOOKUP_BY_DEV[(row.get('company'), row.get('name'))] = row


def extract_ios_artist_ids(links):
    ids = []
    for m in re.finditer(r'apps\.apple\.com/developer/id(\d+)', links or ''):
        if m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def regenerate(all_apps, affected_companies):
    monitor.regenerate_files(all_apps, set(affected_companies))


def main():
    try:
        load_lookup_cache()
        all_apps = load_apps()
        existing = set((a['platform'], a['pkg_or_id']) for a in all_apps)
        candidates = load_candidates()
        affected = set()
        added_by_company = {}
        added_by_platform = {'GP': 0, 'iOS': 0}

        log(f'Loaded {len(all_apps)} existing apps')
        log(f'Importing {len(candidates)} candidate developers')

        for idx, row in enumerate(candidates, 1):
            company = row['company']
            name = row['name']
            platform = row['platform']
            found = []

            if 'GP' in platform:
                gp_apps = fetch_gp_developer_apps(name, company)
                found.extend(gp_apps)
            if 'iOS' in platform:
                for artist_id in extract_ios_artist_ids(row.get('links', '')):
                    found.extend(fetch_ios_developer_apps(artist_id, company))
                    time.sleep(0.3)

            added = 0
            for app in found:
                key = (app['platform'], app['pkg_or_id'])
                if key in existing:
                    continue
                existing.add(key)
                all_apps.append(app)
                affected.add(app['company_cn'])
                added += 1
                added_by_platform[app['platform']] = added_by_platform.get(app['platform'], 0) + 1

            if added:
                added_by_company[company] = added_by_company.get(company, 0) + added
            log(f'[{idx}/{len(candidates)}] {company} / {name}: found {len(found)}, added {added}')
            time.sleep(1.5)

        with open('/tmp/all_apps_v6.json', 'w', encoding='utf-8') as f:
            json.dump(all_apps, f, ensure_ascii=False, indent=2)

        if affected:
            regenerate(all_apps, affected)

        log('=' * 60)
        log(f'Added total: {sum(added_by_company.values())} ({added_by_platform})')
        for company, count in sorted(added_by_company.items(), key=lambda item: (-item[1], item[0])):
            log(f'  {company}: +{count}')
        log(f'Database total: {len(all_apps)}')
        if FAILED_ITEMS:
            log(f'Failed items: {len(FAILED_ITEMS)}')
            for item in FAILED_ITEMS:
                log(f"  {item}")

        report_path = os.path.join(BASE_DIR, f'import_developers_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.txt')
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(IMPORT_REPORT))
        log(f'Report saved: {report_path}')
    finally:
        close_gp_release_driver()


if __name__ == '__main__':
    main()
