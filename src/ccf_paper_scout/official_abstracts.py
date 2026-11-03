"""Exact-paper abstract extraction for PMLR, NeurIPS and ACL (no title search)."""
from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit


def official_url(paper: dict) -> str | None:
    url = urlsplit(str(paper.get("ee", "")))
    if url.scheme not in ("http", "https") or url.username or url.password or url.port:
        return None
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
    return None


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
        elif tag in ("h2", "h4", "h5"):
            self.heading = []
        elif tag in ("div", "p", "span"):
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
            self.awaiting_abstract = "".join(self.heading).strip().lower() == "abstract"
            self.heading = None

    def handle_data(self, data):
        if self.depth:
            self.abstract.append(data)
        elif self.heading is not None:
            self.heading.append(data)


def parse_abstract(document: str, paper: dict) -> str:
    parsed = PaperHTML()
    parsed.feed(document)
    titles = parsed.meta.get("citation_title", [])
    authors = {author_key(a) for a in parsed.meta.get("citation_author", [])}
    if len(titles) != 1 or normalized(titles[0]) != normalized(paper["title"]):
        raise ValueError("official_title_mismatch")
    if not authors.intersection(author_key(a) for a in paper.get("authors", [])):
        raise ValueError("official_author_mismatch")
    abstract = " ".join("".join(parsed.abstract).split())
    if len(abstract) < 80:
        raise ValueError("official_abstract_missing")
    return abstract
