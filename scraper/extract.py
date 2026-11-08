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
    rf"\b(?:every\s+)?{DAY}(?:\s*(?:-|–|to|thru|through|&|and|,)\s*{DAY})*\b|\bdaily\b|\bevery\s?day\b|\bweekdays\b|\bweekends\b|\bnightly\b",
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

def _plain(text):
    """Turn any HTML-ish text into plain text.

    Handles tags, entity codes (&amp; &#8217; &nbsp;), text that was encoded
    twice (&lt;p&gt; -> <p> -> nothing), literal "\\n" sequences, and the
    "[…]" / "[&hellip;]" marks WordPress adds to cut-off excerpts.
    """
    text = str(text or "")
    for _ in range(3):  # some sites encode more than once
        if not re.search(r"&[#a-zA-Z0-9]+;|<[a-zA-Z/!][^>]*>", text):
            break
        text = html.unescape(text)
        if re.search(r"<[a-zA-Z/!][^>]*>", text):
            text = BeautifulSoup(text, "lxml").get_text(" ")
    text = text.replace("\\n", " ").replace("\\t", " ").replace("\xa0", " ")
    text = re.sub(r"\s*\[(?:…|\.\.\.)\]\s*$", "…", text.strip())
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    return text


def _clean(text, limit=None):
    text = re.sub(r"\s+", " ", _plain(text)).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


def _local_dt(v):
    """Structured-data date/time -> "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM" as written on the
    venue's own clock. Sites often label local times as UTC (+00:00), so offsets are
    ignored; 1-5 AM usually means "all day" in event plugins, so that time is dropped;
    odd forms like "2026-11-6T19-19-00-00" are repaired."""
    v = str(v or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2})(?::(\d{2})|-\d{1,2}-(\d{2})))?", v)
    if not m:
        return v or None
    day = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    if m.group(4) is None:
        return day
    h, mi = int(m.group(4)), int(m.group(5) or m.group(6) or 0)
    if 1 <= h < 5 or h > 23 or mi > 59:
        return day
    return f"{day}T{h:02d}:{mi:02d}"


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


def _with_time(day, time_text):
    """"2026-11-24" + "6:30 PM" -> "2026-11-24T18:30" (start time of a range)."""
    if not (day and time_text):
        return day
    # A range that names am/pm once, "6:45-10:00 pm": the start shares the end's
    # am/pm unless that would put it after the end ("11-2pm" starts at 11am).
    rng = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*(?:-|–|—|to|until|til)\s*"
                    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)", time_text, re.I)
    if rng:
        h, mnt = int(rng.group(1)), int(rng.group(2) or 0)
        if rng.group(3):
            ap = rng.group(3).lower()[0]
        else:
            end_ap, end_h = rng.group(6).lower()[0], int(rng.group(4))
            ap = end_ap if (h % 12) <= (end_h % 12) else ("a" if end_ap == "p" else "p")
    else:
        m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)", time_text, re.I)
        if not m:
            return day
        h, mnt, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()[0]
    if h > 12 or mnt > 59:
        return day
    h = h % 12 + (12 if ap == "p" else 0)
    return f"{day}T{h:02d}:{mnt:02d}"


ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "last": -1}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHLY_RE = re.compile(
    r"\b(first|1st|second|2nd|third|3rd|fourth|4th|last)\s+"
    r"(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?\s+(?:of\s+)?(?:every|each|the|a)?\s*month",
    re.I,
)
LOCAL_TZ = os.environ.get("TIMEZONE", "America/New_York")


def _monthly(text):
    """"first Monday of every month" -> (1, 0, "First Monday of every month"), or None."""
    m = MONTHLY_RE.search(text or "")
    if not m:
        return None
    nth = ORDINALS[m.group(1).lower()]
    wd = next(i for i, d in enumerate(WEEKDAYS) if d.startswith(m.group(2).lower()))
    return nth, wd, f"{m.group(1).capitalize()} {WEEKDAYS[wd].capitalize()} of every month"


def _next_monthly(nth, wd, today=None):
    """Next date (today or later) that is the nth weekday of its month (nth=-1: last)."""
    if today is None:
        try:
            from zoneinfo import ZoneInfo
            today = datetime.now(ZoneInfo(LOCAL_TZ)).date()
        except Exception:
            today = datetime.now().date()
    y, mo = today.year, today.month
    for _ in range(14):
        first = datetime(y, mo, 1).date()
        days = [first + timedelta(days=i) for i in range(31) if (first + timedelta(days=i)).month == mo]
        hits = [d for d in days if d.weekday() == wd]
        d = hits[nth - 1] if nth > 0 and len(hits) >= nth else (hits[-1] if nth == -1 else None)
        if d and d >= today:
            return d.isoformat()
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    return None


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
        best = max(day_matches, key=lambda m: len(m.group(0)))  # "Monday through Friday" over "Fridays"
        out = _clean(f"{best.group(0)} {t.group(0) if t else ''}", 60)
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


