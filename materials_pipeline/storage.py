"""PostgreSQL persistence and leased document jobs; file corpus must be shared."""
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from .core import identity
from .documents import SOURCE_HOSTS, checked_url, download, ingest
from .offers import validate_offer

log = logging.getLogger(__name__)


def connect():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        raise ValueError('Install PostgreSQL support: pip install -e .[postgres]') from None
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        raise ValueError('Set DATABASE_URL')
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10)


def jsonb(value):
    from psycopg.types.json import Jsonb
    return Jsonb(value)


def initialize():
    with connect() as db:
        db.execute(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))


def save_document(db, doc):
    db.execute("""INSERT INTO pipeline_documents(id,source_url,sha256,kind,record)
        VALUES (%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET record=EXCLUDED.record""",
        (doc['id'],doc['source_url'],doc['sha256'],doc['kind'],jsonb(doc)))


def import_corpus(root, materials=None, offers=None):
    root=Path(root)
    counts={'documents':0,'materials':0,'links':0,'offers':0,'registry_cards':0}
    with connect() as db:
        for path in sorted((root/'registry').glob('*.json')):
            card=json.loads(path.read_text(encoding='utf-8'))
            db.execute('INSERT INTO pipeline_registry_cards(id,designation,source_url,sha256,record) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING',
                (card['id'],card['designation'],card['source_url'],card['sha256'],jsonb(card)))
            counts['registry_cards']+=1
        for path in sorted((root/'documents').glob('*.json')):
            save_document(db,json.loads(path.read_text(encoding='utf-8')))
            counts['documents']+=1
        for path in sorted((root/'links').glob('*.json')):
            link=json.loads(path.read_text(encoding='utf-8'))
            db.execute("""INSERT INTO pipeline_material_document_links(id,material_id,document_id,record)
                VALUES(%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                (link['id'],link['material_id'],link['document_id'],jsonb(link)))
            counts['links']+=1
        if materials:
            report=json.loads(Path(materials).read_text(encoding='utf-8'))
            for item in report['materials']:
                key=identity(item['material_id'],item['html_sha256'])
                db.execute("""INSERT INTO pipeline_material_snapshots(id,material_id,sha256,record)
                    VALUES(%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET record=EXCLUDED.record""",
                    (key,item['material_id'],item['html_sha256'],jsonb(item)))
                counts['materials']+=1
        if offers:
            for raw in json.loads(Path(offers).read_text(encoding='utf-8')):
                offer=validate_offer(raw)
                db.execute("""INSERT INTO pipeline_offer_observations(id,material_id,retailer,city,observed_at,record)
                    VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                    (offer['id'],offer.get('material_id'),offer['retailer'],offer['city'],offer['observed_at'],jsonb(offer)))
                counts['offers']+=1
    return counts


def enqueue_manifest(entries, run_id):
    if not isinstance(entries,list) or not run_id:
        raise ValueError('Manifest must be a list; run_id is required')
    count=0
    with connect() as db:
        for entry in entries:
            source=entry['source']
            checked_url(entry['url'],SOURCE_HOSTS[source])
            key=identity(run_id,entry)
            cursor=db.execute("""INSERT INTO pipeline_tasks(id,run_id,payload) VALUES(%s,%s,%s)
                ON CONFLICT(id) DO NOTHING""",(key,run_id,jsonb(entry)))
            count+=cursor.rowcount
    return count


def claim(agent_id, lease_seconds):
    if lease_seconds < 10 or not agent_id:
        raise ValueError('lease_seconds must be >=10; agent_id is required')
    token=uuid.uuid4().hex
    with connect() as db:
        db.execute("""UPDATE pipeline_tasks SET state='failed',owner=NULL,token=NULL,lease_until=NULL,
            error='Attempt limit exhausted after worker loss',updated_at=clock_timestamp()
            WHERE state='running' AND lease_until<clock_timestamp() AND attempts>=max_attempts""")
        return db.execute("""WITH candidate AS (
            SELECT id FROM pipeline_tasks WHERE attempts<max_attempts AND
              ((state IN ('pending','retry_wait') AND available_at<=clock_timestamp())
                OR (state='running' AND lease_until<clock_timestamp()))
            ORDER BY available_at,created_at,id FOR UPDATE SKIP LOCKED LIMIT 1
          ) UPDATE pipeline_tasks t SET state='running',owner=%s,token=%s,
            attempts=t.attempts+1,lease_until=clock_timestamp()+(%s * interval '1 second'),
            updated_at=clock_timestamp()
          FROM candidate c WHERE t.id=c.id RETURNING t.*""",(agent_id,token,lease_seconds)).fetchone()


def heartbeat(job, lease_seconds):
    with connect() as db:
        return db.execute("""UPDATE pipeline_tasks SET lease_until=clock_timestamp()+(%s * interval '1 second'),
            updated_at=clock_timestamp() WHERE id=%s AND state='running' AND owner=%s AND token=%s
            AND lease_until>clock_timestamp()""",
            (lease_seconds,job['id'],job['owner'],job['token'])).rowcount == 1


def finish(job, doc):
    with connect() as db:
        owned=db.execute("""SELECT id FROM pipeline_tasks WHERE id=%s AND state='running'
            AND owner=%s AND token=%s AND lease_until>clock_timestamp() FOR UPDATE""",
            (job['id'],job['owner'],job['token'])).fetchone()
        if not owned:
            raise ValueError('Lease lost; stale result rejected')
        save_document(db,doc)
        db.execute("""UPDATE pipeline_tasks SET state='succeeded',document_id=%s,owner=NULL,token=NULL,
            lease_until=NULL,error=NULL,updated_at=clock_timestamp() WHERE id=%s""",(doc['id'],job['id']))


def fail(job, exc):
    review=isinstance(exc, ValueError) or isinstance(exc,HTTPError) and exc.code in [401,403]
    permanent=isinstance(exc,HTTPError) and exc.code in [404,410]
    state='needs_review' if review else 'failed' if permanent or job['attempts']>=job['max_attempts'] else 'retry_wait'
    import random
    delay=min(3600,2**job['attempts']*5)+random.uniform(0,3)
    if isinstance(exc,HTTPError) and exc.code == 429:
        value=exc.headers.get('Retry-After','') if exc.headers else ''
        if value.isdigit():
            delay=max(delay,int(value))
    with connect() as db:
        db.execute("""UPDATE pipeline_tasks SET state=%s,available_at=clock_timestamp()+(%s * interval '1 second'),
            owner=NULL,token=NULL,lease_until=NULL,error=%s,updated_at=clock_timestamp()
            WHERE id=%s AND state='running' AND owner=%s AND token=%s AND lease_until>clock_timestamp()""",
            (state,delay,type(exc).__name__+': '+str(exc),job['id'],job['owner'],job['token']))


def work(root, agent_id, lease_seconds=120, once=False):
    completed=0
    while True:
        job=claim(agent_id,lease_seconds)
        if job is None:
            if once:
                return completed
            time.sleep(1)
            continue
        stopped=threading.Event()
        def renew(job=job, stopped=stopped):
            while not stopped.wait(lease_seconds/3):
                try:
                    if not heartbeat(job,lease_seconds):
                        return
                except Exception:
                    log.exception('Heartbeat failed for task %s',job['id'])
                    return
        thread=threading.Thread(target=renew,daemon=True)
        thread.start()
        try:
            entry=job['payload']
            data,retrieval=download(entry['url'],root,entry['source'],refresh=entry.get('refresh',False))
            doc=ingest(data,root,entry['url'],entry.get('kind','normative_document'),entry.get('metadata'),retrieval)
            finish(job,doc)
            completed+=1
        except Exception as exc:
            fail(job,exc)
            log.warning('Task %s attempt %s failed: %s',job['id'],job['attempts'],exc)
        finally:
            stopped.set()
            thread.join(timeout=15)
        if once:
            return completed
