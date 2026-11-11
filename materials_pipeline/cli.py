"""Executable catalogue, document corpus, NLP and retail workflows."""
import argparse
import json
import sys
from pathlib import Path
from .core import atomic_json, identity
from .documents import SOURCE_HOSTS, download, ingest, collect_manifest
from .export import export_nlp
from .offers import validate_offer, rank_offers


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def collect_documents(report_path, root, max_documents=10, refresh=False):
    report = read_json(report_path)
    errors, ids = [], []
    for material in report['materials']:
        for entry in material.get('documents', [])[:max_documents]:
            try:
                data, retrieval = download(entry['url'], root, 'technonicol', refresh=refresh)
                doc = ingest(data, root, entry['url'], metadata={
                    'title': entry['title'],
                    'document_type': entry.get('document_type'), 'source_role': 'manufacturer'},
                    retrieval=retrieval)
                entry.update(download_status='saved', document_id=doc['id'])
                # A shared instruction can reference multiple materials, without overwriting associations.
                link_id = identity(material['material_id'], doc['id'])
                atomic_json(Path(root)/'links'/(link_id+'.json'), {
                    'id': link_id, 'material_id': material['material_id'], 'document_id': doc['id'],
                    'relation': 'linked_by_manufacturer', 'review_status': 'unreviewed',
                    'evidence': entry.get('evidence')})
                ids.append(doc['id'])
            except Exception as exc:
                entry.update(download_status='error', error=type(exc).__name__+': '+str(exc))
                errors.append({'url': entry['url'], 'error': entry['error']})
    atomic_json(report_path, report)
    result = {'document_ids': sorted(set(ids)), 'errors': errors}
    atomic_json(Path(root)/'manufacturer_documents_report.json', result)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    c = commands.add_parser('catalogue', help='Run the existing multi-process TECHNONICOL crawler')
    c.add_argument('arguments', nargs=argparse.REMAINDER)
    c = commands.add_parser('documents', help='Download manufacturer documents from a materials report')
    c.add_argument('--materials', type=Path, required=True)
    c.add_argument('--root', type=Path, default=Path('corpus'))
    c.add_argument('--max-documents', type=int, default=10)
    c.add_argument('--refresh', action='store_true')
    c = commands.add_parser('import-document', help='Import a supplied PDF or HTML source')
    c.add_argument('path', type=Path)
    c.add_argument('--url', required=True, help='Original source URL, retained as provenance')
    c.add_argument('--kind', choices=['manufacturer_document', 'normative_document'], default='manufacturer_document')
    c.add_argument('--metadata', type=Path, help='JSON metadata; status remains unverified')
    c.add_argument('--root', type=Path, default=Path('corpus'))
    c = commands.add_parser('collect', help='Download explicit document URLs from a JSON manifest')
    c.add_argument('manifest', type=Path)
    c.add_argument('--root', type=Path, default=Path('corpus'))
    c.add_argument('--refresh', action='store_true')
    c = commands.add_parser('registry', help='Collect official ГОСТ/СП registry metadata and linked PDFs')
    c.add_argument('manifest', type=Path)
    c.add_argument('--root', type=Path, default=Path('corpus'))
    c.add_argument('--refresh', action='store_true')
    c.add_argument('--download-text', action='store_true')
    c = commands.add_parser('export-nlp')
    c.add_argument('--root', type=Path, default=Path('corpus'))
    c.add_argument('--materials', type=Path)
    c.add_argument('--out', type=Path, required=True)
    c = commands.add_parser('offers-import', help='Validate retailer observations, keeping technical data separate')
    c.add_argument('path', type=Path)
    c.add_argument('--out', type=Path, required=True)
    c = commands.add_parser('offers-compare')
    c.add_argument('path', type=Path)
    for name in ['material-id', 'variant-id', 'city', 'unit']:
        c.add_argument('--'+name, required=True)
    c.add_argument('--quantity', required=True)
    c.add_argument('--currency', default='RUB')
    c.add_argument('--lat', type=float)
    c.add_argument('--lon', type=float)
    c.add_argument('--radius-km', type=float)
    c.add_argument('--include-nearby', action='store_true', help='Allow other cities within the radius')
    c.add_argument('--district')
    c.add_argument('--max-age-hours', type=float, default=24)
    c.add_argument('--delivered', action='store_true')
    c.add_argument('--delivery-destination')
    c.add_argument('--out', type=Path, required=True)
    commands.add_parser('sources', help='List configured official source host allowlists')
    commands.add_parser('db-init', help='Apply PostgreSQL schema using DATABASE_URL')
    c = commands.add_parser('db-import', help='Idempotently persist a corpus, materials and/or offers')
    c.add_argument('--root', type=Path, default=Path('corpus'))
    c.add_argument('--materials', type=Path)
    c.add_argument('--offers', type=Path)
    c = commands.add_parser('enqueue', help='Persist document collection jobs from a manifest')
    c.add_argument('manifest', type=Path)
    c.add_argument('--run-id', required=True)
    c = commands.add_parser('queue-worker', help='Process durable PostgreSQL document jobs')
    c.add_argument('--root', type=Path, default=Path('corpus'))
    c.add_argument('--agent-id', required=True)
    c.add_argument('--lease-seconds', type=int, default=120)
    c.add_argument('--once', action='store_true')
    args = p.parse_args(argv)
    if args.command == 'catalogue':
        import parser as catalogue
        arguments = args.arguments
        if arguments and arguments[0] == '--':
            arguments = arguments[1:]
        old = sys.argv
        try:
            sys.argv = ['parser.py'] + arguments
            return catalogue.main()
        finally:
            sys.argv = old
    if args.command == 'documents':
        if args.max_documents < 1:
            p.error('--max-documents must be positive')
        result = collect_documents(args.materials, args.root, args.max_documents, args.refresh)
    elif args.command == 'import-document':
        if args.path.stat().st_size > 32*1024*1024:
            p.error('Document exceeds 32 MiB limit')
        result = ingest(args.path.read_bytes(), args.root, args.url, args.kind,
                        read_json(args.metadata) if args.metadata else None)
        result = {'document_id': result['id'], 'quality_issues': result['quality_issues']}
    elif args.command == 'collect':
        result = collect_manifest(args.manifest, args.root, args.refresh)
    elif args.command == 'registry':
        from .regulations import collect_registry
        result = collect_registry(args.manifest, args.root, args.refresh, args.download_text)
    elif args.command == 'export-nlp':
        result = {'records': export_nlp(args.root, args.out, args.materials), 'path': str(args.out)}
    elif args.command == 'offers-import':
        result = [validate_offer(x) for x in read_json(args.path)]
        atomic_json(args.out, result)
        result = {'offers': len(result), 'path': str(args.out)}
    elif args.command == 'offers-compare':
        if (args.lat is None) != (args.lon is None):
            p.error('--lat and --lon must be provided together')
        origin = {'lat': args.lat, 'lon': args.lon} if args.lat is not None else None
        result = rank_offers(read_json(args.path), args.material_id, args.city, args.quantity,
            args.unit, args.currency, origin, args.radius_km, args.delivered, args.max_age_hours,
            variant_id=args.variant_id, delivery_destination=args.delivery_destination,
            include_nearby=args.include_nearby, district=args.district)
        atomic_json(args.out, result)
    elif args.command == 'sources':
        result = SOURCE_HOSTS
    else:
        from . import storage
        if args.command == 'db-init':
            storage.initialize()
            result = {'schema': 'initialized'}
        elif args.command == 'db-import':
            result = storage.import_corpus(args.root, args.materials, args.offers)
        elif args.command == 'enqueue':
            count = storage.enqueue_manifest(read_json(args.manifest), args.run_id)
            result = {'enqueued': count}
        else:
            count = storage.work(args.root, args.agent_id, args.lease_seconds, args.once)
            result = {'completed': count}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if isinstance(result, dict) and result.get('errors') else 0


def entrypoint():
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as exc:
        raise SystemExit(str(exc))