# Listings about renting the place out, not things the public can come to.
PRIVATE_RE = re.compile(
    r"private (?:event|part|dining|room)|party rooms?|banquet|\brental\b|\brent (?:our|the|a)\b|"
    r"book (?:your|a|an) (?:party|event|private)|host (?:your|a) (?:party|event)",
    re.I,
)


def _tidy_title(title):
    """Turn FAQ-style questions ("Do you have a happy hour?") into labels ("Happy hour")."""
    t = _clean(title)
    t2 = re.sub(r"^(?:join us|enjoy|sign up|come (?:in|out|by))\s+(?:for\s+)?(?:one of\s+)?(?:for|on|every)?\s*(?:our|the)?\s*(?:now\s+)?(?:famous\s+)?", "", t, flags=re.I)
    if t2 != t and len(t2.split()) >= 2:
        t = t2[0].upper() + t2[1:]
    t3 = re.sub(r"\s+(?:at|from|starting at)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)\b.*$", "", t, flags=re.I)
    if len(t3.split()) >= 2:
        t = t3                                          # "Wine-tasting dinners at 6:30 PM" -> "Wine-tasting dinners"
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
            name = _clean(obj.get("name"), 160)
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

            desc = obj.get("description")
            out.append({
                "kind": "event",
                "title": name,
                "description": _clean(desc, 500),
                "start": _local_dt(obj.get("startDate")),
                "end": _local_dt(obj.get("endDate")),
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


FILE_JUNK = {"jpg", "jpeg", "png", "webp", "gif", "avif", "img", "image", "photo", "post", "website", "web",
             "thumb", "final", "copy", "new", "revised", "edit", "mv", "flyer", "graphic", "insta", "fb", "story"}


def _name_from_file(src):
    """A readable name from an image file name, or None.

    kids-eat-free-website-jpg_001.webp -> "Kids eat free"
    10-99-lunch-jpg_001.webp            -> "$10.99 lunch"
    serrarestaurant_2for22tuesdays_post_7-18-25-jpg.webp -> "2 for $22"
    monday-jpg.webp                     -> None (only a day name)
    """
    name = unquote(urlparse(src or "").path.rsplit("/", 1)[-1])
    name = re.sub(r"\.(jpe?g|png|gif|webp|avif)$", "", name, flags=re.I)
    name = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", name)
    name = re.sub(r"[_\-+~.]+", " ", name)
    name = re.sub(r"\b\d{1,2} \d{1,2} \d{2,4}\b", " ", name)          # dates like 7-18-25
    words = [w for w in name.split() if w.lower() not in FILE_JUNK and len(w) <= 12]
    text = " ".join(words)
    text = re.sub(r"\b(\d+) for (\d+)\b", r"\1 for $\2", text)
    text = re.sub(r"\b(\d{1,2}) (\d{2})\b", r"$\1.\2", text)
    text = re.sub(r"(?<![$.\w])\d+(?![\w.])(?! for)", " ", text)           # stray numbers
    text = DAYS_RE.sub(" ", text)
    text = _clean(text)
    if len(re.findall(r"[a-z]{2,}|\$[\d.]+", text, re.I)) < 2:
        return None
    return text[0].upper() + text[1:]


def _picture_under(heading):
    """The image directly under a heading that has no text of its own, if any."""
    for sib in _following(heading):
        text = _clean(sib.get_text(" "))
        links = " ".join(_clean(a.get_text(" ")) for a in (sib.find_all("a") if sib.name != "a" else [sib]))
        if text and (len(text.split()) > 4 or _clean(text.replace(links, "")) if links else True):
            return None
        img = sib if sib.name == "img" else sib.find("img")
        if img is not None and (img.get("src") or img.get("data-src")):
            return img
    return None


def heuristics(soup, base_url):
    for t in soup(["script", "style", "noscript", "svg", "form", "nav", "iframe"]):
        t.decompose()
    out, seen_text = [], set()

    def add(title, body, anchor, weekly=False):
        if re.fullmatch(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", title.strip()):
            return  # a phone number
        if DATE_RE.fullmatch(title.strip(" -–:")) and body:
            rest = re.sub(r"^\s*" + re.escape(title.strip(" -–:")) + r"\s*[-–:|]?\s*", "", body)
            title = re.split(r"\s+(?:-|–|\|)\s+|(?<=[.!])\s", rest)[0][:90] or title
        title = re.sub(r"^is\s+", "", title.strip(), flags=re.I)
        title = _tidy_title(title)
        full = f"{title} {body}"
        bare = _clean(TIMES_RE.sub(" ", DAYS_RE.sub(" ", re.sub(r"\b(?:every|each|on|at|from)\b", " ", title, flags=re.I))))
        if len(bare.split()) == 0:
            # Title is only a schedule ("Every Friday"): name it by what happens.
            m = re.search(r"live music|live band|music bingo|trivia|karaoke|bingo|open mic|comedy|happy\s*hour|brunch|wine tasting|dj\b", body, re.I)
            if m:
                t = m.group(0).lower()
                title = t[0].upper() + t[1:]
        if PRIVATE_RE.search(full):
            return
        if re.search(r"facebook|instagram|follow us|newsletter|subscribe", title, re.I):
            return
        if re.fullmatch(r"(?:our |weekly |daily |current |today'?s )?(?:specials?|deals|events?|happy hours?|menu)!?", title.strip().lower()) \
                and not (DAYS_RE.search(body) or TIMES_RE.search(body) or re.search(r"\$\s?\d|%", body)):
            return  # a section title ("SPECIALS") with only an intro line under it
        if weekly:
            # "Burger Wednesdays": a standing weekly special, even if the page shows this week's date.
            start = None
            if DAYS_RE.fullmatch(title.rstrip(":!").strip()) and body:
                # Heading is just "Tuesday"; the special itself is the text under it.
                title = f"{title.rstrip(':!').strip()}: {re.split(r'(?<=[.!])\s', body)[0][:100]}"
            day = WEEKDAY_TITLE_RE.search(title).group(0).capitalize()
            day = day if day.endswith("s") else day + "s"
            t = TIMES_RE.search(body)
            recurring = f"{day} {t.group(0)}" if t else day
            kind = "event" if EVENT_RE.search(title) or (EVENT_RE.search(body) and not SPECIAL_RE.search(full)) else "special"
        else:
            start = _parse_date(full)
            if start:
                tm = TIMES_RE.search(full)
                start = _with_time(start, tm.group(0) if tm else None)
            recurring = None if start else _recurring(full)
            monthly = None if start else _monthly(full)
            if monthly:
                # "first Monday of every month at 6:30 PM": show the next actual date.
                nth, wd, phrase = monthly
                t = TIMES_RE.search(full)
                start = _with_time(_next_monthly(nth, wd), t.group(0) if t else None)
                recurring = phrase
            kind = _kind(full)
            if monthly and EVENT_RE.search(full) and re.search(r"dinner|tasting|class|night|club|party", full, re.I):
                kind = "event"
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

    def add_picture(title, img):
        """A special shown only as a picture under a short heading ("Eldersburg: Monday")."""
        prefix, _, rest = title.rpartition(":")
        prefix, rest = prefix.strip(), (rest.strip() or title)
        if not prefix or DAYS_RE.fullmatch(prefix):
            prefix, rest = "", title.strip()
        src = urljoin(base_url, img.get("src") or img.get("data-src"))
        sched = _recurring(rest) if (DAYS_RE.search(rest) or TIMES_RE.search(rest)) else None
        leftover = _clean(re.sub(r"\b(?:thru|through|to|and|only)\b|[-–&,!]", " ", TIMES_RE.sub(" ", DAYS_RE.sub(" ", rest)), flags=re.I))
        if len(leftover.split()) >= 2 or (leftover and WEEKDAY_TITLE_RE.search(rest)):
            name = rest  # the heading already names it: "Crab Cake Thursdays!", "Happy Hour Deals"
        else:
            name = _name_from_file(src) or (f"{rest} special" if len(rest.split()) <= 4 else rest)
        if sched and re.fullmatch(r"every\s?day", sched, re.I):
            sched = "Every day"
        if re.search(r"private (event|part)|facebook|instagram|follow us|gift card", f"{title} {name}", re.I):
            return
        pictures_used.add(id(img))
        if sched and re.fullmatch(r"(?!every)[A-Za-z]+day", sched, re.I):
            sched += "s"
        link = None
        box = img.parent
        while box is not None and box.find(HEADINGS) is None and box.parent is not None:
            box = box.parent  # the block holding this heading + picture
        for a in (box.find_all("a", href=True) if box is not None else []):
            if a.find_previous("img") is img and re.search(r"\.pdf($|\?)", a["href"], re.I):
                link = a
                break
        out.append({
            "kind": "event" if EVENT_RE.search(f"{rest} {name}") and not SPECIAL_RE.search(name) else "special",
            "title": _clean(f"{prefix}: {name}" if prefix else name, 120),
            "description": "" if sched else _clean(rest, 200) if rest.lower() not in name.lower() else "",
            "start": None, "end": None, "recurring": sched,
            "url": urljoin(base_url, link["href"]) if link else base_url,
            "image": src, "price": None, "venue": None, "address": None, "lat": None, "lng": None,
        })

    # Headings that announce a special or event, with the text beneath them.
    heads = soup.find_all(HEADINGS)
    used, pictures_used = set(), set()
    specials_page = bool(re.search(r"special|deal|happy", base_url, re.I)) or any(
        SPECIAL_RE.search(_clean(x.get_text(" "))) for x in heads)
    for idx, h in enumerate(heads):
        if id(h) in used:
            continue
        title = _clean(h.get_text(" "))
        if not (3 <= len(title) <= 120):
            continue
        body = _section_text(h)
        # "Tuesday November 24th" heading, then a heading with the event's name.
        dm = DATE_RE.search(title)
        if dm and not body and idx + 1 < len(heads):
            rest = _clean(DAYS_RE.sub(" ", title.replace(dm.group(0), " ")).strip(" ,-–:"))
            nxt = heads[idx + 1]
            name = _clean(nxt.get_text(" "))
            if len(rest.split()) <= 1 and 3 <= len(name) <= 120 and not DATE_RE.search(name):
                used.add(id(nxt))
                details = _section_text(nxt)
                when = _parse_date(title)
                if when and not re.search(r"private (event|part)", f"{name} {details}", re.I):
                    t = TIMES_RE.search(details)
                    price = re.search(r"\$\s?\d+(?:\.\d\d)?\s*(?:per person|pp\b|/person|per ticket|admission|cover)", details, re.I)
                    out.append({
                        "kind": "event", "title": name, "description": _clean(details, 500),
                        "start": _with_time(when, t.group(0) if t else None), "end": None, "recurring": None,
                        "url": next((urljoin(base_url, a["href"]) for a in [nxt.find_next("a", href=True)]
                                     if a is not None and a.find_previous(HEADINGS) is nxt), base_url),
                        "image": None, "price": price.group(0) if price else None,
                        "venue": None, "address": None, "lat": None, "lng": None,
                    })
                    seen_text.add(_clean(details)[:200])
                for sib in _following(nxt):
                    for el in ([sib] if sib.name in ("p", "li") else sib.find_all(["p", "li"])):
                        t = _clean(el.get_text(" "))
                        if t:
                            seen_text.add(t[:200])
                continue
        link_text = " ".join(_clean(a.get_text(" ")) for sib in _following(h) for a in ([sib] if sib.name == "a" else sib.find_all("a")))
        bare = not body or (len(body.split()) <= 4 and _clean(body.replace(link_text, "")) == "") if link_text else not body
        label = title.rpartition(":")[2] or title
        offer_like = (DAYS_RE.search(label) or TIMES_RE.search(label) or SPECIAL_RE.search(label)
                      or re.search(r"\b(lunch|brunch|dinner|breakfast|late night)\b", label, re.I))
        if bare and specials_page and offer_like and len(title.split()) <= 8:
            pic = _picture_under(h)
            if pic is not None:
                add_picture(title, pic)
                continue
        # A heading over a list of several specials ("Weekly Specials") is a
        # container: let the line-by-line pass below pick up each one instead.
        lines = []
        for sib in _following(h):
            lines += [x for x in ([sib] if sib.name in ("p", "li") else sib.find_all(["li", "p"]))]
        if sum(1 for x in lines if SPECIAL_RE.search(x.get_text(" ")) or EVENT_RE.search(x.get_text(" "))) >= 2:
            continue
        day_only = bool(DAYS_RE.fullmatch(title.rstrip(":!").strip()))
        nxt_t = _clean(heads[idx + 1].get_text(" ")) if idx + 1 < len(heads) else ""
        schedule_next = bool(nxt_t) and not day_only and len(nxt_t) <= 60 and bool(DAYS_RE.search(nxt_t) or TIMES_RE.search(nxt_t)) \
            and not DATE_RE.search(title) and not DATE_RE.search(nxt_t)
        if not body and (SPECIAL_RE.search(title) or day_only or schedule_next):
            # Some layouts put the details in the next headings instead of text:
            # "Tuesday" / "$4.50 Any Single Taco", "Happy Hour" / "$1 off drafts" /
            # "Tues-Fri 2-6pm", or "Burger of the Week" / "Miss Mary's Mac Burger".
            of_week = bool(re.search(r"of the (week|month)", title, re.I))
            extra = []
            for nxt in heads[idx + 1: idx + 4]:
                t = _clean(nxt.get_text(" "))
                if not t or len(t) > 120 or DAYS_RE.fullmatch(t.rstrip(":!").strip()):
                    break
                if day_only or (of_week and not extra):
                    extra.append(t)
                    used.add(id(nxt))
                    if of_week and _section_text(nxt):
                        extra.append(_section_text(nxt))
                    break
                if re.search(r"\$\s?\d|%|\boff\b", t) or TIMES_RE.search(t) or (schedule_next and (DAYS_RE.search(t) or re.search(r"\d", t))):
                    extra.append(t)
                    used.add(id(nxt))
                else:
                    break
            if extra:
                used.add(id(h))
                if day_only:
                    title, body = f"{title.rstrip(':!').strip()}: {extra[0]}", extra[0]
                elif of_week:
                    title = f"{title.capitalize() if title.isupper() else title}: {extra[0]}"
                    body = " ".join(extra[1:]) or extra[0]
                else:
                    body = " ".join(extra)
        weekly = bool(WEEKDAY_TITLE_RE.search(title)) and not DATE_RE.search(title) and len(title.split()) <= 6
        if weekly:
            add(title, body, h.parent, weekly=True)
        elif SPECIAL_RE.search(title) or (EVENT_RE.search(title) and (DATE_RE.search(title + " " + body) or DAYS_RE.search(title + " " + body))):
            add(title, body, h.parent)

    # Picture blocks: a picture named for a day ("mondays-25.jpg", "steak-night.jpg")
    # followed by the deal in plain text, up to the next picture.
    if specials_page:
        for img in soup.find_all("img"):
            if id(img) in pictures_used:
                continue
            src = img.get("src") or img.get("data-src") or ""
            fname = unquote(urlparse(src).path.rsplit("/", 1)[-1])
            label = re.sub(r"[_\-+~.]+", " ", re.sub(r"\.(jpe?g|png|gif|webp|avif)$", "", fname, flags=re.I))
            lines, size = [], 0
            for el in img.next_elements:
                tag = getattr(el, "name", None)
                if tag == "img" or (tag in HEADINGS and _clean(el.get_text(" "))):
                    break
                if isinstance(el, str):
                    t = _clean(el)
                    if t:
                        lines.append(t)
                        size += len(t)
                        if size > 600:
                            break
            text = " ".join(lines)
            if not lines or not re.search(r"\$\s?\d|%|1/2|half|\boff\b|\bfor 1\b|happy\s*hour|bottomless", text, re.I):
                continue
            day_m = WEEKDAY_TITLE_RE.search(label)
            file_name = _name_from_file(src)
            if not (day_m or file_name or DAYS_RE.search(text)):
                continue
            pictures_used.add(id(img))
            clock = [m.group(0) for m in TIMES_RE.finditer(text) if re.search(r"\d", m.group(0))]
            head = lines[0]
            head_is_offer = re.match(r"^[-•*]?\s*\$|^\d+(/\d)?\b", head) is not None
            head_used = False
            if file_name:
                title = file_name                       # "Steak night", "Orange crushes"
            elif not head_is_offer and len(head) <= 70:
                title = _tidy_title(head)               # "TWOsdays", "Sunday BRUNCH"
                head_used = True
            else:
                title = re.sub(r"^[-•*]\s*", "", head if head_is_offer else lines[0])[:70]
            if day_m:
                day = day_m.group(0).capitalize()
                day = day if day.endswith("s") else day + "s"
                if not WEEKDAY_TITLE_RE.search(title):
                    title = f"{day.rstrip('s')}: {title}" if (head_is_offer or file_name is None and len(title) > 40) else title
                recurring = _clean(f"{day} {clock[0] if clock else ''}")
            else:
                recurring = _recurring(text)
                if recurring and not DAYS_RE.search(recurring):
                    m = re.search(r"\b(?:till|til|until)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm))", text, re.I)
                    recurring = f"Until {m.group(1)}" if m else recurring
            details = "; ".join(re.sub(r"^[-•*]\s*", "", l) for l in lines if not (head_used and l == head))
            out.append({
                "kind": "special", "title": _clean(title, 120), "description": _clean(details, 500),
                "start": None, "end": None, "recurring": recurring, "url": base_url,
                "image": urljoin(base_url, src), "price": None, "venue": None, "address": None, "lat": None, "lng": None,
            })
            seen_text.add(_clean(text)[:200])
            for l in lines:
                seen_text.add(l[:200])

    # Site builders (Wix, Squarespace...) split one notice into short lines:
    # "TUESDAY NIGHT" / "TRIVIA!" / "Every Tuesday 6:30pm" / "Bar Area".
    lines = []
    for el in soup.find_all(["p", "li"] + HEADINGS):
        if el.find(["p", "li"] + HEADINGS):
            continue
        t = _clean(el.get_text(" ")).strip("\u200b ")
        if t:
            lines.append((el, t, id(el) in used))  # used = already read by the headings pass
    bare_day = lambda t: bool(DAYS_RE.fullmatch(t.rstrip(":!").strip()))
    for blk in soup.find_all(["div", "section"]):
        ps = [p for p in blk.find_all("p", recursive=False)]
        if len(ps) < 2:
            continue
        ls = [x for x in (_clean(p.get_text(" ")).strip("\u200b ") for p in ps) if x]
        text = " ".join(ls)
        if not (2 <= len(ls) <= 8 and 15 <= len(text) <= 400) or any(bare_day(x) for x in ls):
            continue
        if not ((EVENT_RE.search(text) or SPECIAL_RE.search(text)) and (DAYS_RE.search(text) or TIMES_RE.search(text))):
            continue
        # The title is the lines before the schedule line ("Every Tuesday 6:30pm").
        k = next((i for i, x in enumerate(ls) if i and (TIMES_RE.search(x) or re.match(r"(?:every|on|from|each)\b|\d", x, re.I))), 0)
        title = " ".join(ls[:k]) if k else ls[0]
        if len(title) > 80:
            continue
        add(title, text, blk)
        for x in ls:
            seen_text.add(x[:200])

    # A day on its own line ("Mondays") with the deal on the next lines.
    STOP = re.compile(r"^(?:from |read more|by [A-Z]|follow|contact|hours|menu$)", re.I)
    for i, (el, t, was_used) in enumerate(lines):
        if not bare_day(t) or was_used:
            continue
        body = []
        for el2, t2, used2 in lines[i + 1:i + 7]:
            if used2 or bare_day(t2) or len(t2) > 150 or STOP.search(t2):
                break
            body.append(t2)
        joined = " ".join(body)
        if not body or not re.search(r"\$\s?\d|%|\boff\b|free|half|bogo|special|night|happy", joined, re.I):
            continue
        day = t.rstrip(":!").strip()
        plural = day[0].upper() + day[1:].lower()
        plural = plural if plural.endswith("s") else plural + "s"
        clock = next((m.group(0) for m in TIMES_RE.finditer(joined) if re.search(r"\d", m.group(0))), "")
        title = f"{plural.rstrip('s')}: {re.sub(r'[!.]+$', '', body[0])}"
        if any(re.sub(r"[^a-z0-9]+", " ", o["title"].lower()).strip() == re.sub(r"[^a-z0-9]+", " ", title.lower()).strip() for o in out):
            continue
        out.append({
            "kind": "event" if EVENT_RE.search(joined) and not SPECIAL_RE.search(joined) else "special",
            "title": _clean(title, 120), "description": _clean("; ".join(body), 500),
            "start": None, "end": None, "recurring": _clean(f"{plural} {clock}"), "url": base_url,
            "image": None, "price": None, "venue": None, "address": None, "lat": None, "lng": None,
        })
        for b in body:
            seen_text.add(b[:200])

    # Stand-alone paragraphs / list items that describe a special with a day or time.
    for el in soup.find_all(["p", "li", "dd", "td"]):
        text = _clean(el.get_text(" "))
        if not (15 <= len(text) <= 350):
            continue
        if any(text[:200] in s or s in text for s in seen_text if s):
            continue
        if re.search(r"facebook|instagram|follow us|newsletter|subscribe|sign up for (?:our |the )?(?:email|mailing|text|newsletter)", text, re.I):
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
            elif not (SPECIAL_RE.search(title) or EVENT_RE.search(title)):
                # Title came out as filler ("Once life"); name it by the offer instead.
                m = re.search(r"happy\s*hour|brunch|wing night|taco tuesday|[a-z]+ specials?", text, re.I)
                if m:
                    title = m.group(0).strip().capitalize()
            add(title, text, el)
        elif EVENT_RE.search(text) and (DATE_RE.search(text) or (DAYS_RE.search(text) and not re.search(r"private|book|rent", text, re.I))):
            title = re.split(r"(?<=[.!:])\s|\s[–—-]\s", text)[0]
            bare = _clean(TIMES_RE.sub(" ", DAYS_RE.sub(" ", re.sub(r"\b(?:every|each|on|at|from)\b", " ", title, flags=re.I))))
            if len(bare.split()) <= 1 or len(title) > 80:
                # Title is only a schedule ("Every Friday"): name it by what happens.
                m = re.search(r"live music|live band|trivia|karaoke|bingo|open mic|comedy|dj\b|music bingo|wine tasting|tasting", text, re.I)
                if m:
                    title = m.group(0).capitalize() if m.group(0).islower() else m.group(0)
                    title = title[0].upper() + title[1:].lower()
            add(title, text, el)

    # Weekly specials posted only as pictures, labelled by their alt text or
    # file name ("Wings Wednesday.jpg", "BurgerMonday_revised2025.jpg").
    for img in soup.find_all("img"):
        if id(img) in pictures_used:
            continue
        label = img.get("alt") or ""
        if DATE_RE.search(label):
            continue  # a dated banner ("Tuesday November 24th"), not a weekly special
        if not WEEKDAY_TITLE_RE.search(label):
            label = unquote(urlparse(img.get("src") or "").path.rsplit("/", 1)[-1])
            label = re.sub(r"\.(jpe?g|png|gif|webp|avif)$", "", label, flags=re.I)
        label = re.sub(r"\.(?:jpe?g|png|gif|webp|avif)\b", " ", label, flags=re.I)
        label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", re.sub(r"[_\-+~]+", " ", label))
        words = [w for w in label.split() if not re.search(r"\d", w) and w.lower() not in FILE_JUNK]
        label = " ".join(words)
        if WEEKDAY_TITLE_RE.search(label) and 2 <= len(words) <= 5 and not DATE_RE.search(label):
            label = label[0].upper() + label[1:]
            norm = lambda x: re.sub(r"[^a-z0-9]+", " ", x.lower()).strip()
            if norm(label) not in {norm(o["title"]) for o in out}:
                add(label, "", img, weekly=True)

    # De-duplicate by title.
    uniq, titles = [], set()
    for it in out:
        key = re.sub(r"[^a-z0-9]+", " ", it["title"].lower()).strip()
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


# ---------------------------------------------------------------- strategy: page data (Next.js sites)

WEEKDAY_WORD_RE = re.compile(r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)days?\b", re.I)


def from_page_data(html, base_url):
    """Many chain sites (Next.js + a content system) ship their promo cards as JSON
    inside the page instead of as text. Read cards that name a weekday."""
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S) or \
        re.search(r"<!--NEXT_DATA\n(.*?)\n-->", html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1).replace("- -", "--") if "<!--NEXT_DATA" in m.group(0) else m.group(1))
    except (ValueError, TypeError):
        return []

    def text_of(v):
        if isinstance(v, str):
            return _clean(v)
        if isinstance(v, dict):
            f = v.get("fields", v)
            return _clean(f.get("text") or f.get("title") or "") if isinstance(f, dict) else ""
        if isinstance(v, list):
            return _clean(" ".join(text_of(x) for x in v))
        return ""

    out, seen = [], set()

    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("description"), str) and any(k in o for k in ("title", "subtitle", "name", "heading")):
                parts = {k: text_of(o.get(k)) for k in ("title", "subtitle", "name", "heading")}
                desc = _clean(o["description"])
                joined = " ".join(list(parts.values()) + [desc])
                if WEEKDAY_WORD_RE.search(joined) and (SPECIAL_RE.search(joined) or EVENT_RE.search(joined) or re.search(r"free|bogo|off\b", joined, re.I)):
                    title = next((parts[k] for k in ("subtitle", "name", "heading", "title") if parts[k] and len(parts[k]) > 3
                                  and not re.fullmatch(r"(?:every\s+)?" + DAYS_RE.pattern, parts[k], re.I)), "")
                    title = title or desc[:80]
                    day = WEEKDAY_WORD_RE.search(joined).group(0).capitalize()
                    day = day if day.endswith("s") else day + "s"
                    key = title.lower()
                    if key not in seen:
                        seen.add(key)
                        out.append({
                            "kind": "event" if EVENT_RE.search(joined) and not SPECIAL_RE.search(joined) else "special",
                            "title": _clean(title, 120), "description": _clean(desc, 400),
                            "start": None, "end": None, "recurring": day, "url": base_url,
                            "image": None, "price": None, "venue": None, "address": None, "lat": None, "lng": None,
                        })
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return out


