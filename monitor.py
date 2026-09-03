#!/usr/bin/env python3
"""
竞品监控脚本 - 检查所有开发者是否有新产品，已有产品是否有更新。
手动运行: python3 monitor.py
"""
import json
import html
import os
import re
import time
import subprocess
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = '/tmp/all_apps_v6.json'
GP_METRICS_STATE_PATH = os.path.join(BASE_DIR, '.gp_metrics_state.json')
GP_METRICS_BATCH_SIZE = int(os.environ.get('GP_METRICS_BATCH_SIZE', '800'))
REPORT_LINES = []
GP_DEVELOPER_SCAN_STATS = {}

GP_REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}

# ── helpers ──────────────────────────────────────────────────────────────────

MONTH_MAP = {
    'Jan': '01', 'Feb': '02', 'Mar': '03', 'Apr': '04',
    'May': '05', 'Jun': '06', 'Jul': '07', 'Aug': '08',
    'Sep': '09', 'Oct': '10', 'Nov': '11', 'Dec': '12',
}

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    REPORT_LINES.append(line)

def normalize_date(d):
    if not d:
        return ''
    d = d.strip()
    if re.match(r'^\d{4}/\d{2}/\d{2}$', d):
        return d
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', d)
    if m:
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
    m = re.match(r'^([A-Z][a-z]{2})\s+(\d{1,2}),?\s+(\d{4})$', d)
    if m:
        month = MONTH_MAP.get(m.group(1), '01')
        return f"{m.group(3)}/{month}/{m.group(2).zfill(2)}"
    return d

def normalize_past_or_today_date(d, today=None):
    normalized = normalize_date(d)
    if not normalized:
        return ''
    if today is None:
        today = date.today().strftime('%Y/%m/%d')
    if re.match(r'^\d{4}/\d{2}/\d{2}$', normalized) and normalized > today:
        return ''
    return normalized

def normalize_release_or_expected_date(d, today=None):
    normalized = normalize_date(d)
    if not normalized:
        return ''
    if today is None:
        today = date.today().strftime('%Y/%m/%d')
    if re.match(r'^\d{4}/\d{2}/\d{2}$', normalized) and normalized > today:
        return f'{normalized}[预]'
    return normalized

def ios_store_item_is_available(item, today=None):
    """Return whether Apple exposes evidence that the item has launched."""
    normalized = normalize_date(str(item.get('releaseDate', ''))[:10])
    if not normalized:
        return False
    if today is None:
        today = date.today().strftime('%Y/%m/%d')
    if normalized > today:
        return False
    if item.get('price') is not None or bool(item.get('formattedPrice')):
        return True

    # Apple Arcade titles have no standalone price. A later version date is
    # strong evidence that the expected release has become an actual release.
    version_date = normalize_past_or_today_date(
        str(item.get('currentVersionReleaseDate', ''))[:10], today
    )
    return bool(version_date and version_date > normalized)

def normalize_ios_release_date(item, today=None):
    """Keep unavailable App Store items marked as expected, even after a stale date."""
    normalized = normalize_date(str(item.get('releaseDate', ''))[:10])
    if not normalized:
        return ''
    if ios_store_item_is_available(item, today):
        return normalized
    return f'{normalized}[预]'

def format_downloads(n):
    if n <= 0:
        return ''
    if n >= 1_000_000_000:
        v = n / 1_000_000_000
        return f"{v:.1f}B+" if v < 10 else f"{v:.0f}B+"
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{v:.1f}M+" if v < 10 else f"{v:.0f}M+"
    if n >= 1_000:
        v = n / 1_000
        return f"{v:.1f}K+" if v < 10 else f"{v:.0f}K+"
    return f"{n}+"

def parse_downloads(dl_str):
    if not dl_str:
        return 0
    dl_str = str(dl_str).replace('+', '').replace(',', '').replace('\n', '').replace('Downloads', '').strip()
    m = re.match(r'([\d.]+)\s*([KMBkmb])?', dl_str)
    if not m:
        return 0
    num = float(m.group(1))
    suffix = (m.group(2) or '').upper()
    if suffix == 'K': return int(num * 1000)
    elif suffix == 'M': return int(num * 1000000)
    elif suffix == 'B': return int(num * 1000000000)
    return int(num)

def parse_count_text(value):
    if value is None:
        return 0
    text = str(value).replace(',', '').replace('+', '').strip()
    m = re.search(r'([\d.]+)\s*([KMBkmb])?', text)
    if not m:
        return 0
    num = float(m.group(1))
    suffix = (m.group(2) or '').upper()
    if suffix == 'K':
        num *= 1000
    elif suffix == 'M':
        num *= 1000000
    elif suffix == 'B':
        num *= 1000000000
    return int(num)

