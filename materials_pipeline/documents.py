"""Bounded downloads and immutable source corpus. No automatic legal conclusions."""
import io
import os
import uuid
import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from lxml import html
from .core import atomic_json, digest, identity, now, evidence, references, SCHEMA_VERSION

SOURCE_HOSTS = {
    'technonicol': ['www.tn.ru', 'tn.ru', 'nav.tn.ru'],
    'rosstandart': ['protect.gost.ru'],
    'gostinfo': ['new.gostinfo.ru', 'www.gostinfo.ru', 'gostinfo.ru'],
    'minstroy': ['www.minstroyrf.gov.ru', 'minstroyrf.gov.ru'],
    'fcs': ['www.faufcc.ru', 'faufcc.ru', 'api.faufcc.ru'],
    'pravo': ['publication.pravo.gov.ru'],
    'rospotrebnadzor': ['www.rospotrebnadzor.ru', 'rospotrebnadzor.ru']}


def checked_url(url, hosts):
    p = urlsplit(url)
    if p.scheme != 'https' or p.hostname not in hosts or p.username or p.password or p.port not in (None, 443):
        raise ValueError('URL outside configured HTTPS source: ' + url)
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, ''))


class Redirects(HTTPRedirectHandler):
    def __init__(self, hosts):
        super().__init__()
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_url(newurl, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def reserve_slot(root, host, delay):
    if delay < 1:
        raise ValueError('Source interval must be >= 1 second')
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(root / 'source_limits.sqlite', timeout=30) as db:
        db.execute('CREATE TABLE IF NOT EXISTS limits (host TEXT PRIMARY KEY, next_at REAL NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT next_at FROM limits WHERE host=?', (host,)).fetchone()
        slot = max(time.time(), row[0] if row else 0)
        db.execute('INSERT OR REPLACE INTO limits VALUES (?, ?)', (host, slot + delay))
        db.commit()
    time.sleep(max(0, slot-time.time()))


def download(url, root, source, refresh=False, timeout=30, max_bytes=32*1024*1024, delay=2):
    if source not in SOURCE_HOSTS:
        raise ValueError('Unknown source: ' + source)
    hosts = SOURCE_HOSTS[source]
    url = checked_url(url, hosts)
    root = Path(root)
    cache = root / 'requests' / (identity(url) + '.json')
    if cache.exists() and not refresh:
        meta = json.loads(cache.read_text(encoding='utf-8'))
        raw = root / meta['raw_path']
        if raw.exists() and digest(raw.read_bytes()) == meta['sha256']:
            return raw.read_bytes(), meta
    reserve_slot(Path(os.environ.get('MATERIALS_RATE_LIMIT_ROOT', '.materials_network')), source, delay)
    request = Request(url, headers={'User-Agent': 'MaterialsStudyParser/0.2 (+manufacturer evidence collector)'})
    with build_opener(Redirects(hosts)).open(request, timeout=timeout) as response:
        checked_url(response.geturl(), hosts)
        mime = response.headers.get('Content-Type', '').lower()
        if not any(t in mime for t in ['pdf', 'html', 'octet-stream']):
            raise ValueError('Unsupported content type: ' + mime)
        data = response.read(max_bytes+1)
        if len(data) > max_bytes:
            raise ValueError('Document exceeds size limit')
        meta = {'source_url': url, 'final_url': response.geturl(), 'retrieved_at': now(),
                'http_status': response.status, 'content_type': mime,
                'etag': response.headers.get('ETag'), 'last_modified': response.headers.get('Last-Modified')}
    sha = digest(data)
    raw = root / 'raw' / sha
    raw.parent.mkdir(parents=True, exist_ok=True)
    if not raw.exists():
        temp = raw.with_name(raw.name + '.' + uuid.uuid4().hex + '.tmp')
        temp.write_bytes(data)
        temp.replace(raw)
    meta.update(sha256=sha, raw_path=raw.relative_to(root).as_posix())
    atomic_json(cache, meta)
    return data, meta


def extract_pages(data, max_pages=1000):
    if data.startswith(b'%PDF-'):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('Encrypted PDF requires a provided readable copy')
        if len(reader.pages) > max_pages:
            raise ValueError('PDF exceeds page limit')
        pages = []
        for index, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or '') if page.get_contents() is not None else ''
            pages.append({'page': index, 'text': text,
                          'extraction_status': 'text_extracted' if text.strip() else 'needs_ocr'})
        return pages, 'pdf'
    if b'<html' not in data[:4096].lower() and b'<!doctype html' not in data[:4096].lower():
        raise ValueError('Expected a PDF or HTML document, received another format')
    tree = html.fromstring(data, parser=html.HTMLParser(encoding='utf-8'))
    for node in tree.xpath('//script|//style|//nav|//header|//footer'):
        node.drop_tree()
    body = tree.xpath('//main|//article')
    node = body[0] if body else tree
    for element in node.xpath('.//p|.//li|.//tr|.//h1|.//h2|.//h3|.//br'):
        element.tail = '\n' + (element.tail or '')
    text = '\n'.join(' '.join(x.split()) for x in node.text_content().splitlines() if x.strip())
    return [{'page': None, 'text': text, 'extraction_status': 'text_extracted' if text else 'empty'}], 'html'


