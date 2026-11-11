"""Stable identities, evidence and conservative value normalization."""
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = '2.0'
EXTRACTOR_VERSION = 'rules-0.2.0'


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    return ' '.join((value or '').split())


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identity(*parts):
    return digest(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode())


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    import os
    tmp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    tmp.replace(path)


def evidence(url, sha, locator, quote, page=None):
    return {'source_url': url, 'content_sha256': sha, 'locator': locator,
            'page': page, 'quote': quote, 'extractor_version': EXTRACTOR_VERSION}


REFERENCE = re.compile(r'(?<!\w)(?:ГОСТ(?:\s+Р)?(?:\s+ИСО)?\s+\d+(?:\.\d+)*(?:\s*[-–—]\s*\d{2,4})?|СП\s+\d+(?:\.\d+)+(?:[-–—]\d{2,4})?|СанПиН\s+\d+(?:\.\d+)*\s*[-–—]\s*\d{2,4})(?!\d)', re.I)


def references(text, origin):
    return [{'designation': re.sub(r'\s*[-–—]\s*', '-', clean(m.group())),
             'relation': 'mentioned_in_source', 'review_status': 'unverified',
             'edition_resolution': 'not_resolved',
             'evidence': dict(origin, quote=text[max(0, m.start()-80):m.end()+80])}
            for m in REFERENCE.finditer(text)]


def normalize_value(raw, unit=None, comparator=None):
    text = clean(raw)
    value = None
    if re.fullmatch(r'[+-]?\d+(?:[.,]\d+)?', text):
        value = float(text.replace(',', '.'))
        if not math.isfinite(value):
            value = None
    signs = {'не менее': '>=', 'не более': '<=', 'более': '>', 'менее': '<',
             '≥': '>=', '≤': '<=', '>=': '>=', '<=': '<=', '>': '>', '<': '<', '=': '='}
    return {'raw_value': text, 'numeric_value': value, 'unit': clean(unit) or None,
            'comparator': signs.get(clean(comparator).lower()),
            'raw_comparator': clean(comparator) or None,
            'normalization_status': 'scalar' if value is not None else 'raw_only'}


PROFILES = {
    'mounting_foam': {'name': 'Монтажные пены', 'properties': {
        'температура применения': 'application_temperature', 'температура эксплуатации': 'service_temperature',
        'выход': 'foam_yield', 'вторичное расширение': 'secondary_expansion',
        'время первичной обработки': 'cutting_time', 'адгезия': 'adhesion',
        'теплопроводность': 'thermal_conductivity', 'водопоглощение': 'water_absorption'}},
    'mineral_insulation': {'name': 'Минеральная теплоизоляция', 'properties': {
        'теплопроводность': 'thermal_conductivity', 'плотность': 'density',
        'прочность на сжатие': 'compressive_strength', 'водопоглощение': 'water_absorption',
        'паропроницаемость': 'vapour_permeability', 'горючесть': 'combustibility',
        'толщина': 'thickness', 'сжимаемость': 'compressibility'}}}


def property_key(label, profile):
    matches = [value for key, value in PROFILES.get(profile, {}).get('properties', {}).items()
               if key in label.lower()]
    return matches[0] if len(set(matches)) == 1 else None
