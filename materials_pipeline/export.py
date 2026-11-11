"""Streaming NLP JSONL. Text, source metadata and candidate facts stay distinct."""
import json
from pathlib import Path
from .core import identity, SCHEMA_VERSION
from .documents import chunks


def iter_records(root, materials=None):
    root=Path(root)
    for path in sorted((root/'registry').glob('*.json')):
        yield json.loads(path.read_text(encoding='utf-8'))
    for path in sorted((root/'documents').glob('*.json')):
        doc=json.loads(path.read_text(encoding='utf-8'))
        yield {'schema_version':SCHEMA_VERSION,'record_type':'document_metadata',
            'id':identity(doc['id'],'metadata'),'document_id':doc['id'],
            'source_url':doc['source_url'],'sha256':doc['sha256'],'kind':doc['kind'],
            'metadata':doc.get('metadata',{}),'normative':doc.get('normative'),
            'quality_issues':doc['quality_issues'],'raw_path':doc['raw_path'],
            'pages':[{'page':p['page'],'extraction_status':p['extraction_status']} for p in doc['pages']]}
        for ref in doc.get('regulatory_references',[]):
            yield {'schema_version':SCHEMA_VERSION,'record_type':'regulatory_reference',
                   'id':identity(doc['id'],ref),'document_id':doc['id'],**ref}
        for chunk in chunks(doc['id'],doc['pages'],doc['source_url'],doc['sha256']):
            chunk.update(document_kind=doc['kind'],metadata=doc.get('metadata',{}),
                         normative=doc.get('normative'),quality_issues=doc['quality_issues'])
            yield chunk
    for path in sorted((root/'links').glob('*.json')):
        yield {'schema_version':SCHEMA_VERSION,'record_type':'material_document_link',
               **json.loads(path.read_text(encoding='utf-8'))}
    if not materials:
        return
    report=json.loads(Path(materials).read_text(encoding='utf-8'))
    for item in report['materials']:
        snapshot_id=identity(item['material_id'],item['html_sha256'])
        yield {'schema_version':SCHEMA_VERSION,'record_type':'material_metadata',
            'id':identity(snapshot_id,'metadata'),'material_id':item['material_id'],
            'snapshot_id':snapshot_id,'name':item['name'],'manufacturer':item['manufacturer'],
            'category':item['category'],'category_profile':item.get('category_profile'),
            'identifiers':item.get('identifiers',{}),'source_url':item['url'],
            'sha256':item['html_sha256'],'review_status':item.get('review_status','unreviewed')}
        for section in item.get('sections',[]):
            for chunk in chunks(item['material_id'],[{'page':None,'text':section['text']}],
                                item['url'],item['html_sha256']):
                chunk['id']=identity(section['id'],chunk['start_char'],chunk['end_char'])
                chunk.pop('document_id',None)
                chunk.update(record_type='catalogue_chunk',material_id=item['material_id'],
                             snapshot_id=snapshot_id,
                             section=section['section'],section_id=section['id'])
                chunk['evidence']['locator']=section['evidence']['locator']
                yield chunk
        for ref in item.get('regulatory_references',[]):
            yield {'schema_version':SCHEMA_VERSION,'record_type':'regulatory_reference',
                   'id':identity(item['material_id'],ref),'material_id':item['material_id'],**ref}
        for kind,key in [('property','properties'),('applicability_statement','applicability')]:
            for fact in item.get(key,[]):
                yield {'schema_version':SCHEMA_VERSION,'record_type':kind,
                       'material_id':item['material_id'],**fact}


def export_nlp(root,destination,materials=None):
    destination=Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    import uuid
    temp=destination.with_name(destination.name+'.'+uuid.uuid4().hex+'.tmp')
    count=0
    try:
        with temp.open('w',encoding='utf-8') as out:
            for record in iter_records(root,materials):
                out.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
                count+=1
        temp.replace(destination)
    finally:
        if temp.exists():
            temp.unlink()
    return count
