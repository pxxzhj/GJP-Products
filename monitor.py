#!/usr/bin/env python3
"""
竞品监控脚本 - 检查所有开发者是否有新产品，已有产品是否有更新。
手动运行: python3 monitor.py
"""
import json
import os
import re
import time
import subprocess
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = '/tmp/all_apps_v6.json'
REPORT_LINES = []

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
                    'last_update': normalize_date(str(r.get('currentVersionReleaseDate', ''))[:10]),
                    'tags': ', '.join(r.get('genres', [])),
                    'removed': False,
                    'release_date': normalize_date(str(r.get('releaseDate', ''))[:10]),
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

def extract_gp_developers(all_apps):
    """Extract unique GP developer URLs from the database."""
    devs = {}
    for a in all_apps:
        if a['platform'] != 'GP':
            continue
        dev_link = a.get('dev_link', '')
        if not dev_link or 'play.google.com' not in dev_link:
            continue
        if dev_link not in devs:
            devs[dev_link] = {
                'url': dev_link,
                'company': a['company_cn'],
                'developer': a.get('developer', ''),
                'known_pkgs': set(),
            }
        devs[dev_link]['known_pkgs'].add(a['pkg_or_id'])
    return devs

def check_gp_developers(all_apps):
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options

    devs = extract_gp_developers(all_apps)
    log(f"GP: checking {len(devs)} developers...")

    opts = Options()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--lang=en-US')
    opts.add_argument('--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(30)

    new_gp_pkgs = {}  # pkg -> {company, dev_url}
    checked = 0
    errors = 0

    for dev_url, info in devs.items():
        checked += 1
        try:
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

            missing = found_pkgs - info['known_pkgs']
            for pkg in missing:
                if pkg not in new_gp_pkgs:
                    new_gp_pkgs[pkg] = {'company': info['company'], 'dev_url': dev_url}
                    log(f"  NEW GP pkg: {pkg} -> {info['company']}")

            if checked % 10 == 0:
                log(f"  GP progress: {checked}/{len(devs)} (new: {len(new_gp_pkgs)})")

            time.sleep(3)

        except Exception as e:
            errors += 1
            log(f"  GP ERROR [{checked}/{len(devs)}] {dev_url[:60]}: {str(e)[:80]}")
            time.sleep(2)

    # Deduplicate against existing DB
    existing_pkgs = set(a['pkg_or_id'] for a in all_apps if a['platform'] == 'GP')
    truly_new = {p: v for p, v in new_gp_pkgs.items() if p not in existing_pkgs}

    if not truly_new:
        driver.quit()
        log(f"GP check done: 0 new apps (checked {checked}, errors {errors})")
        return []

    log(f"GP: fetching details for {len(truly_new)} new apps...")
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

            downloads = ''
            try:
                body = driver.page_source
                m = re.search(r'([\d,.]+[KMB]?\+?)\s*Downloads', body)
                if m:
                    downloads = m.group(1).strip()
            except:
                pass

            developer = ''
            try:
                developer = driver.find_element(By.CSS_SELECTOR, 'div.Vbfug a span').text.strip()
            except:
                pass

            last_update = ''
            try:
                body = driver.page_source
                m = re.search(r'Updated on\s*</div>.*?<div[^>]*>(.*?)</div>', body, re.S)
                if m:
                    last_update = normalize_date(m.group(1).strip())
            except:
                pass

            removed = False
            try:
                if "We're sorry" in driver.page_source or "not found" in driver.title.lower():
                    removed = True
            except:
                pass

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
                'rating_count': 0,
                'last_update': last_update,
                'tags': '',
                'removed': removed,
                'release_date': '',
            }
            new_gp_apps.append(app)
            log(f"  Fetched: {name} ({pkg}) dl={downloads}")
            time.sleep(2)

        except Exception as e:
            log(f"  Fetch ERROR: {pkg} - {str(e)[:80]}")
            new_gp_apps.append({
                'name': pkg, 'company_cn': info['company'], 'icon': '', 'platform': 'GP',
                'pkg_or_id': pkg, 'store_link': url, 'dev_link': info['dev_url'],
                'developer': '', 'downloads': '', 'rating_count': 0,
                'last_update': '', 'tags': '', 'removed': True, 'release_date': '',
            })

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
            new_update = normalize_date(str(r.get('currentVersionReleaseDate', ''))[:10])
            old_update = a.get('last_update', '')
            if new_update and new_update != old_update and new_update > old_update:
                updates.append({
                    'pkg_or_id': a['pkg_or_id'],
                    'name': a['name'],
                    'company': a['company_cn'],
                    'old_update': old_update,
                    'new_update': new_update,
                })
                a['last_update'] = new_update

            new_rc = r.get('userRatingCount', 0)
            if isinstance(new_rc, int) and new_rc > 0:
                a['rating_count'] = new_rc

        log(f"  iOS updates batch {i // batch_size + 1}: checked {min(i + batch_size, len(ios_apps))}/{len(ios_apps)}")
        time.sleep(1)

    log(f"iOS updates done: {len(updates)} apps updated")
    return updates

# ── Step 4: regenerate files ─────────────────────────────────────────────────

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

# ── Step 5: git commit + push ────────────────────────────────────────────────

def git_commit_push(new_count, update_count):
    os.chdir(BASE_DIR)
    subprocess.run(['git', 'add', 'data/', 'index.html'], check=True)

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
    affected = set()
    for app in new_ios + new_gp:
        key = (app['platform'], app['pkg_or_id'])
        if key not in existing_keys:
            all_apps.append(app)
            existing_keys.add(key)
            added += 1
            affected.add(app['company_cn'])

    # Step 3: iOS update check
    updates = check_ios_updates(all_apps)
    for u in updates:
        affected.add(u['company'])

    # Save database
    with open(DB_PATH, 'w') as f:
        json.dump(all_apps, f, ensure_ascii=False, indent=2)

    # Step 4: Regenerate files
    if affected:
        regenerate_files(all_apps, affected)

    # Step 5: Git
    if added > 0 or updates:
        git_commit_push(added, len(updates))

    # Report
    log("")
    log("=" * 60)
    log("监控报告")
    log("=" * 60)
    log(f"新产品: {added} ({len(new_ios)} iOS + {len(new_gp)} GP)")

    if new_ios:
        log("  iOS 新产品:")
        for a in new_ios:
            log(f"    {a['company_cn']}: {a['name']} (id={a['pkg_or_id']})")
    if new_gp:
        log("  GP 新产品:")
        for a in new_gp:
            log(f"    {a['company_cn']}: {a['name']} (pkg={a['pkg_or_id']})")

    log(f"产品更新: {len(updates)}")
    if updates:
        for u in updates:
            log(f"    {u['company']}: {u['name']} ({u['old_update']} -> {u['new_update']})")

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