# ---------------------------------------------------------------- strategy: calendar feeds (iCal)

def _ical_unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text)


def _ical_text(v):
    return (v or "").replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def from_ical(text, base_url):
    """Events from an .ics calendar feed (WordPress "The Events Calendar", Google
    Calendar and most calendar tools publish one)."""
    out = []
    for block in _ical_unfold(text).split("BEGIN:VEVENT")[1:]:
        block = block.split("END:VEVENT")[0]
        f = {}
        for line in block.splitlines():
            m = re.match(r"^([A-Z-]+)((?:;[^:]*)?):(.*)$", line)
            if m and m.group(1) not in f:
                f[m.group(1)] = (m.group(2), m.group(3))
        def when(key):
            if key not in f:
                return None
            v = f[key][1].strip()
            m = re.match(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2}))?", v)
            if not m:
                return None
            d = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            if m.group(4) and not ("VALUE=DATE" in f[key][0] and "T" not in v):
                if v.endswith("Z"):  # UTC: convert to local time
                    try:
                        from zoneinfo import ZoneInfo
                        dt = datetime.strptime(v[:15], "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(LOCAL_TZ))
                        return dt.strftime("%Y-%m-%dT%H:%M")
                    except Exception:
                        pass
                return f"{d}T{m.group(4)}:{m.group(5)}"
            return d
        title = _clean(_ical_text(f.get("SUMMARY", ("", ""))[1]), 160)
        start = when("DTSTART")
        if not title or not start:
            continue
        end = when("DTEND")
        if end and len(end) == 10 and start and len(start) == 10 and end > start:
            # all-day events end the morning after; make the end inclusive
            end = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=1)).date().isoformat()
        loc = _clean(_ical_text(f.get("LOCATION", ("", ""))[1]), 200) or None
        geo_v = f.get("GEO", ("", ""))[1]
        lat = lng = None
        if ";" in geo_v:
            try:
                lat, lng = (float(x) for x in geo_v.split(";")[:2])
            except ValueError:
                lat = lng = None
        out.append({
            "kind": "special" if SPECIAL_RE.search(title) and not EVENT_RE.search(title) else "event",
            "title": title,
            "description": _clean(_ical_text(f.get("DESCRIPTION", ("", ""))[1]), 400),
            "start": start, "end": end if end and end != start else None, "recurring": None,
            "url": (f.get("URL", ("", ""))[1].strip() or base_url),
            "image": None, "price": None, "venue": None,
            "address": re.sub(r",\s*United States$", "", loc) if loc else None,
            "lat": lat, "lng": lng,
        })
    return out


