"""Address lookup through OpenStreetMap's Nominatim, cached in a JSON file."""
import asyncio
import json
import os
import re
import time

import httpx

GEOCODER_URL = os.environ.get("GEOCODER_URL", "https://nominatim.openstreetmap.org/search")
USER_AGENT = os.environ.get("USER_AGENT", "LocalHappenings/1.0 (GitHub Actions)")
COUNTRY_CODES = os.environ.get("GEOCODE_COUNTRIES", "us")


class Geocoder:
    def __init__(self, cache_path):
        self.path = cache_path
        self._last = 0.0
        try:
            with open(cache_path) as f:
                self.cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.cache = {}

    async def lookup(self, query):
        """Find coordinates, falling back to less exact versions of the address.

        OpenStreetMap often lacks house numbers on long roads, so
        "2021 Liberty Road, Sykesville, MD 21784" falls back to
        "Liberty Road, Sykesville, MD 21784", then "Sykesville, MD 21784".
        """
        q = (query or "").strip()
        tries = [q]
        no_number = re.sub(r"^\s*\d+[A-Za-z]?\s+", "", q)
        if no_number != q:
            tries.append(no_number)
        parts = [x.strip() for x in q.split(",")]
        if len(parts) >= 3:
            tries.append(", ".join(parts[1:]))
        for t in tries:
            hit = await self._lookup_one(t)
            if hit:
                return hit
        return None

    async def _lookup_one(self, query):
        q = (query or "").strip()
        if not q:
            return None
        key = q.lower()
        if key in self.cache:
            return self.cache[key]
        # Nominatim usage policy: at most one request per second.
        wait = 1.1 - (time.time() - self._last)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            params = {"q": q, "format": "json", "limit": 1}
            if COUNTRY_CODES:
                params["countrycodes"] = COUNTRY_CODES
            async with httpx.AsyncClient(timeout=15, headers={"User-Agent": USER_AGENT}) as client:
                r = await client.get(GEOCODER_URL, params=params)
                r.raise_for_status()
                data = r.json()
        except (httpx.HTTPError, ValueError):
            return None  # don't cache failures; try again next run
        finally:
            self._last = time.time()
        result = {"lat": float(data[0]["lat"]), "lng": float(data[0]["lon"])} if data else None
        self.cache[key] = result
        return result

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self.cache, f, indent=1, sort_keys=True)
