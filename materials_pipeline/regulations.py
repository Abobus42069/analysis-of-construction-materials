"""Official registry cards are metadata, never substitutes for full normative text."""
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from lxml import html
from .core import clean, digest, identity, evidence, atomic_json, SCHEMA_VERSION
from .documents import download, ingest, SOURCE_HOSTS

LABELS={'Обозначение':'designation','Полное обозначение':'full_designation',
    'Заглавие на русском языке':'title','Дата введения в действие':'effective_from',
    'Дата ограничения срока действия':'effective_to','Дата приказа':'order_date',
    'Дата утверждении':'approval_date','Приказ об утверждении':'approval_order',
    'Номер приказа':'approval_order','Орган, утвердивший свод правил':'approved_by',
    'Код ОКС':'ics_code','Ключевые слова':'keywords','Область применения':'scope_raw',
    'Обозначение заменяемого(ых)':'replaces_raw','Обозначение заменяющего':'replaced_by_raw',
    'Обозначение заменяемого в части':'partly_replaces_raw',
    'Обозначение заменяющего в части':'partly_replaced_by_raw',
    'Организация - Разработчик':'developer','Технический комитет':'technical_committee'}
STOPS={'Основные сведения','Регистрация в фонде','Поисковые образы','Ссылочные данные',
    'Сведения о разработчике','Сведения о регистрации в МГС','Документы не найдены',
    'Библиография','Изменения и поправки','Просмотр документа','Росстандарт'}


def parse_registry_card(data,url):
    tree=html.fromstring(data,parser=html.HTMLParser(encoding='utf-8'))
    headings=tree.xpath('//h1')
    designation=clean(headings[0].text_content()) if headings else None
    if not designation or not designation.startswith(('ГОСТ','СП ','СанПиН')):
        raise ValueError('Registry designation not found; source layout requires review')
    # Text lines are retained so line-based locators can be checked against the original snapshot.
    for node in tree.xpath('//script|//style|//nav|//header|//footer'):
        node.drop_tree()
    lines=[clean(line) for text in tree.itertext() for line in text.splitlines() if clean(line)]
    sha=digest(data)
    fields,origins={},{}
    for index,line in enumerate(lines):
        if line not in LABELS:
            continue
        collected=[]
        for value in lines[index+1:]:
            if value in LABELS or value in STOPS:
                break
            collected.append(value)
        if collected:
            key=LABELS[line]
            fields[key]='\n'.join(collected)
            origins[key]=evidence(url,sha,'registry_text_line:'+str(index+1),fields[key])
    statuses={'Действует','Заменен','Заменён','Отменен','Отменён','Утратил силу в РФ','Не действует'}
    status=next((line for line in lines if line in statuses),None)
    candidates=[]
    seen=set()
    for node in tree.xpath('//*[@href or @data-href or @src or @data]'):
        for attr in ['href','data-href','src','data']:
            raw=node.get(attr)
            if not raw:
                continue
            link=urljoin(url,raw)
            if urlsplit(link).scheme!='https' or not urlsplit(link).path.lower().endswith('.pdf') or link in seen:
                continue
            seen.add(link)
            source=next((name for name,hosts in SOURCE_HOSTS.items() if urlsplit(link).hostname in hosts),None)
            candidates.append({'url':link,'title':clean(node.text_content()),'source':source,
                               'review_status':'candidate'})
    return {'schema_version':SCHEMA_VERSION,'record_type':'normative_registry_card',
        'id':identity('registry',url,sha),'source_url':url,'sha256':sha,
        'designation':fields.get('designation',designation),'fields':fields,
        'declared_status':status,'status':'unverified','mandatory_status':'not_assessed',
        'field_evidence':origins,'registry_text_lines':lines,'pdf_candidates':candidates,
        'full_text_status':'linked_unverified' if candidates else 'not_found'}


def collect_registry(manifest,root,refresh=False,download_text=False):
    entries=json.loads(Path(manifest).read_text(encoding='utf-8'))
    if not isinstance(entries,list):
        raise ValueError('Registry manifest must be a list')
    root=Path(root)
    cards,documents,errors=[],[],[]
    for entry in entries:
        try:
            data,retrieval=download(entry['url'],root,entry['source'],refresh=refresh)
            card=parse_registry_card(data,entry['url'])
            card['retrieval']=retrieval
            card['document_ids']=[]
            if download_text:
                for link in card['pdf_candidates']:
                    try:
                        if not link['source']:
                            raise ValueError('PDF source is outside configured official/manufacturer sources')
                        content,meta=download(link['url'],root,link['source'],refresh=refresh)
                        doc=ingest(content,root,link['url'],'normative_document',{
                            'designation':card['designation'],'registry_card_id':card['id'],
                            'registry_url':entry['url'],'declared_status':card['declared_status'],
                            'source_role':'registry_linked_document'},meta)
                        card['document_ids'].append(doc['id'])
                        documents.append(doc['id'])
                    except Exception as exc:
                        errors.append({'url':link['url'],'error':type(exc).__name__+': '+str(exc)})
            if card['document_ids']:
                card['full_text_status']='downloaded_unverified'
            atomic_json(root/'registry'/(card['id']+'.json'),card)
            cards.append(card['id'])
        except Exception as exc:
            errors.append({'url':entry.get('url'),'error':type(exc).__name__+': '+str(exc)})
    result={'registry_cards':cards,'documents':documents,'errors':errors}
    atomic_json(root/'registry_report.json',result)
    return result
