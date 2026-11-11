import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from materials_pipeline.core import atomic_json
from materials_pipeline.regulations import parse_registry_card, collect_registry
from materials_pipeline.export import export_nlp

URL='https://protect.gost.ru/sp/details/synthetic'
HTML="""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<h1>СП 123.456.2026</h1><p>Действует</p><h2>Основные сведения</h2>
<dl><dt>Обозначение</dt><dd>СП 123.456.2026</dd>
<dt>Заглавие на русском языке</dt><dd>СИНТЕТИЧЕСКИЙ ПРИМЕР, НЕ НОРМАТИВ</dd>
<dt>Область применения</dt><dd>Для тестового извлечения.</dd><dd>Исключение должно сохраниться.</dd>
</dl><h2>Ссылочные данные</h2><dl><dt>Обозначение заменяемого(ых)</dt><dd>СП 123.456.2020</dd></dl>
<h2>Документы не найдены</h2></body></html>""".encode()


class RegistryTests(unittest.TestCase):
    def test_scope_exceptions_replacements_and_status_are_preserved(self):
        card=parse_registry_card(HTML,URL)
        self.assertEqual(card['designation'],'СП 123.456.2026')
        self.assertEqual(card['fields']['replaces_raw'],'СП 123.456.2020')
        self.assertIn('Исключение',card['fields']['scope_raw'])
        self.assertNotIn('Ссылочные данные',card['fields']['scope_raw'])
        self.assertEqual(card['declared_status'],'Действует')
        self.assertEqual(card['status'],'unverified')
        self.assertEqual(card['full_text_status'],'not_found')
        self.assertEqual(card['mandatory_status'],'not_assessed')

    def test_collect_does_not_confuse_registry_with_full_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            manifest=root/'manifest.json';atomic_json(manifest,[{'source':'rosstandart','url':URL}])
            with patch('materials_pipeline.regulations.download',return_value=(HTML,{})):
                result=collect_registry(manifest,root,download_text=True)
            self.assertEqual(len(result['registry_cards']),1)
            self.assertEqual(result['documents'],[])
            self.assertEqual(result['errors'],[])
            out=root/'out.jsonl';export_nlp(root,out)
            self.assertEqual(json.loads(out.read_text(encoding='utf-8').splitlines()[0])['record_type'],'normative_registry_card')

    def test_pdf_links_are_candidates_and_external_hosts_are_not_trusted(self):
        data=HTML.replace(b'</body>',b'<a href="https://example.org/doc.pdf">PDF</a></body>')
        card=parse_registry_card(data,URL)
        self.assertEqual(card['full_text_status'],'linked_unverified')
        self.assertIsNone(card['pdf_candidates'][0]['source'])

    def test_wrong_page_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_registry_card(b'<html><h1>Sign in</h1></html>',URL)