def fetch_text_url(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', 'ignore')


def fetch_gp_text(url, timeout=30):
    req = urllib.request.Request(url, headers=GP_REQUEST_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', 'ignore')


def extract_gp_downloads_from_html(body):
    for pattern in (
        r'<div class="ClM7O">\s*([^<]+?)\s*</div>\s*<div class="g1rdde">Downloads</div>',
        r'([\d,.]+[KMB]?\+?)\s*</div>\s*<div[^>]*>Downloads</div>',
        r'([\d,.]+[KMB]?\+?)\s*Downloads',
    ):
        match = re.search(pattern, body, re.I | re.S)
        if match:
            return clean_detail_text(match.group(1))
    return ''

def itunes_lookup(endpoint, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(endpoint, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                log(f"  iTunes API failed: {endpoint[:80]} - {e}")
                return None

# ── Step 1: iOS developer check ──────────────────────────────────────────────

def extract_ios_developers(all_apps):
    """Extract unique iOS developer IDs from the database."""
    devs = {}
    for a in all_apps:
        if a['platform'] != 'iOS':
            continue
        dev_link = a.get('dev_link', '')
        m = re.search(r'/id(\d+)', dev_link)
        if not m:
            continue
        dev_id = m.group(1)
        if dev_id not in devs:
            devs[dev_id] = {
                'dev_id': dev_id,
                'company': a['company_cn'],
                'developer': a.get('developer', ''),
                'known_ids': set(),
            }
        devs[dev_id]['known_ids'].add(a['pkg_or_id'])
    return devs

def check_ios_developers(all_apps):
    devs = extract_ios_developers(all_apps)
    log(f"iOS: checking {len(devs)} developers...")

    new_ios_apps = []
    for dev_id, info in devs.items():
        url = f"https://itunes.apple.com/lookup?id={dev_id}&entity=software&country=us&limit=200"
        data = itunes_lookup(url)
        if not data:
            continue

        results = data.get('results', [])
        found_ids = set()
        app_details = {}
        for r in results:
            if r.get('wrapperType') == 'software':
                aid = str(r.get('trackId', ''))
                found_ids.add(aid)
                app_details[aid] = r

        missing = found_ids - info['known_ids']
        if missing:
            for aid in missing:
                r = app_details.get(aid, {})
                app = {
                    'name': r.get('trackName', aid),
                    'company_cn': info['company'],
                    'icon': r.get('artworkUrl512', r.get('artworkUrl100', '')),
                    'platform': 'iOS',
                    'pkg_or_id': aid,
                    'store_link': f"https://apps.apple.com/app/id{aid}",
                    'dev_link': f"https://apps.apple.com/developer/id{dev_id}",
                    'developer': r.get('artistName', info['developer']),
                    'downloads': '',
                    'rating_count': r.get('userRatingCount', 0),
                    'last_update': normalize_past_or_today_date(str(r.get('currentVersionReleaseDate', ''))[:10]),
                    'tags': ', '.join(r.get('genres', [])),
                    'removed': False,
                    'release_date': normalize_ios_release_date(r),
                }
                new_ios_apps.append(app)
                log(f"  NEW iOS: {app['name']} ({aid}) -> {info['company']}")

        checked = len([d for d in devs if d <= dev_id])
        if checked % 10 == 0:
            log(f"  iOS progress: {checked}/{len(devs)}")

        time.sleep(1)

    log(f"iOS check done: {len(new_ios_apps)} new apps found")
    return new_ios_apps

# ── Step 2: GP developer check ──────────────────────────────────────────────

def normalize_gp_developer_url(dev_link, developer=''):
    """Canonicalize name-based Play developer URLs without changing numeric IDs."""
    if not dev_link or 'play.google.com' not in dev_link:
        return dev_link

    parsed = urllib.parse.urlsplit(dev_link)
    if parsed.path.rstrip('/').endswith('/developer') and developer:
        query = urllib.parse.parse_qs(parsed.query)
        query['id'] = [developer]
        query.setdefault('hl', ['en'])
        query.setdefault('gl', ['us'])
        encoded = urllib.parse.urlencode(query, doseq=True)
        return urllib.parse.urlunsplit((
            parsed.scheme or 'https',
            parsed.netloc or 'play.google.com',
            parsed.path,
            encoded,
            '',
        ))
    return dev_link


def extract_gp_developer_identity(body):
    """Read the current developer name and store link from a Play app page."""
    match = re.search(
        r'<div class="Vbfug[^\"]*"><a href="([^"]+)"><span>(.*?)</span>',
        body,
        re.S,
    )
    if not match:
        return '', ''

    href = html.unescape(match.group(1))
    developer = clean_detail_text(match.group(2))
    dev_url = urllib.parse.urljoin('https://play.google.com', href)
    return developer, normalize_gp_developer_url(dev_url, developer)


def extract_gp_developers(all_apps):
    """Extract unique GP developer URLs from the database."""
    devs = {}
    for a in all_apps:
        if a['platform'] != 'GP':
            continue
        dev_link = a.get('dev_link', '')
        if not dev_link or 'play.google.com' not in dev_link:
            continue
        normalized_link = normalize_gp_developer_url(dev_link, a.get('developer', ''))
        if normalized_link != dev_link:
            a['dev_link'] = normalized_link
        dev_link = normalized_link
        if dev_link not in devs:
            devs[dev_link] = {
                'url': dev_link,
                'company': a['company_cn'],
                'developer': a.get('developer', ''),
                'known_pkgs': set(),
                'seed_pkgs': [],
                'apps': [],
            }
        devs[dev_link]['known_pkgs'].add(a['pkg_or_id'])
        devs[dev_link]['apps'].append(a)
        if not a.get('removed'):
            devs[dev_link]['seed_pkgs'].append(a['pkg_or_id'])
    return devs

def clean_detail_text(value):
    value = re.sub(r'<[^>]+>', '', value or '')
    return html.unescape(value).strip()

def extract_gp_detail_value(driver, label):
    """Read a Google Play detail value from page HTML or visible text."""
    try:
        body = driver.page_source
        m = re.search(rf'{re.escape(label)}\s*</div>.*?<div[^>]*>(.*?)</div>', body, re.S)
        if m:
            value = clean_detail_text(m.group(1))
            if value:
                return value
    except Exception:
        pass

    try:
        from selenium.webdriver.common.by import By
        text = driver.find_element(By.TAG_NAME, 'body').text
    except Exception:
        return ''

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        if line == label:
            for value in lines[i + 1:]:
                if value not in ('arrow_forward', 'chevron_right', 'expand_more'):
                    return value
    return ''

def make_selenium_options():
    from selenium.webdriver.chrome.options import Options

    proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY')
    opts = Options()
    opts.page_load_strategy = 'none'
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--lang=en-US')
    opts.add_argument('--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    opts.add_experimental_option('prefs', {
        'profile.managed_default_content_settings.images': 2,
    })
    if proxy:
        opts.add_argument(f'--proxy-server={proxy}')
    return opts

def make_selenium_driver(timeout=35):
    from selenium import webdriver

    driver = webdriver.Chrome(options=make_selenium_options())
    driver.set_page_load_timeout(timeout)
    return driver

def fetch_appmagic_release_date(pkg, driver=None, wait=30):
    """Fetch Google Play release date from AppMagic when Play Store omits it."""
    owns_driver = driver is None
    if driver is None:
        driver = make_selenium_driver(timeout=45)

    try:
        quoted_pkg = urllib.parse.quote(pkg, safe='')
        driver.get(f'https://appmagic.rocks/google-play/x/{quoted_pkg}/?hl=en')
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                from selenium.webdriver.common.by import By
                text = driver.find_element(By.TAG_NAME, 'body').text
            except Exception:
                text = ''
            if pkg not in text:
                time.sleep(1)
                continue

            release_date = normalize_date(extract_gp_detail_value(driver, 'Release Date'))
            if not release_date:
                release_date = normalize_date(extract_gp_detail_value(driver, 'First Detected'))
            if release_date:
                return release_date
            time.sleep(1)
    except Exception:
        return ''
    finally:
        if owns_driver:
            driver.quit()

    return ''

def fetch_apkcombo_release_date(pkg, name=''):
    """Fetch a complete date from APKCombo when available."""
    slugs = []
    if name:
        slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
        if slug:
            slugs.append(slug)
    slugs.append(pkg)

    for slug in dict.fromkeys(slugs):
        url = f'https://apkcombo.com/{urllib.parse.quote(slug)}/{urllib.parse.quote(pkg)}/'
        try:
            body = fetch_text_url(url, timeout=20)
        except Exception:
            continue

        for pattern in (
            r'"datePublished"\s*:\s*"([^"]+)"',
            r'"dateModified"\s*:\s*"([^"]+)"',
            r'(?:Release(?:d)?|Published|First Released|Uploaded)(?:\s*Date)?\s*</[^>]+>\s*<[^>]+>\s*([^<]+)',
        ):
            m = re.search(pattern, body, re.I | re.S)
            if not m:
                continue
            value = clean_detail_text(m.group(1))
            release_date = normalize_date(value[:10] if re.match(r'^\d{4}-\d{2}-\d{2}', value) else value)
            if re.match(r'^\d{4}/\d{2}/\d{2}$', release_date):
                return release_date
    return ''

def fetch_gp_release_date_with_fallbacks(pkg, driver, last_update='', name=''):
    release_date = normalize_date(extract_gp_detail_value(driver, 'Released on'))
    if release_date:
        return release_date, 'Google Play Released on'

    release_date = fetch_appmagic_release_date(pkg, driver)
    if release_date:
        return release_date, 'AppMagic'

    release_date = fetch_apkcombo_release_date(pkg, name)
    if release_date:
        return release_date, 'APKCombo'

    if last_update:
        return last_update, 'Google Play Updated on fallback'

    return '', ''

def open_gp_about_panel(driver):
    """Open the Google Play about panel where Released on is often shown."""
    try:
        from selenium.webdriver.common.by import By
        controls = driver.find_elements(By.CSS_SELECTOR, 'button, div[role="button"]')
        for control in controls:
            label = (control.text or control.get_attribute('aria-label') or '').strip()
            if label == 'arrow_forward' or 'About this' in label:
                driver.execute_script('arguments[0].click()', control)
                time.sleep(1)
                return True
    except Exception:
        pass
    return False

def extract_gp_metrics(driver):
    """Extract Google Play downloads and rating/review count from an app page."""
    downloads = ''
    rating_count = 0

    try:
        body = driver.page_source
    except Exception:
        body = ''

    if body:
        m = re.search(r'([\d,.]+[KMB]?\+?)\s*Downloads', body)
        if m:
            downloads = m.group(1).strip()

        review_patterns = (
            r'([\d,.]+[KMB]?)\s+reviews',
            r'([\d,.]+[KMB]?)\s+ratings',
            r'"([^"]*?[\d,.]+[KMB]?\s+reviews[^"]*?)"',
            r'"([^"]*?[\d,.]+[KMB]?\s+ratings[^"]*?)"',
        )
        for pattern in review_patterns:
            m = re.search(pattern, body, re.I)
            if m:
                rating_count = parse_count_text(m.group(1))
                if rating_count:
                    break

    if not downloads or not rating_count:
        try:
            from selenium.webdriver.common.by import By
            text = driver.find_element(By.TAG_NAME, 'body').text
        except Exception:
            text = ''

        if text:
            if not downloads:
                m = re.search(r'([\d,.]+[KMB]?\+?)\s*\n?\s*Downloads', text, re.I)
                if m:
                    downloads = m.group(1).strip()
            if not rating_count:
                m = re.search(r'([\d,.]+[KMB]?)\s*(?:reviews|ratings)', text, re.I)
                if m:
                    rating_count = parse_count_text(m.group(1))

    return downloads, rating_count

def load_gp_metrics_state():
    try:
        with open(GP_METRICS_STATE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return int(data.get('next_index', 0))
    except Exception:
        return 0

def save_gp_metrics_state(next_index):
    data = {
        'next_index': int(next_index),
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    with open(GP_METRICS_STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def check_gp_metrics_updates(all_apps):
    if GP_METRICS_BATCH_SIZE <= 0:
        log("GP metrics update skipped: GP_METRICS_BATCH_SIZE <= 0")
        return []

    gp_apps = [a for a in all_apps if a.get('platform') == 'GP' and not a.get('removed')]
    if not gp_apps:
        return []

    total = len(gp_apps)
    batch_size = min(GP_METRICS_BATCH_SIZE, total)
    start = load_gp_metrics_state() % total
    selected = [gp_apps[(start + i) % total] for i in range(batch_size)]
    next_index = (start + batch_size) % total

    log(f"GP metrics: checking {batch_size}/{total} apps (start={start})...")
    updates = []
    errors = 0
    driver = make_selenium_driver(timeout=25)

    for i, app in enumerate(selected, 1):
        pkg = app.get('pkg_or_id', '')
        url = app.get('store_link') or f"https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=us"
        try:
            driver.get(url)
            time.sleep(1.5)
            downloads, rating_count = extract_gp_metrics(driver)
            changed = {}

            old_downloads = app.get('downloads', '')
            if downloads and parse_downloads(downloads) > parse_downloads(old_downloads):
                app['downloads'] = downloads
                changed['downloads'] = (old_downloads, downloads)

            old_rating_count = app.get('rating_count', 0)
            if not isinstance(old_rating_count, (int, float)):
                old_rating_count = parse_count_text(old_rating_count)
            if rating_count and rating_count > int(old_rating_count or 0):
                app['rating_count'] = rating_count
                changed['rating_count'] = (old_rating_count, rating_count)

            if changed:
                updates.append({
                    'pkg_or_id': pkg,
                    'name': app.get('name', pkg),
                    'company': app.get('company_cn', ''),
                    'changes': changed,
                })

            if i % 50 == 0 or i == batch_size:
                log(f"  GP metrics progress: {i}/{batch_size} (updated: {len(updates)}, errors: {errors})")

        except Exception as e:
            errors += 1
            log(f"  GP metrics ERROR [{i}/{batch_size}] {pkg}: {str(e)[:80]}")
            try:
                driver.quit()
            except Exception:
                pass
            driver = make_selenium_driver(timeout=25)
            time.sleep(1)

    try:
        driver.quit()
    except Exception:
        pass

    save_gp_metrics_state(next_index)
    log(f"GP metrics done: {len(updates)} apps updated, {errors} errors, next_index={next_index}")
    return updates

def check_gp_developers(all_apps):
    from selenium.webdriver.common.by import By

    global GP_DEVELOPER_SCAN_STATS
    original_links = {
        id(app): app.get('dev_link', '')
        for app in all_apps
        if app.get('platform') == 'GP'
    }
    devs = extract_gp_developers(all_apps)
    normalized_apps = [
        app for app in all_apps
        if app.get('platform') == 'GP'
        and app.get('dev_link', '') != original_links.get(id(app), '')
    ]
    identity_changes = []
    identity_splits = []
    error_details = []
    deep_scan = os.environ.get('GP_DEEP_DEVELOPER_SCAN') == '1'
    mode = 'deep browser scan' if deep_scan else 'daily storefront scan'
    log(f"GP: checking {len(devs)} developers ({mode})...")

    new_gp_pkgs = {}  # pkg -> {company, dev_url}
    checked = 0
    errors = 0
    driver = None

    def fetch_developer_packages(dev_url):
        driver.get(dev_url)
        time.sleep(3)

        last_height = driver.execute_script("return document.body.scrollHeight")
        for _ in range(10):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.5)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        links = driver.find_elements(By.CSS_SELECTOR, 'a[href*="/store/apps/details?id="]')
        found_pkgs = set()
        for link in links:
            href = link.get_attribute('href') or ''
            m = re.search(r'id=([a-zA-Z0-9_.]+)', href)
            if m:
                found_pkgs.add(m.group(1))
        return found_pkgs

    def record_packages(dev_url, info, found_pkgs):
        missing = found_pkgs - info['known_pkgs']
        for pkg in missing:
            if pkg not in new_gp_pkgs:
                new_gp_pkgs[pkg] = {'company': info['company'], 'dev_url': dev_url}
                log(f"  NEW GP pkg: {pkg} -> {info['company']}")

    def apply_recovered_identity(info, dev_url, developer):
        old_url = info['url']
        for app in info['apps']:
            app['dev_link'] = dev_url
            if developer:
                app['developer'] = developer
        identity_changes.append({
            'company': info['company'],
            'developer': developer or info['developer'],
            'old_url': old_url,
            'new_url': dev_url,
        })
        log(
            f"  GP developer identity recovered: {info['company']} / "
            f"{developer or info['developer']}"
        )

    if deep_scan:
        driver = make_selenium_driver(timeout=30)
        for dev_url, info in devs.items():
            checked += 1
            try:
                try:
                    found_pkgs = fetch_developer_packages(dev_url)
                except Exception as e:
                    if 'invalid session' not in str(e).lower():
                        raise
                    log(f"  GP driver session lost; restarting and retrying [{checked}/{len(devs)}]")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = make_selenium_driver(timeout=30)
                    found_pkgs = fetch_developer_packages(dev_url)

                record_packages(dev_url, info, found_pkgs)
                if checked % 10 == 0:
                    log(f"  GP progress: {checked}/{len(devs)} (new: {len(new_gp_pkgs)})")
                time.sleep(3)
            except Exception as e:
                errors += 1
                error_details.append({
                    'company': info['company'],
                    'developer': info['developer'],
                    'url': dev_url,
                    'error': str(e)[:200],
                })
                log(f"  GP ERROR [{checked}/{len(devs)}] {dev_url[:60]}: {str(e)[:80]}")
                time.sleep(2)
    else:
        def fetch_storefront(dev_url):
            req = urllib.request.Request(dev_url, headers=GP_REQUEST_HEADERS)
            last_error = None
            for attempt in range(2):
                try:
                    with urllib.request.urlopen(req, timeout=30) as response:
                        body = response.read().decode('utf-8', errors='ignore')
                    packages = set(re.findall(
                        r'/store/apps/details\?id=([a-zA-Z0-9_.]+)', body
                    ))
                    if not packages:
                        raise RuntimeError('developer page returned no apps')
                    return packages
                except Exception as e:
                    last_error = e
                    if attempt == 0:
                        time.sleep(1)
            raise last_error

        def recover_developer_identity(info):
            identities = {}
            seed_errors = []
            for pkg in list(dict.fromkeys(info['seed_pkgs']))[:3]:
                url = f'https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=us'
                try:
                    req = urllib.request.Request(url, headers=GP_REQUEST_HEADERS)
                    with urllib.request.urlopen(req, timeout=30) as response:
                        body = response.read().decode('utf-8', errors='ignore')
                    developer, dev_url = extract_gp_developer_identity(body)
                    if not dev_url:
                        raise RuntimeError('developer identity not found on app page')
                    identity = identities.setdefault(dev_url, {
                        'developer': developer,
                        'pkgs': [],
                    })
                    identity['pkgs'].append(pkg)
                except Exception as e:
                    seed_errors.append(f'{pkg}: {str(e)[:80]}')

            if not identities:
                detail = '; '.join(seed_errors[:3]) or 'no active seed apps'
                raise RuntimeError(f'developer recovery failed ({detail})')
            if len(identities) > 1:
                split = {
                    'company': info['company'],
                    'developer': info['developer'],
                    'identities': {
                        url: value['pkgs'] for url, value in identities.items()
                    },
                }
                identity_splits.append(split)
                raise RuntimeError(
                    f'developer identity split across {len(identities)} store pages'
                )

            dev_url, identity = next(iter(identities.items()))
            return dev_url, identity['developer']

        def fetch_storefront_with_recovery(dev_url, info):
            try:
                return fetch_storefront(dev_url), dev_url, '', False
            except Exception as original_error:
                recovered_url, developer = recover_developer_identity(info)
                if recovered_url == dev_url:
                    raise original_error
                packages = fetch_storefront(recovered_url)
                return packages, recovered_url, developer, True

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(fetch_storefront_with_recovery, dev_url, info): (dev_url, info)
                for dev_url, info in devs.items()
            }
            for future in as_completed(futures):
                dev_url, info = futures[future]
                checked += 1
                try:
                    found_pkgs, current_url, developer, recovered = future.result()
                    if recovered:
                        apply_recovered_identity(info, current_url, developer)
                    record_packages(current_url, info, found_pkgs)
                except Exception as e:
                    errors += 1
                    error_details.append({
                        'company': info['company'],
                        'developer': info['developer'],
                        'url': dev_url,
                        'error': str(e)[:200],
                    })
                    log(f"  GP ERROR [{checked}/{len(devs)}] {dev_url[:60]}: {str(e)[:80]}")
                if checked % 25 == 0 or checked == len(devs):
                    log(f"  GP progress: {checked}/{len(devs)} (new: {len(new_gp_pkgs)}, errors: {errors})")

    affected_companies = {
        app['company_cn'] for app in normalized_apps
    } | {
        item['company'] for item in identity_changes
    }
    GP_DEVELOPER_SCAN_STATS = {
        'checked': checked,
        'errors': errors,
        'normalized_apps': len(normalized_apps),
        'recovered_developers': identity_changes,
        'identity_splits': identity_splits,
        'error_details': error_details,
        'affected_companies': sorted(affected_companies),
    }

    # Deduplicate against existing DB
    existing_pkgs = set(a['pkg_or_id'] for a in all_apps if a['platform'] == 'GP')
    truly_new = {p: v for p, v in new_gp_pkgs.items() if p not in existing_pkgs}

    if not truly_new:
        if driver:
            driver.quit()
        log(f"GP check done: 0 new apps (checked {checked}, errors {errors})")
        return []

    log(f"GP: fetching details for {len(truly_new)} new apps...")
    if driver is None:
        driver = make_selenium_driver(timeout=30)
    new_gp_apps = []
    for pkg, info in truly_new.items():
        url = f"https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=us"
        try:
            driver.get(url)
            time.sleep(3)

            try:
                name = driver.find_element(By.CSS_SELECTOR, 'h1[itemprop="name"]').text.strip()
            except:
                try:
                    name = driver.find_element(By.CSS_SELECTOR, 'h1').text.strip()
                except:
                    name = pkg

            try:
                icon = driver.find_element(By.CSS_SELECTOR, 'img[itemprop="image"]').get_attribute('src')
            except:
                try:
                    imgs = driver.find_elements(By.CSS_SELECTOR, 'img[alt="Icon image"]')
                    icon = imgs[0].get_attribute('src') if imgs else ''
                except:
                    icon = ''

            downloads, rating_count = extract_gp_metrics(driver)
            try:
                if not downloads:
                    body = driver.page_source
                    downloads = extract_gp_downloads_from_html(body)
            except:
                pass
            if not downloads:
                try:
                    downloads = extract_gp_downloads_from_html(fetch_gp_text(url))
                except Exception:
                    pass

            developer = ''
            try:
                developer = driver.find_element(By.CSS_SELECTOR, 'div.Vbfug a span').text.strip()
            except:
                pass

            last_update = normalize_date(extract_gp_detail_value(driver, 'Updated on'))
            open_gp_about_panel(driver)
            if not last_update:
                last_update = normalize_date(extract_gp_detail_value(driver, 'Updated on'))
            release_date, release_source = fetch_gp_release_date_with_fallbacks(pkg, driver, last_update, name)

            removed = False
            try:
                if "not found" in driver.title.lower():
                    removed = True
                elif name == pkg:
                    body_text = driver.find_element(By.TAG_NAME, 'body').text.strip().lower()
                    removed = body_text.startswith("we're sorry") or body_text.startswith('not found')
            except:
                pass

            if release_date and release_source != 'Google Play Released on':
                log(f"  {release_source} release date: {pkg} -> {release_date}")
            elif not release_date:
                log(f"  release date not shown: {pkg}")

            if removed:
                log(f"  Skipped unavailable GP app: {pkg}")
                continue

            app = {
                'name': name,
                'company_cn': info['company'],
                'icon': icon,
                'platform': 'GP',
                'pkg_or_id': pkg,
                'store_link': url,
                'dev_link': info['dev_url'],
                'developer': developer,
                'downloads': downloads,
                'rating_count': rating_count,
                'last_update': last_update,
                'tags': '',
                'removed': removed,
                'release_date': release_date,
            }
            new_gp_apps.append(app)
            log(f"  Fetched: {name} ({pkg}) dl={downloads}")
            time.sleep(2)

        except Exception as e:
            log(f"  Fetch ERROR: {pkg} - {str(e)[:80]}")
            log(f"  Skipped GP app without accessible details: {pkg}")

    driver.quit()
    log(f"GP check done: {len(new_gp_apps)} new apps (checked {checked}, errors {errors})")
    return new_gp_apps

# ── Step 3: iOS update check ────────────────────────────────────────────────

def check_ios_updates(all_apps):
    ios_apps = [a for a in all_apps if a['platform'] == 'iOS' and not a.get('removed')]
    if not ios_apps:
        return []

    log(f"iOS updates: checking {len(ios_apps)} apps...")
    updates = []
    batch_size = 200

    for i in range(0, len(ios_apps), batch_size):
        batch = ios_apps[i:i + batch_size]
        ids = ','.join(a['pkg_or_id'] for a in batch)
        url = f"https://itunes.apple.com/lookup?id={ids}&country=us"
        data = itunes_lookup(url)
        if not data:
            continue

        lookup = {}
        for r in data.get('results', []):
            aid = str(r.get('trackId', ''))
            lookup[aid] = r

        for a in batch:
            r = lookup.get(a['pkg_or_id'])
            if not r:
                continue
            change = {
                'pkg_or_id': a['pkg_or_id'],
                'name': a['name'],
                'company': a['company_cn'],
            }
            new_update = normalize_past_or_today_date(str(r.get('currentVersionReleaseDate', ''))[:10])
            old_update = a.get('last_update', '')
            if new_update and new_update != old_update and new_update > old_update:
                change['old_update'] = old_update
                change['new_update'] = new_update
                a['last_update'] = new_update

            old_release = a.get('release_date', '')
            if old_release.endswith('[预]'):
                new_release = normalize_ios_release_date(r)
                if new_release and new_release != old_release:
                    change['old_release'] = old_release
                    change['new_release'] = new_release
                    a['release_date'] = new_release

            if len(change) > 3:
                updates.append(change)

            new_rc = r.get('userRatingCount', 0)
            if isinstance(new_rc, int) and new_rc > 0:
                a['rating_count'] = new_rc

        log(f"  iOS updates batch {i // batch_size + 1}: checked {min(i + batch_size, len(ios_apps))}/{len(ios_apps)}")
        time.sleep(1)

    log(f"iOS updates done: {len(updates)} apps updated")
    return updates

# ── Step 4: regenerate files ─────────────────────────────────────────────────

def write_product_index(all_apps):
    fields = [
        'name', 'company_cn', 'platform', 'developer', 'dev_link',
        'store_link', 'pkg_or_id', 'downloads', 'rating_count',
        'last_update', 'release_date', 'icon', 'removed',
    ]
    index_apps = []
    for app in all_apps:
        item = {}
        for field in fields:
            if field in app and app.get(field) not in ('', None):
                item[field] = app.get(field)
        index_apps.append(item)

    js_content = 'window._loadProductIndex('
    js_content += json.dumps(index_apps, ensure_ascii=False, separators=(',', ':'))
    js_content += ');'
    index_path = os.path.join(BASE_DIR, 'product_index.js')
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(js_content)
    log(f"Updated product_index.js: {len(index_apps)} apps")

def regenerate_files(all_apps, affected_companies):
    if not affected_companies:
        return

    companies = {}
    for a in all_apps:
        companies.setdefault(a['company_cn'], []).append(a)

    for company in affected_companies:
        apps = companies.get(company, [])
        js_content = f'window._loadCompany("{company}", '
        js_content += json.dumps(apps, ensure_ascii=False, indent=2)
        js_content += ');'
        js_path = os.path.join(BASE_DIR, 'data', f'{company}.js')
        with open(js_path, 'w', encoding='utf-8') as f:
            f.write(js_content)

    log(f"Regenerated {len(affected_companies)} company files: {', '.join(affected_companies)}")

    # Update companiesData in index.html
    co_stats = {}
    for a in all_apps:
        co = a['company_cn']
        if co not in co_stats:
            co_stats[co] = {'name': co, 'devs': set(), 'gp': 0, 'ios': 0, 'gp_dl': 0, 'ios_rat': 0, 'lu': ''}
        c = co_stats[co]
        if a.get('developer'):
            c['devs'].add(a['developer'])
        if a['platform'] == 'GP':
            c['gp'] += 1
            rc = a.get('rating_count', 0)
            if not isinstance(rc, (int, float)):
                rc = 0
            dl = parse_downloads(a.get('downloads', ''))
            c['gp_dl'] += max(int(rc), dl)
        else:
            c['ios'] += 1
            rc = a.get('rating_count', 0)
            if isinstance(rc, (int, float)):
                c['ios_rat'] += int(rc)
        lu = a.get('last_update', '') or ''
        if lu > c['lu']:
            c['lu'] = lu

    companies_list = []
    for co in sorted(co_stats.keys()):
        c = co_stats[co]
        companies_list.append({
            'name': c['name'],
            'developer_count': len(c['devs']),
            'gp_count': c['gp'],
            'ios_count': c['ios'],
            'total_count': c['gp'] + c['ios'],
            'gp_downloads': c['gp_dl'],
            'ios_ratings': c['ios_rat'],
            'latest_update': c['lu'],
        })

    index_path = os.path.join(BASE_DIR, 'index.html')
    with open(index_path, 'r', encoding='utf-8') as f:
        html = f.read()
    pattern = r'const companiesData = \[.*?\];'
    new_data = 'const companiesData = ' + json.dumps(companies_list, ensure_ascii=False, indent=6) + ';'
    html = re.sub(pattern, new_data, html, flags=re.DOTALL)
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(html)
    log("Updated index.html companiesData")
    write_product_index(all_apps)

# ── Step 5: git commit + push ────────────────────────────────────────────────

def git_commit_push(new_count, update_count):
    os.chdir(BASE_DIR)
    subprocess.run(['git', 'add', 'data/', 'index.html', 'product_index.js'], check=True)

    diff = subprocess.run(['git', 'diff', '--cached', '--stat'], capture_output=True, text=True)
    if not diff.stdout.strip():
        log("No changes to commit")
        return

    today = datetime.now().strftime('%Y-%m-%d')
    msg = f"监控更新 {today}: +{new_count} new, {update_count} updated\n\nCo-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
    subprocess.run(['git', 'commit', '-m', msg], check=True)
    log("Git commit done")

    result = subprocess.run(['git', 'push'], capture_output=True, text=True, timeout=120)
    if result.returncode == 0:
        log("Git push done")
    else:
        log(f"Git push failed: {result.stderr[:200]}")

# ── main ─────────────────────────────────────────────────────────────────────

def main():
    log("=" * 60)
    log("竞品监控开始")
    log("=" * 60)

    all_apps = json.load(open(DB_PATH))
    existing_keys = set((a['platform'], a['pkg_or_id']) for a in all_apps)
    log(f"数据库: {len(all_apps)} apps, {len(set(a['company_cn'] for a in all_apps))} companies")

    # Step 1: iOS new apps
    new_ios = check_ios_developers(all_apps)

    # Step 2: GP new apps
    new_gp = check_gp_developers(all_apps)

    # Add new apps
    added = 0
    added_apps = []
    affected = set(GP_DEVELOPER_SCAN_STATS.get('affected_companies', []))
    for app in new_ios + new_gp:
        key = (app['platform'], app['pkg_or_id'])
        if key not in existing_keys:
            all_apps.append(app)
            existing_keys.add(key)
            added += 1
            added_apps.append(app)
            affected.add(app['company_cn'])

    # Step 3: iOS update check
    updates = check_ios_updates(all_apps)
    for u in updates:
        affected.add(u['company'])

    # Step 3b: GP metrics check
    metric_updates = check_gp_metrics_updates(all_apps)
    for u in metric_updates:
        affected.add(u['company'])

    # Save database
    with open(DB_PATH, 'w') as f:
        json.dump(all_apps, f, ensure_ascii=False, indent=2)

    # Step 4: Regenerate files
    if affected:
        regenerate_files(all_apps, affected)

    # Step 5: Git
    if added > 0 or updates or metric_updates:
        git_commit_push(added, len(updates) + len(metric_updates))

    # Report
    log("")
    log("=" * 60)
    log("监控报告")
    log("=" * 60)
    added_ios = [a for a in added_apps if a['platform'] == 'iOS']
    added_gp = [a for a in added_apps if a['platform'] == 'GP']
    log(f"新产品: {added} ({len(added_ios)} iOS + {len(added_gp)} GP)")

    if added_ios:
        log("  iOS 新产品:")
        for a in added_ios:
            log(f"    {a['company_cn']}: {a['name']} (id={a['pkg_or_id']})")
    if added_gp:
        log("  GP 新产品:")
        for a in added_gp:
            log(f"    {a['company_cn']}: {a['name']} (pkg={a['pkg_or_id']})")

    log(f"产品更新: {len(updates)}")
    if updates:
        for u in updates:
            changes = []
            if 'new_update' in u:
                changes.append(f"更新日期 {u['old_update'] or '-'} -> {u['new_update']}")
            if 'new_release' in u:
                changes.append(f"上架日期 {u['old_release']} -> {u['new_release']}")
            log(f"    {u['company']}: {u['name']} ({'; '.join(changes)})")

    log(f"指标更新: {len(metric_updates)}")
    if metric_updates:
        for u in metric_updates[:80]:
            parts = []
            changes = u.get('changes', {})
            if 'downloads' in changes:
                old, new = changes['downloads']
                parts.append(f"downloads {old or '-'} -> {new}")
            if 'rating_count' in changes:
                old, new = changes['rating_count']
                parts.append(f"rating_count {old or 0} -> {new}")
            log(f"    {u['company']}: {u['name']} ({'; '.join(parts)})")
        if len(metric_updates) > 80:
            log(f"    ... {len(metric_updates) - 80} more metric updates")

    log(f"数据库总计: {len(all_apps)} apps")
    log(f"受影响公司: {', '.join(affected) if affected else '无'}")
    log("=" * 60)

    # Save report
    report_path = os.path.join(BASE_DIR, f'monitor_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(REPORT_LINES))
    log(f"报告已保存: {report_path}")

if __name__ == '__main__':
    main()
