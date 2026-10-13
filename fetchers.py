"""Сбор статей: RSS/Atom, HTML-страницы, Hugging Face Daily Papers, Hacker News (Algolia API).

Плюс поиск ленты у сайта, который добавляет пользователь.
"""
import asyncio
import calendar
import json
import re
import warnings
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
import httpx
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
# Некоторые сайты (ai.meta.com) отдают страницу только краулерам превью ссылок
UA_FALLBACK = "facebookexternalhit/1.1"
HEADERS = {"User-Agent": UA_BROWSER, "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7"}
TEXT_LIMIT = 3000
HTML_DETAILS_LIMIT = 15  # сколько новых страниц статей открывать за один обход HTML-источника
PRERELEASE = re.compile(r"(alpha|beta|[\d.]rc\d*|nightly|\.dev\d*|preview)", re.I)
FEED_PATHS = ["feed", "rss", "rss.xml", "feed.xml", "atom.xml", "index.xml", "feed/", "rss/"]


@dataclass
class Item:
    url: str
    title: str
    published: int | None = None
    text: str = ""
    metric: int | None = None
    metric_kind: str | None = None


class FetchError(Exception):
    pass


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=HEADERS, timeout=40, follow_redirects=True)


async def get(http: httpx.AsyncClient, url: str) -> httpx.Response:
    try:
        r = await http.get(url)
        if r.status_code in (400, 403, 406, 429, 503):
            alt = await http.get(url, headers={"User-Agent": UA_FALLBACK})
            if alt.status_code < 400:
                return alt
        r.raise_for_status()
        return r
    except httpx.HTTPStatusError as e:
        raise FetchError(f"HTTP {e.response.status_code}") from None
    except httpx.HTTPError as e:
        raise FetchError(type(e).__name__ + (f": {e}" if str(e) else "")) from None


# ---------------------------------------------------------------- утилиты

def normalize_url(url: str) -> str:
    """Убирает якорь и utm-метки, чтобы одна статья не сохранялась дважды."""
    p = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "ref", "fbclid", "gclid"))]
    return urlunparse((p.scheme, p.netloc.lower(), p.path, "", urlencode(query), ""))


def html_to_text(html: str, limit: int = TEXT_LIMIT) -> str:
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)[:limit]


def parse_date(value) -> int | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        parsed = feedparser._parse_date(str(value))  # RFC 822 и прочие форматы лент
        return calendar.timegm(parsed) if parsed else None


# ---------------------------------------------------------------- форматы

def parse_feed(content: bytes, source_url: str) -> tuple[str, list[Item]]:
    feed = feedparser.parse(content)
    if not feed.entries:
        raise FetchError("в ленте нет записей")
    github = re.match(r"https://github\.com/[^/]+/([^/]+)/releases", source_url)
    items = []
    for e in feed.entries:
        link = e.get("link")
        title = html_to_text(e.get("title", ""), 300)
        if not link or not title:
            continue
        if github:
            if PRERELEASE.search(title):
                continue  # альфы, беты и rc — шум
            repo = github.group(1)
            if repo.lower() not in title.lower():
                title = f"{repo} {title}"
        ts = e.get("published_parsed") or e.get("updated_parsed")
        body = e["content"][0].get("value", "") if e.get("content") else e.get("summary", "")
        item = Item(normalize_url(link), title, calendar.timegm(ts) if ts else None, html_to_text(body))
        if "hnrss.org" in source_url and (m := re.search(r"Points:\s*(\d+)", e.get("summary", ""))):
            item.metric, item.metric_kind = int(m.group(1)), "hn"
        items.append(item)
    return feed.feed.get("title", ""), items


def parse_hf_papers(content: bytes) -> list[Item]:
    items = []
    for it in json.loads(content):
        p = it.get("paper") or {}
        if not p.get("id"):
            continue
        items.append(Item(
            url=f"https://huggingface.co/papers/{p['id']}",
            title=html_to_text(p.get("title") or it.get("title", ""), 300),
            published=parse_date(p.get("submittedOnDailyAt") or it.get("publishedAt") or p.get("publishedAt")),
            text=(p.get("ai_summary") or p.get("summary") or "")[:TEXT_LIMIT],
            metric=p.get("upvotes"), metric_kind="hf"))
    return items


def parse_hn(content: bytes) -> list[Item]:
    items = []
    for h in json.loads(content).get("hits", []):
        hn_link = f"https://news.ycombinator.com/item?id={h['objectID']}"
        items.append(Item(
            url=normalize_url(h.get("url") or hn_link),
            title=h.get("title") or "",
            published=h.get("created_at_i"),
            text=(html_to_text(h.get("story_text") or "") + f" (обсуждение: {hn_link})").strip(),
            metric=h.get("points"), metric_kind="hn"))
    return items


