"""TECHNONICOL adapter. Extract statements, never infer permission to use."""
import json
from urllib.parse import urljoin, urlsplit
from lxml import html
from .core import (clean, digest, identity, now, evidence, references,
                   normalize_value, property_key, SCHEMA_VERSION)


def cls(name):
    return 'contains(concat(" ", normalize-space(@class), " "), " ' + name + ' ")'


def document_type(title):
    value = title.lower()
    for needle, kind in [('технический лист', 'technical_sheet'), ('техлист', 'technical_sheet'),
                         ('безопасност', 'safety_data_sheet'), ('инструкц', 'installation_guide'),
                         ('декларац', 'declaration'), ('сертифик', 'certificate'),
                         ('сгр', 'hygiene_registration'), ('альбом', 'system_solutions')]:
        if needle in value:
            return kind
    return 'other_document'


def parse_product(data, url, category, from_cache=False, profile=None):
    tree = html.fromstring(data, parser=html.HTMLParser(encoding='utf-8'))
    sha = digest(data)
    name = clean(' '.join(tree.xpath('//h1//text()')))
    if not name:
        raise ValueError('Нет h1: требуется проверка адаптера')
    if profile is None:
        label = (category + ' ' + name).lower()
        profile = ('mounting_foam' if 'пен' in label else
                   'mineral_insulation' if any(s in label for s in ['минеральн', 'каменная вата', 'технолайт']) else None)
    facts, properties, sections, documents, seen = [], [], [], [], set()
    for table in tree.xpath('//div[' + cls('tn-product-tabs-box') + ']//table'):
        rows = table.xpath('.//tr')
        if not rows:
            continue
        headers = [clean(x.text_content()) for x in rows[0].xpath('./th|./td')]
        if not any('Наименование' in h for h in headers):
            continue
        for row in rows[1:]:
            cells = [clean(x.text_content()) for x in row.xpath('./th|./td')]
            if len(cells) != len(headers) or not cells[0]:
                continue
            fact = dict(zip(headers, cells))
            facts.append(fact)
            label = cells[0]
            def field(*names):
                return next((v for k, v in fact.items() if any(n in k.lower() for n in names)), None)
            origin = evidence(url, sha, tree.getroottree().getpath(row), ' | '.join(cells))
            properties.append({'id': identity(url, sha, origin['locator']), 'name': label,
                'property_key': property_key(label, profile),
                **normalize_value(field('значение') or cells[-1], field('ед.', 'единиц'), field('критерий')),
                'test_method_raw': field('метод'), 'conditions_raw': field('услов'),
                'review_status': 'extracted_unreviewed', 'evidence': origin})
    if not facts:
        raise ValueError('Не найдены таблицы характеристик: требуется проверка адаптера')
    # Read direct content blocks, excluding recommended products, navigation and Q&A.
    nodes = tree.xpath('//div[' + cls('tn-product-text') + ']')
    for box in tree.xpath('//div[' + cls('tn-product-tabs-box') + ']'):
        tab = box.get('data-tab-index', '')
        if tab in ('#description', '#use'):
            content = box.xpath('./div[' + cls('content_') + ']')
            nodes.extend(content or [box])
    for node in nodes:
        cloned = html.fromstring(html.tostring(node))
        for unwanted in cloned.xpath('.//script|.//style|.//div[' + cls('tn-product-more') + ']'):
            unwanted.drop_tree()
        # Preserve paragraph, list and br boundaries for NLP.
        for br in cloned.xpath('.//br'):
            br.tail = '\n' + (br.tail or '')
        for element in cloned.xpath('.//p|.//li|.//h2|.//h3'):
            element.tail = '\n' + (element.tail or '')
        text = '\n'.join(clean(line) for line in cloned.text_content().splitlines() if clean(line))
        if not text:
            continue
        ancestors = node.xpath('ancestor-or-self::*[@data-tab-index]')
        label = ancestors[-1].get('data-tab-index') if ancestors else '#summary'
        locator = tree.getroottree().getpath(node)
        sections.append({'id': identity(url, sha, locator), 'section': label.lstrip('#'),
                         'text': text, 'evidence': evidence(url, sha, locator, text)})
    for node in tree.xpath('//*[@href or @data-href]'):
        raw = node.get('data-href') or node.get('href')
        link = urljoin(url, raw)
        if urlsplit(link).scheme != 'https' or not urlsplit(link).path.lower().endswith('.pdf'):
            continue
        title = clean(node.text_content())
        containers = node.xpath('ancestor::*[' + cls('tn-product-docs-item') + ']')
        if containers:
            title = clean(containers[-1].text_content()) or title
        if link in seen:
            continue
        seen.add(link)
        documents.append({'title': title or urlsplit(link).path.rsplit('/', 1)[-1], 'url': link,
                          'document_type': document_type(title), 'download_status': 'not_requested',
                          'evidence': evidence(url, sha, tree.getroottree().getpath(node), title)})
    applicability = []
    headings = {'область применения': 'application', 'применение': 'application',
                'ограничения': 'restrictions', 'условия применения': 'application_conditions',
                'производство работ': 'installation', 'хранение': 'storage',
                'транспортирование': 'transport', 'упаковка': 'packaging',
                'безопасность': 'safety'}
    for section in sections:
        kind = 'application' if section['section'] == 'use' else 'description'
        for index, line in enumerate(section['text'].splitlines()):
            lowered = line.lower().rstrip(': .')
            matched = next((v for k, v in headings.items() if lowered == k), None)
            if matched:
                kind = matched
                continue
            if kind != 'description':
                applicability.append({'id': identity(section['id'], index), 'aspect': kind,
                    'raw_text': line, 'interpretation_status': 'requires_nlp',
                    'polarity': 'unknown', 'evidence': dict(section['evidence'], quote=line)})
    refs = []
    for section in sections:
        refs.extend(references(section['text'], section['evidence']))
    for prop in properties:
        refs.extend(references(prop['evidence']['quote'], prop['evidence']))
    identifiers = {}
    for node in tree.xpath('//script[@type="application/ld+json"]'):
        try:
            value = json.loads(node.text or '')
            entries = value if isinstance(value, list) else [value]
            for entry in list(entries):
                if isinstance(entry, dict):
                    entries.extend(entry.get('@graph', []))
                    if entry.get('@type') == 'Product':
                        identifiers.update({k: entry[k] for k in ['sku', 'mpn', 'gtin13', 'gtin'] if entry.get(k)})
        except (ValueError, TypeError):
            pass
    descriptions = tree.xpath('//div[' + cls('tn-product-text') + ']')
    return {'schema_version': SCHEMA_VERSION, 'material_id': identity('tn', url),
        'name': name, 'manufacturer': 'ТЕХНОНИКОЛЬ', 'category': category,
        'category_profile': profile, 'identifiers': identifiers, 'url': url,
        'parsed_at': now(), 'html_sha256': sha, 'from_cache': from_cache,
        'source_role': 'manufacturer', 'review_status': 'extracted_unreviewed',
        'description': clean(descriptions[0].text_content()) if descriptions else None,
        'characteristics': facts, 'properties': properties, 'sections': sections,
        'applicability': applicability, 'regulatory_references': refs,
        'documents': documents,
        'shop_urls': list(dict.fromkeys(urljoin(url, a.get('href')) for a in tree.xpath('//a[@href]')
             if urlsplit(urljoin(url, a.get('href'))).netloc == 'shop.tn.ru')),
        'price': None, 'currency': None,
        'price_note': 'Коммерческие предложения хранятся отдельно от сведений производителя.',
        'quality_issues': ([] if applicability else ['applicability_not_extracted'])
                         + ([] if documents else ['documents_not_found'])}
