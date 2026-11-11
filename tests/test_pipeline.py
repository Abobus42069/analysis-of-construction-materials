import copy
import hashlib
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import parser
from materials_pipeline.core import normalize_value, references, evidence, atomic_json
from materials_pipeline.manufacturers import parse_product
from materials_pipeline.documents import (ingest, extract_pages, chunks, checked_url,
    Redirects, collect_manifest, download)
from materials_pipeline.export import export_nlp
from materials_pipeline.offers import validate_offer, rank_offers, distance_km
from materials_pipeline.cli import collect_documents

FIXTURES=Path(__file__).with_name('fixtures')
URL='https://www.tn.ru/catalogue/synthetic/'


def sample_offer(**changes):
    result={'retailer':'synthetic', 'retailer_sku':'sku1', 'source_url':'https://example.org/sku1',
        'observed_at':(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat(),
        'city':'Москва','material_id':'mat1','manufacturer_variant_id':'variant1',
        'matching_status':'confirmed','availability':'in_stock','price':'100.00',
        'currency':'RUB','vat_included':True,'pack_quantity':'2','quantity_unit':'m2',
        'coordinates':{'lat':55.75,'lon':37.62}}
    result.update(changes)
    return result


def compare(offers, **changes):
    values={'material_id':'mat1','city':'Москва','quantity':'5','unit':'m2','variant_id':'variant1'}
    values.update(changes)
    return rank_offers(offers, **values)


class ManufacturerTests(unittest.TestCase):
    def setUp(self):
        self.data=(FIXTURES/'manufacturer.html').read_bytes()
        self.item=parse_product(self.data, URL, 'Монтажные пены')

    def test_data_href_and_query_pdf_are_discovered_and_deduplicated(self):
        docs=self.item['documents']
        self.assertEqual(len(docs),2)
        self.assertEqual(docs[0]['url'],'https://www.tn.ru/upload/synthetic.pdf?version=1')
        self.assertEqual(docs[0]['document_type'],'technical_sheet')
        self.assertEqual(self.item['identifiers']['sku'],'SYNTHETIC-001')

    def test_application_and_storage_stay_separate_and_uninterpreted(self):
        items=self.item['applicability']
        self.assertTrue(any('полиэтилене' in x['raw_text'] and x['aspect']=='application' for x in items))
        self.assertTrue(any(x['aspect']=='storage' and '+25' in x['raw_text'] for x in items))
        self.assertTrue(any(x['aspect']=='transport' for x in items))
        self.assertTrue(all(x['polarity']=='unknown' and x['interpretation_status']=='requires_nlp' for x in items))

    def test_numbers_ranges_comparators_and_evidence(self):
        scalar, interval=self.item['properties']
        self.assertEqual(scalar['numeric_value'],0.5)
        self.assertEqual(scalar['comparator'],'>=')
        self.assertEqual(scalar['test_method_raw'],'ГОСТ Р 59985-2022')
        self.assertEqual(interval['property_key'],'service_temperature')
        self.assertIsNone(interval['numeric_value'])
        self.assertEqual(interval['raw_value'],'от -40 до +80')
        self.assertEqual(scalar['evidence']['content_sha256'],hashlib.sha256(self.data).hexdigest())
        self.assertIn('/tr[2]',scalar['evidence']['locator'])
        self.assertEqual(self.item['regulatory_references'][0]['review_status'],'unverified')

    def test_mineral_insulation_profile(self):
        item=parse_product(self.data.replace('Тестовая пена'.encode(),'Каменная вата'.encode()),URL,'Минеральная изоляция')
        self.assertEqual(item['category_profile'],'mineral_insulation')

    def test_pagination_visits_second_page(self):
        page1=b'first'; page2=b'second'
        next_url=URL+'?PAGEN_3=2'
        def discovery(data,url):
            return ('Category',[URL+'a/'],[next_url]) if data==page1 else ('Category',[URL+'b/'],[])
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sys,'argv',['parser.py','--category',URL,'--limit','2','--out',directory]), \
                 patch.object(parser,'fetch',side_effect=[(page1,True),(page2,True)]) as fetch, \
                 patch.object(parser,'discover',side_effect=discovery), \
                 patch.object(parser,'collect_products') as collect:
                self.assertEqual(parser.main(),0)
                self.assertEqual(fetch.call_count,2)
                self.assertEqual(collect.call_args.args[0],[URL+'a/',URL+'b/'])

    def test_cached_real_cards_keep_all_original_facts(self):
        saved=Path(__file__).resolve().parents[1]/'output/materials.json'
        if not saved.exists():
            self.skipTest('Optional local source cache is absent')
        report=json.loads(saved.read_text(encoding='utf-8'))
        for old in report['materials']:
            path=saved.parent/'html_cache'/(hashlib.sha256(old['url'].encode()).hexdigest()+'.html')
            item=parse_product(path.read_bytes(),old['url'],old['category'],True)
            self.assertEqual(item['characteristics'],old['characteristics'])
            self.assertTrue(item['applicability'])
            self.assertTrue(item['documents'])


