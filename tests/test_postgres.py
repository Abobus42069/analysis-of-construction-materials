"""Opt-in integration tests; isolated schema in a dedicated test database."""
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
from materials_pipeline import storage
from materials_pipeline.documents import ingest


@unittest.skipUnless(os.environ.get('RUN_POSTGRES_TESTS')=='1' and os.environ.get('DATABASE_URL'),
                     'Set RUN_POSTGRES_TESTS=1 and DATABASE_URL for a dedicated PostgreSQL test database')
class PostgresTests(unittest.TestCase):
    def setUp(self):
        self.schema='pipeline_test_'+uuid.uuid4().hex
        import psycopg
        from psycopg import sql
        self.admin=psycopg.connect(os.environ['DATABASE_URL'],autocommit=True)
        self.admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.environment=patch.dict(os.environ,{'PGOPTIONS':'-c search_path='+self.schema})
        self.environment.start()
        storage.initialize()
        self.entry={'source':'technonicol','url':'https://www.tn.ru/upload/synthetic.pdf'}

    def tearDown(self):
        from psycopg import sql
        self.environment.stop()
        self.admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        self.admin.close()

    def test_enqueue_claim_recovery_and_stale_token_rejection(self):
        self.assertEqual(storage.enqueue_manifest([self.entry],'run1'),1)
        self.assertEqual(storage.enqueue_manifest([self.entry],'run1'),0)
        first=storage.claim('agent-a',30)
        self.assertIsNotNone(first)
        self.assertIsNone(storage.claim('agent-b',30))
        self.assertTrue(storage.heartbeat(first,30))
        with storage.connect() as db:
            db.execute("UPDATE pipeline_tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(first['id'],))
        second=storage.claim('agent-b',30)
        self.assertEqual(second['id'],first['id'])
        self.assertNotEqual(second['token'],first['token'])
        with tempfile.TemporaryDirectory() as root:
            doc=ingest(b'<html><body><p>synthetic</p></body></html>',root,self.entry['url'])
            with self.assertRaises(ValueError):
                storage.finish(first,doc)
            storage.finish(second,doc)
        with storage.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM pipeline_tasks WHERE id=%s',(first['id'],)).fetchone()['state'],'succeeded')
            self.assertEqual(db.execute('SELECT count(*) AS n FROM pipeline_documents').fetchone()['n'],1)

    def test_exhausted_expired_task_becomes_failed(self):
        storage.enqueue_manifest([self.entry],'run2')
        job=storage.claim('agent-a',30)
        with storage.connect() as db:
            db.execute("UPDATE pipeline_tasks SET attempts=max_attempts,lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(job['id'],))
        self.assertIsNone(storage.claim('agent-b',30))
        with storage.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM pipeline_tasks WHERE id=%s',(job['id'],)).fetchone()['state'],'failed')
