"""Check every site in sources.yml and write docs/data/items.json.

Run from the repo root:  python -m scraper.run
"""
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import yaml

from .extract import TIMES_RE, _clean, _monthly, _next_monthly, _with_time, fetch_source
from .geo import Geocoder
from . import site

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = os.path.join(ROOT, "sources.yml")
OUT_DIR = os.path.join(ROOT, "docs", "data")
ITEMS = os.path.join(OUT_DIR, "items.json")
STATUS = os.path.join(OUT_DIR, "status.json")
GEOCACHE = os.path.join(ROOT, "scraper", "geocache.json")


def load_config():
    """Site settings at the top of sources.yml (site_url, google_site_verification)."""
    with open(SOURCES) as f:
        data = yaml.safe_load(f) or {}
    return {k: v for k, v in data.items() if k != "sites"}


def load_sources():
    with open(SOURCES) as f:
        data = yaml.safe_load(f) or {}
    sources = data.get("sites") or []
    out = []
    for i, s in enumerate(sources):
        if not isinstance(s, dict) or not s.get("name") or not (s.get("urls") or s.get("manual")):
            print(f"Skipping entry {i + 1} in sources.yml: it needs a name and at least one URL or manual item.", file=sys.stderr)
            continue
        if s.get("paused"):
            continue
        sel = s.get("selectors") or {}
        out.append({
            "id": hashlib.sha1(s["name"].encode()).hexdigest()[:10],
            "name": s["name"],
            "urls": (s.get("urls") if isinstance(s.get("urls"), list) else [s["urls"]]) if s.get("urls") else [],
            "manual": s.get("manual") or [],
            "no_details": bool(s.get("no_details")),
            "read_images": s.get("read_images") or False,
            "skip": [str(x).lower() for x in (s.get("skip") if isinstance(s.get("skip"), list) else [s["skip"]] if s.get("skip") else [])],
            "only": [str(x).lower() for x in (s.get("only") if isinstance(s.get("only"), list) else [s["only"]] if s.get("only") else [])],
            "address": s.get("address"),
            "lat": s.get("lat"),
            "lng": s.get("lng"),
            "item_selector": sel.get("item"),
            "title_selector": sel.get("title"),
            "date_selector": sel.get("date"),
        })
    return out


def load_previous_status():
    try:
        with open(STATUS) as f:
            return {x["site"]: x for x in json.load(f).get("sites", [])}
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        return {}