class CorpusTests(unittest.TestCase):
    def test_references_include_dotted_sp_year(self):
        text='СП 50.13330.2024, ГОСТ Р 59985-2022 и СанПиН 1.2.3685-21'
        refs=references(text,evidence(URL,'hash','test',text))
        self.assertEqual([r['designation'] for r in refs],['СП 50.13330.2024','ГОСТ Р 59985-2022','СанПиН 1.2.3685-21'])
        self.assertTrue(all(r['relation']=='mentioned_in_source' for r in refs))

    def test_normative_import_idempotency_versions_and_nlp_offsets(self):
        data=(FIXTURES/'normative.html').read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            doc=ingest(data,root,URL,'normative_document',{'designation':'SYNTHETIC','declared_status':'active'})
            again=ingest(data,root,URL,'normative_document')
            self.assertEqual(doc['id'],again['id'])
            self.assertEqual(doc['normative']['status'],'unverified')
            self.assertEqual(doc['normative']['mandatory_status'],'not_assessed')
            self.assertEqual(len(doc['regulatory_references']),3)
            self.assertEqual(len(doc['clause_candidates']),3)
            self.assertNotIn('not source text',doc['pages'][0]['text'])
            self.assertEqual((root/doc['raw_path']).read_bytes(),data)
            revised=ingest(data.replace(b'2.1',b'2.2'),root,URL,'normative_document')
            self.assertNotEqual(doc['id'],revised['id'])
            out=root/'nlp.jsonl'
            count=export_nlp(root,out)
            self.assertGreater(count,0)
            records=[json.loads(x) for x in out.read_text(encoding='utf-8').splitlines()]
            self.assertTrue(all('normative' in x for x in records if x['record_type']=='document_chunk'))
            for chunk in chunks(doc['id'],doc['pages'],URL,doc['sha256'],size=35):
                self.assertEqual(chunk['text'],doc['pages'][0]['text'][chunk['start_char']:chunk['end_char']])

    def test_references_without_year_remain_unresolved(self):
        text='ГОСТ 12.1.005, ГОСТ 530 и СП 230.1325800'
        refs=references(text,evidence(URL,'hash','test',text))
        self.assertEqual([r['designation'] for r in refs],['ГОСТ 12.1.005','ГОСТ 530','СП 230.1325800'])
        self.assertTrue(all(r['edition_resolution']=='not_resolved' for r in refs))

    def test_ocr_documents_have_metadata_in_nlp_export(self):
        from pypdf import PdfWriter
        writer=PdfWriter();writer.add_blank_page(width=100,height=100)
        data=io.BytesIO();writer.write(data)
        with tempfile.TemporaryDirectory() as directory:
            ingest(data.getvalue(),directory,URL)
            out=Path(directory)/'out.jsonl';export_nlp(directory,out)
            records=[json.loads(x) for x in out.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(records[0]['record_type'],'document_metadata')
            self.assertIn('ocr_required',records[0]['quality_issues'])
            self.assertFalse(any(x['record_type']=='document_chunk' for x in records))

    def test_blank_pdf_marks_ocr_and_retains_page(self):
        from pypdf import PdfWriter
        writer=PdfWriter(); writer.add_blank_page(width=100,height=100)
        target=io.BytesIO(); writer.write(target)
        pages,fmt=extract_pages(target.getvalue())
        self.assertEqual(fmt,'pdf')
        self.assertEqual(pages[0]['page'],1)
        self.assertEqual(pages[0]['extraction_status'],'needs_ocr')
        with self.assertRaises(ValueError):
            extract_pages(target.getvalue(),max_pages=0)

    def test_pdf_text_extraction_and_page_number(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer=PdfWriter(); page=writer.add_blank_page(width=300,height=300)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),
             NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
        stream=DecodedStreamObject(); stream.set_data(b'BT /F1 12 Tf 20 250 Td (SYNTHETIC TEST DOCUMENT) Tj ET BT /F1 12 Tf 0 1 -1 0 100 100 Tm (ROTATED EVIDENCE) Tj ET')
        page[NameObject('/Contents')]=stream
        output=io.BytesIO(); writer.write(output)
        pages,_=extract_pages(output.getvalue())
        self.assertIn('SYNTHETIC TEST DOCUMENT',pages[0]['text'])
        self.assertIn('ROTATED EVIDENCE',pages[0]['text'])
        self.assertEqual(pages[0]['extraction_status'],'text_extracted')

    def test_download_host_and_redirect_policy(self):
        for url in ['http://www.tn.ru/a.pdf','https://www.tn.ru.evil.org/a.pdf',
                    'https://www.tn.ru@evil.org/a.pdf','https://www.tn.ru:444/a.pdf']:
            with self.assertRaises(ValueError):
                checked_url(url,['www.tn.ru'])
        self.assertEqual(checked_url('https://www.tn.ru/upload/a.pdf#x',['www.tn.ru']),
                         'https://www.tn.ru/upload/a.pdf')
        with self.assertRaises(ValueError):
            Redirects(['www.tn.ru']).redirect_request(None,None,302,'',{},'https://evil.org/test.pdf')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                download('https://example.org/a.pdf',directory,'technonicol')

    def test_wrong_content_and_manifest_errors_are_explicit(self):
        with self.assertRaises(ValueError):
            extract_pages(b'{"not":"a document"}')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'manifest.json'
            atomic_json(path,[{'source':'technonicol','url':'https://example.org/wrong.pdf'}])
            result=collect_manifest(path,directory)
            self.assertEqual(result['documents'],[])
            self.assertEqual(len(result['errors']),1)

    def test_material_metadata_and_snapshot_are_exported(self):
        item=parse_product((FIXTURES/'manufacturer.html').read_bytes(),URL,'Монтажные пены')
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)/'materials.json';atomic_json(report,{'materials':[item]})
            out=Path(directory)/'out.jsonl';export_nlp(directory,out,report)
            records=[json.loads(x) for x in out.read_text(encoding='utf-8').splitlines()]
            metadata=next(x for x in records if x['record_type']=='material_metadata')
            chunk=next(x for x in records if x['record_type']=='catalogue_chunk')
            self.assertEqual(metadata['name'],item['name'])
            self.assertEqual(metadata['snapshot_id'],chunk['snapshot_id'])
            self.assertNotIn('document_id',chunk)

    def test_shared_document_keeps_both_material_links(self):
        data=(FIXTURES/'normative.html').read_bytes()
        entries=[{'material_id':m,'documents':[{'url':URL,'title':'Shared instruction'}]} for m in ['a','b']]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'materials.json'; atomic_json(path,{'materials':entries})
            with patch('materials_pipeline.cli.download',return_value=(data,{})):
                result=collect_documents(path,directory)
            self.assertEqual(len(result['document_ids']),1)
            self.assertEqual(len(list((Path(directory)/'links').glob('*.json'))),2)

    def test_cli_import_and_export_in_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            cmd=[sys.executable,'-X','utf8','-B','-m','materials_pipeline']
            result=subprocess.run(cmd+['import-document',str(FIXTURES/'normative.html'),
                '--url',URL,'--kind','normative_document','--root',directory],capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(result.returncode,0,result.stderr)
            result=subprocess.run(cmd+['export-nlp','--root',directory,'--out',str(Path(directory)/'out.jsonl')],
                                  capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertGreater(json.loads(result.stdout)['records'],0)


class OfferTests(unittest.TestCase):
    def test_pack_rounding_changes_cheapest_choice(self):
        a=sample_offer(retailer='a',price='100',pack_quantity='2')
        b=sample_offer(retailer='b',price='260',pack_quantity='6')
        ranked=compare([a,b])['ranked']
        self.assertEqual(ranked[0]['retailer'],'b')
        self.assertEqual(ranked[0]['comparison_total'],'260')
        self.assertEqual(ranked[1]['required_packs'],3)

    def test_unmatched_wrong_variant_city_unit_and_stale_are_excluded(self):
        offers=[sample_offer(matching_status='needs_review'),sample_offer(manufacturer_variant_id='different'),
                sample_offer(city='Тула'),sample_offer(quantity_unit='kg'),
                sample_offer(observed_at=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat())]
        result=compare(offers)
        self.assertEqual(result['ranked'],[])
        self.assertEqual(len(result['excluded']),5)

    def test_geo_radius_and_missing_coordinates(self):
        result=compare([sample_offer(),sample_offer(coordinates=None),
            sample_offer(coordinates={'lat':59.93,'lon':30.31})],
            origin={'lat':55.75,'lon':37.62},radius_km=10)
        self.assertEqual(len(result['ranked']),1)
        self.assertAlmostEqual(result['ranked'][0]['distance_km'],0)
        self.assertGreater(distance_km({'lat':55.75,'lon':37.62},{'lat':59.93,'lon':30.31}),600)

    def test_nearby_cities_and_district_are_explicit_filters(self):
        offer=sample_offer(city='Химки',district='Центральный')
        self.assertEqual(compare([offer])['ranked'],[])
        result=compare([offer],origin={'lat':55.75,'lon':37.62},radius_km=20,include_nearby=True,district='Центральный')
        self.assertEqual(len(result['ranked']),1)
        self.assertEqual(compare([offer],origin={'lat':55.75,'lon':37.62},radius_km=20,include_nearby=True,district='Другой')['ranked'],[])
        with self.assertRaises(ValueError):
            compare([offer],include_nearby=True)

    def test_unknown_delivery_never_becomes_free(self):
        result=compare([sample_offer(),sample_offer(retailer='b',delivery_price='200',delivery_destination='site-1')],
                       delivered=True,delivery_destination='site-1')
        self.assertEqual(len(result['ranked']),1)
        self.assertEqual(result['ranked'][0]['comparison_total'],'500.00')
        self.assertEqual(len(compare([sample_offer(delivery_price='0',delivery_destination='site-2')],
            delivered=True,delivery_destination='site-1')['ranked']),0)

    def test_minimum_order_and_stock(self):
        ranked=compare([sample_offer(minimum_packs=5)])['ranked']
        self.assertEqual(ranked[0]['required_packs'],5)
        self.assertEqual(compare([sample_offer(available_packs=2)])['ranked'],[])

    def test_bad_price_coordinates_dates_and_missing_variant_are_rejected(self):
        for fields in [{'price':'NaN'},{'price':'-1'},{'pack_quantity':0},
                       {'coordinates':{'lat':100,'lon':0}},{'observed_at':'2026-01-01'},
                       {'manufacturer_variant_id':None},{'minimum_packs':1.5}]:
            with self.subTest(fields=fields),self.assertRaises(ValueError):
                validate_offer(sample_offer(**fields))
        with self.assertRaises(ValueError):
            compare([sample_offer()],variant_id=None)
        with self.assertRaises(ValueError):
            compare([sample_offer()],delivered=True)

    def test_price_identity_is_consistent_for_decimal_inputs(self):
        offer=sample_offer(price='100.00')
        self.assertEqual(validate_offer(offer)['id'],validate_offer(copy.deepcopy(offer))['id'])


if __name__=='__main__':
    unittest.main()
