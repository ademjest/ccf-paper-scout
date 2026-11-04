"""Literature adapters share a page contract; publication evidence stays explicit."""
from __future__ import annotations

import datetime as dt
import hashlib
from functools import lru_cache
from html.parser import HTMLParser
from importlib.resources import files
import json
import os
import re
from urllib.parse import quote, urljoin, urlsplit
import xml.etree.ElementTree as ET

from . import identity, official_abstracts, proceedings
from .source_http import SourceError

HOSTS = {
    'dblp': {'sparql.dblp.org'}, 'openalex': {'api.openalex.org'},
    'pmlr': {'proceedings.mlr.press'}, 'neurips': {'proceedings.neurips.cc'},
    'acl': {'aclanthology.org'}, 'cvf': {'openaccess.thecvf.com'}, 'aaai': {'ojs.aaai.org'},
    'arxiv': {'export.arxiv.org'}, 'crossref': {'api.crossref.org'},
    'semantic_scholar': {'api.semanticscholar.org'}, 'openreview': {'api2.openreview.net'},
}
OFFICIAL = {'pmlr', 'neurips', 'acl', 'cvf', 'aaai'}


@lru_cache(maxsize=1)
def catalog() -> dict:
    result = json.loads(files('ccf_paper_scout').joinpath('data/official_sources.json').read_text(encoding='utf-8'))
    for issue in proceedings.registry().get('publisher_issues', {}).values():
        if issue.get('status') == 'verified' and issue.get('issue_source'):
            result['volumes'].append(dict(source='aaai', venue='aaai', year=int(issue['proceedings_id'].rsplit('/', 1)[-1]),
                url=issue['issue_source'], path_pattern=r'/index.php/AAAI/article/view/\d+', track=issue['track'],
                proceedings_id=issue['proceedings_id']))
    return result


def key(source, arguments):
    return source + ':' + hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()[:24]


def page(task_key, records, state):
    return dict(task_key=task_key, records=records, state=state)


def record(source, identifier, title, authors, year, **extra):
    if not isinstance(title, str) or not title.strip():
        raise SourceError('protocol_error', 'missing_title')
    return dict(source=source, source_record_id=identifier, title=' '.join(title.split()), authors=authors,
                year=int(year or 0), abstract='', venue='', venue_key='', rank='', type='conference',
                publication_status='unknown', **extra)


def records_array(payload, field):
    value = payload.get(field)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise SourceError('protocol_error', 'missing_' + field)
    return value


def queries(task):
    return task.get('queries') or [' '.join(task.get('explicit_interests', ['reinforcement learning']))]


class DirectoryHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.stack = [], []
        self.current = None
        self.heading = None
        self.paper_title = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('p', 'dt') and 'title' in attrs.get('class', ''):
            self.heading = []
        if tag == 'a':
            self.current = dict(url=attrs.get('href', ''), text=[], title=self.paper_title,
                                track=next((track for _, track in reversed(self.stack) if track), ''))
        if tag not in ('meta', 'link', 'img', 'input', 'br', 'hr', 'source', 'wbr'):
            self.stack.append((tag, attrs.get('data-track', '')))

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.current['text'] = ''.join(self.current['text']).strip()
            self.links.append(self.current)
            self.current = None
        if tag in ('p', 'dt') and self.heading is not None:
            self.paper_title = ''.join(self.heading).strip()
            self.heading = None
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.current is not None:
            self.current['text'].append(data)
        if self.heading is not None:
            self.heading.append(data)


