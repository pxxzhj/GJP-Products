"""Authorize new native-account apps against pre-existing AppMagic anchors."""
from collections import defaultdict
from urllib.parse import parse_qs, urlsplit


def native_id(url):
    return parse_qs(urlsplit(url).query).get('id', [''])[0]


def authorize_candidates(apps, candidates, search=None):
    from audit_domestic_reconciliation import summarize_mapping
    from audit_web_detail_review import search_appmagic_by_ids
    search = search or search_appmagic_by_ids
    accounts = {native_id(row['dev_url']) for row in candidates.values()}
    anchors = [app for app in apps if app['platform'] == 'GP'
               and native_id(app.get('dev_link', '')) in accounts]
    ids = sorted({app['pkg_or_id'] for app in anchors} | set(candidates))
    mappings = {}
    for offset in range(0, len(ids), 500):
        batch = ids[offset:offset + 500]
        raw = search([{'platform': 'GP', 'id': pkg} for pkg in batch])
        for pkg in batch:
            mappings[pkg] = summarize_mapping(raw.get('GP:' + pkg), 'GP', pkg)
    publishers = defaultdict(set)
    account_publishers = defaultdict(set)
    for app in anchors:
        mapped = mappings[app['pkg_or_id']]
        pid = mapped.get('publisher_id')
        if mapped['status'] == 'mapped' and pid:
            publishers[pid].add(app['company_cn'])
            # A transferred app's old AppMagic publisher does not authorize
            # its new native account. Require the native ID to agree too.
            if mapped.get('store_publisher_id') == native_id(app.get('dev_link', '')):
                account_publishers[(app['company_cn'], native_id(app['dev_link']))].add(pid)
    accepted, pending = {}, []
    for pkg, candidate in candidates.items():
        mapped = mappings[pkg]
        pid = mapped.get('publisher_id')
        company = candidate['company']
        account_pids = account_publishers[(company, native_id(candidate['dev_url']))]
        allowed = (mapped['status'] == 'mapped' and publishers.get(pid) == {company})
        if mapped['status'] == 'not_found' and len(account_pids) == 1:
            allowed = publishers.get(next(iter(account_pids))) == {company}
        if allowed:
            accepted[pkg] = candidate
        else:
            pending.append({'id': pkg, **candidate, 'appmagic': mapped,
                            'reason': 'new account/publisher not authorized by existing anchors'})
    return accepted, pending
