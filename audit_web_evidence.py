#!/usr/bin/env python3
"""Audit official web/privacy/support evidence for the product library.

This script intentionally writes private evidence outside the repo by default.
It does not mutate data/*.js or index.html. The output is meant for review before
any newly discovered app/developer leads are imported.
"""
import argparse
import csv
import hashlib
import html
import json
import os
import queue
import re
import threading
import time
import urllib.parse
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime

from audit_web_fetcher import fetch_url, itunes_lookup_batch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRIVATE_BASE = os.environ.get(
    'GPL_PRIVATE_EVIDENCE_DIR',
    os.path.abspath(os.path.join(BASE_DIR, '..', 'product_library_private_evidence')),
)

COMMON_EMAIL_DOMAINS = {
    'gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com', 'live.com',
    'msn.com', 'icloud.com', 'me.com', 'mac.com', 'yahoo.com', 'ymail.com',
    'proton.me', 'protonmail.com', 'aol.com', 'qq.com', 'foxmail.com',
    '163.com', '126.com', 'yeah.net', 'sina.com', 'sohu.com', '139.com',
    'gmail.co', 'mail.com', 'gmx.com', 'gmx.net', 'naver.com', 'daum.net',
    'yandex.com', 'yandex.ru',
}

NOISE_DOMAINS = {
    'apple.com', 'apps.apple.com', 'mzstatic.com', 'itunes.apple.com',
    'play.google.com', 'googleusercontent.com', 'gstatic.com', 'google.com',
    'youtube.com', 'youtu.be', 'ytimg.com', 'facebook.com', 'instagram.com',
    'twitter.com', 'x.com', 'tiktok.com', 'linkedin.com', 'pinterest.com',
    'w3.org', 'schema.org', 'cloudflare.com', 'doubleclick.net',
    'googletagmanager.com', 'google-analytics.com', 'fonts.googleapis.com',
    'fonts.gstatic.com', 'microsoft.com', 'samsung.com', 'paypal.com',
    'intl.paypal.com', 'mailchimp.com', 'zapier.com', 'sentry.io', 'vk.com',
    'tumblr.com', 'wix.com', 'wixpress.com', 'wix-domains.com',
    'wixanswers.com', 'wixforms.com', 'wixinvoices.com', 'readymag.com',
    'shopline.com', 'myshopline.com', 'appointlet.com', 'glueup.com',
    'freshdesk.com', 'zendesk.com', 'helpshift.com', 'appsflyer.com',
    'applovin.com', 'ironsrc.com', 'unity.com', 'unity3d.com',
    'digitalturbine.com', 'getui.com', 'umeng.com', 'auth0.com',
    'feishu.cn', 'latisglobal.com', 'lightspeedhq.com', 'mailchimp.com',
    'printify.com', 'samsung.com.cn', 'vk-portal.net', 'zoom.com', 'zoom.us',
}

PLACEHOLDER_DOMAINS = {
    'acme.com', 'acmecorp.com', 'bigcorp.com', 'business.com', 'client.com',
    'company.com', 'company.site', 'companyemail.com', 'contoso.app',
    'doe.com', 'email.com', 'example.com', 'mail.io', 'mybusiness.org',
    'mydomain.com', 'myemail.com', 'mysite.com', 'sample.dev',
    'test-account.dev', 'test.com', 'yourcompany.com', 'yourdomain.com',
    'yourshop.com', 'xx.com',
}

PUBLIC_HOSTING_DOMAINS = {
    'sites.google.com', 'docs.google.com', 'forms.gle', 'github.io',
    'github.com', 'notion.site', 'notion.so', 'wixsite.com', 'wordpress.com',
    'blogspot.com', 'firebaseapp.com', 'web.app', 'weebly.com',
}

MULTI_PART_SUFFIXES = {
    'com.cn', 'net.cn', 'org.cn', 'com.hk', 'com.tw', 'com.au', 'co.uk',
    'co.jp', 'co.kr', 'com.br', 'com.sg', 'com.tr', 'com.vn', 'co.in',
}

RELEVANT_PATH_WORDS = {
    'app', 'apps', 'game', 'games', 'privacy', 'policy', 'support', 'contact',
    'about', 'product', 'products', 'portfolio', 'studio', 'developer',
    'publish', 'publishing', 'terms', 'service', 'help', 'home',
}

STATIC_EXTENSIONS = {
    '.css', '.js', '.mjs', '.map', '.png', '.jpg', '.jpeg', '.gif', '.webp',
    '.svg', '.ico', '.woff', '.woff2', '.ttf', '.otf', '.eot', '.mp4', '.mov',
    '.webm', '.mp3', '.wav', '.zip', '.gz', '.br', '.pdf',
}

VALID_TLDS = {
    'ac', 'ad', 'ae', 'ai', 'am', 'app', 'asia', 'at', 'au', 'be', 'biz',
    'br', 'ca', 'cc', 'ch', 'click', 'club', 'cn', 'co', 'com', 'company',
    'de', 'dev', 'digital', 'email', 'es', 'eu', 'fi', 'fr', 'fun', 'game',
    'games', 'global', 'hk', 'id', 'ie', 'in', 'inc', 'info', 'io', 'jp',
    'kr', 'la', 'life', 'limited', 'live', 'ltd', 'me', 'mobi', 'net',
    'network', 'nl', 'online', 'org', 'pro', 'ru', 'sg', 'site', 'studio',
    'support', 'tech', 'technology', 'top', 'tr', 'tv', 'tw', 'uk', 'us',
    'vip', 'vn', 'wang', 'website', 'world', 'xyz',
}

INVALID_TLDS = {
    'js', 'css', 'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'json', 'xml',
    'txt', 'map', 'html', 'htm', 'php', 'asp', 'aspx',
}


