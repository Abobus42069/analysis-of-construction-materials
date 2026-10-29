import os
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import parser

HTML = '''<html><h1>Тестовый материал</h1><div class="tn-product-text">Описание</div>
<div class="tn-product-tabs"><div class="tn-product-tabs-box"><table>
<tr><td>Наименование показателя</td><td>Ед. изм.</td><td>Критерий</td><td>Значение</td></tr>
<tr><td>Адгезия</td><td>МПа</td><td>не менее</td><td>0,5</td></tr></table>
<a href="/upload/test.pdf">Технический лист</a></div></div></html>'''.encode()


def crash_once_worker(connection, config, gate, last):
    while True:
        task = connection.recv()
        if task is None:
            return
        marker = Path(config['cache']) / 'already_crashed'
        try:
            with marker.open('x') as f:
                f.write('first attempt')
        except FileExistsError:
            connection.send(('ok', parser.parse_product(HTML, task[0], task[1])))
        else:
            os._exit(7)  # Настоящая авария процесса, без отправки результата.


class ParsingTests(unittest.TestCase):
    def test_preserves_units_comparator_and_document(self):
        result = parser.parse_product(HTML, 'https://www.tn.ru/catalogue/a/b/', 'Смеси')
        self.assertEqual(result['characteristics'][0]['Критерий'], 'не менее')
        self.assertEqual(result['characteristics'][0]['Значение'], '0,5')
        self.assertEqual(result['documents'][0]['url'], 'https://www.tn.ru/upload/test.pdf')
        self.assertIsNone(result['price'])

    def test_rejects_page_without_characteristics(self):
        with self.assertRaises(ValueError):
            parser.parse_product(b'<h1>Access denied</h1>', 'https://www.tn.ru/catalogue/a/b/', 'Тест')

    def test_rejects_external_domain(self):
        with self.assertRaises(ValueError):
            parser.allowed_url('https://example.org/catalogue/test/')

    def test_reassigns_after_process_crash(self):
        original = parser.worker
        parser.worker = crash_once_worker
        try:
            with tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                config = {'agents': 2, 'attempts': 2, 'task_timeout': 15, 'cache': directory}
                report = {'category_url': parser.DEFAULT_CATEGORY, 'materials': [], 'errors': []}
                parser.collect_products(['https://www.tn.ru/catalogue/a/b/', 'https://www.tn.ru/catalogue/a/c/'], 'Смеси', config, report, folder)
                self.assertEqual(len(report['materials']), 2)
                self.assertEqual(report['errors'], [])
                self.assertTrue((folder / 'materials.json').exists())
        finally:
            parser.worker = original


if __name__ == '__main__':
    unittest.main()
