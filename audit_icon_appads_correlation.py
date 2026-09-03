#!/usr/bin/env python3
"""Compare app icons and app-ads.txt files as auxiliary ownership evidence."""

import argparse
import csv
import hashlib
import io
import json
from datetime import datetime
from itertools import combinations
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps


HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# These exchanges commonly occur in unrelated apps. Exact seller IDs still
# carry evidence, but rows from these systems receive less aggregate weight.
COMMON_AD_SYSTEMS = {
    'adcolony.com',
    'admanmedia.com',
    'appads.in',
    'appier.com',
    'appnexus.com',
    'applovin.com',
    'bigo.sg',
    'bidmachine.io',
    'chartboost.com',
    'google.com',
    'inmobi.com',
    'ironsrc.com',
    'liftoff.io',
    'mintegral.com',
    'moloco.com',
    'pubmatic.com',
    'rubiconproject.com',
    'smaato.com',
    'unity.com',
    'unityads.unity3d.com',
    'verve.com',
    'vungle.com',
}


def fetch_bytes(source):
    path = Path(source).expanduser()
    if path.is_file():
        return path.read_bytes(), str(path.resolve())
    response = requests.get(source, headers=HEADERS, timeout=30, allow_redirects=True)
    response.raise_for_status()
    return response.content, response.url


def load_icon(source):
    content, final_source = fetch_bytes(source)
    return Image.open(io.BytesIO(content)).convert('RGBA'), final_source


def normalized_rgb(image, size=256):
    canvas = Image.new('RGBA', image.size, 'white')
    canvas.alpha_composite(image)
    fitted = ImageOps.fit(
        canvas.convert('RGB'), (size, size), Image.Resampling.LANCZOS
    )
    return np.asarray(fitted)


def phash_bits(rgb):
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    resized = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    block = cv2.dct(resized)[:8, :8]
    median = np.median(block.flatten()[1:])
    return (block > median).flatten()


def dhash_bits(rgb):
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    resized = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    return (resized[:, 1:] > resized[:, :-1]).flatten()


def orb_similarity(left, right):
    left_gray = cv2.cvtColor(left, cv2.COLOR_RGB2GRAY)
    right_gray = cv2.cvtColor(right, cv2.COLOR_RGB2GRAY)
    orb = cv2.ORB_create(nfeatures=1000)
    left_keys, left_desc = orb.detectAndCompute(left_gray, None)
    right_keys, right_desc = orb.detectAndCompute(right_gray, None)
    if left_desc is None or right_desc is None or not left_keys or not right_keys:
        return 0.0, 0
    matches = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(
        left_desc, right_desc, k=2
    )
    good = [
        first
        for pair in matches
        if len(pair) == 2
        for first, second in [pair]
        if first.distance < 0.75 * second.distance
    ]
    denominator = max(1, min(len(left_keys), len(right_keys)))
    return round(len(good) / denominator, 4), len(good)


def icon_metrics(left_image, right_image):
    left = normalized_rgb(left_image)
    right = normalized_rgb(right_image)
    orb_ratio, orb_matches = orb_similarity(left, right)
    pixel_difference = np.mean(
        np.abs(left.astype(np.int16) - right.astype(np.int16))
    )
    return {
        'normalized_sha_equal': (
            hashlib.sha256(left.tobytes()).digest()
            == hashlib.sha256(right.tobytes()).digest()
        ),
        'phash_distance_64': int(np.count_nonzero(phash_bits(left) != phash_bits(right))),
        'dhash_distance_64': int(np.count_nonzero(dhash_bits(left) != dhash_bits(right))),
        'pixel_similarity': round(1.0 - float(pixel_difference) / 255.0, 4),
        'orb_good_match_ratio': orb_ratio,
        'orb_good_matches': orb_matches,
    }