# ---------------------------------------------------------------- strategy: event cards

CAL_LINK_RE = re.compile(r"google\.com/calendar|calendar/render|format=ical|\.ics\b|outlook\.(?:live|office)|addtocalendar|ical=1", re.I)
CAL_TEXT_RE = re.compile(r"^(?:google calendar|ics|ical|outlook|add to calendar|export)$", re.I)
READ_MORE_RE = re.compile(r"^(read|learn|see|view|find out) more|^more info|^details|^tickets?|^register|^rsvp|^book|^reserve|^buy", re.I)


def event_cards(soup, base_url):
    """Calendars that list events as repeated cards: a title link, a date, maybe a
    time, place and picture. Finds groups of 3+ same-styled blocks that each have
    a calendar date and their own link, and reads one event from each."""
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    groups = {}
    for el in soup.find_all(lambda t: t.get("class") or t.name == "tr"):
        key = (el.name, " ".join(sorted(el.get("class", []))))
        groups.setdefault(key, []).append(el)
    good = []
    for key, els in groups.items():
        if len(els) < 3:
            continue
        hrefs, ok = set(), 0
        for el in els:
            text = _clean(el.get_text(" "))
            links = [a for a in el.find_all("a", href=True) if not a["href"].startswith(("#", "mailto:", "tel:", "javascript"))
                     and not CAL_LINK_RE.search(a["href"]) and not CAL_TEXT_RE.search(_clean(a.get_text(" ")))]
            is_row = el.name == "tr"
            if not (20 <= len(text) <= (1500 if is_row else 800) and DATE_RE.search(text) and (links or is_row)):
                continue
            hrefs.add(links[0]["href"] if links else id(el))
            ok += 1
        rows = key[0] == "tr"
        if ok >= 3 and ok >= (0.6 if rows else 0.8) * len(els) and len(hrefs) >= (1 if rows else 0.8 * ok):
            good.append(els)
    # Keep the innermost groups (the card, not the row of cards around it).
    members = {id(e) for els in good for e in els}
    good = [els for els in good if not any(id(d) in members for e in els for d in e.find_all(True))]
    out = []
    for els in good:
        for el in els:
            text = _clean(el.get_text(" "))
            when = _parse_date(text)
            if not when:
                continue
            title_el = (el.select_one("[class*=title] a, a[class*=title], [class*=title]")
                        or (el.select_one("strong, b") if el.name == "tr" else None)
                        or next((h for h in el.find_all(HEADINGS) if not DATE_RE.search(h.get_text(" "))), None)
                        or max((a for a in el.find_all("a") if not READ_MORE_RE.search(_clean(a.get_text(" ")))),
                               key=lambda a: len(_clean(a.get_text(" "))), default=None))
            title = _clean(title_el.get_text(" ") if title_el else "", 160)
            if CAL_TEXT_RE.search(title):
                continue
            title = re.sub(r"^(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s*[-–:|]?\s*", "", title, flags=re.I) or title
            if len(title) < 3:
                continue
            link = title_el if title_el is not None and title_el.name == "a" else (title_el.find("a", href=True) if title_el else None)
            link = link or el.find("a", href=True)
            t = TIMES_RE.search(text.replace(title, " "))
            loc_el = el.select_one("[class*=location], [class*=address], [class*=venue]")
            place = _clean(loc_el.get_text(" "), 160) if loc_el else None
            img = el.find("img", src=True)
            if img is None and el.parent is not None and len(el.parent.find_all(DATE_RE)) <= 1:
                img = el.parent.find("img", src=True)  # picture sits beside the text block
            desc_el = el.select_one("[class*=desc] p, [class*=summary], [class*=excerpt], p")
            out.append({
                "kind": "event", "title": title,
                "description": _clean(desc_el.get_text(" "), 300) if desc_el else "",
                "start": _with_time(when, t.group(0) if t else None), "end": None, "recurring": None,
                "url": urljoin(base_url, link["href"]) if link else base_url,
                "image": urljoin(base_url, img["src"]) if img else None,
                "price": None, "venue": None,
                # A street address places the event on the map; a bare town doesn't need to.
                "address": place if place and re.search(r"\d", place) else None,
                "lat": None, "lng": None,
            })
    return out


