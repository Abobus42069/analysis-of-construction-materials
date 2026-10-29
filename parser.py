"""Каталог ТЕХНОНИКОЛЬ -> JSON/TXT. Python 3.10+, lxml."""
import argparse
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

from lxml import html

BASE = 'https://www.tn.ru'
DEFAULT_CATEGORY = BASE + '/catalogue/montazhnye-peny/'
USER_AGENT = 'MaterialsStudyParser/0.1 (+educational material catalog collector)'


def clean(value):
    return ' '.join((value or '').split())


def cls(name):
    return 'contains(concat(" ", normalize-space(@class), " "), " ' + name + ' ")'


def allowed_url(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.netloc != 'www.tn.ru' or not parts.path.startswith('/catalogue/'):
        raise ValueError('Разрешены только HTTPS-страницы каталога www.tn.ru')
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))


class CatalogRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, cache, delay, timeout, gate, last_request, refresh=False):
    """Общий лимит запросов для всех процессов; HTML-кеш для повтора."""
    from urllib.request import build_opener
    url = allowed_url(url)
    path = Path(cache) / (hashlib.sha256(url.encode()).hexdigest() + '.html')
    if path.exists() and not refresh:
        return path.read_bytes(), True
    # SQLite освобождает блокировки при аварии процесса. Нельзя держать
    # multiprocessing.Lock в агенте, который может быть принудительно остановлен.
    import sqlite3
    with sqlite3.connect(str(Path(cache) / 'rate_limit.sqlite'), timeout=30) as db:
        db.execute('CREATE TABLE IF NOT EXISTS rate_limit (id INTEGER PRIMARY KEY, next_at REAL)')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT next_at FROM rate_limit WHERE id=1').fetchone()
        slot = max(time.time(), row[0] if row else 0)
        db.execute('INSERT OR REPLACE INTO rate_limit VALUES (1, ?)', (slot + delay,))
        db.commit()
    time.sleep(max(0, slot - time.time()))
    request = Request(url, headers={'User-Agent': USER_AGENT})
    with build_opener(CatalogRedirectHandler()).open(request, timeout=timeout) as response:
        content_type = response.headers.get('Content-Type', '')
        if 'html' not in content_type.lower():
            raise ValueError('Вместо HTML получен ' + content_type)
        data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError('Страница превышает ограничение 8 МБ')
    temp = path.with_suffix('.tmp-' + str(__import__('os').getpid()))
    temp.write_bytes(data)
    temp.replace(path)
    return data, False


def discover(data, url):
    tree = html.fromstring(data, parser=html.HTMLParser(encoding="utf-8"))
    products = []
    for a in tree.xpath('//a[' + cls('tn-product-title') + '][@href]'):
        link = allowed_url(urljoin(url, a.get('href')))
        if link not in products:
            products.append(link)
    next_pages = []
    for a in tree.xpath('//a[@href]'):
        link = urljoin(url, a.get('href'))
        if urlsplit(link).path == urlsplit(url).path and re.search(r'[?&]PAGEN_3=\d+', link):
            if link not in next_pages:
                next_pages.append(allowed_url(link))
    return clean(' '.join(tree.xpath('//h1//text()'))), products, next_pages