class SafeWriter:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.fp = open(path, 'a', encoding='utf-8')

    def write_json(self, item):
        with self.lock:
            self.fp.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + '\n')
            self.fp.flush()

    def close(self):
        with self.lock:
            self.fp.close()


def now_ts():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def clean_text(value):
    value = html.unescape(value or '')
    value = re.sub(r'<[^>]+>', ' ', value)
    value = re.sub(r'\s+', ' ', value)
    return value.strip()


def decode_escaped(value):
    if not value:
        return ''
    value = html.unescape(value)
    value = value.replace('\\/', '/')
    value = value.replace('\\u0040', '@').replace('\\U0040', '@')
    value = value.replace('\\x40', '@')
    return value


def hostname(url_or_host):
    if not url_or_host:
        return ''
    if '://' not in url_or_host:
        url_or_host = 'https://' + url_or_host
    try:
        host = urllib.parse.urlparse(url_or_host).hostname or ''
    except Exception:
        return ''
    host = host.lower().strip('.')
    if host.startswith('www.'):
        host = host[4:]
    return host


def registered_domain(host):
    host = hostname(host)
    if not host:
        return ''
    parts = host.split('.')
    if len(parts) <= 2:
        return host
    suffix2 = '.'.join(parts[-2:])
    suffix3 = '.'.join(parts[-3:])
    if suffix2 in {'com', 'net', 'org'}:
        return host
    if suffix2 in MULTI_PART_SUFFIXES:
        return '.'.join(parts[-3:])
    if '.'.join(parts[-2:]) in PUBLIC_HOSTING_DOMAINS:
        return '.'.join(parts[-3:])
    if suffix3 in {'github.io'}:
        return suffix3
    return '.'.join(parts[-2:])


def is_common_email_domain(domain):
    domain = hostname(domain)
    reg = registered_domain(domain)
    return domain in COMMON_EMAIL_DOMAINS or reg in COMMON_EMAIL_DOMAINS


def is_public_hosting_domain(domain):
    domain = hostname(domain)
    reg = registered_domain(domain)
    return domain in PUBLIC_HOSTING_DOMAINS or reg in PUBLIC_HOSTING_DOMAINS


def valid_domain(domain):
    domain = hostname(domain)
    if not domain or '.' not in domain:
        return False
    if not re.match(r'^[a-z0-9.-]+$', domain):
        return False
    if '..' in domain:
        return False
    labels = domain.split('.')
    if any(not label or label.startswith('-') or label.endswith('-') for label in labels):
        return False
    tld = labels[-1]
    if tld in INVALID_TLDS:
        return False
    if all(label.isdigit() for label in labels[:-1]):
        return False
    return tld in VALID_TLDS or (len(tld) == 2 and tld.isalpha())


def is_noise_url(url):
    host = hostname(url)
    if not host:
        return True
    if not valid_domain(host):
        return True
    reg = registered_domain(host)
    if host in {'apps.apple.com', 'play.google.com', 'itunes.apple.com'}:
        return False
    return host in NOISE_DOMAINS or reg in NOISE_DOMAINS or reg in PLACEHOLDER_DOMAINS


def normalize_url(raw, base_url=''):
    if not raw:
        return ''
    raw = decode_escaped(raw).strip()
    raw = raw.strip(' \t\r\n"\'()[]{}<>')
    raw = raw.replace('&amp;', '&')
    if not raw or raw.startswith(('#', 'javascript:', 'tel:', 'sms:')):
        return ''
    if raw.startswith('mailto:'):
        return raw
    if raw.startswith('//'):
        raw = 'https:' + raw
    elif base_url and not re.match(r'^[a-zA-Z][a-zA-Z0-9+\-.]*:', raw):
        raw = urllib.parse.urljoin(base_url, raw)
    if not raw.startswith(('http://', 'https://')):
        return ''
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError:
        return ''
    if not parsed.hostname:
        return ''
    clean = parsed._replace(fragment='').geturl()
    return clean.rstrip('/')


def has_static_extension(url):
    path = urllib.parse.urlparse(url).path.lower()
    _, ext = os.path.splitext(path)
    return ext in STATIC_EXTENSIONS


def url_sort_key(url):
    parsed = urllib.parse.urlparse(url)
    return (hostname(url), parsed.path, parsed.query)


def extract_title(text):
    m = re.search(r'<title[^>]*>(.*?)</title>', text or '', re.I | re.S)
    return clean_text(m.group(1))[:180] if m else ''


def extract_emails(text):
    text = decode_escaped(text or '')
    candidates = set(re.findall(
        r'(?<![A-Za-z0-9._%+\-])([A-Za-z0-9._%+\-]{1,80}@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24})(?![A-Za-z0-9._%+\-])',
        text,
    ))
    cleaned = set()
    for email in candidates:
        email = email.strip('.-_%+').lower()
        if not email or '@' not in email:
            continue
        local, domain = email.rsplit('@', 1)
        if not local or not domain:
            continue
        domain = domain.lower().strip('.')
        if not valid_domain(domain):
            continue
        if domain.endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg')):
            continue
        if domain in {'example.com', 'email.com', 'domain.com'}:
            continue
        cleaned.add(f'{local}@{domain}')
    return sorted(cleaned)


def extract_urls(text, base_url=''):
    text = decode_escaped(text or '')
    values = []
    attr_pattern = r'''(?:href|src|action|data-href|url)=["']([^"']+)["']'''
    values.extend(re.findall(attr_pattern, text, re.I))
    values.extend(re.findall(r'https?://[^\s"\'<>\\]+', text, re.I))

    urls = set()
    for raw in values:
        url = normalize_url(raw, base_url)
        if not url:
            continue
        if url.startswith('mailto:'):
            urls.add(url)
            continue
        if len(url) > 500:
            continue
        urls.add(url)
    return sorted(urls, key=url_sort_key)