def html_links(html: str, page_url: str, link_prefix: str | None = None) -> list[tuple[str, str]]:
    """Ссылки на статьи со страницы-списка: тот же сайт, путь на один уровень глубже страницы-списка
    (или глубже link_prefix, если статьи лежат не под ней — например, /stories/ на главной)."""
    page = urlparse(page_url)
    prefix = link_prefix or page.path.rstrip("/") + "/"
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(page_url, a["href"])
        p = urlparse(url)
        if p.netloc != page.netloc or p.query or not p.path.startswith(prefix):
            continue
        rest = p.path[len(prefix):].strip("/")
        if link_prefix and "/" in rest:
            # Ссылки на подразделы статьи (/idea/<slug>/build) сводим к самой статье
            rest = rest.split("/")[0]
            url = f"{p.scheme}://{p.netloc}{prefix}{rest}"
        if not rest or "/" in rest or rest in ("rss", "feed", "page") or rest.startswith(("category", "tag", "page")):
            continue
        url = normalize_url(url)
        text = a.get_text(" ", strip=True)
        if len(text) > len(found.get(url, "")):
            found[url] = text
    return list(found.items())


def article_details(html: str, url: str, fallback_title: str) -> Item:
    soup = BeautifulSoup(html, "html.parser")

    def meta(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    title = meta("og:title", "twitter:title") or (soup.title.get_text(strip=True) if soup.title else fallback_title)
    published = parse_date(meta("article:published_time", "og:published_time", "date", "publish-date"))
    if not published and (t := soup.find("time", attrs={"datetime": True})):
        published = parse_date(t["datetime"])
    if not published:
        for script in soup.find_all("script", type="application/ld+json"):
            if m := re.search(r'"datePublished"\s*:\s*"([^"]+)"', script.string or ""):
                published = parse_date(m.group(1))
                break
    paragraphs = " ".join(p.get_text(" ", strip=True) for p in (soup.find("article") or soup).find_all("p"))
    text = (meta("og:description", "description") or "") + " " + paragraphs
    return Item(url, html_to_text(title, 300) or fallback_title, published, re.sub(r"\s+", " ", text).strip()[:TEXT_LIMIT])


# ---------------------------------------------------------------- сбор с источника

async def fetch_source(http: httpx.AsyncClient, kind: str, url: str, is_new_url=None, first_run: bool = False,
                       link_prefix: str | None = None) -> list[Item]:
    """Статьи источника. is_new_url(list[url]) -> set новых ссылок нужен HTML-источникам,
    чтобы открывать страницы только новых статей; на первом обходе подробно — только 3 свежие."""
    r = await get(http, url)
    if kind == "rss":
        return parse_feed(r.content, url)[1]
    if kind == "hf_papers":
        return parse_hf_papers(r.content)
    if kind == "hn":
        return parse_hn(r.content)
    if kind != "html":
        raise FetchError(f"неизвестный тип источника {kind}")

    links = html_links(r.text, url, link_prefix)
    if not links:
        raise FetchError("на странице не нашлось ссылок на статьи")
    fresh = set(is_new_url([u for u, _ in links])) if is_new_url else {u for u, _ in links}
    detail_budget = 3 if first_run else HTML_DETAILS_LIMIT
    items = []
    for link, text in links:
        if link not in fresh:
            continue
        if detail_budget > 0:
            detail_budget -= 1
            try:
                page = await get(http, link)
                items.append(article_details(page.text, link, text))
                continue
            except FetchError:
                pass
        items.append(Item(link, text[:300] or link))
    return items


# ---------------------------------------------------------------- добавление сайта

def _site_name(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("meta", attrs={"property": "og:site_name"})
    if tag and tag.get("content"):
        return tag["content"].strip()
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)[:60]
    return urlparse(url).netloc


async def discover(url: str) -> dict:
    """Определяет, как читать сайт: {name, url, kind, items}. Сначала ищет ленту, потом пробует HTML."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    async with client() as http:
        r = await get(http, url)
        feed = feedparser.parse(r.content)
        if feed.entries:
            title, items = parse_feed(r.content, url)
            return {"name": title or urlparse(url).netloc, "url": url, "kind": "rss", "items": items}

        html = r.text
        soup = BeautifulSoup(html, "html.parser")
        candidates = [urljoin(url, l["href"]) for l in soup.find_all("link", href=True)
                      if "rss" in (l.get("type") or "") or "atom" in (l.get("type") or "")]
        candidates += [urljoin(url, a["href"]) for a in soup.find_all("a", href=True)
                       if re.search(r"/(rss|feed|atom)(\.xml)?/?$", a["href"])]
        base = url.rstrip("/") + "/"
        candidates += [urljoin(base, p) for p in FEED_PATHS]
        root = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
        if root != base:
            candidates += [urljoin(root, p) for p in FEED_PATHS[:4]]
        seen = set()
        for cand in candidates:
            if cand in seen:
                continue
            seen.add(cand)
            try:
                fr = await get(http, cand)
                title, items = parse_feed(fr.content, cand)
            except FetchError:
                continue
            # Лента всего сайта, когда добавили раздел, тоже подходит — лучше, чем разбирать HTML
            return {"name": title or _site_name(html, url), "url": cand, "kind": "rss", "items": items}

        items = await fetch_source(http, "html", url, first_run=True)
        return {"name": _site_name(html, url), "url": url, "kind": "html", "items": items}


async def gather_limited(coros, limit: int = 6):
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros), return_exceptions=True)
