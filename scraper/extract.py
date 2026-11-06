"""Reads event and special listings out of web pages.

Three extraction strategies, in order of reliability:
  1. Structured data: schema.org Event objects in JSON-LD (used by most event
     platforms, ticketing sites and many venue sites / WordPress event plugins).
  2. Custom CSS selectors set per source in the admin page (for sites you want
     to tune precisely).
  3. Heuristics: headings and text blocks that talk about happy hours, specials,
     deals, trivia, live music, etc., with day/time detection for recurring ones.
"""
import asyncio
import html
import json
import os
import re
from datetime import datetime, timedelta
from urllib import robotparser
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dateparser


BOT_NAME = "LocalHappenings"
USER_AGENT = os.environ.get("USER_AGENT", f"{BOT_NAME}/1.0")
MAX_ITEMS_PER_PAGE = 40

SPECIAL_RE = re.compile(
    r"happy\s*hour|specials?\b|deals?\b|discount|\d+\s*%\s*off|half[\s-]*(off|price)|bogo|"
    r"buy one|\$\s?\d|\bbrunch\b|all[\s-]day|wing night|taco tuesday|prix fixe|"
    r"kids eat free|two for|2 for \$|drink of the|featured (drink|cocktail|beer)|of the (week|month)",
    re.I,
)
EVENT_RE = re.compile(
    r"\bevents?\b|trivia|live music|karaoke|open mic|comedy|tasting|concert|"
    r"\bshow\b|festival|market|workshop|class\b|bingo|dj\b|band\b|performance|tonight",
    re.I,
)
DAY = (r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
       r"mon|tues|tue|weds|wed|thurs|thur|thu|fri|sat|sun)(?:s)?\.?")
DAYS_RE = re.compile(
    rf"\b(?:every\s+)?{DAY}(?:\s*(?:-|–|to|thru|through|&|and|,)\s*{DAY})*\b|\bdaily\b|\bweekdays\b|\bweekends\b|\bnightly\b",
    re.I,
)
TIME = r"\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?"
TIMES_RE = re.compile(
    rf"\b{TIME}\s*(?:-|–|to|until|til)\s*{TIME}|\b\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)\b|\buntil close\b|\ball[\s-]day\b",
    re.I,
)
DATE_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b(?!\s*(?:off|price|lb|oz|pound|of))",
    re.I,
)
WEEKDAY_TITLE_RE = re.compile(r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)days?\b", re.I)
HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]


# ---------------------------------------------------------------- helpers

def _clean(text, limit=None):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


def _types(obj):
    t = obj.get("@type", [])
    return [t] if isinstance(t, str) else [x for x in t if isinstance(x, str)]


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _url_of(v):
    v = _first(v)
    if isinstance(v, dict):
        v = v.get("url") or v.get("@id")
    return v if isinstance(v, str) else None