def parse_query_id(url):
    try:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        if 'id' in qs and qs['id']:
            return qs['id'][0]
    except Exception:
        pass
    return ''


def extract_store_refs(text, urls=None):
    text = decode_escaped(text or '')
    urls = list(urls or []) + extract_urls(text)
    gp_packages = set()
    ios_ids = set()
    gp_developers = set()
    ios_developers = set()
    appmagic_urls = set()

    for url in urls:
        parsed = urllib.parse.urlparse(url)
        host = hostname(url)
        if host == 'play.google.com':
            if parsed.path.endswith('/store/apps/details') or '/store/apps/details' in parsed.path:
                pkg = parse_query_id(url)
                if re.match(r'^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$', pkg or ''):
                    gp_packages.add(pkg)
            if '/store/apps/dev' in parsed.path or '/store/apps/developer' in parsed.path:
                dev_id = parse_query_id(url)
                if dev_id:
                    gp_developers.add(url)
        if host in {'apps.apple.com', 'itunes.apple.com'}:
            if '/developer/' in parsed.path:
                for did in re.findall(r'/developer/[^/?#]+/id(\d{6,12})(?:[/?#]|$)', url):
                    ios_developers.add(did)
            else:
                for aid in re.findall(r'/id(\d{7,12})(?:[/?#]|$)', url):
                    ios_ids.add(aid)
        if 'appmagic.rocks' in host:
            appmagic_urls.add(url)

    for pkg in re.findall(r'market://details\?id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)', text):
        gp_packages.add(pkg)
    for pkg in re.findall(r'(?:package|pkg|applicationId|bundleId)["\':=\s]+([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+){2,})', text):
        gp_packages.add(pkg)
    for aid in re.findall(r'apps\.apple\.com/[^\s"\'<>]+/id(\d{7,12})', text):
        ios_ids.add(aid)

    return {
        'gp_packages': sorted(gp_packages),
        'ios_ids': sorted(ios_ids),
        'gp_developers': sorted(gp_developers),
        'ios_developers': sorted(ios_developers),
        'appmagic_urls': sorted(appmagic_urls),
    }


def extract_gp_contact_section(text):
    text = decode_escaped(text or '')
    marker = 'id="developer-contacts"'
    idx = text.find(marker)
    if idx < 0:
        return ''
    end = text.find('</section>', idx)
    return text[idx:end if end > idx else idx + 60000]


def extract_gp_contact_urls(text):
    section = extract_gp_contact_section(text)
    if not section:
        return []
    urls = []
    for raw in re.findall(r'''href=["']([^"']+)["']''', section, re.I):
        url = normalize_url(raw)
        if not url:
            continue
        if url.startswith('mailto:') or not is_noise_url(url):
            urls.append(url)
    return sorted(set(urls), key=url_sort_key)


def extract_ios_official_urls(text, lookup_row):
    """Extract only official App Store external links, not recommendations."""
    text = decode_escaped(text or '')
    urls = set()
    if lookup_row.get('sellerUrl'):
        url = normalize_url(lookup_row.get('sellerUrl'))
        if url:
            urls.add(url)

    for label in ('Developer Website', 'Privacy Policy', 'App Support'):
        pattern = (
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>[^<]*(?:<span[^>]*>)?'
            + re.escape(label)
        )
        for raw in re.findall(pattern, text, re.I | re.S):
            url = normalize_url(raw)
            if url and not is_noise_url(url):
                urls.add(url)

    for key in ('supportAction', 'privacyPolicyUrl', 'privacyPolicyURL', 'websiteURL', 'sellerUrl'):
        idx = text.find(f'"{key}"')
        if idx < 0:
            continue
        snippet = text[idx:idx + 1500]
        for raw in re.findall(r'"url"\s*:\s*"([^"]+)"', snippet):
            url = normalize_url(raw)
            if url and not is_noise_url(url):
                urls.add(url)

    for m in re.finditer(r'(Developer Website|Privacy Policy|App Support)', text, re.I):
        snippet = text[max(0, m.start() - 900):m.end() + 900]
        for raw in re.findall(r'''href=["']([^"']+)["']''', snippet, re.I):
            url = normalize_url(raw)
            if url and not is_noise_url(url):
                urls.add(url)

    return sorted(urls, key=url_sort_key)


def classify_source_url(url):
    low = url.lower()
    if low.startswith('mailto:'):
        return 'email'
    if 'privacy' in low or 'policy' in low:
        return 'privacy'
    if 'support' in low or 'help' in low:
        return 'support'
    if hostname(url) in {'apps.apple.com', 'play.google.com'}:
        return 'store'
    return 'website'


def relevant_same_domain_link(url, root_domain):
    if registered_domain(url) != root_domain:
        return False
    if has_static_extension(url):
        return False
    path = urllib.parse.urlparse(url).path.lower()
    if path in {'', '/'}:
        return True
    words = set(re.findall(r'[a-z0-9]+', path))
    if words & RELEVANT_PATH_WORDS:
        return True
    if path.count('/') <= 1 and len(path) <= 40:
        return True
    return False


def load_apps(include_companies=None, exclude_companies=None, exclude_prefixes=None):
    include_companies = set(include_companies or [])
    exclude_companies = set(exclude_companies or [])
    exclude_prefixes = tuple(exclude_prefixes or [])
    apps = []
    data_dir = os.path.join(BASE_DIR, 'data')
    for filename in sorted(os.listdir(data_dir)):
        if not filename.endswith('.js'):
            continue
        path = os.path.join(data_dir, filename)
        with open(path, encoding='utf-8') as f:
            content = f.read()
        m = re.match(r'window\._loadCompany\("(.*?)",\s*(\[.*\])\);\s*$', content, re.S)
        if not m:
            raise ValueError(f'Cannot parse {filename}')
        company_apps = json.loads(m.group(2))
        company = m.group(1)
        if include_companies and company not in include_companies:
            continue
        if company in exclude_companies:
            continue
        if exclude_prefixes and company.startswith(exclude_prefixes):
            continue
        apps.extend(company_apps)
    return apps