def official(task, runtime):
    source = task['source']
    entries = [v for v in catalog()['volumes'] if v['source'] == source and v['year'] in task['years']
               and v['venue'] in task['venue_keys']]
    if not entries:
        raise SourceError('not_configured', 'no_reviewed_volume')
    # Round-robin directories across runs; a large first volume must not starve the others.
    entries.sort(key=lambda v: task['states'].get(key(source, v['url']), {}).get('checked_at', ''))
    tokens = {t.lower() for q in queries(task) for t in re.findall(r'[a-zA-Z]{4,}', q)}
    for volume in entries:
        task_key = key(source, volume['url'])
        previous = task['states'].get(task_key, {})
        html = runtime.get(volume['url'], max_bytes=12_000_000)
        if '</html>' not in html.lower():
            raise SourceError('protocol_error', 'incomplete_directory')
        parser = DirectoryHTML(); parser.feed(html)
        links = {}
        for link in parser.links:
            url = urljoin(volume['url'], link['url'])
            parsed = urlsplit(url)
            if parsed.hostname != urlsplit(volume['url']).hostname or not re.fullmatch(volume['path_pattern'], parsed.path):
                continue
            if volume.get('entry_track') and link['track'] != volume['entry_track']:
                continue
            links[url] = link['title'] or link['text']
        if not links:
            raise SourceError('protocol_error', 'directory_has_no_papers')
        done = set(previous.get('done', []))
        pending = sorted((u for u in links if identity.url(u) not in done), key=lambda url: (-sum(t in links[url].lower() for t in tokens), url))
        digest = hashlib.sha256(html.encode()).hexdigest()
        # Bound work per directory as well as per source to preserve coverage.
        for url in pending[:int(task['settings'].get('papers_per_volume', 12))]:
            document = runtime.get(url)
            parsed = official_abstracts.PaperHTML(); parsed.feed(document)
            titles = parsed.meta.get('citation_title', [])
            authors = parsed.meta.get('citation_author', [])
            if len(titles) != 1 or not authors:
                raise SourceError('protocol_error', 'missing_official_identity')
            paper = record(source, url, titles[0], authors, volume['year'], url=url, ee=url)
            abstract = ' '.join(''.join(parsed.abstract).split())
            if not abstract:
                abstract = ' '.join(parsed.meta.get('description', []))
            paper.update(doi=identity.doi(next(iter(parsed.meta.get('citation_doi', [])), '')),
                         venue_key=volume['venue'], venue=volume['venue'].upper(), rank='A',
                         proceedings_id=volume['proceedings_id'], publication_status='published',
                         abstract=abstract if len(abstract) >= 80 and not official_abstracts.is_bibliographic_placeholder(abstract) else '',
                         abstract_source=source, abstract_source_url=url)
            paper['publication_evidence'] = [dict(kind='official_volume_membership', source=source,
                volume_url=volume['url'], paper_url=url, entry_track=volume.get('entry_track', ''),
                venue_key=volume['venue'], year=volume['year'], track=volume['track'],
                proceedings_id=volume['proceedings_id'], content_hash=digest,
                rule_version=catalog()['version'], retrieved_at=dt.datetime.now(dt.timezone.utc).isoformat())]
            done.add(identity.url(url))
            yield page(task_key, [paper], dict(done=sorted(done), directory_hash=digest,
                checked_at=dt.datetime.now(dt.timezone.utc).isoformat(), exhausted=len(done & {identity.url(u) for u in links}) == len(links)))
        if not pending:
            yield page(task_key, [], dict(previous, checked_at=dt.datetime.now(dt.timezone.utc).isoformat(), exhausted=True))


def openalex(task, runtime):
    api_key = os.environ.get('OPENALEX_API_KEY', '')
    headers = {'Authorization': 'Bearer ' + api_key} if api_key else {}
    for query in queries(task):
        task_key = key('openalex', [query, task['years']])
        for number in range(1, int(task['settings'].get('pages_per_query', 1)) + 1):
            payload = runtime.json('https://api.openalex.org/works', headers=headers, params={
                'search': query, 'filter': f"publication_year:{min(task['years'])}-{max(task['years'])}",
                'per_page': min(100, int(task['settings'].get('page_size', 25))), 'page': number}, max_bytes=8_000_000)
            rows = records_array(payload, 'results')
            papers = [openalex_record(row) for row in rows]
            yield page(task_key, papers, dict(page=number, exhausted=not rows))
            if not rows:
                break


def openalex_record(row):
    paper = record('openalex', row['id'].rsplit('/', 1)[-1], row.get('title'),
        [a['author']['display_name'] for a in row.get('authorships', []) if a.get('author', {}).get('display_name')],
        row.get('publication_year'), url=row.get('doi') or row['id'], doi=identity.doi(row.get('doi')))
    words = [(pos, word) for word, positions in (row.get('abstract_inverted_index') or {}).items() for pos in positions]
    paper.update(abstract=' '.join(word for _, word in sorted(words)), abstract_source='openalex',
                 publication_date=row.get('publication_date'))
    for location in row.get('locations') or []:
        landing = location.get('landing_page_url') or ''
        if urlsplit(landing).hostname in set().union(*(HOSTS[s] for s in OFFICIAL)):
            paper['ee'] = landing
            break
    return paper


