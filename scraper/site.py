"""Builds the search-engine-friendly parts of the site from the latest results.

Every update writes:
  docs/places/<place>/index.html   a plain page per place: its specials and events,
                                   with schema.org Event markup Google can show in search
  docs/index.html (marked parts)   the coming week's listings as plain text, links to every
                                   place page, the canonical address and site markup
  docs/sitemap.xml, docs/robots.txt

Settings come from the top of sources.yml:
  site_url: https://example.com/            the site's public address (ends with /)
  google_site_verification: abc123          the code from Google Search Console (optional)
"""
import html
import json
import os
import re
import shutil
import unicodedata
from datetime import date, datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
PLACES = os.path.join(DOCS, "places")
TZ = os.environ.get("TIMEZONE", "America/New_York")
SITE_NAME = "Carroll Deals"

esc = html.escape


def slugify(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"['’]", "", s.lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "place"


def _today():
    try:
        return datetime.now(ZoneInfo(TZ)).date()
    except Exception:
        return date.today()


def _dt(s):
    try:
        return datetime.strptime(s[:16], "%Y-%m-%dT%H:%M") if len(s) > 10 else datetime.strptime(s[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _iso_with_offset(s):
    """Local "2026-11-14T20:00" -> "2026-11-14T20:00:00-05:00" (Google prefers a time zone)."""
    d = _dt(s or "")
    if d is None:
        return None
    if len(s) <= 10:
        return d.date().isoformat()
    try:
        return d.replace(tzinfo=ZoneInfo(TZ)).isoformat()
    except Exception:
        return d.isoformat()


def _time_text(d):
    return d.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")


def when_text(it):
    """Readable date or schedule: "Sat, Nov 14, 8 PM", "Nov 14 to Nov 20", "Thursdays 7pm"."""
    start = it.get("start") or ""
    d = _dt(start)
    if d is not None:
        end = _dt(it.get("end") or "")
        try:
            from zoneinfo import ZoneInfo
            today = datetime.now(ZoneInfo("America/New_York")).date()
        except Exception:
            today = datetime.now().date()
        if end is not None and d.date() < today <= end.date():
            return "Now through " + end.strftime("%a, %b ") + str(end.day)
        txt = d.strftime("%a, %b ") + str(d.day)
        if len(start) > 10:
            txt += ", " + _time_text(d)
        end = _dt(it.get("end") or "")
        if end is not None and end.date() != d.date():
            txt += " to " + end.strftime("%a, %b ") + str(end.day)
        return txt
    return (it.get("recurring") or "Ongoing").strip()


def _upcoming(items, today, days):
    horizon = (today + timedelta(days=days)).isoformat()
    t = today.isoformat()
    out = []
    for it in items:
        s, e = (it.get("start") or "")[:10], (it.get("end") or "")[:10]
        if s and ((e or s) < t or s > horizon):
            continue
        out.append(it)
    out.sort(key=lambda i: (0 if i.get("start") else 1, i.get("start") or "", i["title"].lower()))
    return out


def _stub(it):
    d = _dt(it.get("start") or "")
    if d is not None:
        tm = f"<br>{esc(_time_text(d))}" if len(it["start"]) > 10 else ""
        return (f'<span class="small">{d.strftime("%a")}</span><span class="big">{d.day}</span>'
                f'<span class="small">{d.strftime("%b")}{tm}</span>')
    label = it.get("recurring") or ("Ongoing" if it.get("kind") == "special" else "See site")
    return f'<span class="small">{esc(label)}</span>'


def _card(it, place_href=None):
    kind = "special" if it.get("kind") == "special" else "event"
    place = it.get("source") or it.get("venue") or ""
    place_html = f'<a href="{esc(place_href)}">{esc(place)}</a>' if place_href else esc(place)
    desc = f'<p class="desc">{esc(it["description"])}</p>' if it.get("description") else ""
    link = ""
    if (it.get("url") or "").startswith("http"):
        host = re.sub(r"^www\.", "", re.sub(r"^https?://([^/]+).*$", r"\1", it["url"]))
        link = f'<div class="links"><a href="{esc(it["url"])}" rel="noopener">Open {esc(host)}</a></div>'
    return (f'<article class="card {kind}"><div class="stub">{_stub(it)}</div><div class="body">'
            f'<h2>{esc(it["title"])}</h2><p class="meta"><span class="tag">{kind.capitalize()}</span>{place_html}</p>'
            f'{desc}{link}</div></article>')


def event_jsonld(it, src):
    """schema.org Event, for events with a date (what Google's event results need)."""
    start = _iso_with_offset(it.get("start"))
    if not start:
        return None
    ev = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": it["title"],
        "startDate": start,
        "eventStatus": "https://schema.org/EventScheduled",
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "location": {
            "@type": "Place",
            "name": it.get("venue") or src["name"],
            "address": it.get("address") or src.get("address") or "",
        },
        "organizer": {"@type": "Organization", "name": src["name"]},
    }
    if it.get("end"):
        ev["endDate"] = _iso_with_offset(it["end"])
    if it.get("description"):
        ev["description"] = it["description"]
    if (it.get("url") or "").startswith("http"):
        ev["url"] = it["url"]
    if (it.get("image") or "").startswith("http"):
        ev["image"] = [it["image"]]
    if it.get("lat") is not None:
        ev["location"]["geo"] = {"@type": "GeoCoordinates", "latitude": it["lat"], "longitude": it["lng"]}
    return ev


def _jsonld_script(obj):
    return '<script type="application/ld+json">' + json.dumps(obj, ensure_ascii=False).replace("</", "<\\/") + "</script>"


def _town(address):
    m = re.search(r",\s*([A-Za-z .]+),\s*MD", address or "")
    return m.group(1).strip() if m else ""


PLACE_CSS = """:root{--ink:#1E2A3A;--muted:#566673;--paper:#EEF2F0;--surface:#fff;--line:#CDD6D3;--event:#3550C8;--event-soft:#E4E9FB;--special:#F2B705;--special-ink:#4A3600;--special-soft:#FDF3D1;
--display:"Bricolage Grotesque",system-ui,sans-serif;--body:"Atkinson Hyperlegible",system-ui,sans-serif}
@media (prefers-color-scheme:dark){:root{--ink:#E6ECEA;--muted:#9AA9A4;--paper:#141B22;--surface:#1C252E;--line:#2E3A44;--event:#8FA3FF;--event-soft:#252F52;--special:#F5C842;--special-ink:#F5C842;--special-soft:#3A3115}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 var(--body)}
a{color:var(--event)}:focus-visible{outline:3px solid var(--event);outline-offset:2px}
.wrap{max-width:760px;margin:0 auto;padding:20px 16px 48px}
.top{display:flex;align-items:center;gap:10px;font:800 22px/1 var(--display);letter-spacing:-.03em;text-decoration:none;color:var(--ink)}
.top img{width:34px;height:34px}
.crumbs{font-size:14px;color:var(--muted);margin:14px 0 6px}
h1{font:800 clamp(30px,6vw,44px)/1.05 var(--display);letter-spacing:-.03em;margin:0 0 8px}
.addr{color:var(--muted);margin:0 0 6px}
.actions{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0 22px;font-weight:700}
h2.sec{font:700 22px/1.2 var(--display);margin:28px 0 10px}
.list{display:grid;gap:12px}
.card{display:grid;grid-template-columns:88px 1fr;background:var(--surface);border:1px solid var(--line);border-radius:6px}
.stub{padding:12px 8px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:2px;border-right:2px dashed var(--line);font-family:var(--display)}
.event .stub{background:var(--event-soft);color:var(--event)}.special .stub{background:var(--special-soft);color:var(--special-ink);border-right-style:solid}
.stub .big{font-size:26px;font-weight:800;line-height:1}.stub .small{font-size:13px;font-weight:700;line-height:1.2}
.body{padding:12px 14px;min-width:0}.body h2{font:700 17px/1.25 var(--display);margin:0 0 3px}
.meta{font-size:14px;color:var(--muted);margin:0 0 6px}.desc{font-size:14px;margin:0}.links{margin-top:6px;font-size:14px;font-weight:700}
.tag{display:inline-block;font-size:12px;font-weight:700;padding:1px 7px;border-radius:4px;margin-right:6px}
.event .tag{background:var(--event-soft);color:var(--event)}.special .tag{background:var(--special-soft);color:var(--special-ink)}
.empty{color:var(--muted)}
.others{columns:2 220px;padding:0;list-style:none;margin:0}.others li{margin:0 0 6px}
footer{margin-top:36px;font-size:14px;color:var(--muted)}
"""


def _place_page(src, items, all_sources, site_url, today, updated):
    slug = slugify(src["name"])
    url = f"{site_url}places/{slug}/"
    town = _town(src.get("address"))
    up = _upcoming(items, today, 60)
    weekly = [i for i in up if not i.get("start")]
    dated = [i for i in up if i.get("start")]
    kinds = []
    text = " ".join(i["title"].lower() for i in up)
    for word, label in (("happy hour", "happy hour"), ("trivia", "trivia"), ("music", "live music"), ("bingo", "bingo"),
                        ("karaoke", "karaoke"), ("brunch", "brunch"), ("wing", "wing deals"), ("pizza", "pizza deals")):
        if word in text and label not in kinds:
            kinds.append(label)
    what = ", ".join(kinds[:4]) if kinds else "specials and events"
    desc = (f"{src['name']}{' in ' + town if town else ''}: {what}, with days and times. "
            f"{len(weekly)} weekly specials and {len(dated)} upcoming events, updated every few hours.")
    title = f"{src['name']} specials & events{' | ' + town + ', MD' if town else ''} | {SITE_NAME}"
    jsonld = [e for e in (event_jsonld(i, src) for i in dated if i.get("kind") == "event") if e][:60]
    crumbs = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": SITE_NAME, "item": site_url},
        {"@type": "ListItem", "position": 2, "name": src["name"], "item": url}]}
    first_url = next((u for u in src.get("urls") or [] if u.startswith("http") and "ical" not in u.lower()), None)
    others = sorted((s for s in all_sources if s["name"] != src["name"] and s.get("has_items")), key=lambda s: s["name"].lower())
    near_town = [s for s in others if town and _town(s.get("address")) == town]
    other_list = (near_town or others)[:24]
    sections = []
    if weekly:
        sections.append('<h2 class="sec">Weekly specials and regular events</h2><div class="list">' +
                        "".join(_card(i) for i in weekly) + "</div>")
    if dated:
        sections.append('<h2 class="sec">Coming up</h2><div class="list">' + "".join(_card(i) for i in dated) + "</div>")
    if not sections:
        sections.append('<p class="empty">Nothing listed right now. Check back soon: this page updates every few hours.</p>')
    return url, f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{esc(url)}">