def parse_app_ads(text):
    rows = set()
    variables = {}
    for raw_line in text.splitlines():
        line = raw_line.split('#', 1)[0].strip()
        if not line:
            continue
        if '=' in line and ',' not in line:
            key, value = line.split('=', 1)
            variables[key.strip().upper()] = value.strip().lower()
            continue
        parts = [part.strip() for part in line.split(',')]
        if len(parts) < 3:
            continue
        rows.add((
            parts[0].lower().rstrip('.'),
            parts[1].lower(),
            parts[2].upper(),
            parts[3].lower() if len(parts) > 3 else '',
        ))
    return rows, variables


def row_weight(row):
    system, _seller_id, relationship, _authority = row
    weight = 3.0 if relationship == 'DIRECT' else 0.25
    if system in COMMON_AD_SYSTEMS:
        weight *= 0.35
    return weight


def weighted_jaccard(left, right):
    union = left | right
    if not union:
        return 0.0
    intersection_weight = sum(row_weight(row) for row in left & right)
    union_weight = sum(row_weight(row) for row in union)
    return round(intersection_weight / union_weight, 6)


def app_ads_metrics(left, right):
    left_rows, left_variables = left
    right_rows, right_variables = right
    left_direct = {row for row in left_rows if row[2] == 'DIRECT'}
    right_direct = {row for row in right_rows if row[2] == 'DIRECT'}
    shared = left_rows & right_rows
    shared_direct = left_direct & right_direct
    specific_direct = {
        row for row in shared_direct if row[0] not in COMMON_AD_SYSTEMS
    }
    return {
        'all_shared': len(shared),
        'all_union': len(left_rows | right_rows),
        'all_jaccard': round(len(shared) / max(1, len(left_rows | right_rows)), 6),
        'direct_shared': len(shared_direct),
        'direct_union': len(left_direct | right_direct),
        'direct_jaccard': round(
            len(shared_direct) / max(1, len(left_direct | right_direct)), 6
        ),
        'weighted_jaccard': weighted_jaccard(left_rows, right_rows),
        'specific_direct_shared': len(specific_direct),
        'owner_domain_equal': bool(
            left_variables.get('OWNERDOMAIN')
            and left_variables.get('OWNERDOMAIN')
            == right_variables.get('OWNERDOMAIN')
        ),
        'manager_domain_equal': bool(
            left_variables.get('MANAGERDOMAIN')
            and left_variables.get('MANAGERDOMAIN')
            == right_variables.get('MANAGERDOMAIN')
        ),
        'shared_direct_rows': sorted(shared_direct),
    }


def safe_filename(value):
    return ''.join(char if char.isalnum() else '_' for char in value).strip('_')[:80]


def make_icon_sheet(icon_results, output_dir):
    if not icon_results:
        return ''
    font_path = '/System/Library/Fonts/Supplemental/Arial.ttf'
    font = ImageFont.truetype(font_path, 18) if Path(font_path).exists() else None
    small = ImageFont.truetype(font_path, 14) if Path(font_path).exists() else None
    row_height = 300
    sheet = Image.new('RGB', (920, row_height * len(icon_results)), 'white')
    draw = ImageDraw.Draw(sheet)
    for index, result in enumerate(icon_results):
        y = index * row_height
        left = ImageOps.fit(result['_left_image'].convert('RGB'), (220, 220))
        right = ImageOps.fit(result['_right_image'].convert('RGB'), (220, 220))
        sheet.paste(left, (20, y + 45))
        sheet.paste(right, (270, y + 45))
        draw.text((20, y + 12), result['label'], fill='black', font=font)
        metrics = result['metrics']
        detail = (
            f"pHash: {metrics['phash_distance_64']}/64\n"
            f"dHash: {metrics['dhash_distance_64']}/64\n"
            f"pixel: {metrics['pixel_similarity']:.1%}\n"
            f"ORB matches: {metrics['orb_good_matches']}"
        )
        draw.multiline_text((530, y + 75), detail, fill='black', font=small, spacing=10)
        draw.line((0, y + row_height - 1, 920, y + row_height - 1), fill='#cccccc')
    path = output_dir / 'icon_comparison.png'
    sheet.save(path)
    return str(path)