def load_previous():
    try:
        with open(ITEMS) as f:
            return json.load(f).get("items", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


async def main():
    sources = load_sources()
    previous = load_previous()
    prev_status = load_previous_status()
    geo = Geocoder(GEOCACHE)
    today = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    all_items, report = [], []

    for src in sources:
        if (src["lat"] is None or src["lng"] is None) and src["address"]:
            g = await geo.lookup(src["address"])
            if g:
                src["lat"], src["lng"] = g["lat"], g["lng"]

        items, errors = await fetch_source(src) if src["urls"] else ([], [])

        # Items typed in by hand (for sites that block automated checks or only post on social media).
        for m in src["manual"]:
            if not isinstance(m, dict) or not m.get("title"):
                continue
            when = str(m.get("when") or "")
            dated = re.match(r"^\d{4}-\d{2}-\d{2}", when)
            start = when if dated else None
            monthly = None if dated else _monthly(when)
            if monthly:  # "First Monday of every month 6:30 PM" -> next actual date
                t = TIMES_RE.search(when)
                start = _with_time(_next_monthly(monthly[0], monthly[1]), t.group(0) if t else None)
            items.append({
                "kind": "event" if str(m.get("kind", "")).lower().startswith("event") else "special",
                "title": str(m["title"]), "description": str(m.get("details") or ""),
                "start": start, "end": None, "recurring": None if dated else (when or None),
                "url": m.get("url") or (src["urls"][0] if src["urls"] else None),
                "image": None, "price": m.get("price"), "venue": None, "address": None, "lat": None, "lng": None,
            })

        for it in items:
            if it["lat"] is None and it.get("address") and it["address"] != src["address"]:
                g = await geo.lookup(it["address"])
                if g:
                    it["lat"], it["lng"] = g["lat"], g["lng"]
            if it["lat"] is None:
                it["lat"], it["lng"] = src["lat"], src["lng"]
            it["venue"] = it["venue"] or src["name"]
            it["address"] = it["address"] or src["address"]
            it["source"] = src["name"]

        status = "ok" if not errors else ("partial" if items else "error")
        # If a site couldn't be reached this time, keep what it had before.
        if errors and not items:
            items = [p for p in previous if p.get("source") == src["name"]]
            if items:
                status = "error (showing last results)"
        # A site that suddenly comes back empty (often a temporary bot check) keeps its
        # last results for up to 4 checks in a row (about a day) before it counts as empty.
        empty_runs = 0
        if not items and not errors:
            empty_runs = int(prev_status.get(src["name"], {}).get("empty_runs", 0)) + 1
            if empty_runs <= 4:
                items = [p for p in previous if p.get("source") == src["name"]]
                if items:
                    status = f"empty this time (showing last results, {empty_runs} of 4)"

        kept, seen, seen_days = [], set(), set()
        for it in items:
            # Plain text only, whatever the source (including results kept from a previous run).
            for k, limit in (("title", 160), ("description", 500), ("venue", 120), ("address", 200), ("price", 40), ("recurring", 80)):
                if it.get(k):
                    it[k] = _clean(it[k], limit) or None
            if not it.get("title"):
                continue
            # "Sat, Nov 21 Line Dancing Workshop" -> "Line Dancing Workshop" (the date is shown on the card)
            if it.get("start"):
                t2 = re.sub(r"^(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s*[-–:|]?\s*", "", it["title"], flags=re.I)
                if len(t2) >= 3:
                    it["title"] = t2
            # Small title tidy-ups: "2026 ~ The Few", "We have live music every Saturday! Click here for more"
            t3 = re.sub(r"^\d{4}\s*~\s*", "", it["title"])
            t3 = re.sub(r"\s*click here.*$", "", t3, flags=re.I)
            t3 = re.sub(r"^we (?:have|host|offer)\s+", "", t3, flags=re.I)
            if len(t3) >= 3 and t3 != it["title"]:
                it["title"] = t3[0].upper() + t3[1:]
            if src["no_details"]:
                it["description"] = ""  # the site's descriptions are wrong or copied; show names only
            tl = it["title"].lower()
            # Skip words: plain words match anywhere in the title; "^brunch$" style matches exactly.
            if any((re.search(w, tl) if re.search(r"[\^$]", w) else w in tl) for w in src["skip"]):
                continue  # words listed under "skip:" for this site
            # One page shared by several locations ("Eldersburg: Monday", "Westminster: Tuesday"):
            # keep this location's items and the ones for all locations, and drop the label.
            if src["only"]:
                m = re.match(r"^\s*([^:]{2,40}):\s*(.+)$", it["title"])
                if m and not re.search(r"\d|\$", m.group(1)):
                    label = m.group(1).lower()
                    if any(o in label for o in src["only"]) or re.search(r"\b(both|all) locations?\b", label):
                        it["title"] = m.group(2)
                    else:
                        continue
            start, end = (it.get("start") or "")[:10], (it.get("end") or "")[:10]
            if start and (end or start) < today:
                continue  # already over
            day_key = (it["title"].lower(), (it.get("start") or "")[:10])
            if it.get("start") and day_key in seen_days:
                continue
            seen_days.add(day_key)
            key = hashlib.sha1(f"{src['name']}|{it['kind']}|{it['title'].lower()}|{it.get('start') or ''}".encode()).hexdigest()[:12]
            if key in seen:
                continue
            seen.add(key)
            it["id"] = key
            kept.append(it)
        # A listing repeated on many dates (weekly piano night) shows its next date only.
        counts = {}
        for it in kept:
            if it.get("start"):
                counts[it["title"].lower()] = counts.get(it["title"].lower(), 0) + 1
        try:
            from zoneinfo import ZoneInfo
            local_today = datetime.now(ZoneInfo(os.environ.get("TIMEZONE", "America/New_York"))).date().isoformat()
        except Exception:
            local_today = today
        shown, slim = set(), []
        for it in sorted(kept, key=lambda x: x.get("start") or "9999"):
            k = it["title"].lower()
            if counts.get(k, 0) >= 3:
                if k in shown or (it.get("end") or it["start"])[:10] < local_today:
                    continue  # show the next upcoming date only
                shown.add(k)
                desc = re.sub(r"^Repeats: \d+ upcoming dates listed on their site\. ", "", it.get("description") or "")
                it["description"] = _clean(f"Repeats: {counts[k]} upcoming dates listed on their site. " + desc, 500)
            slim.append(it)
        kept = slim

        all_items += kept

        report.append({"site": src["name"], "status": status, "found": len(kept), "errors": errors, "empty_runs": empty_runs,
                       "mapped": src["lat"] is not None})
        flag = "" if src["lat"] is not None else "  (no map location: add an address or lat/lng)"
        print(f"{src['name']}: {len(kept)} items, {status}{flag}")
        for e in errors:
            print(f"   - {e}")

    # The same event listed by two sources (a venue and the county calendar): show it once.
    once, seen_any = [], set()
    for it in all_items:
        k = (re.sub(r"[^a-z0-9]+", " ", it["title"].lower()).strip(), (it.get("start") or "")[:10])
        if it.get("start") and k in seen_any:
            continue
        seen_any.add(k)
        once.append(it)
    all_items = once

    geo.save()
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(ITEMS, "w") as f:
        json.dump({"updated": stamp, "items": all_items}, f, indent=1, ensure_ascii=False)
    with open(STATUS, "w") as f:
        json.dump({"updated": stamp, "sites": report}, f, indent=1, ensure_ascii=False)
    print(f"\nSaved {len(all_items)} items from {len(sources)} sites.")
    try:
        site.build(all_items, sources, load_config(), stamp)
    except Exception as e:  # the listings still save even if page-building has a problem
        print(f"Could not build place pages: {e}")


if __name__ == "__main__":
    asyncio.run(main())
