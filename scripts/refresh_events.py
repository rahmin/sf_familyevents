#!/usr/bin/env python3
"""Rebuild events.json for the family day-out finder.

Sources, in order of reliability:
  1. SF Public Library event pages (one page per event, linked from branch pages)
  2. California Academy of Sciences daily calendar (one page per day)
  3. SF Children & Nature event API (WordPress "The Events Calendar" REST API)
  4. Funcheap kids & families listings (schema.org Event data embedded in each page)
  5. data/manual.json (anything you add by hand, kept until it ends)

Optional: if ANTHROPIC_API_KEY is set, Funcheap and Children & Nature events are
sent to Claude to drop anything unsuitable for kids under 5 and to estimate how
exposed to wind and how warm each location is. Without a key, Funcheap events are
skipped and everything else gets safe defaults.

Run locally:  python scripts/refresh_events.py
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "events.json"
MANUAL = ROOT / "data" / "manual.json"
GEOCACHE = ROOT / "data" / "geocache.json"

TZ = ZoneInfo("America/Los_Angeles")
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD") or "7")
CONTACT = os.environ.get("CONTACT_EMAIL", "")
UA = (
    "Mozilla/5.0 (compatible; family-day-out/1.0; "
    + (f"+mailto:{CONTACT}" if CONTACT else "personal family events page")
    + ")"
)
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"})

# Bay Area bounding box, used to sanity-check coordinates.
BBOX = (37.3, 38.2, -122.8, -121.8)

SFPL_BRANCHES = {
    # slug: (name as it appears on event pages, lat, lng)
    "main-library": ("Main", 37.7790, -122.4157),
    "anza": ("Anza", 37.7778, -122.4970),
    "bayview": ("Bayview", 37.7334, -122.3906),
    "bernal-heights": ("Bernal Heights", 37.7389, -122.4167),
    "chinatown": ("Chinatown", 37.7955, -122.4105),
    "eureka-valley": ("Eureka Valley", 37.7635, -122.4330),
    "excelsior": ("Excelsior", 37.7255, -122.4343),
    "glen-park": ("Glen Park", 37.7337, -122.4337),
    "golden-gate-valley": ("Golden Gate Valley", 37.7980, -122.4258),
    "ingleside": ("Ingleside", 37.7238, -122.4560),
    "marina": ("Marina", 37.8008, -122.4362),
    "merced": ("Merced", 37.7270, -122.4765),
    "mission": ("Mission", 37.7525, -122.4205),
    "mission-bay": ("Mission Bay", 37.7716, -122.3912),
    "noe-valley": ("Noe Valley", 37.7505, -122.4329),
    "north-beach": ("North Beach", 37.8030, -122.4140),
    "ocean-view": ("Ocean View", 37.7143, -122.4653),
    "ortega": ("Ortega", 37.7518, -122.4977),
    "park": ("Park", 37.7695, -122.4500),
    "parkside": ("Parkside", 37.7430, -122.4760),
    "portola": ("Portola", 37.7270, -122.4065),
    "potrero": ("Potrero", 37.7598, -122.3994),
    "presidio": ("Presidio", 37.7881, -122.4446),
    "richmond": ("Richmond", 37.7814, -122.4675),
    "sunset": ("Sunset", 37.7635, -122.4760),
    "visitacion-valley": ("Visitacion Valley", 37.7119, -122.4049),
    "west-portal": ("West Portal", 37.7404, -122.4660),
    "western-addition": ("Western Addition", 37.7834, -122.4373),
}
KID_AUDIENCE = re.compile(r"Babies, Toddlers or Preschoolers|Families", re.I)

CAS = dict(name_area="Golden Gate Park", lat=37.7699, lng=-122.4661)
CAS_KEEP = {
    "penguin-feeding": ("Penguin feeding at the Academy", "Biologists feed the African penguin colony and talk about the birds. Included with admission.", 30),
    "coral-reef-dive": ("Coral reef dive at the Academy", "Watch a diver go into the living coral reef tank. Included with admission.", 30),
    "childrens-puppet-theater": ("Children's puppet theater at the Academy", "A kid-friendly puppet show in the East Garden. Included with admission.", 30),
    "sensory-storytime": ("Sensory Storytime at the Academy", "An interactive storytime about animal senses. Included with admission.", 30),
    "lagoon-senses": ("Lagoon Senses (stingrays)", "See how rays find food at the Reef Lagoon. Included with admission.", 30),
}


def log(*a):
    print(*a, file=sys.stderr)


def get(url, **kw):
    try:
        r = S.get(url, timeout=25, **kw)
        if r.status_code != 200:
            log(f"  {r.status_code} {url}")
            return None
        return r
    except requests.RequestException as e:
        log(f"  error {url}: {e}")
        return None


def text_lines(page_html: str) -> list[str]:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", page_html, flags=re.S)
    t = html.unescape(re.sub(r"<[^>]+>", "\n", t))
    return [l.strip() for l in t.split("\n") if l.strip()]


def local_iso(d: date, hhmm: str) -> str:
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).isoformat()


def in_bbox(lat, lng) -> bool:
    try:
        return BBOX[0] < float(lat) < BBOX[1] and BBOX[2] < float(lng) < BBOX[3]
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------- SFPL
def sfpl_time_range(s: str) -> tuple[str, str] | None:
    """SFPL pages show '10:30 - 11:00' or '1:00 - 2:30' without am/pm."""
    m = re.match(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", s)
    if not m:
        return None
    h1, m1, h2, m2 = map(int, m.groups())
    # Library programs run 8am-8pm; hours 1-7 are afternoon.
    if h1 < 8:
        h1 += 12
    if h2 < 8 or h2 < h1 % 12:
        h2 += 12
    if h2 < h1:
        h2 += 12
    return f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}"


SFPL_SLUG_HINT = re.compile(
    r"storytime|story|famil|kid|child|bab|toddler|preschool|performance|puppet|film|play|music|dance|"
    r"craft|art|lego|bubble|dog|animal|halloween|pumpkin|science|stem|magic|sing|garden|celebration|open-house"
)


MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                       "august", "september", "october", "november", "december"], 1)}


def sfpl_closures(year: int) -> set[date]:
    """Dates named in the site-wide alert, e.g. 'All ... Libraries will be CLOSED Monday, October 12'."""
    r = get("https://sfpl.org/")
    out = set()
    if r:
        for mon, day in re.findall(r"CLOSED\s+\w+day,\s+([A-Za-z]+)\s+(\d{1,2})", html.unescape(r.text)):
            if mon.lower() in MONTHS:
                out.add(date(year, MONTHS[mon.lower()], int(day)))
    if out:
        log(f"  library closures: {sorted(out)}")
    return out


def scrape_sfpl(start: date, end: date) -> list[dict]:
    log("SFPL")
    closed = sfpl_closures(start.year)
    links = set()
    # sfpl.org/events is a chronological listing, about 25 events per page.
    for page in range(40):
        r = get(f"https://sfpl.org/events?page={page}")
        if not r:
            break
        found = re.findall(r'href="(/events/(\d{4})/(\d{2})/(\d{2})/([^"#?]+))"', r.text)
        if not found:
            break
        past_end = True
        for path, y, m, d, slug in found:
            dd = date(int(y), int(m), int(d))
            if dd <= end:
                past_end = False
            if start <= dd <= end and dd not in closed and SFPL_SLUG_HINT.search(slug):
                links.add(path)
        if past_end:
            break
        time.sleep(0.3)
    out = []
    for path in sorted(links):
        r = get("https://sfpl.org" + path)
        if not r:
            continue
        lines = text_lines(r.text)
        i = next((k for k, l in enumerate(lines) if re.match(r"(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day, \d{1,2}/\d{1,2}/\d{4}$", l)), None)
        if i is None or i + 1 >= len(lines):
            continue
        title = lines[i - 1] if i else ""
        # The line right above the date is sometimes a subtitle; the H1 is in <title>.
        tm = re.search(r"<title>(.*?)\|", r.text, re.S)
        if tm:
            title = html.unescape(tm.group(1)).strip()
        if title.lower().startswith("canceled") or any(l.lower().startswith("canceled") for l in lines[max(0, i - 3) : i]):
            continue
        window = " ".join(lines[i : i + 14])
        if not KID_AUDIENCE.search(window):
            continue
        if re.search(r"\bTeens\b|Adults", window) and not re.search(r"Babies, Toddlers or Preschoolers", window):
            continue
        tr = sfpl_time_range(lines[i + 1])
        if not tr:
            continue
        mm, dd, yy = map(int, re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", lines[i]).groups())
        d = date(yy, mm, dd)
        branch = None
        for slug, (bname, lat, lng) in SFPL_BRANCHES.items():
            if re.search(rf"\b{re.escape(bname)}\b", window):
                branch = (bname, lat, lng)
                if bname != "Main":
                    break
        if not branch:
            continue
        bname, lat, lng = branch
        area = "Main Library" if bname == "Main" else f"{bname} Branch Library"
        nice = re.sub(r"^Storytime:\s*[Ff]or\s+", "Storytime for ", title)
        nice = re.sub(r"^(Activity|Performance|Workshop|Film|Social|Early Learning|Make \+ Do|Celebration):\s*", "", nice)
        out.append(
            dict(
                id="sfpl-" + path.split("/events/")[1].replace("/", "-"),
                kind="event", source="sfpl", name=nice, area=area, lat=lat, lng=lng,
                indoor=True, exp=0, off=0, free=True,
                start=local_iso(d, tr[0]), end=local_iso(d, tr[1]),
                note="Free library program for young children and their grown-ups.",
                url="https://sfpl.org" + path,
            )
        )
        time.sleep(0.3)
    log(f"  {len(out)} events")
    return out


# ---------------------------------------------------------------- Cal Academy
def scrape_calacademy(start: date, end: date) -> list[dict]:
    log("Cal Academy")
    out = []
    d = start
    while d <= end:
        r = get(f"https://www.calacademy.org/daily-calendar-view/{d.isoformat()}")
        if r:
            t = re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S)
            toks = re.findall(r'href="(https://www\.calacademy\.org/events/programs/[^"]+)"|>([^<]+)<', t)
            last = None
            seen = set()
            sunday = d.weekday() == 6
            for href, txt in toks:
                txt = html.unescape(txt).strip()
                if re.fullmatch(r"\d{1,2}(?::\d\d)? [ap]\.m\.", txt):
                    last = txt
                if not href or href in seen or not last:
                    continue
                seen.add(href)
                key = next((k for k in CAS_KEEP if k in href), None)
                if not key:
                    continue
                hh, mm, ap = re.match(r"(\d{1,2})(?::(\d\d))? ([ap])", last).groups()
                h = int(hh) % 12 + (12 if ap == "p" else 0)
                m = int(mm or 0)
                if sunday and h < 11:  # members-only before 11 on Sundays
                    continue
                name, note, dur = CAS_KEEP[key]
                s = datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)
                out.append(
                    dict(
                        id=f"cas-{key}-{d.isoformat()}-{h:02d}{m:02d}", kind="event", source="calacademy",
                        name=name, area="California Academy of Sciences, Golden Gate Park",
                        lat=CAS["lat"], lng=CAS["lng"], indoor=True, exp=0, off=0, free=False,
                        start=s.isoformat(), end=(s + timedelta(minutes=dur)).isoformat(), note=note, url=href,
                        recurring=True,
                    )
                )
        d += timedelta(days=1)
        time.sleep(0.5)
    log(f"  {len(out)} events")
    return out


# ---------------------------------------------------------------- Mission Science Workshop
# Its site has no event calendar, but its Programs page lists a fixed school-year schedule
# (checked Oct 10, 2026): https://www.missionscienceworkshop.org/programs
MSW_URL = "https://www.missionscienceworkshop.org/programs"
MSW_SITES = {
    "mission": ("Mission Science Workshop, 3750 18th St (Church St between 17th and 18th)", 37.7616, -122.4283),
    "excelsior": ("Mission Science Workshop Excelsior, 4458 Mission St", 37.7253, -122.4344),
    "bayview": ("Mission Science Workshop Bayview, 50 Pomona St (inside Charles Drew Elementary)", 37.7336, -122.3921),
}
MSW_NOTE = ("Free drop-in hands-on science: build and tinker, explore bones, fossils and rocks, and meet the "
            "workshop's animals. Runs during the school year, so check the workshop's site around holidays.")


def in_school_year(d: date) -> bool:
    # Roughly mid-August to early June; the workshop doesn't publish exact break dates.
    return not (date(d.year, 6, 6) <= d <= date(d.year, 8, 14))


def msw_events(start: date, end: date) -> list[dict]:
    log("Mission Science Workshop (published schedule)")
    out = []
    d = start
    while d <= end:
        if in_school_year(d):
            nth = (d.day - 1) // 7 + 1
            slot = None
            if d.weekday() == 5 and nth in (1, 2, 3):
                site = {1: "bayview", 2: "mission", 3: "excelsior"}[nth]
                slot = (site, "10:00", "15:00", f"Mission Science Workshop free Saturday open house", False)
            elif d.weekday() == 1:
                slot = ("excelsior", "15:00", "17:30", "Mission Science Workshop after-school drop-in", True)
            if slot:
                site, a, b, name, recurring = slot
                area, lat, lng = MSW_SITES[site]
                out.append(dict(id=f"msw-{site}-{d.isoformat()}", kind="event", source="Mission Science Workshop",
                                name=name, area=area, lat=lat, lng=lng, indoor=True, exp=0, off=0, free=True,
                                start=local_iso(d, a), end=local_iso(d, b), note=MSW_NOTE, url=MSW_URL,
                                recurring=recurring))
        d += timedelta(days=1)
    log(f"  {len(out)} events")
    return out


# ---------------------------------------------------------------- "The Events Calendar" sites
# Many WordPress sites (SF Children & Nature, Mission Local) use the same calendar plugin,
# which exposes a JSON feed at /wp-json/tribe/events/v1/events.
def scrape_tribe(base: str, source: str, prefix: str, start: date, end: date, **extra) -> list[dict]:
    log(source)
    events, page = [], 1
    while page <= 5:
        r = get(f"{base}/wp-json/tribe/events/v1/events",
                params=dict(start_date=start.isoformat(), end_date=end.isoformat(), per_page=50, page=page, **extra))
        if not r:
            break
        js = r.json()
        events += js.get("events", [])
        if page >= int(js.get("total_pages") or 1):
            break
        page += 1
    out = []
    for e in events:
        v = e.get("venue") or {}
        lat, lng = v.get("geo_lat"), v.get("geo_lng")
        if not in_bbox(lat, lng):
            addr = ", ".join(x for x in [v.get("venue"), v.get("address"), v.get("city")] if x)
            lat, lng = geocode(addr) if addr else (None, None)
        if not in_bbox(lat, lng):
            continue
        s = datetime.fromisoformat(e["start_date"]).replace(tzinfo=TZ)
        en = datetime.fromisoformat(e["end_date"]).replace(tzinfo=TZ)
        desc = re.sub(r"<[^>]+>", " ", html.unescape(e.get("excerpt") or e.get("description") or ""))
        out.append(
            dict(
                id=f"{prefix}-{e['id']}-{s.date().isoformat()}", kind="event", source=source,
                name=(re.sub(r"^[^:]{2,40}:\s+", "", html.unescape(e["title"])) if source == "Mission Local"
                      else html.unescape(e["title"])),
                area=html.unescape(v.get("venue") or "San Francisco"),
                lat=float(lat), lng=float(lng), indoor=False, exp=0.5, off=0,
                free=not (e.get("cost") or "").strip(),
                start=s.isoformat(), end=en.isoformat(),
                note=" ".join(desc.split())[:220], url=e["url"],
            )
        )
    log(f"  {len(out)} events")
    return out


def scrape_children_nature(start, end):
    return scrape_tribe("https://sfchildrennature.org", "SF Children & Nature", "scn", start, end, categories="for_tots")


# Mission Local's neighborhood calendar lists galleries, venues and street events in the Mission,
# most of them for adults, so it is only used with Claude curation, or with a keyword filter.
FAMILY_WORDS = re.compile(r"\b(kid|kids|child|children|family|families|famil|toddler|baby|babies|lantern|parade|"
                          r"storytime|story time|puppet|open house|festival|block party|halloween|d[ií]a de)\b", re.I)


def scrape_mission_local(start, end):
    return scrape_tribe("https://missionlocal.org", "Mission Local", "ml", start, end)


# ---------------------------------------------------------------- Funcheap
def scrape_funcheap(start: date, end: date) -> list[dict]:
    log("Funcheap")
    urls = set()
    for page in ["", "page/2/", "page/3/"]:
        r = get(f"https://sf.funcheap.com/category/event/event-types/kids-families/{page}")
        if not r:
            continue
        urls |= set(re.findall(r'href="(https://sf\.funcheap\.com/[a-z0-9-]+/)"', r.text))
        time.sleep(1)
    skip = re.compile(r"/(category|city-guide|region|events|page|about|weekend|today|free-events|win|subscribe|submit-form)/")
    out = []
    for u in sorted(urls):
        if skip.search(u):
            continue
        r = get(u)
        time.sleep(1)
        if not r:
            continue
        for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', r.text, re.S):
            try:
                ev = json.loads(block)
            except json.JSONDecodeError:
                continue
            if ev.get("@type") != "Event" or not ev.get("startDate"):
                continue
            s = datetime.fromisoformat(ev["startDate"])
            if not (start <= s.date() <= end):
                continue
            en = datetime.fromisoformat(ev["endDate"]) if ev.get("endDate") else None
            loc = ev.get("location") or {}
            addr = loc.get("address") if isinstance(loc.get("address"), str) else ""
            geo = loc.get("geo") or {}
            lat, lng = geo.get("latitude"), geo.get("longitude")
            if not in_bbox(lat, lng):
                lat, lng = geocode(", ".join(x for x in [loc.get("name"), addr] if x))
            if not in_bbox(lat, lng):
                continue
            desc = re.sub(r"Disclaimer:.*", "", html.unescape(ev.get("description") or ""), flags=re.S)
            out.append(
                dict(
                    id="fc-" + u.rstrip("/").rsplit("/", 1)[1][:60] + "-" + s.date().isoformat(),
                    kind="event", source="funcheap", name=html.unescape(ev["name"]),
                    area=html.unescape(loc.get("name") or ""), lat=float(lat), lng=float(lng),
                    indoor=False, exp=0.5, off=0, free=None,
                    start=s.isoformat(), end=en.isoformat() if en else None,
                    note=" ".join(desc.split())[:300], url=u,
                )
            )
            break
    log(f"  {len(out)} events")
    return out


# ---------------------------------------------------------------- geocoding (OpenStreetMap)
_geocache: dict = {}


def geocode(q: str):
    q = (q or "").strip()
    if not q:
        return None, None
    if q in _geocache:
        return tuple(_geocache[q])
    time.sleep(1.1)  # Nominatim usage policy: max 1 request per second
    r = get(
        "https://nominatim.openstreetmap.org/search",
        params=dict(q=q, format="jsonv2", limit=1, countrycodes="us", viewbox="-122.8,38.2,-121.8,37.3", bounded=1),
    )
    lat = lng = None
    if r:
        js = r.json()
        if js:
            lat, lng = float(js[0]["lat"]), float(js[0]["lon"])
    _geocache[q] = [lat, lng]
    return lat, lng


# ---------------------------------------------------------------- Claude curation
CURATE_PROMPT = """You help curate a weekend outings page for parents of children aged 0 to 5 in the San Francisco Bay Area.
For each event below, decide whether a family with a baby, a toddler and a preschooler would reasonably enjoy it,
and estimate the conditions at the location. Reply with only a JSON array, one object per event, in the same order:
{"id": string, "keep": boolean, "indoor": boolean, "exp": number 0-1 (wind exposure: 0 sheltered or indoors, 1 open beach or bayfront),
 "off": integer -8 to 8 (typical warmth versus downtown SF in degrees F: coast negative, East Bay and inland positive),
 "free": boolean or null, "note": one plain sentence (under 25 words) telling parents what it is}