def parse_labeled_source(value):
    if '=' in value:
        label, source = value.split('=', 1)
        return label.strip(), source.strip()
    parsed = urlsplit(value)
    label = parsed.netloc or Path(value).stem
    return label, value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--icon-pair', action='append', nargs=3, metavar=('LABEL', 'LEFT', 'RIGHT'),
        default=[], help='Compare two icon URLs or local image paths.',
    )
    parser.add_argument(
        '--app-ads', action='append', default=[], metavar='LABEL=URL',
        help='Add an app-ads.txt URL or local file. All inputs are compared pairwise.',
    )
    parser.add_argument('--output-dir', default='')
    args = parser.parse_args()
    if not args.icon_pair and len(args.app_ads) < 2:
        parser.error('provide an icon pair or at least two app-ads.txt inputs')

    output_dir = Path(args.output_dir).expanduser() if args.output_dir else Path(
        '/tmp', 'app_correlation_audit_' + datetime.now().strftime('%Y%m%d_%H%M%S')
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    icon_results = []
    for label, left_source, right_source in args.icon_pair:
        left_image, left_final = load_icon(left_source)
        right_image, right_final = load_icon(right_source)
        icon_results.append({
            'label': label,
            'left_source': left_source,
            'left_final_source': left_final,
            'right_source': right_source,
            'right_final_source': right_final,
            'metrics': icon_metrics(left_image, right_image),
            '_left_image': left_image,
            '_right_image': right_image,
        })

    app_ads = {}
    app_ads_files = {}
    raw_dir = output_dir / 'app-ads'
    for value in args.app_ads:
        label, source = parse_labeled_source(value)
        content, final_source = fetch_bytes(source)
        text = content.decode('utf-8', errors='replace')
        rows, variables = parse_app_ads(text)
        app_ads[label] = (rows, variables)
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f'{safe_filename(label)}.txt').write_text(text, encoding='utf-8')
        app_ads_files[label] = {
            'source': source,
            'final_source': final_source,
            'bytes': len(content),
            'sha256': hashlib.sha256(content).hexdigest(),
            'rows': len(rows),
            'direct_rows': sum(row[2] == 'DIRECT' for row in rows),
            'variables': variables,
        }

    app_ads_comparisons = {}
    for left_label, right_label in combinations(app_ads, 2):
        key = f'{left_label} vs {right_label}'
        app_ads_comparisons[key] = app_ads_metrics(
            app_ads[left_label], app_ads[right_label]
        )

    sheet_path = make_icon_sheet(icon_results, output_dir)
    clean_icon_results = [
        {key: value for key, value in result.items() if not key.startswith('_')}
        for result in icon_results
    ]
    report = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'interpretation': {
            'priority': 'Auxiliary evidence only; apply the configured primary ownership source first.',
            'icon': 'Near-identical icons support an app-level match but do not prove account ownership alone.',
            'app_ads': (
                'OWNERDOMAIN, exact DIRECT seller IDs, and weighted overlap matter more than raw overlap. '
                'Common ad systems and RESELLER rows are downweighted.'
            ),
        },
        'icon_pairs': clean_icon_results,
        'app_ads_files': app_ads_files,
        'app_ads_comparisons': app_ads_comparisons,
        'artifacts': {'icon_comparison': sheet_path},
    }
    report_path = output_dir / 'audit_result.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    if clean_icon_results:
        fields = [
            'label', 'left_source', 'right_source', 'normalized_sha_equal',
            'phash_distance_64', 'dhash_distance_64', 'pixel_similarity',
            'orb_good_match_ratio', 'orb_good_matches',
        ]
        with (output_dir / 'icon_metrics.csv').open(
            'w', newline='', encoding='utf-8-sig'
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for result in clean_icon_results:
                writer.writerow({
                    **{key: result.get(key, '') for key in fields},
                    **result['metrics'],
                })

    print(json.dumps({
        'report': str(report_path),
        'icon_pairs': len(clean_icon_results),
        'app_ads_files': len(app_ads_files),
        'app_ads_comparisons': len(app_ads_comparisons),
        'icon_comparison': sheet_path,
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
