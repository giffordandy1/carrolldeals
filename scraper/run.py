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

from .extract import _clean, fetch_source
from .geo import Geocoder

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = os.path.join(ROOT, "sources.yml")
OUT_DIR = os.path.join(ROOT, "docs", "data")
ITEMS = os.path.join(OUT_DIR, "items.json")
STATUS = os.path.join(OUT_DIR, "status.json")
GEOCACHE = os.path.join(ROOT, "scraper", "geocache.json")


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
            "address": s.get("address"),
            "lat": s.get("lat"),
            "lng": s.get("lng"),
            "item_selector": sel.get("item"),
            "title_selector": sel.get("title"),
            "date_selector": sel.get("date"),
        })
    return out


def load_previous():
    try:
        with open(ITEMS) as f:
            return json.load(f).get("items", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


async def main():
    sources = load_sources()
    previous = load_previous()
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
            items.append({
                "kind": "event" if str(m.get("kind", "")).lower().startswith("event") else "special",
                "title": str(m["title"]), "description": str(m.get("details") or ""),
                "start": when if dated else None, "end": None, "recurring": None if dated else (when or None),
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

        kept, seen = [], set()
        for it in items:
            # Plain text only, whatever the source (including results kept from a previous run).
            for k, limit in (("title", 160), ("description", 500), ("venue", 120), ("address", 200), ("price", 40), ("recurring", 80)):
                if it.get(k):
                    it[k] = _clean(it[k], limit) or None
            if not it.get("title"):
                continue
            start, end = (it.get("start") or "")[:10], (it.get("end") or "")[:10]
            if start and (end or start) < today:
                continue  # already over
            key = hashlib.sha1(f"{src['name']}|{it['kind']}|{it['title'].lower()}|{it.get('start') or ''}".encode()).hexdigest()[:12]
            if key in seen:
                continue
            seen.add(key)
            it["id"] = key
            kept.append(it)
        all_items += kept

        report.append({"site": src["name"], "status": status, "found": len(kept), "errors": errors,
                       "mapped": src["lat"] is not None})
        flag = "" if src["lat"] is not None else "  (no map location: add an address or lat/lng)"
        print(f"{src['name']}: {len(kept)} items, {status}{flag}")
        for e in errors:
            print(f"   - {e}")

    geo.save()
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(ITEMS, "w") as f:
        json.dump({"updated": stamp, "items": all_items}, f, indent=1, ensure_ascii=False)
    with open(STATUS, "w") as f:
        json.dump({"updated": stamp, "sites": report}, f, indent=1, ensure_ascii=False)
    print(f"\nSaved {len(all_items)} items from {len(sources)} sites.")


if __name__ == "__main__":
    asyncio.run(main())
