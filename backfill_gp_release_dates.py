#!/usr/bin/env python3
"""Backfill Google Play release_date by opening the About panel."""
import json
import os
import re
import time
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

import monitor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_LINES = []


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    REPORT_LINES.append(line)


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


def make_driver():
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


def fetch_release_date(driver, pkg):
    driver.get(f'https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=us')
    time.sleep(1.5)
    monitor.open_gp_about_panel(driver)
    return monitor.normalize_date(monitor.extract_gp_detail_value(driver, 'Released on'))


def main():
    apps = load_apps()
    missing = [a for a in apps if a.get('platform') == 'GP' and not a.get('release_date') and not a.get('removed')]
    log(f'Missing GP release_date: {len(missing)}')

    updated = 0
    failed = []
    affected = set()
    driver = make_driver()
    try:
        for idx, app in enumerate(missing, 1):
            pkg = app['pkg_or_id']
            try:
                date = fetch_release_date(driver, pkg)
            except Exception as e:
                failed.append((app['company_cn'], pkg, str(e)[:120]))
                log(f'[{idx}/{len(missing)}] FAIL {app["company_cn"]} {pkg}: {str(e)[:80]}')
                if len(failed) % 20 == 0:
                    driver.quit()
                    driver = make_driver()
                continue

            if date:
                app['release_date'] = date
                updated += 1
                affected.add(app['company_cn'])
                log(f'[{idx}/{len(missing)}] OK {app["company_cn"]} {pkg} -> {date}')
            else:
                failed.append((app['company_cn'], pkg, 'Released on not shown'))
                log(f'[{idx}/{len(missing)}] EMPTY {app["company_cn"]} {pkg}')

            if idx % 50 == 0:
                log(f'Progress {idx}/{len(missing)} updated={updated} failed={len(failed)}')
    finally:
        driver.quit()

    if affected:
        monitor.regenerate_files(apps, affected)
        with open('/tmp/all_apps_v6.json', 'w', encoding='utf-8') as f:
            json.dump(apps, f, ensure_ascii=False, indent=2)

    log('=' * 60)
    log(f'Updated: {updated}')
    log(f'Failed/empty: {len(failed)}')
    for item in failed[:200]:
        log(f'  {item}')

    report_path = os.path.join(BASE_DIR, f'backfill_gp_release_dates_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.txt')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(REPORT_LINES))
    log(f'Report saved: {report_path}')


if __name__ == '__main__':
    main()