def arxiv(task, runtime):
    ns = {'a': 'http://www.w3.org/2005/Atom', 'x': 'http://arxiv.org/schemas/atom'}
    for query in queries(task):
        # Treat user input as words, not as untrusted provider query syntax.
        terms = re.findall(r'[\w-]+', query)
        expression = ' AND '.join(f'all:"{term}"' for term in terms)
        size = min(100, int(task['settings'].get('page_size', 25)))
        xml = runtime.get('https://export.arxiv.org/api/query', params={'search_query':
            f'({expression}) AND submittedDate:[{min(task["years"])}01010000 TO {max(task["years"])}12312359]',
            'start': 0, 'max_results': size, 'sortBy': 'submittedDate', 'sortOrder': 'descending'})
        if '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
            raise SourceError('protocol_error', 'xml_entities')
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            raise SourceError('protocol_error', 'invalid_xml') from None
        if root.tag != '{http://www.w3.org/2005/Atom}feed':
            raise SourceError('protocol_error', 'invalid_atom_feed')
        papers = []
        for node in root.findall('a:entry', ns):
            url = node.findtext('a:id', '', ns)
            if '/api/errors' in url:
                raise SourceError('protocol_error', 'arxiv_error_entry')
            identifier = url.split('/abs/')[-1]
            published = node.findtext('a:published', '', ns)
            paper = record('arxiv', re.sub(r'v\d+$', '', identifier), node.findtext('a:title', '', ns),
                [a.findtext('a:name', '', ns) for a in node.findall('a:author', ns)], published[:4], url=url,
                doi=identity.doi(node.findtext('x:doi', '', ns)))
            paper.update(abstract=node.findtext('a:summary', '', ns), publication_status='preprint',
                external_ids={'arxiv': re.sub(r'v\d+$', '', identifier)}, publication_date=published, version=identifier,
                abstract_source='arxiv')
            papers.append(paper)
        yield page(key('arxiv', [query, task['years']]), papers, dict(exhausted=len(papers) < size))


def crossref(task, runtime):
    for query in queries(task):
        params = {'query.bibliographic': query, 'filter': f"from-pub-date:{min(task['years'])}-01-01,until-pub-date:{max(task['years'])}-12-31",
                  'rows': min(100, int(task['settings'].get('page_size', 25)))}
        if os.environ.get('CROSSREF_MAILTO'):
            params['mailto'] = os.environ['CROSSREF_MAILTO']
        payload = runtime.json('https://api.crossref.org/works', params=params, max_bytes=8_000_000)
        rows = records_array(payload.get('message', {}), 'items')
        papers = [crossref_record(row) for row in rows]
        yield page(key('crossref', [query, task['years']]), papers, dict(exhausted=not papers))


def crossref_record(row):
    date = (row.get('published') or row.get('published-print') or row.get('published-online') or {}).get('date-parts', [[0]])[0]
    paper = record('crossref', row['DOI'].lower(), next(iter(row.get('title', [])), ''),
        [' '.join(filter(None, (a.get('given'), a.get('family')))) for a in row.get('author', [])],
        date[0], doi=identity.doi(row['DOI']), url=row.get('URL', ''))
    paper.update(abstract=re.sub(r'<[^>]+>', '', row.get('abstract', '')), abstract_source='crossref',
                 venue_claim=' '.join(row.get('container-title', [])), publication_status='published')
    return paper


def lookup(task, runtime):
    """Exact identity lookups share the source's request and wall-clock budget."""
    source = task['source']
    for paper in task.get('lookups', []):
        task_key = key(source, ['lookup', paper['id']])
        previous = task['states'].get(task_key, {})
        if previous.get('retry_at', '') > dt.datetime.now(dt.timezone.utc).isoformat():
            continue
        doi = identity.doi(paper.get('doi')) or identity.doi(paper.get('ee'))
        try:
            if source in OFFICIAL:
                url = official_abstracts.official_url(paper)
                if not url or urlsplit(url).hostname not in HOSTS[source]:
                    continue
                abstract = official_abstracts.parse_abstract(runtime.get(url), paper, allow_missing=True)
                result = dict(paper, source_record_id=identity.url(url), abstract=abstract,
                              abstract_source=source, abstract_source_url=url)
            elif source == 'openalex' and doi:
                token = os.environ.get('OPENALEX_API_KEY', '')
                result = openalex_record(runtime.json('https://api.openalex.org/works/' + quote('https://doi.org/' + doi, safe=''),
                    headers={'Authorization': 'Bearer ' + token} if token else {}))
            elif source == 'crossref' and doi:
                params = {'mailto': os.environ['CROSSREF_MAILTO']} if os.environ.get('CROSSREF_MAILTO') else {}
                result = crossref_record(runtime.json('https://api.crossref.org/works/' + quote(doi, safe=''), params=params)['message'])
            else:
                continue
            if source not in OFFICIAL and identity.doi(result.get('doi')) != doi:
                raise SourceError('protocol_error', 'lookup_identity_mismatch')
            records = [result]
        except SourceError as exc:
            if exc.status != 'not_found':
                raise
            records = []
        # Missing abstracts and exact-ID 404s are retried later, not on every daily run.
        yield page(task_key, records, dict(retry_at=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=7)).isoformat()))


