"""Exact-paper abstract extraction for PMLR, NeurIPS and ACL (no title search)."""
from __future__ import annotations

import re
import unicodedata
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

PARSER_VERSION = 'official-abstract-v2'


class IdentityMismatch(ValueError):
    pass


def official_url(paper: dict) -> str | None:
    url = urlsplit(str(paper.get("ee", "")))
    if url.scheme not in ("http", "https") or url.username or url.password or url.port:
        return None
    if url.hostname in ("doi.org", "dx.doi.org"):
        match = re.fullmatch(r"/10\.18653/v1/((\d{4})\.acl-(?:long|short)\.\d+)", url.path, re.I)
        if match and int(match[2]) == paper.get("year"):
            return "https://aclanthology.org/" + match[1].lower() + "/"
    if url.hostname == "proceedings.mlr.press" and re.fullmatch(r"/v\d+/[\w-]+\.html", url.path):
        return urlunsplit(("https", url.hostname, url.path, "", ""))
    if url.hostname == "aclanthology.org":
        match = re.fullmatch(r"/(\d{4})\.acl-(?:long|short)\.\d+/", url.path)
        if match and int(match[1]) == paper.get("year"):
            return urlunsplit(("https", url.hostname, url.path, "", ""))
    if url.hostname in ("papers.nips.cc", "papers.neurips.cc", "proceedings.neurips.cc"):
        match = re.fullmatch(r"/paper(?:_files/paper)?/(\d{4})/hash/[a-f0-9]+-Abstract(?:-Conference)?\.html", url.path)
        if match and int(match[1]) == paper.get("year"):
            return urlunsplit(("https", "proceedings.neurips.cc", url.path, "", ""))
    if url.hostname == 'openaccess.thecvf.com' and re.fullmatch(r'/content/[^/]+/html/[^/]+_paper\.html', url.path):
        return urlunsplit(('https', url.hostname, url.path, '', ''))
    if url.hostname == 'ojs.aaai.org' and re.fullmatch(r'/index.php/AAAI/article/view/\d+', url.path):
        return urlunsplit(('https', url.hostname, url.path, '', ''))
    return None


def is_bibliographic_placeholder(text: str) -> bool:
    """Reject the observed author + proceedings + year pseudo-abstract shape."""
    return len(text) < 800 and bool(re.search(r"\.\s*Proceedings of .+\.\s*\d{4}\.?\s*$", text, re.I))


def usable_abstract(text: str) -> str:
    text = ' '.join((text or '').split())
    boilerplate = re.search(r'How to Cite.*(?:More Citation Formats|Issue Vol\.)|'
                           r'making sure you.re not a bot|<title>just a moment', text, re.I)
    return text if len(text) >= 80 and not boilerplate and not is_bibliographic_placeholder(text) else ''


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url: str, user_agent: str) -> str:
    # One request, no implicit retries/redirects outside the shared metadata budget.
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError("response_too_large")
        return body.decode("utf-8")


def normalized(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def author_key(text: str) -> str:
    # DBLP disambiguation suffixes are not part of the author's name.
    return " ".join(sorted(re.findall(r"[^\W\d_]+", unicodedata.normalize("NFKC", text).casefold())))


class PaperHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.abstract: list[str] = []
        self.depth = 0
        self.heading: list[str] | None = None
        self.awaiting_abstract = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            self.meta.setdefault(attrs.get("name", "").lower(), []).append(attrs.get("content", ""))
        if tag in ("meta", "br", "img", "link", "input", "hr", "source", "wbr"):
            if self.depth:
                self.abstract.append(" ")
            return
        if self.depth:
            self.depth += 1
            if tag in ('h2', 'h4', 'h5'):
                self.heading = []
        elif tag in ("h2", "h4", "h5"):
            self.heading = []
        elif tag in ("div", "p", "span", "section"):
            classes = set(attrs.get("class", "").split())
            if not self.abstract and (attrs.get("id") == "abstract" or classes & {"abstract", "paper-abstract"} or self.awaiting_abstract):
                self.depth = 1
                self.awaiting_abstract = False

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1
        if tag in ("h2", "h4", "h5") and self.heading is not None:
            self.awaiting_abstract = not self.depth and "".join(self.heading).strip().lower() == "abstract"
            self.heading = None

    def handle_data(self, data):
        if self.heading is not None:
            self.heading.append(data)
        elif self.depth:
            self.abstract.append(data)


def abstract_fields(parsed: PaperHTML) -> dict:
    """Use the observed official abstract containers, never generic page descriptions."""
    text = usable_abstract(''.join(parsed.abstract))
    return dict(abstract=text, abstract_status='valid' if text else 'missing',
                abstract_method='container' if text else '',
                abstract_parser_version=PARSER_VERSION,
                abstract_diagnostic='' if text else 'no_valid_abstract')


def parse_abstract(document: str, paper: dict, *, allow_missing: bool = False) -> str:
    parsed = PaperHTML()
    parsed.feed(document)
    titles = parsed.meta.get("citation_title", [])
    authors = {author_key(a) for a in parsed.meta.get("citation_author", [])}
    if len(titles) != 1 or normalized(titles[0]) != normalized(paper["title"]):
        raise IdentityMismatch("official_title_mismatch")
    if not authors.intersection(author_key(a) for a in paper.get("authors", [])):
        raise IdentityMismatch("official_author_mismatch")
    abstract = abstract_fields(parsed)['abstract']
    if len(abstract) < 80 and not allow_missing:
        raise ValueError("official_abstract_missing")
    return usable_abstract(abstract)