<meta name="theme-color" content="#1E2A3A">
<link rel="icon" href="../../icon.svg" type="image/svg+xml">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{SITE_NAME}">
<meta property="og:title" content="{esc(src['name'])} specials &amp; events">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{esc(url)}">
<meta property="og:image" content="{esc(site_url)}share-card.png">
<meta name="twitter:card" content="summary_large_image">
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&family=Bricolage+Grotesque:opsz,wght@12..96,700;12..96,800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="../place.css">
{_jsonld_script(crumbs)}
{"".join(_jsonld_script(e) for e in jsonld)}
</head>
<body>
<div class="wrap">
  <a class="top" href="../../"><img src="../../logo.svg" alt="" width="34" height="34">{SITE_NAME}</a>
  <nav class="crumbs" aria-label="Breadcrumb"><a href="../../">All places</a> / {esc(src['name'])}</nav>
  <h1>{esc(src['name'])}</h1>
  <p class="addr">{esc(src.get('address') or '')}</p>
  <div class="actions">
    <a href="../../#place={esc(src['name'])}">See it on the map</a>
    {f'<a href="{esc(first_url)}" rel="noopener">Their website</a>' if first_url else ''}
  </div>
  {''.join(sections)}
  <h2 class="sec">{'More in ' + esc(town) if near_town else 'More places'}</h2>
  <ul class="others">{''.join(f'<li><a href="../{slugify(s["name"])}/">{esc(s["name"])}</a></li>' for s in other_list)}</ul>
  <footer>Listings come from {esc(src['name'])}'s own website and are checked every few hours. Last updated {esc(updated)}. Deals can change: check with the business before you go.</footer>