def _address_text(addr):
    addr = _first(addr)
    if isinstance(addr, str):
        return _clean(addr)
    if isinstance(addr, dict):
        parts = [addr.get(k) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode")]
        return _clean(", ".join(str(p) for p in parts if p))
    return None


def _parse_date(text, now=None):
    """Find a specific calendar date in text; returns ISO date string or None."""
    now = now or datetime.now()
    m = DATE_RE.search(text or "")
    if not m:
        return None
    raw = m.group(0)
    try:
        d = dateparser.parse(raw, default=datetime(now.year, 1, 1), fuzzy=True)
    except (ValueError, OverflowError):
        return None
    has_year = bool(re.search(r"\d{4}|/\d{2,4}$", raw))
    if not has_year and d < now - timedelta(days=60):
        d = d.replace(year=d.year + 1)  # "Jan 10" seen in November means next year
    if abs((d - now).days) > 400:
        return None
    return d.date().isoformat()


def _recurring(text):
    """Short schedule phrase like "all day Tuesdays and 4pm-6pm Wednesday through Friday"."""
    text = text or ""
    day_matches = list(DAYS_RE.finditer(text))
    matches = day_matches + list(TIMES_RE.finditer(text))
    if not matches:
        return None
    a, b = min(m.start() for m in matches), max(m.end() for m in matches)
    span = _clean(text[a:b])
    stems = re.findall(r"\b(mon|tue|wed|thu|fri|sat|sun)", span, re.I)
    repeats = len(stems) != len({x.lower() for x in stems})
    if repeats:
        t = TIMES_RE.search(text)
        out = _clean(f"{day_matches[-1].group(0)} {t.group(0) if t else ''}", 60)
        return out[0].upper() + out[1:]
    # Keep the whole phrase only if what sits between the days/times is just joining words.
    ordered = sorted(matches, key=lambda m: m.start())
    gaps = [text[x.end():y.start()] for x, y in zip(ordered, ordered[1:]) if y.start() > x.end()]
    joiners_only = all(len(re.findall(r"[a-z0-9$%]+", g, re.I)) <= 2 and "$" not in g and not re.search(r"[.!?]\s", g) for g in gaps)
    if len(span) <= 70 and joiners_only:
        span = re.sub(r"\s*:\s*", " ", span)
        return span[0].upper() + span[1:]
    days, times = DAYS_RE.search(text), TIMES_RE.search(text)
    bits = [m.group(0).strip() for m in (days, times) if m]
    return _clean(" ".join(bits), 60) or None


QUESTION_RE = re.compile(
    r"^(?:do|does|can|is|are|will)\s+(?:you|we|i|there|they)\s+(?:have|offer|host|do|get|be|a)?\s*(?:a|an|any|some)?\s*",
    re.I,
)


def _tidy_title(title):
    """Turn FAQ-style questions ("Do you have a happy hour?") into labels ("Happy hour")."""
    t = _clean(title)
    t2 = re.sub(r"^(?:join us|come (?:in|out|by))\s+(?:for|on|every)?\s*(?:our|the)?\s*(?:now\s+)?(?:famous\s+)?", "", t, flags=re.I)
    if t2 != t and len(t2.split()) >= 2:
        t = t2[0].upper() + t2[1:]
    if t.endswith("?"):
        t2 = QUESTION_RE.sub("", t[:-1]).strip()
        if 2 < len(t2) < len(t):
            t = t2[0].upper() + t2[1:]
    return t


def _kind(text):
    return "special" if SPECIAL_RE.search(text or "") else "event"


# ---------------------------------------------------------------- strategy 1: JSON-LD

def _walk(o):
    if isinstance(o, list):
        for x in o:
            yield from _walk(x)
    elif isinstance(o, dict):
        yield o
        for k in ("@graph", "itemListElement", "item", "subEvent", "event", "events", "mainEntity"):
            if k in o:
                yield from _walk(o[k])


def from_jsonld(soup, base_url):
    out = []
    for tag in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _walk(data):
            if not any(t.endswith("Event") for t in _types(obj)):
                continue
            name = _clean(html.unescape(str(obj.get("name") or "")), 160)
            if not name:
                continue
            loc = _first(obj.get("location"))
            venue = address = lat = lng = None
            if isinstance(loc, dict):
                venue = _clean(loc.get("name"), 120) or None
                address = _address_text(loc.get("address"))
                g = loc.get("geo") or {}
                try:
                    lat, lng = float(g.get("latitude")), float(g.get("longitude"))
                except (TypeError, ValueError):
                    lat = lng = None
            elif isinstance(loc, str):
                address = _clean(loc)

            price = None
            offers = _first(obj.get("offers"))
            if isinstance(offers, dict) and offers.get("price") not in (None, ""):
                p = str(offers.get("price"))
                price = ("Free" if p in ("0", "0.0", "0.00") else f"{offers.get('priceCurrency', '$')} {p}".replace("USD ", "$"))
            elif obj.get("isAccessibleForFree") in (True, "true", "True"):
                price = "Free"

            desc = html.unescape(BeautifulSoup(str(obj.get("description") or ""), "lxml").get_text(" "))
            out.append({
                "kind": "event",
                "title": name,
                "description": _clean(desc, 500),
                "start": obj.get("startDate"),
                "end": obj.get("endDate"),
                "recurring": None,
                "url": urljoin(base_url, _url_of(obj.get("url")) or "") or base_url,
                "image": _url_of(obj.get("image")),
                "price": price,
                "venue": venue,
                "address": address,
                "lat": lat,
                "lng": lng,
            })
    return out


# ---------------------------------------------------------------- strategy 2: CSS selectors

def from_selectors(soup, base_url, item_sel, title_sel=None, date_sel=None):
    out = []
    for el in soup.select(item_sel)[:MAX_ITEMS_PER_PAGE]:
        text = _clean(el.get_text(" "))
        if not text:
            continue
        t_el = el.select_one(title_sel) if title_sel else el.find(HEADINGS + ["strong", "b"])
        title = _clean(t_el.get_text(" ") if t_el else text, 120)
        d_el = el.select_one(date_sel) if date_sel else None
        date_text = d_el.get("datetime") or d_el.get_text(" ") if d_el else text
        start = None
        if d_el and d_el.get("datetime"):
            start = d_el["datetime"]
        else:
            start = _parse_date(date_text)
        link = el.find("a", href=True) if el.name != "a" else el
        img = el.find("img", src=True)
        out.append({
            "kind": _kind(text),
            "title": title,
            "description": _clean(text.replace(title, "", 1), 500),
            "start": start,
            "end": None,
            "recurring": None if start else _recurring(text),
            "url": urljoin(base_url, link["href"]) if link else base_url,
            "image": urljoin(base_url, img["src"]) if img else None,
            "price": None, "venue": None, "address": None, "lat": None, "lng": None,
        })
    return out


# ---------------------------------------------------------------- strategy 3: heuristics

def _following(heading):
    """Blocks that come after a heading, up to the next heading.

    Page builders (Elementor, Wix, etc.) wrap each heading in its own boxes, so
    climb out of wrappers that hold nothing but the heading before looking.
    """
    node = heading
    for _ in range(4):
        if node.find_next_sibling() is not None or node.parent is None or node.parent.name in ("body", "html"):
            break
        node = node.parent
    for sib in node.find_next_siblings():
        if sib.name in HEADINGS or sib.name in ("footer", "header", "nav", "aside") or sib.find(HEADINGS):
            break
        yield sib


def _section_text(heading):
    """Text that follows a heading, up to the next heading."""
    parts = []
    for sib in _following(heading):
        parts.append(sib.get_text(" "))
        if sum(len(p) for p in parts) > 700:
            break
    text = _clean(" ".join(parts))
    if not text and heading.parent is not None and len(heading.parent.find_all(HEADINGS)) == 1:
        text = _clean(heading.parent.get_text(" ").replace(heading.get_text(" "), "", 1))
    return text


def heuristics(soup, base_url):
    for t in soup(["script", "style", "noscript", "svg", "form", "nav", "iframe"]):
        t.decompose()
    out, seen_text = [], set()

    def add(title, body, anchor, weekly=False):
        title = _tidy_title(title)
        full = f"{title} {body}"
        if re.search(r"private (event|part)|\bbook(ing)?\b|\brent(al)?\b|reserv", full, re.I):
            return
        if weekly:
            # "Burger Wednesdays": a standing weekly special, even if the page shows this week's date.
            start = None
            if DAYS_RE.fullmatch(title.rstrip(":!").strip()) and body:
                # Heading is just "Tuesday"; the special itself is the text under it.
                title, body = f"{title.rstrip(':!').strip()}: {re.split(r'(?<=[.!])\s', body)[0][:100]}", body
            day = WEEKDAY_TITLE_RE.search(title).group(0).capitalize()
            day = day if day.endswith("s") else day + "s"
            t = TIMES_RE.search(body)
            recurring = f"{day} {t.group(0)}" if t else day
            kind = "event" if EVENT_RE.search(title) else "special"
        else:
            start = _parse_date(full)
            recurring = None if start else _recurring(full)
            kind = _kind(full)
        # Require some signal it's time-bound or a real offer, not just a nav label.
        if kind == "event" and not (start or recurring):
            return
        if kind == "special" and not (recurring or re.search(r"\$\s?\d|%|half|bogo|free|happy\s*hour", full, re.I) or len(body) > 20):
            return
        link = anchor.find("a", href=True) if anchor else None
        out.append({
            "kind": kind,
            "title": _clean(title, 120),
            "description": _clean(body, 500),
            "start": start, "end": None, "recurring": recurring,
            "url": urljoin(base_url, link["href"]) if link else base_url,
            "image": None, "price": None, "venue": None, "address": None, "lat": None, "lng": None,
        })
        seen_text.add(_clean(body)[:200])

    # Headings that announce a special or event, with the text beneath them.
    for h in soup.find_all(HEADINGS):
        title = _clean(h.get_text(" "))
        if not (3 <= len(title) <= 120):
            continue
        body = _section_text(h)
        # A heading over a list of several specials ("Weekly Specials") is a
        # container: let the line-by-line pass below pick up each one instead.
        lines = []
        for sib in _following(h):
            lines += [x for x in ([sib] if sib.name in ("p", "li") else sib.find_all(["li", "p"]))]
        if sum(1 for x in lines if SPECIAL_RE.search(x.get_text(" ")) or EVENT_RE.search(x.get_text(" "))) >= 2:
            continue
        if not body and SPECIAL_RE.search(title):
            # "BURGER OF THE WEEK" followed by a heading naming it.
            nxt = h.find_next(HEADINGS)
            if nxt is not None:
                name = _clean(nxt.get_text(" "))
                if 3 <= len(name) <= 80 and not SPECIAL_RE.search(name):
                    title = f"{title.title() if title.isupper() else title}: {name}"
                    body = _section_text(nxt) or name
        weekly = bool(WEEKDAY_TITLE_RE.search(title)) and not DATE_RE.search(title) and len(title.split()) <= 6
        if weekly:
            add(title, body, h.parent, weekly=True)
        elif SPECIAL_RE.search(title) or (EVENT_RE.search(title) and (DATE_RE.search(title + " " + body) or DAYS_RE.search(title + " " + body))):
            add(title, body, h.parent)

    # Stand-alone paragraphs / list items that describe a special with a day or time.
    for el in soup.find_all(["p", "li", "dd", "td"]):
        text = _clean(el.get_text(" "))
        if not (15 <= len(text) <= 350):
            continue
        if any(text[:200] in s or s in text for s in seen_text if s):
            continue
        if SPECIAL_RE.search(text) and (DAYS_RE.search(text) or TIMES_RE.search(text)):
            strong = el.find(["strong", "b"])
            title = _clean(strong.get_text(" ")) if strong and len(strong.get_text(strip=True)) > 3 else re.split(r"(?<=[.!:])\s", text)[0]
            short = re.split(r"\s+(?:every|on|from|is|are)\s+", title, maxsplit=1, flags=re.I)[0].rstrip("!.:")
            if len(short.split()) >= 2:
                title = short
            if DAYS_RE.fullmatch(title.rstrip(":").strip()):
                # "Tuesday: $4.50 Any Single Taco" -> title "$4.50 Any Single Taco"
                rest = _clean(text[len(title):].lstrip(" :-–"))
                if rest:
                    title = re.split(r"(?<=[.!])\s", rest)[0]
            add(title, text, el)
        elif EVENT_RE.search(text) and (DATE_RE.search(text) or (DAYS_RE.search(text) and not re.search(r"private|book|rent", text, re.I))):
            title = re.split(r"(?<=[.!:])\s|\s[–—-]\s", text)[0]
            add(title, text, el)

    # Weekly specials posted only as pictures, labelled by their alt text or
    # file name ("Wings Wednesday.jpg", "BurgerMonday_revised2025.jpg").
    for img in soup.find_all("img"):
        label = img.get("alt") or ""
        if not WEEKDAY_TITLE_RE.search(label):
            label = unquote(urlparse(img.get("src") or "").path.rsplit("/", 1)[-1])
            label = re.sub(r"\.(jpe?g|png|gif|webp|avif)$", "", label, flags=re.I)
        label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", re.sub(r"[_\-+~]+", " ", label))
        words = [w for w in label.split() if not re.search(r"\d", w)
                 and w.lower() not in {"revised", "final", "copy", "new", "flyer", "img", "image", "edit", "mv"}]
        label = " ".join(words)
        if WEEKDAY_TITLE_RE.search(label) and 2 <= len(words) <= 5 and not DATE_RE.search(label):
            label = label[0].upper() + label[1:]
            if label.lower() not in {o["title"].lower() for o in out}:
                add(label, "", img, weekly=True)

    # De-duplicate by title.
    uniq, titles = [], set()
    for it in out:
        key = it["title"].lower()
        if key not in titles:
            titles.add(key)
            uniq.append(it)
    return uniq[:MAX_ITEMS_PER_PAGE]


# ---------------------------------------------------------------- fetching

async def _robots_allows(client, url):
    p = urlparse(url)
    try:
        r = await client.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=10)
    except httpx.HTTPError:
        return True
    if r.status_code >= 400:
        return True
    rp = robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp.can_fetch(BOT_NAME, url)