def chunks(document_id, pages, source_url, sha, size=1800):
    if size < 1:
        raise ValueError('chunk size must be positive')
    for page in pages:
        text = page['text']
        for start in range(0, len(text), size):
            part = text[start:start+size]
            if not part.strip():
                continue
            yield {'schema_version': SCHEMA_VERSION, 'record_type': 'document_chunk',
                'id': identity(document_id, page['page'], start, start+len(part)),
                'document_id': document_id, 'text': part, 'page': page['page'],
                'start_char': start, 'end_char': start+len(part),
                'review_status': 'unreviewed',
                'evidence': evidence(source_url, sha, 'page_text', part, page['page'])}


def ingest(data, root, source_url, kind='manufacturer_document', metadata=None, retrieval=None):
    if kind not in ['manufacturer_document', 'normative_document']:
        raise ValueError('Unknown document kind')
    root = Path(root)
    meta = dict(metadata or {})
    sha = digest(data)
    doc_id = identity(kind, source_url, sha)
    previous_path = root / 'documents' / (doc_id+'.json')
    if previous_path.exists():
        previous = json.loads(previous_path.read_text(encoding='utf-8'))
        meta = {**previous.get('metadata', {}), **meta}
    pages, fmt = extract_pages(data)
    raw = root / 'raw' / sha
    raw.parent.mkdir(parents=True, exist_ok=True)
    if not raw.exists():
        tmp = raw.with_name(raw.name + '.' + uuid.uuid4().hex + '.tmp')
        tmp.write_bytes(data)
        tmp.replace(raw)
    refs, clauses = [], []
    for page in pages:
        origin = evidence(source_url, sha, 'page_text', page['text'], page['page'])
        refs.extend(references(page['text'], origin))
        for m in re.finditer(r'(?m)^\s*(\d+(?:\.\d+)+)\s+([^\n]+)', page['text']):
            clauses.append({'clause_label': m.group(1), 'heading_or_text': m.group(2),
                            'page': page['page'], 'start_char': m.start(),
                            'end_char': m.end(), 'review_status': 'candidate'})
    doc = {'schema_version': SCHEMA_VERSION, 'id': doc_id, 'kind': kind,
        'source_url': source_url, 'sha256': sha, 'format': fmt,
        'raw_path': raw.relative_to(root).as_posix(), 'ingested_at': now(),
        'retrieval': retrieval, 'metadata': meta, 'pages': pages,
        'review_status': 'unreviewed', 'regulatory_references': refs,
        'clause_candidates': clauses,
        'quality_issues': ['table_structure_not_verified'] +
            (['ocr_required'] if any(p['extraction_status'] == 'needs_ocr' for p in pages) else [])}
    if kind == 'normative_document':
        doc['quality_issues'].append('full_text_and_edition_not_verified')
        doc['normative'] = {'designation': meta.get('designation'),
            'document_type': meta.get('document_type'), 'title': meta.get('title'),
            'edition': meta.get('edition'), 'effective_from': meta.get('effective_from'),
            'effective_to': meta.get('effective_to'), 'replaces': meta.get('replaces', []),
            'amendments': meta.get('amendments', []),
            'declared_status': meta.get('declared_status'), 'status': 'unverified',
            'mandatory_status': 'not_assessed', 'status_evidence': meta.get('status_evidence')}
    atomic_json(root / 'documents' / (doc_id+'.json'), doc)
    return doc


def collect_manifest(manifest, root, refresh=False):
    """Explicit reviewed seed URLs, not unrestricted web crawling."""
    entries = json.loads(Path(manifest).read_text(encoding='utf-8'))
    if not isinstance(entries, list):
        raise ValueError('Manifest must be a list')
    results, errors = [], []
    for entry in entries:
        try:
            data, retrieval = download(entry['url'], root, entry['source'], refresh=refresh)
            doc = ingest(data, root, entry['url'], entry.get('kind', 'normative_document'),
                         entry.get('metadata'), retrieval)
            results.append(doc['id'])
        except Exception as exc:
            errors.append({'entry': entry, 'error': type(exc).__name__ + ': ' + str(exc)})
    report = {'documents': results, 'errors': errors, 'finished_at': now()}
    atomic_json(Path(root) / 'collection_report.json', report)
    return report