</div>
</body>
</html>
"""


def _replace_marked(text, name, content):
    pat = re.compile(rf"(<!--SEO:{name}-->)(.*?)(<!--/SEO:{name}-->)", re.S)
    if not pat.search(text):
        return text
    return pat.sub(lambda m: m.group(1) + content + m.group(3), text)


def build(items, sources, config, updated_iso):
    site_url = (config.get("site_url") or "").strip()
    if not site_url:
        print("site_url is not set in sources.yml; skipping place pages and sitemap.")
        return
    if not site_url.endswith("/"):
        site_url += "/"
    today = _today()
    try:
        updated = datetime.fromisoformat(updated_iso).astimezone(ZoneInfo(TZ)).strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")
    except Exception:
        updated = updated_iso

    by_source = {}
    for it in items:
        by_source.setdefault(it.get("source"), []).append(it)
    for s in sources:
        s["has_items"] = bool(by_source.get(s["name"]))

    # Place pages (rebuilt each run; pages for removed places are deleted)
    os.makedirs(PLACES, exist_ok=True)
    with open(os.path.join(PLACES, "place.css"), "w") as f:
        f.write(PLACE_CSS)
    keep, urls = {"place.css"}, []
    for s in sources:
        if not s["has_items"]:
            continue
        url, page = _place_page(s, by_source[s["name"]], sources, site_url, today, updated)
        slug = slugify(s["name"])
        os.makedirs(os.path.join(PLACES, slug), exist_ok=True)
        with open(os.path.join(PLACES, slug, "index.html"), "w") as f:
            f.write(page)
        keep.add(slug)
        urls.append(url)
    for entry in os.listdir(PLACES):
        if entry not in keep:
            p = os.path.join(PLACES, entry)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)

    # Homepage: plain-text listings for the coming week, links to every place, head tags
    index_path = os.path.join(DOCS, "index.html")
    with open(index_path) as f:
        index = f.read()
    week = _upcoming(items, today, 7)
    week = [i for i in week if i.get("start")][:60] + [i for i in week if not i.get("start")][:80]
    slug_of = {s["name"]: slugify(s["name"]) for s in sources if s["has_items"]}
    static_list = "".join(_card(i, f"places/{slug_of[i['source']]}/" if i.get("source") in slug_of else None) for i in week)
    places_nav = ('<nav class="allplaces" aria-labelledby="allplaces-h"><h2 id="allplaces-h">All places</h2><ul>' +
                  "".join(f'<li><a href="places/{slug_of[n]}/">{esc(n)}</a></li>' for n in sorted(slug_of, key=str.lower)) +
                  "</ul></nav>")
    site_ld = {"@context": "https://schema.org", "@type": "WebSite", "name": SITE_NAME, "url": site_url,
               "description": "Happy hours, weekly specials, live music and events at restaurants, bars and breweries around Carroll County, MD."}
    head = (f'\n<link rel="canonical" href="{esc(site_url)}">\n'
            f'<meta property="og:url" content="{esc(site_url)}">\n'
            f'<meta property="og:image" content="{esc(site_url)}share-card.png">\n'
            + (f'<meta name="google-site-verification" content="{esc(config["google_site_verification"])}">\n'
               if config.get("google_site_verification") else "")
            + _jsonld_script(site_ld) + "\n")
    index = _replace_marked(index, "HEAD", head)
    index = _replace_marked(index, "LIST", static_list)
    index = _replace_marked(index, "PLACES", places_nav)
    with open(index_path, "w") as f:
        f.write(index)

    # Sitemap + robots.txt
    lastmod = today.isoformat()
    sm = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
          f"  <url><loc>{esc(site_url)}</loc><lastmod>{lastmod}</lastmod><changefreq>daily</changefreq></url>"]
    sm += [f"  <url><loc>{esc(u)}</loc><lastmod>{lastmod}</lastmod><changefreq>daily</changefreq></url>" for u in urls]
    sm.append("</urlset>")
    with open(os.path.join(DOCS, "sitemap.xml"), "w") as f:
        f.write("\n".join(sm) + "\n")
    with open(os.path.join(DOCS, "robots.txt"), "w") as f:
        f.write(f"User-agent: *\nAllow: /\n\nSitemap: {site_url}sitemap.xml\n")
    print(f"Built {len(urls)} place pages and the sitemap.")
