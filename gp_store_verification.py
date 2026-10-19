"""Native US Play verification. Missing metadata is never removal evidence."""
import hashlib
import html
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

HEADERS = {'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'en-US,en;q=0.9'}
UNAVAILABLE = {'404', 'soft_404'}


def account_key(url):
    parsed = urlsplit(url)
    return parsed.hostname, parsed.path.rstrip('/'), tuple(parse_qs(parsed.query).get('id', []))


class PageMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.text = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'script', 'style'}:
            self.hidden += 1
        if tag == 'meta':
            self.meta[attrs.get('property') or attrs.get('name')] = attrs.get('content', '')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def classify_app_page(body, status_code, final_url, package):
    from monitor import extract_gp_developer_identity

    row = {'store_status': 'error', 'current_developer': '', 'current_dev_link': '',
           'name': '', 'error': '', 'http_status': status_code, 'final_url': final_url,
           'body_sha256': hashlib.sha256(body.encode()).hexdigest()}
    if status_code == 404:
        row.update(store_status='404', error='US app detail HTTP 404')
        return row
    if status_code != 200:
        row['error'] = f'HTTP {status_code}'
        return row
    parser = PageMetadata()
    parser.feed(body)
    visible = ' '.join(parser.text).lower()
    final = urlsplit(final_url)
    if final.hostname != 'play.google.com' or final.path != '/store/apps/details':
        row.update(store_status='incomplete', error='Redirected away from app detail page')
        return row
    if parse_qs(final.query).get('id') != [package]:
        row.update(store_status='incomplete', error='App detail package mismatch')
        return row
    developer, link = extract_gp_developer_identity(body)
    if not link:
        link = parser.meta.get('appstore:developer_url', '')
        link = urljoin('https://play.google.com', html.unescape(link)) if link else ''
    title = parser.meta.get('og:title', '')
    if title and developer and link:
        row.update(store_status='200', current_developer=developer,
                   current_dev_link=link, name=title)
    elif not title and not developer and any(marker in visible for marker in (
        'the requested url was not found on this server',
        "we're sorry, the requested url was not found",
    )):
        row.update(store_status='soft_404', error='Explicit Play not-found message')
    else:
        row.update(store_status='incomplete', error='HTTP 200 without complete app/developer metadata')
    return row


def verify_app(package, attempts=2, timeout=25):
    url = f'https://play.google.com/store/apps/details?id={package}&hl=en&gl=us'
    for attempt in range(attempts):
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout)
            row = classify_app_page(response.text, response.status_code, response.url, package)
        except requests.RequestException as exc:
            row = {'store_status': 'error', 'current_developer': '', 'current_dev_link': '',
                   'name': '', 'error': str(exc), 'http_status': 0, 'final_url': url}
        row['attempts'] = attempt + 1
        if row['store_status'] == '200' or row['store_status'] in UNAVAILABLE:
            break
        if attempt + 1 < attempts:
            time.sleep(1)
    return row


def at(data, path, default=None):
    try:
        for index in path:
            data = data[index]
        return data
    except (IndexError, KeyError, TypeError):
        return default


def parse_developer_page(body, numeric):
    # Google Play ds:3 account inventory; recommendation shelves are excluded.
    matches = re.findall(r"AF_initDataCallback\(\{key: 'ds:3'.*?data:(.*?), sideChannel:.*?\}\);", body, re.S)
    if not matches:
        raise ValueError('Developer inventory ds:3 missing')
    data = json.loads(matches[0])
    base = [0, 1, 0, 21 if numeric else 22]
    items = at(data, base + [0])
    if not isinstance(items, list):
        raise ValueError('Developer inventory layout not recognized')
    packages = set()
    for item in items:
        package = at(item, [0, 0] if numeric else [0, 0, 0])
        if not isinstance(package, str) or not re.fullmatch(r'[\w]+(?:\.[\w]+)+', package):
            raise ValueError('Unrecognized developer app entry')
        packages.add(package)
    return packages, at(data, base + [1, 3, 1])


def scan_developer(url, max_pages=100, timeout=30):
    """Return partial results explicitly on pagination failure; never call them complete."""
    result = {'url': url, 'packages': [], 'pages': 0, 'complete': False, 'error': ''}
    packages = set()
    try:
        response = requests.get(url, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
        dev_id = parse_qs(urlsplit(url).query).get('id', [''])[0]
        packages, token = parse_developer_page(response.text, dev_id.isdigit())
        result['pages'] = 1
        seen = set()
        while token:
            if token in seen or result['pages'] >= max_pages:
                raise ValueError('Pagination repeated token or page limit reached')
            seen.add(token)
            payload = [[None, [[10, [10, 100]], True, None,
                        [96, 27, 4, 8, 57, 30, 110, 79, 11, 16, 49, 1, 3, 9, 12, 104, 55, 56, 51, 10, 34, 77]], None, token]]
            request = [[['qnKhOb', json.dumps(payload), None, 'generic']]]
            response = requests.post(
                'https://play.google.com/_/PlayStoreUi/data/batchexecute',
                params={'rpcids': 'qnKhOb', 'hl': 'en', 'gl': 'us'},
                data={'f.req': json.dumps(request)}, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            # batchexecute may include length-prefixed frames and an XSSI prefix.
            decoder = json.JSONDecoder()
            data = None
            found = False
            for line in response.text.splitlines():
                if not line.startswith('[['):
                    continue
                frame, _ = decoder.raw_decode(line)
                for item in frame:
                    if at(item, [1]) == 'qnKhOb':
                        data = json.loads(item[2])
                        found = True
                        break
                if found:
                    break
            if not found:
                raise ValueError('Pagination RPC payload missing')
            result['pages'] += 1
            if data is None:
                token = None
                break
            items = at(data, [0, 0, 0])
            modern = False
            if not isinstance(items, list):
                section = at(data, [0, 6])
                owner_url = at(section, [2, 4, 2], '')
                if parse_qs(urlsplit(owner_url).query).get('id') != [dev_id]:
                    raise ValueError('Pagination section does not identify requested account')
                items = at(section, [0])
                modern = True
            if not isinstance(items, list):
                raise ValueError('Pagination inventory layout not recognized')
            for item in items:
                package = at(item, [12, 0] if modern else [0, 0, 0])
                # Name-based accounts can return the new card layout inside
                # the legacy inventory envelope.
                if not isinstance(package, str):
                    package = at(item, [12, 0])
                if not isinstance(package, str) or not re.fullmatch(r'[\w]+(?:\.[\w]+)+', package):
                    raise ValueError('Unrecognized pagination app entry')
                packages.add(package)
            token = at(data, [0, 0, 7, 1]) if not modern else at(data, [0, 7, 1])
            time.sleep(0.25)
        result['complete'] = bool(packages)
        if not packages:
            result['error'] = 'No apps returned; account status unresolved'
    except (requests.RequestException, ValueError, TypeError, IndexError) as exc:
        result['error'] = str(exc)
    result['packages'] = sorted(packages)
    return result