def build_domain_seed_paths(domain):
    return [f'https://{domain}']


def app_context(app):
    return {
        'company': app.get('company_cn', ''),
        'app_name': app.get('name', ''),
        'platform': app.get('platform', ''),
        'pkg_or_id': app.get('pkg_or_id', ''),
        'developer': app.get('developer', ''),
    }


def summarize_counter(counter, limit=20):
    return [{'value': value, 'count': count} for value, count in counter.most_common(limit)]


class EvidenceAuditor:
    def __init__(self, args):
        self.args = args
        self.apps = load_apps(
            include_companies=args.include_company,
            exclude_companies=args.exclude_company,
            exclude_prefixes=args.exclude_company_prefix,
        )
        if args.sample:
            self.apps = self.apps[:args.sample]

        self.run_id = datetime.now().strftime('%Y%m%d_%H%M%S')
        os.makedirs(PRIVATE_BASE, exist_ok=True)
        self.run_dir = os.path.join(PRIVATE_BASE, self.run_id)
        os.makedirs(self.run_dir, exist_ok=True)

        self.page_writer = SafeWriter(os.path.join(self.run_dir, 'page_evidence.jsonl'))
        self.lock = threading.Lock()
        self.existing_gp = {a.get('pkg_or_id') for a in self.apps if a.get('platform') == 'GP'}
        self.existing_ios = {a.get('pkg_or_id') for a in self.apps if a.get('platform') == 'iOS'}
        self.known_store_keys = set()
        self.email_rows = []
        self.page_rows = []
        self.store_rows = []
        self.new_leads = []
        self.known_matches = []
        self.domain_contexts = defaultdict(set)
        self.queued_site_urls = set()
        self.visited_site_urls = set()
        self.queued_store_apps = set()
        self.processed_store_apps = set()
        self.domain_page_counts = Counter()
        self.stats = Counter()
        self.site_queue = queue.Queue()
        self.start_time = time.time()
        companies = {app.get('company_cn', '') for app in self.apps}
        self.log(f'Audit scope: {len(companies)} companies, {len(self.apps)} apps')

    def log(self, message):
        elapsed = int(time.time() - self.start_time)
        print(f'[{datetime.now().strftime("%H:%M:%S")} +{elapsed}s] {message}', flush=True)

    def add_email_rows(self, emails, source, context, same_page_refs):
        rows = []
        for email in emails:
            domain = email.rsplit('@', 1)[1].lower()
            row = {
                'discovered_at': now_ts(),
                'run_id': self.run_id,
                'email': email,
                'email_domain': domain,
                'registered_domain': registered_domain(domain),
                'is_common_public_domain': is_common_email_domain(domain),
                'is_public_hosting_domain': is_public_hosting_domain(domain),
                'source_url': source.get('source_url') or source.get('url') or '',
                'final_url': source.get('final_url') or '',
                'source_kind': source.get('source_kind') or '',
                'page_title': source.get('title') or '',
                'company': context.get('company', ''),
                'app_name': context.get('app_name', ''),
                'platform': context.get('platform', ''),
                'pkg_or_id': context.get('pkg_or_id', ''),
                'developer': context.get('developer', ''),
                'same_page_gp_packages': ';'.join(same_page_refs.get('gp_packages', [])),
                'same_page_ios_ids': ';'.join(same_page_refs.get('ios_ids', [])),
                'same_page_appmagic_urls': ';'.join(same_page_refs.get('appmagic_urls', [])),
            }
            rows.append(row)
        if rows:
            with self.lock:
                self.email_rows.extend(rows)
                self.stats['emails_found_total'] += len(rows)
        return rows

    def add_new_leads_from_refs(self, refs, source, context):
        leads = []
        for pkg in refs.get('gp_packages', []):
            key = ('GP', pkg)
            if pkg in self.existing_gp:
                self.known_matches.append({
                    'platform': 'GP', 'pkg_or_id': pkg, 'source_url': source.get('final_url') or source.get('url', ''),
                    'company': context.get('company', ''), 'kind': 'known_app',
                })
                continue
            leads.append({
                'type': 'gp_app', 'platform': 'GP', 'id': pkg,
                'store_link': f'https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=us',
                'source_url': source.get('final_url') or source.get('url', ''),
                'source_kind': source.get('source_kind', ''),
                'source_company': context.get('company', ''),
                'source_app': context.get('app_name', ''),
            })
            if len(self.queued_store_apps) < self.args.max_discovered_store_apps:
                self.queue_store_app('GP', pkg)
        for aid in refs.get('ios_ids', []):
            if aid in self.existing_ios:
                self.known_matches.append({
                    'platform': 'iOS', 'pkg_or_id': aid, 'source_url': source.get('final_url') or source.get('url', ''),
                    'company': context.get('company', ''), 'kind': 'known_app',
                })
                continue
            leads.append({
                'type': 'ios_app', 'platform': 'iOS', 'id': aid,
                'store_link': f'https://apps.apple.com/app/id{aid}',
                'source_url': source.get('final_url') or source.get('url', ''),
                'source_kind': source.get('source_kind', ''),
                'source_company': context.get('company', ''),
                'source_app': context.get('app_name', ''),
            })
            if len(self.queued_store_apps) < self.args.max_discovered_store_apps:
                self.queue_store_app('iOS', aid)
        for url in refs.get('gp_developers', []):
            leads.append({
                'type': 'gp_developer', 'platform': 'GP', 'id': parse_query_id(url) or url,
                'store_link': url,
                'source_url': source.get('final_url') or source.get('url', ''),
                'source_kind': source.get('source_kind', ''),
                'source_company': context.get('company', ''),
                'source_app': context.get('app_name', ''),
            })
        for did in refs.get('ios_developers', []):
            leads.append({
                'type': 'ios_developer', 'platform': 'iOS', 'id': did,
                'store_link': f'https://apps.apple.com/developer/id{did}',
                'source_url': source.get('final_url') or source.get('url', ''),
                'source_kind': source.get('source_kind', ''),
                'source_company': context.get('company', ''),
                'source_app': context.get('app_name', ''),
            })
        for url in refs.get('appmagic_urls', []):
            leads.append({
                'type': 'appmagic_url', 'platform': '', 'id': hashlib.sha1(url.encode()).hexdigest()[:12],
                'store_link': url,
                'source_url': source.get('final_url') or source.get('url', ''),
                'source_kind': source.get('source_kind', ''),
                'source_company': context.get('company', ''),
                'source_app': context.get('app_name', ''),
            })

        if leads:
            with self.lock:
                seen = {(l.get('type'), l.get('id'), l.get('store_link')) for l in self.new_leads}
                for lead in leads:
                    key = (lead.get('type'), lead.get('id'), lead.get('store_link'))
                    if key not in seen:
                        self.new_leads.append(lead)
                        seen.add(key)
                        self.stats['new_leads'] += 1

    def queue_site_url(self, url, context, source_kind='website'):
        url = normalize_url(url)
        if not url or url.startswith('mailto:'):
            return False
        host = hostname(url)
        if not host:
            return False
        if host in {'apps.apple.com', 'itunes.apple.com', 'play.google.com'}:
            return False
        if is_noise_url(url):
            return False
        if has_static_extension(url):
            return False
        root_domain = registered_domain(host)
        if self.domain_page_counts[root_domain] >= self.args.max_pages_per_domain:
            return False
        with self.lock:
            if url in self.queued_site_urls or url in self.visited_site_urls:
                return False
            self.queued_site_urls.add(url)
            if context.get('company'):
                self.domain_contexts[root_domain].add(context.get('company'))
            self.site_queue.put((url, context, source_kind))
            self.stats['site_urls_queued'] += 1
            return True

    def queue_domain_seed(self, domain, context, source_kind='email_domain'):
        domain = hostname(domain)
        if not domain or is_common_email_domain(domain) or is_public_hosting_domain(domain):
            return 0
        if not valid_domain(domain):
            return 0
        if registered_domain(domain) in {registered_domain(d) for d in NOISE_DOMAINS}:
            return 0
        added = 0
        for url in build_domain_seed_paths(domain):
            if self.queue_site_url(url, context, source_kind):
                added += 1
        return added

    def queue_store_app(self, platform, app_id):
        key = (platform, app_id)
        with self.lock:
            if key in self.queued_store_apps or key in self.processed_store_apps:
                return False
            self.queued_store_apps.add(key)
            return True

    def process_gp_app(self, app):
        pkg = app.get('pkg_or_id')
        url = f'https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=us'
        context = app_context(app)
        result = fetch_url(url, timeout=self.args.timeout)
        text = result.get('text') or ''
        contact_text = extract_gp_contact_section(text)
        contact_urls = extract_gp_contact_urls(text)
        emails = extract_emails(contact_text)
        # Play Store pages include recommendation shelves and unrelated apps.
        # Only official contact URLs should seed further discovery.
        refs = extract_store_refs('', contact_urls)
        title = extract_title(text)
        source = {
            'source_url': url,
            'final_url': result.get('final_url', ''),
            'source_kind': 'google_play',
            'title': title,
        }
        self.add_email_rows(emails, source, context, refs)
        self.add_new_leads_from_refs(refs, source, context)
        for contact_url in contact_urls:
            if contact_url.startswith('mailto:'):
                continue
            self.queue_site_url(contact_url, context, classify_source_url(contact_url))

        row = {
            **context,
            'source_kind': 'google_play',
            'source_url': url,
            'final_url': result.get('final_url', ''),
            'status': result.get('status', 0),
            'title': title,
            'contact_urls': contact_urls,
            'email_count': len(emails),
            'error': result.get('error', ''),
        }
        return row

    def process_ios_app(self, app, lookup):
        aid = app.get('pkg_or_id')
        context = app_context(app)
        row = lookup.get(aid, {})
        if row.get('trackViewUrl'):
            store_url = row['trackViewUrl']
        else:
            store_url = app.get('store_link') or f'https://apps.apple.com/app/id{aid}'

        result = fetch_url(store_url, timeout=self.args.timeout)
        text = result.get('text') or ''
        seed_urls = extract_ios_official_urls(text, row)
        # App Store product pages include recommendation shelves and review text.
        # Those are noisy for association, so only official external URLs seed crawl.
        emails = []
        refs = {
            'gp_packages': [],
            'ios_ids': [],
            'gp_developers': [],
            'ios_developers': [],
            'appmagic_urls': [],
        }
        title = extract_title(text)
        source = {
            'source_url': store_url,
            'final_url': result.get('final_url', ''),
            'source_kind': 'app_store',
            'title': title,
        }
        self.add_email_rows(emails, source, context, refs)
        self.add_new_leads_from_refs(refs, source, context)
        for seed_url in sorted(set(seed_urls), key=url_sort_key):
            self.queue_site_url(seed_url, context, classify_source_url(seed_url))

        return {
            **context,
            'source_kind': 'app_store',
            'source_url': store_url,
            'final_url': result.get('final_url', ''),
            'status': result.get('status', 0),
            'title': title,
            'lookup_track_name': row.get('trackName', ''),
            'lookup_artist_name': row.get('artistName', ''),
            'lookup_seller_name': row.get('sellerName', ''),
            'lookup_release_date': row.get('releaseDate', ''),
            'lookup_current_version_release_date': row.get('currentVersionReleaseDate', ''),
            'contact_urls': sorted(set(seed_urls), key=url_sort_key),
            'email_count': len(emails),
            'error': result.get('error', ''),
        }

    def process_discovered_store_app(self, platform, app_id):
        if platform == 'GP':
            app = {
                'platform': 'GP', 'pkg_or_id': app_id, 'name': app_id,
                'company_cn': '__DISCOVERED__', 'developer': '',
            }
            return self.process_gp_app(app)

        app = {
            'platform': 'iOS', 'pkg_or_id': app_id, 'name': app_id,
            'company_cn': '__DISCOVERED__', 'developer': '',
            'store_link': f'https://apps.apple.com/app/id{app_id}',
        }
        lookup = itunes_lookup_batch([app_id])
        return self.process_ios_app(app, lookup)

    def process_site_page(self, url, context, source_kind):
        result = fetch_url(url, timeout=self.args.timeout)
        final_url = result.get('final_url') or url
        text = result.get('text') or ''
        title = extract_title(text)
        urls = extract_urls(text, final_url)
        refs = extract_store_refs(text, urls)
        emails = extract_emails(text)
        source = {
            'source_url': url,
            'final_url': final_url,
            'source_kind': source_kind,
            'title': title,
        }
        self.add_email_rows(emails, source, context, refs)
        self.add_new_leads_from_refs(refs, source, context)

        page_record = {
            'run_id': self.run_id,
            'fetched_at': now_ts(),
            'source_url': url,
            'final_url': final_url,
            'source_kind': source_kind,
            'status': result.get('status', 0),
            'content_type': result.get('content_type', ''),
            'title': title,
            'company': context.get('company', ''),
            'app_name': context.get('app_name', ''),
            'platform': context.get('platform', ''),
            'pkg_or_id': context.get('pkg_or_id', ''),
            'email_count': len(emails),
            'email_domains': sorted({e.rsplit('@', 1)[1] for e in emails}),
            'gp_packages': refs.get('gp_packages', []),
            'ios_ids': refs.get('ios_ids', []),
            'appmagic_urls': refs.get('appmagic_urls', []),
            'out_url_count': len(urls),
            'error': result.get('error', ''),
        }
        self.page_writer.write_json(page_record)
        with self.lock:
            self.page_rows.append(page_record)
            self.visited_site_urls.add(url)
            self.stats['site_pages_fetched'] += 1
            if result.get('error'):
                self.stats['site_page_errors'] += 1
            root = registered_domain(final_url or url)
            self.domain_page_counts[root] += 1

        if self.args.expand_email_domains:
            for email in emails:
                domain = email.rsplit('@', 1)[1]
                self.queue_domain_seed(domain, context, 'email_domain')

        root = registered_domain(final_url or url)
        if not is_public_hosting_domain(root):
            for link in urls:
                if hostname(link) in {'apps.apple.com', 'play.google.com', 'itunes.apple.com'}:
                    continue
                if relevant_same_domain_link(link, root):
                    self.queue_site_url(link, context, 'same_domain_link')

        return page_record

    def record_site_error(self, url, context, source_kind, error):
        page_record = {
            'run_id': self.run_id,
            'fetched_at': now_ts(),
            'source_url': url,
            'final_url': '',
            'source_kind': source_kind,
            'status': 0,
            'content_type': '',
            'title': '',
            'company': context.get('company', ''),
            'app_name': context.get('app_name', ''),
            'platform': context.get('platform', ''),
            'pkg_or_id': context.get('pkg_or_id', ''),
            'email_count': 0,
            'email_domains': [],
            'gp_packages': [],
            'ios_ids': [],
            'appmagic_urls': [],
            'out_url_count': 0,
            'error': str(error),
        }
        self.page_writer.write_json(page_record)
        with self.lock:
            self.page_rows.append(page_record)
            self.visited_site_urls.add(url)
            self.stats['site_pages_fetched'] += 1
            self.stats['site_page_errors'] += 1
            self.domain_page_counts[registered_domain(url)] += 1
        return page_record

    def run_store_phase(self):
        gp_apps = [a for a in self.apps if a.get('platform') == 'GP' and not a.get('removed')]
        ios_apps = [a for a in self.apps if a.get('platform') == 'iOS' and not a.get('removed')]
        self.log(f'Loaded {len(self.apps)} apps ({len(gp_apps)} GP, {len(ios_apps)} iOS)')

        if not self.args.skip_gp:
            self.log(f'Fetching Google Play contact pages: {len(gp_apps)}')
            with ThreadPoolExecutor(max_workers=self.args.workers_store) as executor:
                futures = [executor.submit(self.process_gp_app, app) for app in gp_apps]
                for idx, future in enumerate(as_completed(futures), 1):
                    try:
                        row = future.result()
                        self.store_rows.append(row)
                    except Exception as e:
                        self.stats['gp_errors'] += 1
                        self.log(f'GP worker error: {e}')
                    if idx % 100 == 0:
                        self.log(f'GP progress {idx}/{len(gp_apps)} queued_sites={self.site_queue.qsize()} emails={len(self.email_rows)} leads={len(self.new_leads)}')

        if not self.args.skip_ios:
            self.log(f'Fetching iTunes lookup and App Store pages: {len(ios_apps)}')
            lookup = {}
            ids = [a.get('pkg_or_id') for a in ios_apps]
            for i in range(0, len(ids), 200):
                lookup.update(itunes_lookup_batch(ids[i:i + 200]))
                self.log(f'iTunes lookup {min(i + 200, len(ids))}/{len(ids)}')
                time.sleep(0.2)

            with ThreadPoolExecutor(max_workers=self.args.workers_store) as executor:
                futures = [executor.submit(self.process_ios_app, app, lookup) for app in ios_apps]
                for idx, future in enumerate(as_completed(futures), 1):
                    try:
                        row = future.result()
                        self.store_rows.append(row)
                    except Exception as e:
                        self.stats['ios_errors'] += 1
                        self.log(f'iOS worker error: {e}')
                    if idx % 100 == 0:
                        self.log(f'iOS progress {idx}/{len(ios_apps)} queued_sites={self.site_queue.qsize()} emails={len(self.email_rows)} leads={len(self.new_leads)}')

    def run_site_phase(self):
        self.log(f'Starting website/privacy/support crawl: queued={self.site_queue.qsize()}')
        idle_rounds = 0
        with ThreadPoolExecutor(max_workers=self.args.workers_site) as executor:
            futures = set()
            while True:
                while len(futures) < self.args.workers_site:
                    try:
                        url, context, source_kind = self.site_queue.get_nowait()
                    except queue.Empty:
                        break
                    if len(self.visited_site_urls) >= self.args.max_site_pages:
                        continue
                    future = executor.submit(self.process_site_page, url, context, source_kind)
                    futures.add(future)
                    future._audit_context = (url, context, source_kind)

                if not futures:
                    if self.site_queue.empty():
                        idle_rounds += 1
                        if idle_rounds >= 2:
                            break
                    time.sleep(0.5)
                    continue

                done, futures = wait(futures, timeout=max(self.args.timeout + 10, 30), return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    try:
                        future.result()
                    except Exception as e:
                        self.stats['site_worker_errors'] += 1
                        url, context, source_kind = getattr(future, '_audit_context', ('', {}, ''))
                        if url:
                            self.record_site_error(url, context, source_kind, e)
                        self.log(f'Site worker error: {e}')

                if self.stats['site_pages_fetched'] and self.stats['site_pages_fetched'] % 100 == 0:
                    self.log(
                        f'Site progress fetched={self.stats["site_pages_fetched"]} '
                        f'queued={self.site_queue.qsize()} emails={len(self.email_rows)} leads={len(self.new_leads)}'
                    )
                if len(self.visited_site_urls) >= self.args.max_site_pages:
                    self.log(f'Max site pages reached: {self.args.max_site_pages}')
                    break

    def run_discovered_store_phase(self):
        rounds = 0
        while True:
            pending = [key for key in list(self.queued_store_apps) if key not in self.processed_store_apps]
            if not pending or rounds >= self.args.max_discovered_rounds:
                break
            rounds += 1
            self.log(f'Fetching discovered store apps round {rounds}: {len(pending)}')
            with ThreadPoolExecutor(max_workers=max(1, min(self.args.workers_store, 6))) as executor:
                future_map = {
                    executor.submit(self.process_discovered_store_app, platform, app_id): (platform, app_id)
                    for platform, app_id in pending
                }
                for future in as_completed(future_map):
                    key = future_map[future]
                    try:
                        row = future.result()
                        self.store_rows.append(row)
                    except Exception as e:
                        self.stats['discovered_store_errors'] += 1
                        self.log(f'Discovered store worker error {key}: {e}')
                    finally:
                        with self.lock:
                            self.processed_store_apps.add(key)
            self.run_site_phase()

    def write_outputs(self):
        self.page_writer.close()

        emails_path = os.path.join(self.run_dir, 'private_emails.csv')
        fieldnames = [
            'discovered_at', 'run_id', 'email', 'email_domain', 'registered_domain',
            'is_common_public_domain', 'is_public_hosting_domain', 'source_url',
            'final_url', 'source_kind', 'page_title', 'company', 'app_name',
            'platform', 'pkg_or_id', 'developer', 'same_page_gp_packages',
            'same_page_ios_ids', 'same_page_appmagic_urls',
        ]
        with open(emails_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in self.email_rows:
                writer.writerow(row)

        store_path = os.path.join(self.run_dir, 'store_contact_pages.jsonl')
        with open(store_path, 'w', encoding='utf-8') as f:
            for row in self.store_rows:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')

        leads_path = os.path.join(self.run_dir, 'new_leads.csv')
        lead_fields = ['type', 'platform', 'id', 'store_link', 'source_url', 'source_kind', 'source_company', 'source_app']
        with open(leads_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=lead_fields)
            writer.writeheader()
            for row in self.new_leads:
                writer.writerow(row)

        summary = self.build_summary()
        summary_path = os.path.join(self.run_dir, 'summary.json')
        with open(summary_path, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, sort_keys=True)

        md_path = os.path.join(self.run_dir, 'review_report.md')
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(self.build_markdown(summary))

        return {
            'run_dir': self.run_dir,
            'emails_path': emails_path,
            'store_path': store_path,
            'leads_path': leads_path,
            'summary_path': summary_path,
            'review_path': md_path,
        }

    def build_summary(self):
        email_domain_counter = Counter(row['registered_domain'] for row in self.email_rows if row.get('registered_domain'))
        company_domain_counter = Counter(
            row['registered_domain'] for row in self.email_rows
            if row.get('registered_domain') and not row.get('is_common_public_domain') and not row.get('is_public_hosting_domain')
        )
        source_counter = Counter(row.get('source_kind', '') for row in self.page_rows + self.store_rows)
        status_counter = Counter(str(row.get('status', 0)) for row in self.page_rows + self.store_rows)
        lead_type_counter = Counter(row.get('type', '') for row in self.new_leads)
        companies_with_evidence = sorted({
            row.get('company', '') for row in self.email_rows + self.page_rows + self.store_rows
            if row.get('company') and row.get('company') != '__DISCOVERED__'
        })
        domains_by_company = defaultdict(set)
        for row in self.email_rows:
            company = row.get('company')
            domain = row.get('registered_domain')
            if company and domain and not row.get('is_common_public_domain'):
                domains_by_company[company].add(domain)

        return {
            'run_id': self.run_id,
            'created_at': now_ts(),
            'apps_checked': len(self.apps),
            'gp_apps_checked': len([a for a in self.apps if a.get('platform') == 'GP' and not a.get('removed') and not self.args.skip_gp]),
            'ios_apps_checked': len([a for a in self.apps if a.get('platform') == 'iOS' and not a.get('removed') and not self.args.skip_ios]),
            'store_rows': len(self.store_rows),
            'site_pages_fetched': len(self.page_rows),
            'site_urls_queued': len(self.queued_site_urls),
            'email_evidence_rows': len(self.email_rows),
            'unique_email_domains': len(email_domain_counter),
            'company_like_email_domains': len(company_domain_counter),
            'new_leads_total': len(self.new_leads),
            'known_store_matches': len(self.known_matches),
            'lead_types': dict(lead_type_counter),
            'source_kinds': dict(source_counter),
            'statuses': dict(status_counter),
            'top_email_domains': summarize_counter(email_domain_counter, 30),
            'top_company_like_email_domains': summarize_counter(company_domain_counter, 30),
            'companies_with_evidence': companies_with_evidence,
            'company_domain_summary': {
                company: sorted(domains)
                for company, domains in sorted(domains_by_company.items())
            },
            'stats': dict(self.stats),
            'limits': {
                'max_site_pages': self.args.max_site_pages,
                'max_pages_per_domain': self.args.max_pages_per_domain,
                'max_discovered_store_apps': self.args.max_discovered_store_apps,
                'max_discovered_rounds': self.args.max_discovered_rounds,
            },
        }

    def build_markdown(self, summary):
        lines = []
        lines.append('# Web Evidence Review')
        lines.append('')
        lines.append(f'- Run: `{summary["run_id"]}`')
        lines.append(f'- Apps checked: {summary["apps_checked"]} ({summary["gp_apps_checked"]} GP, {summary["ios_apps_checked"]} iOS)')
        lines.append(f'- Store pages checked: {summary["store_rows"]}')
        lines.append(f'- Website/support/privacy pages fetched: {summary["site_pages_fetched"]}')
        lines.append(f'- Private email evidence rows: {summary["email_evidence_rows"]}')
        lines.append(f'- Email domains: {summary["unique_email_domains"]}, company-like domains: {summary["company_like_email_domains"]}')
        lines.append(f'- New leads needing review: {summary["new_leads_total"]} {summary["lead_types"]}')
        lines.append('')
        lines.append('## Top Company-Like Email Domains')
        for item in summary['top_company_like_email_domains'][:20]:
            lines.append(f'- {item["value"]}: {item["count"]}')
        lines.append('')
        lines.append('## Company Domain Summary')
        for company, domains in summary['company_domain_summary'].items():
            display = ', '.join(domains[:12])
            suffix = ' ...' if len(domains) > 12 else ''
            lines.append(f'- {company}: {display}{suffix}')
        lines.append('')
        lines.append('## Notes')
        lines.append('- Raw email addresses are intentionally kept only in the private CSV backup.')
        lines.append('- New app/developer/AppMagic leads require manual review before import.')
        lines.append('- This report does not modify frontend product data.')
        return '\n'.join(lines) + '\n'

    def run(self):
        self.run_store_phase()
        self.run_site_phase()
        self.run_discovered_store_phase()
        outputs = self.write_outputs()
        summary = self.build_summary()
        self.log('=' * 60)
        self.log(f'Run dir: {outputs["run_dir"]}')
        self.log(f'Private emails: {outputs["emails_path"]}')
        self.log(f'New leads: {outputs["leads_path"]}')
        self.log(f'Review report: {outputs["review_path"]}')
        self.log(
            f'Summary: apps={summary["apps_checked"]} store={summary["store_rows"]} '
            f'site_pages={summary["site_pages_fetched"]} emails={summary["email_evidence_rows"]} '
            f'company_domains={summary["company_like_email_domains"]} leads={summary["new_leads_total"]}'
        )
        return outputs


def parse_args():
    parser = argparse.ArgumentParser(description='Audit website/support/privacy/email evidence.')
    parser.add_argument('--sample', type=int, default=0, help='Only process the first N apps for a smoke test.')
    parser.add_argument('--skip-gp', action='store_true', help='Skip Google Play store pages.')
    parser.add_argument('--skip-ios', action='store_true', help='Skip App Store pages.')
    parser.add_argument('--workers-store', type=int, default=8, help='Concurrent store page workers.')
    parser.add_argument('--workers-site', type=int, default=6, help='Concurrent website/privacy/support workers.')
    parser.add_argument('--timeout', type=int, default=20, help='Request timeout in seconds.')
    parser.add_argument('--max-site-pages', type=int, default=6000, help='Maximum website/privacy/support pages to fetch.')
    parser.add_argument('--max-pages-per-domain', type=int, default=12, help='Maximum crawled pages per registered domain.')
    parser.add_argument('--max-discovered-store-apps', type=int, default=500, help='Maximum newly discovered store apps to recursively inspect.')
    parser.add_argument('--max-discovered-rounds', type=int, default=2, help='Maximum recursive rounds for newly discovered store apps.')
    parser.add_argument('--expand-email-domains', action='store_true', help='Experimental: use non-public email domains as crawl seeds. Disabled by default.')
    parser.add_argument('--include-company', action='append', default=[], help='Only audit this exact company. Repeatable.')
    parser.add_argument('--exclude-company', action='append', default=[], help='Exclude this exact company. Repeatable.')
    parser.add_argument('--exclude-company-prefix', action='append', default=[], help='Exclude company names with this prefix. Repeatable.')
    return parser.parse_args()


def main():
    args = parse_args()
    auditor = EvidenceAuditor(args)
    auditor.run()


if __name__ == '__main__':
    main()