def parse_product(data, url, category, from_cache=False):
    tree = html.fromstring(data, parser=html.HTMLParser(encoding="utf-8"))
    name = clean(' '.join(tree.xpath('//h1//text()')))
    if not name:
        raise ValueError('Нет h1: возможно, изменилась страница или получена заглушка')
    facts = []
    for table in tree.xpath('//div[' + cls('tn-product-tabs-box') + ']//table'):
        rows = table.xpath('.//tr')
        if not rows:
            continue
        headers = [clean(x.text_content()) for x in rows[0].xpath('./th|./td')]
        if not headers or not any('Наименование' in x for x in headers):
            continue
        for row in rows[1:]:
            cells = [clean(x.text_content()) for x in row.xpath('./th|./td')]
            if len(cells) != len(headers) or not cells[0]:
                continue
            facts.append(dict(zip(headers, cells)))
    if not facts:
        raise ValueError('Не найдены таблицы характеристик: требуется проверка адаптера')
    descriptions = tree.xpath('//div[' + cls('tn-product-text') + ']')
    documents, seen = [], set()
    for a in tree.xpath('//div[' + cls('tn-product-tabs') + ']//a[@href]'):
        link = urljoin(url, a.get('href'))
        if urlsplit(link).path.lower().endswith('.pdf') and link not in seen:
            seen.add(link)
            documents.append({'title': clean(a.text_content()), 'url': link})
    shops = list(dict.fromkeys(urljoin(url, a.get('href')) for a in tree.xpath('//a[@href]')
                               if urlsplit(urljoin(url, a.get('href'))).netloc == 'shop.tn.ru'))
    return {
        'name': name, 'manufacturer': 'ТЕХНОНИКОЛЬ', 'category': category,
        'url': url, 'parsed_at': datetime.now(timezone.utc).isoformat(),
        'html_sha256': hashlib.sha256(data).hexdigest(), 'from_cache': from_cache,
        'description': clean(descriptions[0].text_content()) if descriptions else None,
        'characteristics': facts, 'documents': documents, 'shop_urls': shops,
        'price': None, 'currency': None,
        'price_note': 'Цена не извлекается из каталога производителя; интернет-магазин не обходится.'
    }


def worker(connection, config, gate, last_request):
    while True:
        task = connection.recv()
        if task is None:
            return
        url, category = task
        try:
            data, cached = fetch(url, config['cache'], config['delay'], config['timeout'],
                                 gate, last_request, config['refresh'])
            connection.send(('ok', parse_product(data, url, category, cached)))
        except Exception as exc:
            connection.send(('error', type(exc).__name__ + ': ' + str(exc)))


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def save_results(folder, report):
    atomic_json(folder / 'materials.json', report)
    lines = ['Материалов: ' + str(len(report['materials'])), 'Источник: ' + report['category_url']]
    for i, item in enumerate(report['materials'], 1):
        lines.extend(['', '=' * 60, f"{i}. {item['name']}", item['url'],
                      'Категория: ' + item['category'], item['description'] or '', 'Характеристики:'])
        for fact in item['characteristics']:
            lines.append(' | '.join(f'{key}: {value}' for key, value in fact.items()))
        lines.append('Документы:')
        lines.extend(d['title'] + ': ' + d['url'] for d in item['documents'])
        lines.append(item['price_note'])
    if report['errors']:
        lines.append('\nОшибки:')
        lines.extend(str(x) for x in report['errors'])
    (folder / 'materials.txt').write_text('\n'.join(lines), encoding='utf-8')