def extract(html, url, source):
    soup = BeautifulSoup(html, "lxml")
    items = from_jsonld(soup, url)
    if source.get("item_selector"):
        items += from_selectors(soup, url, source["item_selector"], source.get("title_selector"), source.get("date_selector"))
    else:
        # A page that publishes structured events is an events calendar; reading
        # its layout as well only adds clutter (calendar cells, repeated listings).
        if not items:
            items += heuristics(soup, url)
    return items


async def fetch_source(source: dict):
    """Fetch every URL for one source. Returns (items, errors)."""
    urls = source.get("urls") or []
    if isinstance(urls, str):
        urls = [urls]
    items, errors = [], []
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=25) as client:
        for i, url in enumerate(u.strip() for u in urls if u and u.strip()):
            if i:
                await asyncio.sleep(1.5)  # be polite to the same site
            if not await _robots_allows(client, url):
                errors.append(f"robots.txt disallows {url}")
                continue
            try:
                r = await client.get(url)
                r.raise_for_status()
            except httpx.HTTPError as e:
                code = getattr(getattr(e, "response", None), "status_code", "")
                errors.append(f"{url}: {type(e).__name__} {code}".strip())
                continue
            try:
                items += extract(r.text, str(r.url), source)
            except Exception as e:  # a broken page shouldn't stop the run
                errors.append(f"{url}: could not read page ({e})")
    return items, errors