Drop events aimed at adults, teens or older kids only, fundraisers, and bar or nightlife events.
Keep family events that run into the evening (lantern parades, street festivals, outdoor movies) as long as they start before 8 pm; drop anything starting at 8 pm or later.
Events:
"""


def curate(events: list[dict]) -> list[dict]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not events:
        return events
    model = os.environ.get("CLAUDE_MODEL") or "claude-haiku-5-5"
    kept = []
    for i in range(0, len(events), 25):
        batch = events[i : i + 25]
        listing = json.dumps(
            [{k: e.get(k) for k in ("id", "name", "area", "start", "end", "note", "url")} for e in batch],
            ensure_ascii=False,
        )
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": model, "max_tokens": 4000, "messages": [{"role": "user", "content": CURATE_PROMPT + listing}]},
                timeout=120,
            )
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", []))
            verdicts = {v["id"]: v for v in json.loads(re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M))}
        except Exception as e:  # keep the batch unchanged if curation fails
            log(f"  curation failed: {e}")
            kept.extend(batch)
            continue
        for e in batch:
            v = verdicts.get(e["id"])
            if not v or not v.get("keep"):
                continue
            for f in ("indoor", "exp", "off", "free", "note"):
                if v.get(f) is not None:
                    e[f] = v[f]
            kept.append(e)
    log(f"  curation kept {len(kept)} of {len(events)}")
    return kept


# ---------------------------------------------------------------- one-off calendar feed
SPECIALS = ROOT / "specials.ics"


def event_key(e):
    return (" ".join((e.get("name") or "").lower().split()), (e.get("area") or "").lower())


def one_offs(events):
    """Events that happen once: not flagged recurring, not on more than one day, not a regular storytime."""
    days = {}
    for e in events:
        if e.get("kind") == "impact" or not e.get("start"):
            continue
        days.setdefault(event_key(e), set()).add(e["start"][:10])
    out = []
    for e in events:
        if e.get("kind") == "impact" or not e.get("start") or e.get("rangeStart") or e.get("recurring"):
            continue
        if len(days.get(event_key(e), ())) > 1 or re.match(r"(storytime (for|in)\b|(toddler|baby|babies|family|preschool) storytime\b)", (e.get("name") or "").lower()):
            continue
        out.append(e)
    return out


def ics_escape(t: str) -> str:
    return (t or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_fold(line: str) -> str:
    out, b = [], line.encode()
    while len(b) > 74:
        cut = 74
        while (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(b[:cut].decode())
        b = b" " + b[cut:]
    out.append(b.decode())
    return "\r\n".join(out)


def write_specials_ics(events):
    utc = ZoneInfo("UTC")
    stamp = datetime.now(utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//sf_familyevents//one-off family events//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Family events: one day only",
             "X-WR-TIMEZONE:America/Los_Angeles", "REFRESH-INTERVAL;VALUE=DURATION:PT12H"]
    for e in one_offs(events):
        s = datetime.fromisoformat(e["start"])
        en = datetime.fromisoformat(e["end"]) if e.get("end") else s + timedelta(hours=2)
        desc = (e.get("note") or "") + (("\n" + e["url"]) if e.get("url") else "")
        lines += ["BEGIN:VEVENT", f"UID:{e['id']}@sf_familyevents", f"DTSTAMP:{stamp}",
                  f"DTSTART:{s.astimezone(utc):%Y%m%dT%H%M%SZ}", f"DTEND:{en.astimezone(utc):%Y%m%dT%H%M%SZ}",
                  "SUMMARY:" + ics_escape(e.get("name")), "LOCATION:" + ics_escape(e.get("area")),
                  "DESCRIPTION:" + ics_escape(desc)] + ([f"URL:{e['url']}"] if e.get("url") else []) + ["END:VEVENT"]
    lines.append("END:VCALENDAR")
    SPECIALS.write_text("\r\n".join(ics_fold(l) for l in lines) + "\r\n")
    log(f"Wrote {sum(1 for l in lines if l == 'BEGIN:VEVENT')} one-off events to {SPECIALS.name}")


# ---------------------------------------------------------------- main
def main():
    global _geocache
    if GEOCACHE.exists():
        _geocache = json.loads(GEOCACHE.read_text())
    today = datetime.now(TZ).date()
    end = today + timedelta(days=DAYS_AHEAD)

    trusted = scrape_sfpl(today, end) + scrape_calacademy(today, end) + msw_events(today, end)
    loose = scrape_children_nature(today, end)
    ml = scrape_mission_local(today, end)
    fc = scrape_funcheap(today, end)
    if os.environ.get("ANTHROPIC_API_KEY"):
        loose = curate(loose + ml + fc)
    else:
        ml = [e for e in ml if FAMILY_WORDS.search(e["name"]) and e["start"][11:16] < "20:00"]
        log(f"No ANTHROPIC_API_KEY: skipping Funcheap; kept {len(ml)} Mission Local events by keyword")
        loose = loose + ml

    manual = []
    if MANUAL.exists():
        for e in json.loads(MANUAL.read_text()).get("events", []):
            last = e.get("rangeEnd") or (e.get("end") or e.get("start") or "")[:10]
            if last and last >= today.isoformat():
                manual.append(e)

    seen, events = set(), []
    for e in manual + trusted + loose:  # manual entries win on duplicate ids
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        events.append(e)
    events.sort(key=lambda e: (e.get("start") or "", e.get("name") or ""))

    payload = {
        "generated": datetime.now(TZ).isoformat(timespec="minutes"),
        "coverage": f"{today:%b %-d}–{end:%b %-d}",
        "sources": ["SF Public Library", "California Academy of Sciences", "Mission Science Workshop", "SF Children & Nature", "Mission Local", "Funcheap", "manual"],
        "events": events,
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
    write_specials_ics(events)
    GEOCACHE.write_text(json.dumps(_geocache, indent=1, ensure_ascii=False) + "\n")
    log(f"Wrote {len(events)} events to {OUT.name}")


if __name__ == "__main__":
    main()