def collect_products(urls, category, config, report, folder):
    """Родитель знает текущую задачу агента и переназначает её при падении."""
    ctx = mp.get_context('spawn')
    gate, last = None, None
    pending = deque((url, 1) for url in urls)
    slots = []

    def spawn():
        parent, child = ctx.Pipe()
        process = ctx.Process(target=worker, args=(child, config, gate, last))
        process.start()
        child.close()
        return {'process': process, 'pipe': parent, 'task': None, 'start': 0}

    def close(slot):
        if slot['process'].is_alive():
            slot['process'].terminate()
        slot['process'].join(timeout=3)
        if slot['process'].is_alive():
            slot['process'].kill()
            slot['process'].join()
        slot['pipe'].close()

    def retry(task, error):
        url, attempt = task
        if attempt < config['attempts']:
            print(f'Повтор {attempt + 1}: {url} ({error})', flush=True)
            pending.append((url, attempt + 1))
        else:
            report['errors'].append({'url': url, 'attempts': attempt, 'error': error})
        save_results(folder, report)

    try:
        slots = [spawn() for _ in range(min(config['agents'], len(urls)))]
        while pending or any(s['task'] for s in slots):
            for index, slot in enumerate(slots):
                dead = not slot['process'].is_alive()
                expired = slot['task'] and time.monotonic() - slot['start'] > config['task_timeout']
                if dead or expired:
                    active = slot['task']
                    close(slot)  # Старый процесс остановлен до назначения повторной задачи.
                    if active:
                        retry(active, 'Агент остановлен или превышено время задачи')
                    slots[index] = slot = spawn()
                if slot['task'] and slot['pipe'].poll():
                    try:
                        status, result = slot['pipe'].recv()
                    except (EOFError, OSError):
                        close(slot)
                        retry(slot['task'], 'Разрыв связи с агентом')
                        slots[index] = spawn()
                        continue
                    active, slot['task'] = slot['task'], None
                    if status == 'ok':
                        report['materials'].append(result)
                        print(f"Сохранено {len(report['materials'])}: {result['name']}", flush=True)
                        save_results(folder, report)
                    else:
                        retry(active, result)
                if slot['task'] is None and pending:
                    active = pending.popleft()
                    try:
                        slot['pipe'].send((active[0], category))
                    except (BrokenPipeError, OSError):
                        pending.appendleft(active)
                        close(slot)
                        slots[index] = spawn()
                        continue
                    slot['task'], slot['start'] = active, time.monotonic()
            time.sleep(.05)
    finally:
        for slot in slots:
            close(slot)
        report['materials'].sort(key=lambda m: urls.index(m['url']))
        save_results(folder, report)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--category', default=DEFAULT_CATEGORY)
    p.add_argument('--limit', type=int, default=5, help='Максимум карточек')
    p.add_argument('--agents', type=int, default=2)
    p.add_argument('--delay', type=float, default=2, help='Общий интервал запросов, секунды')
    p.add_argument('--timeout', type=float, default=30)
    p.add_argument('--task-timeout', type=float, default=90)
    p.add_argument('--attempts', type=int, default=2)
    p.add_argument('--out', type=Path, default=Path('output'))
    p.add_argument('--refresh', action='store_true', help='Получить страницы заново вместо HTML-кеша')
    args = p.parse_args()
    if min(args.limit, args.agents, args.attempts) < 1 or args.delay < 1:
        p.error('Числа должны быть положительными, --delay не меньше 1 секунды')
    if args.timeout <= 0 or args.task_timeout <= args.timeout:
        p.error('--task-timeout должен превышать положительный --timeout')
    category_url = allowed_url(args.category)
    folder = args.out.resolve()
    cache = folder / 'html_cache'
    cache.mkdir(parents=True, exist_ok=True)
    ctx = mp.get_context('spawn')
    gate, last = None, None
    queue, visited, urls, category = deque([category_url]), set(), [], ''
    while queue and len(visited) < 1 and len(urls) < args.limit:
        current = queue.popleft()
        if current in visited:
            continue
        data, _ = fetch(current, cache, args.delay, args.timeout, gate, last, args.refresh)
        visited.add(current)
        title, found, pages = discover(data, current)
        category = category or title
        for u in found:
            if u not in urls:
                urls.append(u)
        for u in pages:
            if u not in visited and u not in queue:
                queue.append(u)
    urls = urls[:args.limit]
    if not urls:
        raise ValueError('Не найдены карточки: проверьте URL категории или адаптер')
    report = {'source': 'www.tn.ru', 'category_url': category_url,
              'started_at': datetime.now(timezone.utc).isoformat(),
              'requested_limit': args.limit, 'discovered_count': len(urls),
              'visited_category_pages': list(sorted(visited)), 'materials': [], 'errors': []}
    config = vars(args).copy()
    config['cache'] = str(cache)
    print(f'Найдено карточек: {len(urls)}. Агентов: {min(args.agents, len(urls))}', flush=True)
    collect_products(urls, category, config, report, folder)
    print(f"Готово: {len(report['materials'])}, ошибок: {len(report['errors'])}. Папка: {folder}")
    return 2 if report['errors'] else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (HTTPError, URLError, ValueError) as exc:
        raise SystemExit('Ошибка: ' + str(exc))
