"""Read specials and events out of pictures (flyers, specials boards) with Tesseract OCR.

Turned on per site in sources.yml with `read_images`. The page's pictures are downloaded,
their text is read, and the text is split into sections:

    LIVE MUSIC                       ->  heading
    6PM TO 9PM                       ->  when (days / times)
    Wednesday, 11/4 - Tim Morrison   ->  dated line: one event per line

    HAPPY HOUR SPECIALS              ->  heading
    MONDAY - FRIDAY / 3-6 PM         ->  when
    $2 OFF ALL DRAFT BEERS           ->  details
"""
import io
import re
import shutil
import statistics
import subprocess

from .extract import DAYS_RE, EVENT_RE, SPECIAL_RE, TIMES_RE, _parse_date, _with_time

SKIP_IMG_RE = re.compile(r"logo|icon|monogram|favicon|avatar|placeholder|badge|sprite|\.svg|\.gif", re.I)
MAX_IMAGES = 8


def available():
    return shutil.which("tesseract") is not None


# ---------- picture -> lines of text ----------

def _prep(data):
    from PIL import Image, ImageOps, ImageStat
    im = Image.open(io.BytesIO(data))
    if im.width < 400 or im.height < 300:  # thumbnails and buttons
        return None
    im = im.convert("L")
    if ImageStat.Stat(im).mean[0] < 110:  # light text on a dark picture
        im = ImageOps.invert(im)
    if im.width < 1600:
        im = im.resize((im.width * 2, im.height * 2))
    return im.point(lambda v: 255 if v > 150 else 0)


def _words(im):
    buf = io.BytesIO()
    im.save(buf, "PNG")
    out = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "3", "tsv"], input=buf.getvalue(),
                         capture_output=True, timeout=120).stdout.decode("utf-8", "ignore")
    rows = []
    for ln in out.splitlines()[1:]:
        p = ln.split("\t")
        if len(p) < 12 or not p[11].strip():
            continue
        conf, t = float(p[10]), p[11]
        # Dates are often misread with letters: "11/T1" -> "11/11".
        if "/" in t and re.fullmatch(r"[\dTlIiO|/,.]+", t) and re.search(r"\d", t):
            t, conf = t.translate(str.maketrans("TlIi|O", "111110")), max(conf, 60)
        if conf < 0:
            continue
        rows.append({"key": (int(p[2]), int(p[3]), int(p[4])), "x": int(p[6]), "y": int(p[7]),
                     "w": int(p[8]), "h": int(p[9]), "conf": conf, "text": t})
    return rows


def _real(seg):
    """A run of words that is text, not noise read from a photo."""
    real = [w for w in seg if re.search(r"[A-Za-z]{3,}|\d", w["text"]) and w["conf"] >= 60]
    return real and statistics.mean(w["conf"] for w in seg) >= 55 and sum(len(w["text"]) for w in real) >= 3


def image_lines(data):
    """Lines of text in reading order. Side-by-side columns are read one column at a time,
    with a blank line between them."""
    im = _prep(data)
    if im is None:
        return []
    W = im.width
    groups = {}
    for w in _words(im):
        groups.setdefault(w["key"], []).append(w)
    rows = []
    for ws in groups.values():
        ws.sort(key=lambda w: w["x"])
        h = statistics.median(w["h"] for w in ws)
        segs, cur = [], [ws[0]]
        for a, b in zip(ws, ws[1:]):  # a wide gap splits a line into separate columns
            if b["x"] - (a["x"] + a["w"]) > max(2.5 * h, 0.04 * W):
                segs.append(cur)
                cur = [b]
            else:
                cur.append(b)
        segs.append(cur)
        segs = [s for s in segs if _real(s)]
        if segs:
            rows.append({"y": min(w["y"] for w in ws),
                         "segs": [{"x0": s[0]["x"], "x1": s[-1]["x"] + s[-1]["w"],
                                   "text": " ".join(w["text"] for w in s)} for s in segs]})
    rows.sort(key=lambda r: r["y"])
    out, i, mid = [], 0, W / 2
    while i < len(rows):
        if len(rows[i]["segs"]) == 1:
            out.append(rows[i]["segs"][0]["text"])
            i += 1
            continue
        left, right = [], []
        while i < len(rows):  # columns continue while lines stay on one side of the middle
            segs = rows[i]["segs"]
            if len(segs) == 1 and segs[0]["x0"] < mid < segs[0]["x1"]:
                break
            for s in segs:
                (left if (s["x0"] + s["x1"]) / 2 < mid else right).append(s["text"])
            i += 1
        out += [""] + left + [""] + right + [""]
    return out


# ---------- lines of text -> items ----------

DATED_RE = re.compile(r"\b(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s*[-–—:|]\s*(.{2,80})$")