def extract(html, url, source):
    if html.lstrip().startswith("BEGIN:VCALENDAR"):
        return from_ical(html, url)
    soup = BeautifulSoup(html, "lxml")
    items = from_jsonld(soup, url) + from_page_data(html, url)
    if source.get("item_selector"):
        items += from_selectors(soup, url, source["item_selector"], source.get("title_selector"), source.get("date_selector"))
    else:
        # A page that publishes structured events is an events calendar; reading
        # its layout as well only adds clutter (calendar cells, repeated listings).
        if not items:
            cards = event_cards(BeautifulSoup(html, "lxml"), url)
            if cards:
                # A calendar of event cards: take the cards, and only specials from the rest.
                items += cards + [i for i in heuristics(soup, url) if i["kind"] == "special"]
            else:
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
            if source.get("read_images") and "html" in r.headers.get("content-type", "html"):
                items += await _items_from_images(client, r.text, str(r.url), source, errors)
    return items, errors


async def _items_from_images(client, page_html, page_url, source, errors):
    """Read flyers and specials boards on the page with OCR (sources.yml: read_images)."""
    from . import ocr
    if not ocr.available():
        errors.append("read_images is on, but the OCR program (tesseract) isn't installed")
        return []
    pattern = source["read_images"] if isinstance(source["read_images"], str) else None
    out = []
    for img_url in ocr.page_images(BeautifulSoup(page_html, "lxml"), page_url, pattern):
        if not await _robots_allows(client, img_url):
            continue
        await asyncio.sleep(1)
        try:
            r = await client.get(img_url, headers={"Accept": "image/*"})
            r.raise_for_status()
            if not r.headers.get("content-type", "").startswith("image/"):
                continue
            lines = await asyncio.to_thread(ocr.image_lines, r.content)
            found = ocr.items_from_lines(lines, page_url, img_url, source.get("name", ""))
            print(f"    picture {img_url.rsplit('/', 1)[-1]}: {len(found)} items")
            out += found
        except Exception as e:  # one unreadable picture shouldn't stop the rest
            print(f"    picture {img_url}: skipped ({type(e).__name__})")
    return out