def semantic_scholar(task, runtime):
    token = os.environ.get('SEMANTIC_SCHOLAR_API_KEY') or os.environ.get('SEMANTICSCHOLAR_API_KEY', '')
    headers = {'x-api-key': token} if token else {}
    for query in queries(task):
        payload = runtime.json('https://api.semanticscholar.org/graph/v1/paper/search', headers=headers, params={
            'query': query, 'year': f"{min(task['years'])}-{max(task['years'])}", 'limit': 25,
            'fields': 'paperId,title,authors,year,abstract,externalIds,venue,publicationDate,url'}, max_bytes=8_000_000)
        papers = []
        for row in records_array(payload, 'data'):
            ids = row.get('externalIds') or {}
            paper = record('semantic_scholar', row['paperId'], row.get('title'),
                [a['name'] for a in row.get('authors', [])], row.get('year'), doi=identity.doi(ids.get('DOI')), url=row.get('url', ''))
            paper.update(abstract=row.get('abstract') or '', abstract_source='semantic_scholar',
                external_ids={'arxiv': ids.get('ArXiv', '')}, venue_claim=row.get('venue'), publication_date=row.get('publicationDate'))
            papers.append(paper)
        yield page(key('semantic_scholar', [query, task['years']]), papers, dict(exhausted='next' not in payload))


def openreview(task, runtime):
    venues = task['settings'].get('venues', [])
    if not venues:
        raise SourceError('not_configured', 'openreview_venues_required')
    for venue in venues:
        payload = runtime.json('https://api2.openreview.net/notes', params={'content.venueid': venue, 'limit': 50}, max_bytes=8_000_000)
        papers = []
        for row in records_array(payload, 'notes'):
            content = {k: v.get('value') if isinstance(v, dict) else v for k, v in row.get('content', {}).items()}
            year = next((int(y) for y in venue.split('/') if y.isdigit() and len(y) == 4), 0)
            if year not in task['years']:
                continue
            identifier = row.get('forum') or row['id']
            paper = record('openreview', identifier, content.get('title'), content.get('authors') or [], year,
                           url='https://openreview.net/forum?id=' + identifier)
            paper.update(abstract=content.get('abstract') or '', abstract_source='openreview',
                         venue_claim=content.get('venue'), publication_status='submission')
            papers.append(paper)
        # A venue claim is not an accepted-main decision. These records remain search-only.
        yield page(key('openreview', venue), papers, dict(exhausted=len(papers) < 50))


def dblp(task, runtime):
    import paper_scout
    def request(url, user_agent, *args, **kwargs):
        return runtime.json(url, max_bytes=8_000_000)
    partitions = [(v, year) for v in task['venues'] for year in task['years']]
    partitions.sort(key=lambda item: task['states'].get(key('dblp', [item[0]['dblp_key'], item[1]]), {}).get('checked_at', ''))
    for venue, year in partitions:
        task_key = key('dblp', [venue['dblp_key'], year])
        previous = task['states'].get(task_key, {})
        offset = 0 if previous.get('exhausted') else int(previous.get('offset', 0))
        size = min(100, int(task['settings'].get('page_size', 100)))
        for _ in range(int(task['settings'].get('pages_per_partition', 2))):
            rows, total, raw = paper_scout.fetch_dblp_sparql_page(venue, year, size, offset, 'ccf-paper-scout/0.3', request=request)
            for row in rows:
                row.update(source_record_id=row['id'], source='dblp', venue_key=venue['dblp_key'])
            offset += raw
            done = raw < size or (total is not None and offset >= total)
            yield page(task_key, rows, dict(offset=offset, exhausted=done, checked_at=dt.datetime.now(dt.timezone.utc).isoformat()))
            if done:
                break


def collect(task, runtime):
    source = task['source']
    yield from lookup(task, runtime)
    adapters = {'dblp': dblp, 'openalex': openalex, 'arxiv': arxiv, 'crossref': crossref,
                'semantic_scholar': semantic_scholar, 'openreview': openreview}
    yield from (official if source in OFFICIAL else adapters[source])(task, runtime)