def _tidy(line):
    line = re.sub(r"\s+", " ", line).strip(" |_~—-")
    toks = line.split()
    # Drop specks read from a photo in front of a caps line: "Be Ky LIVE MUSIC" -> "LIVE MUSIC".
    while len(toks) > 1 and len(toks[0]) <= 3 and re.fullmatch(r"[A-Za-z'.,]+", toks[0]) and not toks[0].isupper() and any(t.isupper() and len(t) > 2 for t in toks[1:]):
        toks.pop(0)
    return " ".join(toks)


def _is_when(line):
    return bool(DAYS_RE.search(line) or TIMES_RE.search(line)) and len(line.split()) <= 7 and not re.search(r"[$%]", line)


def _heading_like(line):
    letters = re.sub(r"[^A-Za-z]", "", line)
    return (letters.isupper() and len(letters) >= 3 and len(line.split()) <= 5
            and not re.search(r"[\d$%*@(/]", line) and not _is_when(line))


def _sentence(line):
    if re.sub(r"[^A-Za-z]", "", line).isupper():
        line = line.lower()
        if line[:1].isalpha():
            line = line[0].upper() + line[1:]
    return line


def _title(line):
    if re.sub(r"[^A-Za-z]", "", line).isupper():
        line = re.sub(r"[A-Za-z]+('[A-Za-z]+)?", lambda m: m.group(0).capitalize(), line.lower())
    return line


def _when_text(lines):
    s = " ".join(lines).lower()
    return re.sub(r"\b(mon|tues|wednes|thurs|fri|satur|sun)(day)", lambda m: m.group(0).capitalize(), s)


def _details(lines):
    out, prev = "", ""
    for raw in lines:
        ln = _sentence(raw.lstrip("*").strip())
        if not out:
            out = ln
            prev = ln
            continue
        elif re.match(r"(?i)(or|and|&)\b", ln) or out.endswith(",") or len(prev.split()) <= 2:
            out += " " + ln[0].lower() + ln[1:]
        else:
            out += ("" if out.endswith((".", "!", "?")) else ".") + " " + ln
        prev = ln
    return out + ("" if not out or out.endswith((".", "!", "?")) else ".")


def items_from_lines(lines, page_url, image_url, venue_name=""):
    venue_words = set(re.findall(r"[a-z]+", venue_name.lower()))
    lines = [_tidy(l) for l in lines]
    # The venue's own name printed big on the flyer isn't a heading.
    lines = [l for l in lines if not (l and _heading_like(l) and set(re.findall(r"[a-z]+", l.lower())) <= venue_words)]

    def starts_section(i):
        if not _heading_like(lines[i]):
            return False
        for nxt in lines[i + 1:i + 4]:  # a heading is followed (soon) by days/times or dates
            if not nxt:
                return False
            if _is_when(nxt) or DATED_RE.search(nxt):
                return True
        return False

    sections, cur = [], None
    for i, ln in enumerate(lines):
        if not ln:
            cur = None if cur is None or cur["body"] or cur["when"] else cur
            continue
        if starts_section(i):
            if cur and not cur["body"] and not cur["when"] and not cur["dated"]:
                cur["heading"].append(ln)  # "HALF PRICE" / "WINE BOTTLES"
            else:
                cur = {"heading": [ln], "when": [], "body": [], "dated": []}
                sections.append(cur)
            continue
        if cur is None:
            continue
        m = DATED_RE.search(ln)
        if m:
            cur["dated"].append((m.group(1), m.group(2).strip(" .")))
        elif _is_when(ln) and (not cur["body"] or not cur["when"]):
            cur["when"].append(ln)
        else:
            cur["body"].append(ln)

    items = []
    for s in sections:
        title = _title(" ".join(s["heading"]))
        when = _when_text(s["when"])
        text = " ".join(s["heading"] + s["body"])
        base = {"end": None, "url": page_url, "image": image_url, "price": None, "venue": None,
                "address": None, "lat": None, "lng": None}
        if s["dated"]:
            t = TIMES_RE.search(when)
            for d, who in s["dated"]:
                day = _parse_date(d)
                if not day:
                    continue
                items.append({**base, "kind": "event" if not SPECIAL_RE.search(title) or EVENT_RE.search(title) else "special",
                              "title": f"{title}: {_title(who)}",
                              "description": (f"{title} with {_title(who)}" + (f", {t.group(0).lower()}." if t else ".")),
                              "start": _with_time(day, t.group(0) if t else None), "recurring": None})
        elif when:
            kind = "special" if SPECIAL_RE.search(text) or not EVENT_RE.search(text) else "event"
            items.append({**base, "kind": kind, "title": title, "description": _details(s["body"]),
                          "start": None, "recurring": when})
    return items


# ---------- pictures on a page ----------

def page_images(soup, base_url, pattern=None):
    from urllib.parse import urljoin
    seen, out = set(), []
    for img in soup.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        if not src or src.startswith("data:"):
            continue
        url = urljoin(base_url, src)
        label = f"{url} {img.get('alt') or ''} {img.get('title') or ''}"
        if url in seen or SKIP_IMG_RE.search(label):
            continue
        if pattern and not re.search(pattern, label, re.I):
            continue
        seen.add(url)
        out.append(url)
    return out[:MAX_IMAGES]
