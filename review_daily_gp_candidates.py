#!/usr/bin/env python3
"""Review new GP records against an explicit pre-run baseline; optionally quarantine."""
import argparse
import json
from pathlib import Path

import monitor
from gp_candidate_ownership import authorize_candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    output = Path(args.output).resolve()
    root = Path(__file__).resolve().parent
    if output == root or root in output.parents:
        parser.error('Review evidence must stay outside the repository')
    baseline = json.loads(Path(args.baseline).read_text(encoding='utf-8'))
    keys = {(app['platform'], app['pkg_or_id']) for app in baseline}
    apps = monitor.load_current_library()
    anchors = [app for app in apps if (app['platform'], app['pkg_or_id']) in keys]
    new = [app for app in apps if app['platform'] == 'GP' and ('GP', app['pkg_or_id']) not in keys]
    accepted, pending = authorize_candidates(anchors, {
        app['pkg_or_id']: {'company': app['company_cn'], 'dev_url': app.get('dev_link', '')}
        for app in new
    }) if new else ({}, [])
    rejected = {row['id'] for row in pending}
    report = {'accepted': [app for app in new if app['pkg_or_id'] in accepted],
              'pending': pending,
              'quarantined_apps': [app for app in new if app['pkg_or_id'] in rejected]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.apply and rejected:
        monitor.regenerate_files(
            [app for app in apps if not (app['platform'] == 'GP' and app['pkg_or_id'] in rejected)],
            {app['company_cn'] for app in new if app['pkg_or_id'] in rejected},
        )
    print(json.dumps({'accepted': len(accepted), 'pending': len(pending), 'applied': args.apply}))


if __name__ == '__main__':
    main()
