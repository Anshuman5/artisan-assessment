"""
Agentic retrieval layer.

Design goals:
- Ground every output in real, fetched snippets (URL + text), never full-context stuffing.
- Token-efficient: we store compact chunks and feed the LLM short, ranked snippets only.
- Self-contained: fetch the company's own pages directly; use web_search for external signals.
"""
from __future__ import annotations

import asyncio
import re
import hashlib
from dataclasses import dataclass, field, asdict
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
import tldextract

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 OutboundIQ/1.0"
)

# Page types we actively look for when crawling a company's own site.
PRIORITY_PATH_HINTS = {
    "about": ["about", "company", "who-we-are", "mission", "story"],
    "product": ["product", "platform", "solution", "features", "how-it-works", "use-case"],
    "pricing": ["pricing", "plans", "price"],
    "customers": ["customers", "case-stud", "testimonial", "success"],
    "blog": ["blog", "news", "press", "resources", "insights"],
    "careers": ["careers", "jobs", "hiring", "open-roles"],
    "contact": ["contact"],
}


def _normalize_url(u: str) -> str:
    u = u.strip()
    if not u:
        return ""
    if not u.startswith("http"):
        u = "https://" + u
    p = urlparse(u)
    # strip fragments
    return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/") or f"{p.scheme}://{p.netloc}"


def root_domain(u: str) -> str:
    ext = tldextract.extract(u)
    return ".".join(part for part in [ext.domain, ext.suffix] if part)


def company_name_guess(u: str) -> str:
    ext = tldextract.extract(u)
    return (ext.domain or "").replace("-", " ").title()


@dataclass
class Snippet:
    """A compact, citable piece of evidence."""
    id: str
    url: str
    title: str
    text: str
    source_type: str  # "page" | "search"
    page_kind: str = "other"  # about/product/pricing/customers/blog/search...

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "snippet": self.text,
            "source_type": self.source_type,
            "page_kind": self.page_kind,
        }


def _mk_id(url: str, text: str) -> str:
    return "s_" + hashlib.sha1((url + "|" + text[:80]).encode("utf-8", "ignore")).hexdigest()[:10]


def _clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", t or "").strip()
    return t


def _chunk(text: str, max_chars: int = 480) -> list[str]:
    """Split extracted page text into compact, paragraph-ish chunks."""
    text = (text or "").strip()
    if not text:
        return []
    # Split on sentence/paragraph boundaries, then pack into ~max_chars chunks.
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    chunks, cur = [], ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(cur) + len(p) + 1 <= max_chars:
            cur = (cur + " " + p).strip()
        else:
            if cur:
                chunks.append(cur)
            cur = p[:max_chars]
    if cur:
        chunks.append(cur)
    return chunks


async def fetch_html(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        r = await client.get(url, timeout=15, follow_redirects=True)
        if r.status_code >= 400:
            return None
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype and ctype:
            return None
        return r.text
    except Exception:
        return None


def extract_main_text(html: str, url: str) -> tuple[str, str]:
    """Return (title, main_text) using trafilatura, with a BS4 fallback for title."""
    title = ""
    text = ""
    try:
        text = trafilatura.extract(html, include_comments=False, include_tables=False,
                                   favor_recall=True, url=url) or ""
    except Exception:
        text = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    if m:
        title = _clean_text(re.sub(r"<[^>]+>", "", m.group(1)))
    return title, _clean_text(text)


def discover_links(html: str, base_url: str, domain: str) -> dict[str, str]:
    """Find priority internal pages. Returns {page_kind: absolute_url}."""
    found: dict[str, str] = {}
    if not html:
        return found
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.I)
    seen = set()
    for href in hrefs:
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absu = _normalize_url(urljoin(base_url, href))
        if not absu or absu in seen:
            continue
        if root_domain(absu) != domain:
            continue
        seen.add(absu)
        path = urlparse(absu).path.lower()
        for kind, hints in PRIORITY_PATH_HINTS.items():
            if kind in found:
                continue
            if any(h in path for h in hints):
                found[kind] = absu
                break
    return found


class EvidenceStore:
    """Holds all snippets gathered during a run; supports ranked retrieval."""

    def __init__(self):
        self.snippets: dict[str, Snippet] = {}

    def add(self, url: str, title: str, text: str, source_type: str, page_kind: str = "other") -> Snippet | None:
        text = _clean_text(text)
        if len(text) < 40:
            return None
        sid = _mk_id(url, text)
        if sid in self.snippets:
            return self.snippets[sid]
        s = Snippet(id=sid, url=url, title=title or url, text=text,
                    source_type=source_type, page_kind=page_kind)
        self.snippets[sid] = s
        return s

    def all(self) -> list[Snippet]:
        return list(self.snippets.values())

    def search(self, query_terms: list[str], limit: int = 8, prefer_kinds: list[str] | None = None) -> list[Snippet]:
        """Lightweight keyword ranking to pull the most relevant snippets for the LLM."""
        terms = [t.lower() for t in query_terms if t]
        prefer_kinds = prefer_kinds or []
        scored = []
        for s in self.snippets.values():
            blob = (s.text + " " + s.title).lower()
            score = sum(blob.count(t) for t in terms)
            if s.page_kind in prefer_kinds:
                score += 2
            if score > 0:
                scored.append((score, s))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored[:limit]]


async def crawl_company_site(url: str, max_pages: int = 7) -> tuple[EvidenceStore, dict]:
    """
    Fetch a company's own site: homepage + discovered priority pages.
    Returns (EvidenceStore, meta) where meta has resolved domain/name and the pages fetched.
    """
    store = EvidenceStore()
    home = _normalize_url(url)
    domain = root_domain(home)
    meta = {"input_url": url, "home": home, "domain": domain,
            "company_name": company_name_guess(home), "pages_fetched": []}

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    async with httpx.AsyncClient(headers=headers) as client:
        home_html = await fetch_html(client, home)
        if not home_html:
            # try http or www variants
            for alt in [home.replace("https://", "http://"),
                        home.replace("https://", "https://www.")]:
                home_html = await fetch_html(client, alt)
                if home_html:
                    home = alt
                    break
        if not home_html:
            meta["error"] = "Could not fetch the site homepage."
            return store, meta

        title, text = extract_main_text(home_html, home)
        for ch in _chunk(text):
            store.add(home, title, ch, "page", "home")
        meta["pages_fetched"].append({"url": home, "kind": "home", "title": title})

        links = discover_links(home_html, home, domain)
        # Fetch priority pages in parallel (cap to max_pages-1).
        targets = list(links.items())[: max_pages - 1]

        async def grab(kind: str, purl: str):
            html = await fetch_html(client, purl)
            if not html:
                return None
            t, txt = extract_main_text(html, purl)
            n = 0
            for ch in _chunk(txt):
                if store.add(purl, t, ch, "page", kind):
                    n += 1
            return {"url": purl, "kind": kind, "title": t, "chunks": n}

        results = await asyncio.gather(*[grab(k, u) for k, u in targets])
        for r in results:
            if r:
                meta["pages_fetched"].append(r)

    return store, meta
