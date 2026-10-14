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

import csv
import html
import io
import json
import math
import os
import re
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "events.json"
MANUAL = ROOT / "data" / "manual.json"
GEOCACHE = ROOT / "data" / "geocache.json"

TZ = ZoneInfo("America/Los_Angeles")
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD") or "7")        # regular programs (library, museums)
HORIZON_DAYS = int(os.environ.get("HORIZON_DAYS") or "90")   # one-off and ticketed events
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
    r"storytime|story|famil|kid|child|bab|toddler|preschool|performance|puppet|film|play|music|danc|ballet|folklor|nutcracker|"
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


def sfpl_note(lines: list[str]) -> str:
    """The program's own description (text after the branch contact details), plus a short list of highlights."""
    head = lines[:30]
    k = next((n for n, l in enumerate(head) if l.startswith("Contact Telephone")), -1)
    if k < 0:
        return ""
    while k + 1 < len(lines) and ("@" in lines[k + 1] or re.fullmatch(r"[\d\-\s().]{7,}", lines[k + 1])):
        k += 1
    body = []
    for l in lines[k + 1:k + 60]:
        if re.match(r"(?i)(disability|requesting|to request|notice:|this program will be|accommodation|for more information)", l) or l in ("Celebrations", "Storytime", "Activity", "Performance", "Workshop", "Film", "Social", "Early Learning", "Make + Do", "Author", "Community & Neighborhood",
                                                  "Arts & Culture", "Science & Technology", "Health & Wellness", "Literacy & Learning"):
            break
        if "@" in l or re.fullmatch(r"[\d\-\s().]{7,}", l):
            continue
        body.append(l)
    lead, sched, cur = [], [], None
    in_sched = False
    for l in body:
        if l.startswith("Schedule of Activities"):
            in_sched = True
            continue
        if not in_sched:
            lead.append(l)
            continue
        cur = (cur + " " + l) if cur else l
        if cur.endswith(")") or len(cur) > 80:
            sched.append(cur); cur = None
    if cur:
        sched.append(cur)
    text = " ".join(lead)
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    note = ""
    for sen in sentences:
        if len(note) + len(sen) > 300:
            break
        note = (note + " " + sen).strip()
    if not note and text:
        note = text[:300].rsplit(" ", 1)[0] + "…"
    items = [re.sub(r"\s*\([^)]*\)", "", x).strip(" ,") for x in sched]
    items = [x for x in items if 3 < len(x) < 70][:6]
    if items:
        note += " Highlights: " + "; ".join(items) + "."
    return note.strip()


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
        # Pick the longest branch name that appears, so "Mission Bay" wins over "Mission".
        matches = [(bname, lat, lng) for bname, lat, lng in SFPL_BRANCHES.values()
                   if re.search(rf"\b{re.escape(bname)}\b", window)]
        branch = None
        if matches:
            non_main = [m for m in matches if m[0] != "Main"]
            branch = max(non_main or matches, key=lambda m: len(m[0]))
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
                note=sfpl_note(lines[i:]) or "Free library program for young children and their grown-ups.",
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
    out = enrich_events(out)
    log(f"  {len(out)} events")
    return out


# Weekly or monthly series show up once in a week's newsletter but are regular programs.
SERIES = re.compile(r"(?i)\b(mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?)\b|playgroup|tummy time|"
                    r"first steps|pop n play|crafternoons|activity zone|soundtrack|big feed|docent talks|music magic|"
                    r"creative movement|children's music|^(january|february|march|april|may|june|july|august|september|"
                    r"october|november|december) ")


def page_details(url: str) -> dict:
    """Venue and a short description from an event's own page (schema.org data first, then meta tags)."""
    r = get(url)
    if not r:
        return {}
    t = r.text
    info = {}
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', t, re.S):
        try:
            j = json.loads(block)
        except json.JSONDecodeError:
            continue
        items = j if isinstance(j, list) else (j.get("@graph", [j]) if isinstance(j, dict) else [])
        for it in items:
            if isinstance(it, dict) and "Event" in str(it.get("@type")):
                loc = it.get("location") or {}
                loc = loc[0] if isinstance(loc, list) and loc else loc
                if isinstance(loc, dict):
                    addr = loc.get("address")
                    for _ in range(3):  # JSON-LD allows a list, an object or plain text, nested
                        if isinstance(addr, list):
                            addr = addr[0] if addr else ""
                        elif isinstance(addr, dict):
                            addr = addr.get("streetAddress") or addr.get("name") or ""
                    info.update(venue=html.unescape(loc.get("name") or ""), address=addr if isinstance(addr, str) else "")
                info["desc"] = it.get("description") or ""
                break
    if not info.get("desc"):
        m = (re.search(r'<meta[^>]+(?:property="og:description"|name="description")[^>]+content="([^"]*)"', t)
             or re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:property="og:description"|name="description")', t))
        info["desc"] = m.group(1) if m else ""
    d = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(info["desc"]))).split())
    d = re.sub(r"^\d{1,2}[A-Z][a-z]{2}\d{4}\s*From .*?(?:am|pm)\s*(?:to .*?(?:am|pm))?", "", d, flags=re.I).strip()
    d = re.sub(r"^Eventbrite - .*?presents ", "", d)
    at = re.search(r"\bAt ([A-Z][^.\d]{3,60}?)(\d{2,5} [A-Z][a-z]+ [A-Z][a-z]+)", d)
    if at and not info.get("venue"):
        info.update(venue=at.group(1).strip(), address=at.group(2))
    d = re.split(r"\bAt [A-Z][^.\d]{3,60}?\d{2,5} ", d)[0].strip()
    first = re.split(r"(?<=[.!?])\s+", d)
    note = first[0] if first else ""
    if len(note) < 60 and len(first) > 1:
        note += " " + first[1]
    info["note"] = note[:220].strip()
    return info


def enrich_events(events: list[dict]) -> list[dict]:
    cache, descs = {}, {}
    for e in events:
        if e["url"] not in cache:
            cache[e["url"]] = page_details(e["url"])
            time.sleep(0.4)
        descs.setdefault(cache[e["url"]].get("note", ""), set()).add(e["name"])
    out = []
    for e in events:
        info = cache[e["url"]]
        e["name"] = re.sub(r"^[-–•\s]+", "", e["name"])
        if re.search(r"(?i)to qualify|must score|by referral", info.get("desc", "")):
            continue  # eligibility-only programs
        note = info.get("note", "")
        if note and len(descs.get(note, ())) == 1 and not re.search(r"(?i)houses one of|comprehensive|read more", note):
            e["note"] = note + " (Picked by Local Kiddo SF.)"
        if info.get("venue") and not venue_for(e["url"]):
            addr = (info.get("address") or "").split(",")[0]
            e["area"] = info["venue"] + (", " + addr if addr and addr not in info["venue"] else "")
            lat, lng = geocode(addr + ", San Francisco") if addr else (None, None)
            if in_bbox(lat, lng):
                e["lat"], e["lng"] = lat, lng
        if SERIES.search(e["name"]):
            e["recurring"] = True
        out.append(e)
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


# ---------------------------------------------------------------- MAPP (Mission Arts & Performance Project)
# Bimonthly: the first Saturday of every even month, evenings, at pop-up venues around the Mission.
# Its venues post schedules on their own sites and social media, and the big calendars rarely list it.
MAPP_URL = "https://www.redpoppyarthouse.org/tag/mission-arts-performance-project/"


def mapp_events(start: date, end: date) -> list[dict]:
    out = []
    d = start
    while d <= end:
        if d.month % 2 == 0 and d.weekday() == 5 and d.day <= 7:
            out.append(dict(id=f"mapp-{d.isoformat()}", kind="event", source="MAPP",
                            name="MAPP: Mission Arts & Performance Project",
                            area="Pop-up venues around the Mission near 24th St",
                            lat=37.7544, lng=-122.4146, indoor=False, exp=0.25, off=1, free=True,
                            start=local_iso(d, "18:00"), end=local_iso(d, "22:00"),
                            note=("Free art, music, dance and poetry in garages, gardens, galleries and cafes around the Mission. "
                                  "Early sets are the most kid-friendly; check Red Poppy Art House for the night's schedule."),
                            url=MAPP_URL))
        d += timedelta(days=1)
    if out:
        log(f"MAPP: {len(out)} night(s)")
    return out


# ---------------------------------------------------------------- "The Events Calendar" sites
# Many WordPress sites (SF Children & Nature, Mission Local) use the same calendar plugin,
# which exposes a JSON feed at /wp-json/tribe/events/v1/events.
def scrape_tribe(base: str, source: str, prefix: str, start: date, end: date, default_geo=None, **extra) -> list[dict]:
    log(source)
    events, page = [], 1
    while page <= 15:
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
        if not in_bbox(lat, lng) and default_geo:
            lat, lng = default_geo
        if not in_bbox(lat, lng):
            addr = ", ".join(x for x in [v.get("venue"), v.get("address"), v.get("city")] if x)
            lat, lng = geocode(addr) if addr else (None, None)
        if not in_bbox(lat, lng):
            continue
        s = datetime.fromisoformat(e["start_date"]).replace(tzinfo=TZ)
        en = datetime.fromisoformat(e["end_date"]).replace(tzinfo=TZ)
        desc = re.sub(r"<[^>]+>", " ", html.unescape(e.get("excerpt") or e.get("description") or ""))
        full = re.sub(r"<[^>]+>", " ", html.unescape(e.get("description") or ""))
        out.append(
            dict(
                id=f"{prefix}-{e['id']}-{s.date().isoformat()}", kind="event", source=source,
                name=(re.sub(r"^[^:]{2,40}:\s+", "", html.unescape(e["title"])) if source == "Mission Local"
                      else html.unescape(e["title"])),
                area=html.unescape(v.get("venue") or "San Francisco"),
                lat=float(lat), lng=float(lng), indoor=False, exp=0.5, off=0,
                free=bool(re.fullmatch(r"(?i)\s*(free)?\s*", e.get("cost") or "")),
                start=s.isoformat(), end=en.isoformat(),
                note=" ".join(desc.split())[:220], url=e["url"],
                _family=bool(FAMILY_DESC.search(full)),
                _cats=any(c.get("name", "").lower() == "family" for c in (e.get("categories") or [])),
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


# Phrases in a listing's full description that mark it as aimed at families.
FAMILY_DESC = re.compile(r"family[- ]friendly|kids'? activit|children'?s activit|activities for (kids|children|families)|"
                         r"for (the whole )?family|for kids|for children|bring the kids|all-ages block party", re.I)


def scrape_ybg(start, end):
    # Yerba Buena Gardens Festival: free outdoor performances, May through October, all ages.
    ev = scrape_tribe("https://ybgfestival.org", "Yerba Buena Gardens Festival", "ybg", start, end)
    for e in ev:
        e.update(exp=0.35, free=True)
        e.pop("_family", None)
    return ev


def scrape_omca(start, end):
    # Oakland Museum of California: keep events it files under its "Family" category.
    ev = scrape_tribe("https://museumca.org", "Oakland Museum of California", "omca", start, end, default_geo=(37.7987, -122.2640))
    keep = []
    for e in ev:
        if e.pop("_cats", False):
            e.pop("_family", None)
            e.update(off=5, exp=0.3)
            keep.append(e)
    return keep


SFMOMA = ("SFMOMA, 151 Third St", 37.7857, -122.4011)
SFMOMA_FAMILY = re.compile(r"(?i)famil|kid|child|halloween|ween\b|storytime|kite")


def scrape_sfmoma(start: date, end: date) -> list[dict]:
    """SFMOMA's events page lists title, date, time and category; keep family programs."""
    log("SFMOMA")
    r = get("https://www.sfmoma.org/events/")
    if not r:
        return []
    links = re.findall(r'href="(https://www\.sfmoma\.org/event/[^"#?]+)"', r.text)
    text = re.sub(r"(\|\s*)+", "| ", " ".join(html.unescape(re.sub(r"<[^>]+>", " | ",
              re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S))).split()))
    pat = re.compile(r"\| ([^|]{3,120}?) \| (?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day, ([A-Z][a-z]+) (\d{1,2}), (\d{4}) \| ([^|]{2,40}) \| ([^|]{2,40}) (?=\|)")
    out = []
    for title, mon, day, year, when, cat in pat.findall(text):
        if not (SFMOMA_FAMILY.search(title) or re.search(r"(?i)family", cat)):
            continue
        try:
            d = date(int(year), {k[:3]: v for k, v in MONTHS.items()}[mon.lower()[:3]], int(day))
        except (KeyError, ValueError):
            continue
        if not (start <= d <= end):
            continue
        times = re.findall(r"(\d{1,2})(?::(\d\d))?\s*([ap])\.m\.|(Noon)", when)
        def to_hm(t):
            if t[3]:
                return "12:00"
            h = int(t[0]) % 12 + (12 if t[2] == "p" else 0)
            return f"{h:02d}:{int(t[1] or 0):02d}"
        if not times:
            continue
        s_hm = to_hm(times[0]); e_hm = to_hm(times[1]) if len(times) > 1 else None
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        url = next((l for l in links if slug[:20] in l), "https://www.sfmoma.org/events/")
        out.append(dict(id=f"sfmoma-{slug[:50]}-{d.isoformat()}", kind="event", source="SFMOMA", name=title.strip(),
                        area=SFMOMA[0], lat=SFMOMA[1], lng=SFMOMA[2], indoor=True, exp=0, off=0, free=None,
                        start=local_iso(d, s_hm), end=local_iso(d, e_hm) if e_hm else None,
                        note=f"{cat.strip()} at SFMOMA. Check the event page for admission and RSVP details.", url=url))
    log(f"  {len(out)} events")
    return out


def scrape_galeria(start, end):
    # Galería de la Raza (24th St): readings, workshops and openings; family ones kept by the same filter as Mission Local.
    return scrape_tribe("https://www.galeriadelaraza.org", "Galería de la Raza", "gdlr", start, end, default_geo=(37.7527, -122.4108))


# ---------------------------------------------------------------- Local Kiddo SF (weekly newsletter)
# Public issues at localkiddo.com (robots.txt allows crawling). Each pick has a linked name followed by
# "Worth Knowing:" with date and time, place, and cost. We keep those facts, link to the event's own page,
# and credit the issue.
LK_DATE = re.compile(r"(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,\s+([A-Z][a-z]+)\.?\s+(\d{1,2})(?:,\s*(\d{4}))?,?\s+"
                     r"(\d{1,2}(?::\d\d)?\s*[AP]M)(?:\s*[-–]\s*(\d{1,2}(?::\d\d)?\s*[AP]M))?", re.I)


LK_SKIP = re.compile(r"(?i)yoga|lactation|prenatal|postpartum|pregnan|pelvic|job fair|homework|tutorial|"
                     r"support (circle|group)|connection circle|baby ?wearing|bellies|parents? (meetup|night)|class with")


def _hm(t: str) -> str:
    m = re.match(r"(\d{1,2})(?::(\d\d))?\s*([AP])M", t.strip(), re.I)
    h = int(m.group(1)) % 12 + (12 if m.group(3).upper() == "P" else 0)
    if h < 7:  # "3:00AM" in a kids' listing is a typo for 3 pm
        h += 12
    return f"{h:02d}:{int(m.group(2) or 0):02d}"


LANDMARKS = {  # fallbacks when the geocoder can't place a free-text location
    "golden gate park": (37.7694, -122.4862), "fisherman": (37.8080, -122.4177), "yerba buena": (37.7850, -122.4025),
    "dolores park": (37.7596, -122.4269), "crissy field": (37.8040, -122.4640), "presidio": (37.7989, -122.4662),
    "ferry building": (37.7955, -122.3937), "harrison street": (37.7537, -122.4125), "24th st": (37.7527, -122.4108),
    "mission district": (37.7599, -122.4148), "salesforce park": (37.7896, -122.3964), "union square": (37.7880, -122.4075), "treasure island": (37.8235, -122.3705), "chinatown": (37.7941, -122.4078),
}


def place_coords(place: str):
    m = re.search(r"SFPL\s+([A-Za-z ]+?)(?:,|$)", place)
    if m:
        for bname, lat, lng in SFPL_BRANCHES.values():
            if bname.lower() == m.group(1).strip().lower() or (bname == "Main" and "main" in m.group(1).lower()):
                return lat, lng
    q = place.split(",", 1)[1].strip() + ", San Francisco" if "," in place else place + ", San Francisco"
    lat, lng = geocode(q)
    if not in_bbox(lat, lng):
        lat, lng = geocode(place)
    if not in_bbox(lat, lng):
        low = place.lower()
        for k, v in LANDMARKS.items():
            if k in low:
                return v
    return lat, lng


VENUES_FILE = ROOT / "data" / "venues.json"


def venue_for(url: str):
    if VENUES_FILE.exists():
        for v in json.loads(VENUES_FILE.read_text()).get("venues", []):
            if v.get("match") and v["match"] in (url or ""):
                return v
    return None


def scrape_localkiddo(start: date, end: date) -> list[dict]:
    log("Local Kiddo SF")
    r = get("https://localkiddo.com/sitemap.xml")
    if not r:
        return []
    issues = re.findall(r"<loc>(https://www\.localkiddo\.com/p/local-kiddo-sf-[^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>", r.text)
    issues = [u for u, m in sorted(issues, key=lambda x: x[1], reverse=True)[:3]]
    out, seen = [], set()
    month_no = {k[:3]: v for k, v in MONTHS.items()}
    for issue in issues:
        r = get(issue)
        if not r:
            continue
        paras = re.findall(r"<p[^>]*>(.*?)</p>", r.text, re.S)
        i = 0
        while i < len(paras):
            txt = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", paras[i])).split())
            if txt.startswith("Worth Knowing") and i > 0:
                links = []
                for back in (1, 2):  # the linked name is usually in the paragraph just above
                    if i - back >= 0 and not links:
                        links = re.findall(r'<a href="([^"]+)"[^>]*>(.*?)</a>', paras[i - back], re.S)
                details = []
                j = i + 1
                while j < len(paras) and len(details) < 6:
                    t2 = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", paras[j])).split())
                    if t2.startswith("Worth Knowing") or len(t2) > 120 or (t2.endswith(".") and len(t2) > 60):
                        break
                    details.append(t2)
                    j += 1
                if links:
                    url, name = html.unescape(links[-1][0]), " ".join(html.unescape(re.sub(r"<[^>]+>", "", links[-1][1])).split())
                    v = venue_for(url)
                    dates = [LK_DATE.search(d) for d in details]
                    place = next((d for d in details if not LK_DATE.search(d) and not re.fullmatch(r"(?i)(free|\$.*|.*\$\d.*|tickets?.*|rsvp.*|.*required.*)", d)), "")
                    cost = next((d for d in details if re.search(r"(?i)^free\b|\$", d)), "")
                    if v:
                        place = v["area"]
                    for m in [x for x in dates if x]:
                        mon, day, yr, t1, t2_ = m.groups()
                        try:
                            d = date(int(yr) if yr else start.year, month_no[mon.lower()[:3]], int(day))
                        except (KeyError, ValueError):
                            continue
                        if d < start - timedelta(days=1) and not yr:
                            d = date(d.year + 1, d.month, d.day)
                        if not (start <= d <= end):
                            continue
                        key = (name.lower(), d)
                        if key in seen:
                            continue
                        seen.add(key)
                        lat, lng = (v["lat"], v["lng"]) if v else (place_coords(place) if place else (None, None))
                        if not in_bbox(lat, lng):
                            continue
                        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50]
                        out.append(dict(id=f"lk-{slug}-{d.isoformat()}", kind="event", source="Local Kiddo SF", name=name,
                                        area=place, lat=lat, lng=lng, indoor=False, exp=0.4, off=0,
                                        free=True if cost.lower().startswith("free") else (False if "$" in cost else None),
                                        start=local_iso(d, _hm(t1)), end=local_iso(d, _hm(t2_)) if t2_ else None,
                                        note=f"Picked by Local Kiddo SF's weekly newsletter. {cost}".strip(),
                                        url=url))
                i = j
                continue
            i += 1
        # The day-by-day list: "Friday, 10/9:" headers, then "12:00-6:00 - <a>Name</a> - Place" lines (no am/pm).
        day = None
        for para in paras:
            txt = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", para)).split())
            mh = re.match(r"(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,?\s+(\d{1,2})/(\d{1,2}):?$", txt)
            if mh:
                mo, dd = int(mh.group(1)), int(mh.group(2))
                yr = start.year + (1 if mo < start.month - 6 else 0)
                try:
                    day = date(yr, mo, dd)
                except ValueError:
                    day = None
                continue
            ml = re.match(r"(\d{1,2})(?::(\d\d))?\s*(?:-\s*(\d{1,2})(?::(\d\d))?)?\s+-\s+(.+?)\s+-\s+(.+)$", txt)
            link = re.search(r'<a href="([^"]+)"[^>]*>(.*?)</a>', para, re.S)
            if not (day and ml and link) or not (start <= day <= end):
                continue
            if ml.group(6).strip().upper().startswith("SFPL") or "academy of sciences" in ml.group(6).lower() or LK_SKIP.search(txt):
                continue  # library programs come from SFPL directly; skip grown-up-only listings
            def clock(h, m):
                h = int(h); h = h + 12 if 1 <= h <= 6 else h
                return h, int(m or 0)
            h1, m1 = clock(ml.group(1), ml.group(2))
            name = " ".join(html.unescape(re.sub(r"<[^>]+>", "", link.group(2))).split())
            evening = 7 <= h1 <= 8 and re.search(r"(?i)night|party|circus|ballet|concert|show|celebrat|movie|film", name)
            key = (name.lower(), day)
            if key in seen:
                continue
            seen.add(key)
            end_hm = None
            if ml.group(3):
                h2, m2 = clock(ml.group(3), ml.group(4))
                if evening and h2 < 12:
                    h2 += 12
                if h2 < h1 + (12 if evening else 0) and h2 + 12 < 24:
                    h2 += 12
                if h2 < 24:
                    end_hm = f"{h2:02d}:{m2:02d}"
            place = ml.group(6).strip()
            v = venue_for(html.unescape(link.group(1)))
            if v:
                place = v["area"]
            lat, lng = (v["lat"], v["lng"]) if v else place_coords(place)
            if not in_bbox(lat, lng):
                continue
            if evening:
                h1 += 12
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50]
            out.append(dict(id=f"lk-{slug}-{day.isoformat()}", kind="event", source="Local Kiddo SF", name=name, area=place,
                            lat=lat, lng=lng, indoor=False, exp=0.4, off=0, free=None,
                            start=local_iso(day, f"{h1:02d}:{m1:02d}"), end=local_iso(day, end_hm) if end_hm else None,
                            note="Picked by Local Kiddo SF's weekly newsletter.", url=html.unescape(link.group(1))))
            if v and v.get("indoor"):
                out[-1].update(indoor=True, exp=0)
        time.sleep(1)
    log(f"  {len(out)} events")
    return out


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


# ---------------------------------------------------------------- venue watchlist (needs ANTHROPIC_API_KEY)
WATCHLIST = ROOT / "data" / "watchlist.json"
WATCH_PROMPT = """Below is the text of {venue}'s website. List every event or class on it that happens between {start} and {end}
(inclusive) and that families with young children could attend: all-ages or family events, and kids' programs open to
ages 5 and under. Skip adults-only events, camps that need multi-day enrollment, and anything already past.
Reply with only a JSON array (empty if none) of objects:
{{"name": string, "date": "YYYY-MM-DD", "start": "HH:MM" (24-hour), "end": "HH:MM" or null, "free": true/false/null,
  "url": the event's own page if the text names one, else null,
  "note": one plain sentence for parents (under 30 words, mention price or age limits if stated)}}
Website text:
"""


def claude_json(prompt: str):
    key = os.environ.get("ANTHROPIC_API_KEY")
    model = os.environ.get("CLAUDE_MODEL") or "claude-haiku-5-5"
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": model, "max_tokens": 3000, "messages": [{"role": "user", "content": prompt}]}, timeout=120)
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", []))
    return json.loads(re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M))


WATCH_PROMPT_CT = """Below is the text of {venue}'s website. List every public concert, talk, lecture, conversation, reading,
screening, performance or exhibition opening on it that happens between {start} and {end} (inclusive). Skip classes,
private rentals, members-only events and anything already past.
Reply with only a JSON array (empty if none) of objects:
{{"name": string, "date": "YYYY-MM-DD", "start": "HH:MM" (24-hour), "end": "HH:MM" or null, "free": true/false/null,
  "url": the event's own page if the text names one, else null,
  "note": one plain sentence (under 30 words) on who is performing or speaking and what it's about}}
Website text:
"""


def watch_venues(start: date, end: date, edition: str = "family") -> list[dict]:
    if not WATCHLIST.exists():
        return []
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("Watchlist: skipped (needs ANTHROPIC_API_KEY)")
        return []
    out = []
    for v in json.loads(WATCHLIST.read_text()).get("venues", []):
        if v.get("edition", "family") not in (edition, "both"):
            continue
        text = ""
        for u in v.get("pages", []):
            r = get(u)
            if r:
                text += "\n\n" + " ".join(html.unescape(" ".join(text_lines(r.text))).split())[:12000]
        if not text:
            continue
        try:
            prompt = WATCH_PROMPT if edition == "family" else WATCH_PROMPT_CT
            items = claude_json(prompt.format(venue=v["name"], start=start.isoformat(), end=end.isoformat()) + text[:30000])
        except Exception as e:
            log(f"  {v['name']}: extraction failed: {e}")
            continue
        for it in items if isinstance(items, list) else []:
            try:
                d = date.fromisoformat(it["date"])
                if not (start <= d <= end):
                    continue
                slug = re.sub(r"[^a-z0-9]+", "-", (v["name"] + " " + it["name"]).lower()).strip("-")[:60]
                out.append(dict(id=f"w-{slug}-{d.isoformat()}", kind="event", source=v["name"], name=it["name"],
                                area=v["area"], lat=v["lat"], lng=v["lng"], indoor=v.get("indoor", False),
                                exp=v.get("exp", 0.4), off=v.get("off", 0), free=it.get("free"),
                                start=local_iso(d, it["start"]), end=local_iso(d, it["end"]) if it.get("end") else None,
                                note=it.get("note") or "", url=it.get("url") or v["pages"][0],
                                bookAhead=bool(v.get("bookAhead"))))
            except Exception as e:
                log(f"  skipped an item from {v['name']}: {e}")
    log(f"Watchlist: {len(out)} events")
    return out


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


def curate(events: list[dict], prompt: str = None) -> list[dict]:
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
                json={"model": model, "max_tokens": 4000, "messages": [{"role": "user", "content": (prompt or CURATE_PROMPT) + listing}]},
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
    """Special events: not flagged recurring, not repeating 3+ times within two weeks, not a regular storytime."""
    days = {}
    for e in events:
        if e.get("kind") == "impact" or not e.get("start"):
            continue
        days.setdefault(event_key(e), set()).add(e["start"][:10])
    out = []
    for e in events:
        if e.get("kind") == "impact" or not e.get("start") or e.get("rangeStart") or e.get("recurring"):
            continue
        ds = sorted(date.fromisoformat(x) for x in days.get(event_key(e), ()))
        frequent = any((ds[i + 2] - ds[i]).days <= 14 for i in range(len(ds) - 2))
        if e.get("bookAhead"):  # a limited run of a ticketed show still counts as special
            frequent = False
        if frequent or re.match(r"(storytime (for|in)\b|(toddler|baby|babies|family|preschool) storytime\b)", (e.get("name") or "").lower()):
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


def write_specials_ics(events, path=None, calname="Family events: one day only"):
    utc = ZoneInfo("UTC")
    stamp = datetime.now(utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//sf_familyevents//one-off family events//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{calname}",
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
    (path or SPECIALS).write_text("\r\n".join(ics_fold(l) for l in lines) + "\r\n")
    log(f"Wrote {sum(1 for l in lines if l == 'BEGIN:VEVENT')} one-off events to {(path or SPECIALS).name}")


# ---------------------------------------------------------------- main
def main():
    global _geocache
    if GEOCACHE.exists():
        _geocache = json.loads(GEOCACHE.read_text())
    today = datetime.now(TZ).date()
    end = today + timedelta(days=DAYS_AHEAD)

    far = today + timedelta(days=HORIZON_DAYS)
    msw = [e for e in msw_events(today, far) if not e["recurring"] or e["start"][:10] <= end.isoformat()]
    trusted = (scrape_sfpl(today, end) + scrape_calacademy(today, end) + msw + mapp_events(today, far)
               + scrape_ybg(today, far) + scrape_omca(today, far) + scrape_sfmoma(today, far) + watch_venues(today, far))
    loose = scrape_children_nature(today, end)
    ml = scrape_mission_local(today, far) + scrape_galeria(today, far)
    fc = scrape_funcheap(today, end)
    lk = scrape_localkiddo(today, far)
    settle_indoor(lk + ml)
    if os.environ.get("ANTHROPIC_API_KEY"):
        loose = curate(loose + ml + fc + lk)
    else:
        ml = [e for e in ml if (FAMILY_WORDS.search(e["name"]) or e.get("_family")) and e["start"][11:16] < "20:00"]
        log(f"No ANTHROPIC_API_KEY: skipping Funcheap; kept {len(ml)} Mission Local events by keyword")
        loose = loose + ml + lk

    manual = []
    if MANUAL.exists():
        for e in json.loads(MANUAL.read_text()).get("events", []):
            last = e.get("rangeEnd") or (e.get("end") or e.get("start") or "")[:10]
            if last and last >= today.isoformat():
                manual.append(e)

    events = merge(manual + load_suggestions("family", today) + trusted + loose)
    payload = {
        "generated": datetime.now(TZ).isoformat(timespec="minutes"),
        "coverage": f"{today:%b %-d}–{end:%b %-d}, with one-off events through {far:%b %-d}",
        "sources": ["SF Public Library", "California Academy of Sciences", "SFMOMA", "Oakland Museum of California", "Mission Science Workshop", "Yerba Buena Gardens Festival", "SF Children & Nature", "Mission Local", "Galería de la Raza", "Local Kiddo SF", "Funcheap", "manual", "visitor suggestions"],
        "events": events,
    }
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
    write_specials_ics(events)
    log(f"Wrote {len(events)} events to {OUT.name}")
    build_talks_concerts(today, far)
    apply_overrides()
    crosslink()
    write_share_pages()
    GEOCACHE.write_text(json.dumps(_geocache, indent=1, ensure_ascii=False) + "\n")


def merge(items: list[dict]) -> list[dict]:
    """Drop duplicates (same id, same start at the same spot, or near-identical names on the same day); first wins."""
    for e in items:
        e.pop("_family", None)
        e.pop("_cats", None)
    from difflib import SequenceMatcher
    by_day: dict = {}

    def near_duplicate(e):
        n = re.sub(r"[^a-z0-9]+", " ", (e.get("name") or "").lower()).strip()
        for other in by_day.get((e.get("start") or "")[:10], []):
            if (n in other or other in n or SequenceMatcher(None, n, other).ratio() >= 0.75) and min(len(n), len(other)) >= 6:
                return True
        return False

    seen, events = set(), []
    for e in items:  # earlier lists (hand-added first) win on duplicates
        same_slot = (e.get("start"), round(e.get("lat") or 0, 2), round(e.get("lng") or 0, 2))
        same_name = (re.sub(r"[^a-z0-9]+", " ", (e.get("name") or "").lower()).strip(), (e.get("start") or "")[:10])
        if e["id"] in seen or (e.get("start") and (same_slot in seen or same_name in seen or near_duplicate(e))):
            continue
        seen.add(e["id"])
        if e.get("start"):
            seen.add(same_slot)
            seen.add(same_name)
            by_day.setdefault(same_name[1], []).append(same_name[0])
        events.append(e)
    events.sort(key=lambda e: (e.get("start") or "", e.get("name") or ""))
    return events


# ================================================================ second edition: talks & concerts
OUT_CT = ROOT / "talks-concerts.json"
ICS_CT = ROOT / "talks-concerts.ics"
CALENDARS = ROOT / "data" / "calendars.json"
MANUAL_CT = ROOT / "data" / "manual-talks-concerts.json"
CT_WORDS = re.compile(r"(?i)concert|live music|\bband\b|jazz|\bdj\b|symphon|orchestra|quartet|chamber|opera|recital|singer|songwriter|"
                      r"\btalk\b|conversation|lecture|panel|forum|town hall|discussion|in conversation|\bauthor|book launch|reading|poetry|"
                      r"screening|\bfilm\b|exhibit|opening|gallery|comedy|open mic|perform|dance|theater|theatre|cabaret|salon")
OUTDOOR_PLACE = re.compile(r"(?i)\b(park|plaza|lawn|garden|beach|street fair|block party|outdoors?|open air|parklet|alley)\b")
# social events (mixers, singles nights, networking) aren't talks or concerts: names are checked for all of these,
# descriptions only for the unmistakable ones, since a talk may mention a happy hour afterwards
SOCIAL_NAME = re.compile(r"(?i)\bmixers?\b|\bsingles\b(?!\s+(release|launch|club))|speed[- ]?dating|\bdating (event|night|experience|party)\b|"
                         r"\bhappy hour\b|\bnetworking\b|\bsocial hour\b|matchmak|\bmeet[- ]?ups?\b|\bmingle\b")
SOCIAL_NOTE = re.compile(r"(?i)\bsingles (event|night|mixer|experience)|speed[- ]?dating|matchmak")
# family events from newsletters often name a neighborhood, not a venue, so decide indoors/outdoors only on a clear clue:
# outdoor words in the name or place win; then indoor words in the name, place or description; otherwise keep the source's guess
FAM_OUTDOOR = re.compile(r"(?i)\b(parks?|plaza|lawn|gardens?|beach|street|block party|outdoors?|open air|parklet|alley|playground|farm|zoo|trail|"
                         r"hike|hiking|field|green|pier|lake|reservoir|meadow|picnic|parade|pumpkin patch|creek|forest|bike ride|square|civic center|salesforce|festival)\b")
FAM_INDOOR = re.compile(r"(?i)\b(library|museum|theat(er|re)|book(shop|store)?s?|caf[eé]|studio|gallery|church|school|ymca|jcc|gym|ballet|cinema|ybca|"
                        r"z space|somarts|randall|indoors?|playgroups?|rec(reation)? center|community center|kitchen|classroom)\b")

def settle_indoor(events: list[dict]) -> list[dict]:
    for e in events:
        where = e["name"] + " " + (e.get("area") or "")
        if FAM_OUTDOOR.search(where):
            e.update(indoor=False)
        elif FAM_INDOOR.search(where + " " + (e.get("note") or "")):
            e.update(indoor=True, exp=0)
    return events

CT_SKIP = re.compile(r"(?i)\bclass\b|workshop|lesson|beginner|training|where to begin|mahjong|\bmeetup\b|networking")
CT_PROMPT = """You help curate a page of concerts, talks, readings and arts events for adults in San Francisco and Oakland.
For each event below, decide whether it is a public concert, talk, lecture, conversation, reading, screening, performance,
or exhibition opening that a curious adult might attend. Drop classes, kids' programs, sales, markets, fitness and networking-only events.
Reply with only a JSON array, one object per event, in the same order:
{"id": string, "keep": boolean, "indoor": boolean, "free": boolean or null, "note": one plain sentence (under 25 words) on who is performing or speaking and what it's about}
Events:
"""


def ics_events(text: str) -> list[dict]:
    """Minimal iCalendar reader: unfolds lines and returns each VEVENT's fields."""
    text = re.sub(r"\r?\n[ \t]", "", text)
    out = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        ev = {}
        for line in block.strip().splitlines():
            m = re.match(r"([A-Z-]+)((?:;[^:]*)?):(.*)", line)
            if m:
                ev.setdefault(m.group(1), (m.group(2), m.group(3)))
        out.append(ev)
    return out


def ics_time(field):
    if not field:
        return None
    params, v = field
    v = v.strip()
    if re.fullmatch(r"\d{8}", v):
        return datetime.strptime(v, "%Y%m%d").replace(tzinfo=TZ)
    if v.endswith("Z"):
        return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
    tz = re.search(r"TZID=([^;:]+)", params or "")
    return datetime.strptime(v, "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo(tz.group(1)) if tz else TZ)


def ics_text(v: str) -> str:
    return v.replace("\\n", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def luma_feed(url: str):
    """A Luma calendar page (luma.com/name) publishes its feed at api.lu.ma/ics/get?entity=calendar&id=cal-..."""
    r = get(url)
    if not r:
        return None
    ids = re.findall(r"cal-[A-Za-z0-9]{8,}", r.text)
    if not ids:
        return None
    best = max(set(ids), key=ids.count)
    return f"https://api.lu.ma/ics/get?entity=calendar&id={best}"


def scrape_calendars(start: date, end: date, edition: str) -> list[dict]:
    """Every calendar feed in data/calendars.json for this edition: Luma pages, Google public calendars, any .ics URL."""
    if not CALENDARS.exists():
        return []
    out = []
    for c in json.loads(CALENDARS.read_text()).get("calendars", []):
        if c.get("edition", "talks-concerts") not in (edition, "both"):
            continue
        feed = c.get("ics") or (luma_feed(c["url"]) if "lu.ma" in c.get("url", "") or "luma.com" in c.get("url", "") else c.get("url"))
        r = get(feed) if feed else None
        if not r:
            continue
        n = 0
        for ev in ics_events(r.text):
            s = ics_time(ev.get("DTSTART"))
            if not s or not (start <= s.date() <= end) or (ev.get("STATUS", ("", ""))[1].upper() == "CANCELLED"):
                continue
            en = ics_time(ev.get("DTEND"))
            lat = lng = None
            if ev.get("GEO"):
                try:
                    lat, lng = map(float, ev["GEO"][1].split(";"))
                except ValueError:
                    pass
            loc = ics_text(ev.get("LOCATION", ("", ""))[1])
            if re.match(r"(?i)\s*(https?://|www\.)", loc):
                loc = ""  # some hosts put a ticket link in LOCATION; fall back to GEO or the calendar's own place
            if not in_bbox(lat, lng) and loc.strip():
                if re.search(r"(?i)\b(online|virtual|zoom|livestream|live stream)\b", loc):
                    continue
                addr = re.search(r"\d+\s+[A-Z][\w .'-]+?(?:St|Street|Ave|Avenue|Blvd|Way|Rd|Road|Pl|Place|Dr|Drive|Embarcadero)\b\.?[ ,]+[A-Za-zé ]+", loc)
                lat, lng = geocode((addr.group(0) if addr else loc) + ", CA")
                if not in_bbox(lat, lng):
                    continue
            if not in_bbox(lat, lng):
                blurb = ics_text(ev.get("SUMMARY", ("", ""))[1] + " " + ev.get("DESCRIPTION", ("", ""))[1])
                online = re.search(r"(?i)\bonline\b|virtual|zoom|webinar|livestream|digital discourse", blurb)
                if loc.strip() or not c.get("lat") or (c.get("skipNoLocation") and online):
                    continue
                lat, lng = c.get("lat"), c.get("lng")
            desc = ics_text(ev.get("DESCRIPTION", ("", ""))[1])
            desc = re.sub(r"(?i)^get up-to-date information at:?\s*\S+\s*", "", desc)
            desc = re.sub(r"https?://\S+", "", desc).strip()
            note = " ".join(re.split(r"(?<=[.!?])\s+", desc)[:2])[:260]
            url = (ev.get("URL", ("", ""))[1] or "").strip() or c.get("url")
            m = re.search(r"https?://(?:lu\.ma|luma\.com)/[A-Za-z0-9\-_]+", ics_text(ev.get("DESCRIPTION", ("", ""))[1]))
            if m:
                url = m.group(0)
            uid = re.sub(r"[^a-z0-9]+", "-", (ev.get("UID", ("", ""))[1] or ev.get("SUMMARY", ("", ""))[1]).lower())[:40]
            place = re.sub(r"\s{2,}", ", ", loc).split(", San Francisco")[0].strip(" ,") if loc else ""
            m2 = re.match(r"(.+?[A-Za-z)])\s+(\d+\s+\S.*)", place)  # "SPUR Urban Center 654 Mission Street" -> name, address
            if m2 and "," not in m2.group(1):
                place = f"{m2.group(1)}, {m2.group(2)}"
            out.append(dict(id=f"cal-{uid}", kind="event", source=c["name"], name=ics_text(ev.get("SUMMARY", ("", ""))[1]).strip(),
                            area=(place or c.get("area") or "").strip(), lat=lat, lng=lng,
                            indoor=c.get("indoor", True), exp=0 if c.get("indoor", True) else 0.4, off=0, free=c.get("free"),
                            bookAhead=bool(c.get("bookAhead")),
                            start=s.isoformat(), end=en.isoformat() if en else None, note=note, url=url))
            n += 1
        log(f"{c['name']} calendar: {n} events")
    return out


def scrape_commonwealth(start: date, end: date) -> list[dict]:
    """Commonwealth Club's events page: 'Location | Day, Mon DD / H:MM PM PDT | Title' rows. Keeps in-person San Francisco talks."""
    log("Commonwealth Club")
    r = get("https://www.commonwealthclub.org/events")
    if not r:
        return []
    links = list(dict.fromkeys(re.findall(r'href="(/events/\d{4}-\d{2}-\d{2}/[^"#?]+)"', r.text)))
    text = re.sub(r"(\|\s*)+", "| ", " ".join(html.unescape(re.sub(r"<[^>]+>", " | ",
              re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S))).split()))
    out = []
    for loc, mon, day, hm, ap, title in re.findall(
            r"\| ([^|]{3,40}) \| (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), ([A-Z][a-z]{2}) (\d{1,2}) / (\d{1,2}:\d\d) (AM|PM) P[DS]T \| ([^|]{4,160}) \| Info & Tickets", text):
        if loc.strip() != "San Francisco":
            continue  # skip livestream-only and out-of-town events
        try:
            d = date(start.year, {k[:3]: v for k, v in MONTHS.items()}[mon.lower()], int(day))
        except (KeyError, ValueError):
            continue
        if d < start - timedelta(days=200):
            d = date(d.year + 1, d.month, d.day)
        if not (start <= d <= end):
            continue
        h, m = map(int, hm.split(":")); h = h % 12 + (12 if ap == "PM" else 0)
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        url = next((("https://www.commonwealthclub.org" + l) for l in links if l.split("/")[2] == d.isoformat() and
                    any(w in l for w in slug.split("-")[:3] if len(w) > 3)), "https://www.commonwealthclub.org/events")
        out.append(dict(id=f"cwc-{slug[:50]}-{d.isoformat()}", kind="event", source="Commonwealth Club", name=title.strip(),
                        area="Commonwealth Club, 110 The Embarcadero", lat=37.7932, lng=-122.3925, indoor=True, exp=0, off=0,
                        free=None, start=local_iso(d, f"{h:02d}:{m:02d}"), end=None, bookAhead=True,
                        note="A Commonwealth Club talk; tickets and details on the event page.", url=url))
    for e in out:  # each event page's summary says who's speaking and about what
        if e["url"].count("/") > 4:
            n = page_details(e["url"]).get("note")
            if n and len(n) > 40:
                e["note"] = n
            time.sleep(0.3)
    log(f"  {len(out)} events")
    return out


def scrape_independent(start: date, end: date) -> list[dict]:
    """The Independent's calendar loads shows from its TicketWeb plugin; ask it the same way the page does."""
    log("The Independent")
    r = get("https://www.theindependentsf.com/calendar/")
    if not r:
        return []
    nonce = re.search(r"action: 'get_events_for_calendar',\s*nonce: '([^']+)'", r.text)
    params = re.search(r"params: '(\{.*?\})',", r.text, re.S)
    if not (nonce and params):
        return []
    try:
        d = S.post("https://www.theindependentsf.com/wp-admin/admin-ajax.php", timeout=30,
                         data={"action": "get_events_for_calendar", "nonce": nonce.group(1), "start": start.isoformat(),
                               "end": end.isoformat(), "params": params.group(1)}).json()
    except Exception as e:
        log(f"  failed: {e}")
        return []
    pop = d.get("popupdata") or ""
    out = []
    for ev in d.get("events", []):
        sk = ev.get("sortkey") or ""
        if not re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", sk):
            continue
        dd = date.fromisoformat(sk[:10])
        if not (start <= dd <= end):
            continue
        block = pop.split(f'id="tw-event-dialog-{ev["id"]}"')[1].split("tw-event-dialog-")[0] if f'tw-event-dialog-{ev["id"]}"' in pop else ""
        url = (re.search(r'href="(https://www\.theindependentsf\.com/tm-event/[^"]+)"', block) or [None, "https://www.theindependentsf.com/calendar/"])[1]
        tour = re.search(r'class="tw-name-presenting">\s*([^<]+)<', block)
        support = re.search(r'class="tw-attractions">(.*?)</div>', block, re.S)
        sup = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", support.group(1))).split()) if support else ""
        name = html.unescape(ev.get("title", "")).strip()
        note = "Live at The Independent" + (f" ({html.unescape(tour.group(1).strip())})" if tour else "") + \
               (f", with {sup}" if sup else "") + f". Doors {ev.get('doors', '')}, show {ev.get('displayTime', '')}."
        out.append(dict(id=f"indy-{ev['id']}-{dd.isoformat()}", kind="event", source="The Independent", name=name,
                        area="The Independent, 628 Divisadero St", lat=37.7755, lng=-122.4378, indoor=True, exp=0, off=0,
                        free=False, start=local_iso(dd, sk[11:16]), end=None, bookAhead=True, note=note, url=url, tags=["music"]))
    log(f"  {len(out)} shows")
    return out


def scrape_cmc(start: date, end: date) -> list[dict]:
    """Community Music Center (544 Capp St): each event page has 'Date / Time | Day, | Month D, YYYY | 7:00 pm | – 10:00 pm | Cost: ...'."""
    log("Community Music Center")
    r = get("https://sfcmc.org/events/")
    if not r:
        return []
    out = []
    for url in sorted(set(re.findall(r'href="(https://sfcmc\.org/event/[^"]+)"', r.text))):
        pg = get(url)
        if not pg:
            continue
        x = re.sub(r"(\|\s*)+", "| ", " ".join(html.unescape(re.sub(r"<[^>]+>", " | ",
               re.sub(r"<script.*?</script>|<style.*?</style>", "", pg.text, flags=re.S))).split()))
        m = re.search(r"Date / Time \| \w+day, \| ([A-Z][a-z]+) (\d{1,2}), (\d{4}) \| (\d{1,2}:\d\d) ([ap]m)(?: \| – (\d{1,2}:\d\d) ([ap]m))?", x)
        if not m:
            continue
        mon, dd, yy, t1, ap1, t2, ap2 = m.groups()
        try:
            d = date(int(yy), MONTHS[mon.lower()], int(dd))
        except (KeyError, ValueError):
            continue
        if not (start <= d <= end):
            continue
        hm = lambda t, ap: f"{int(t.split(':')[0]) % 12 + (12 if ap == 'pm' else 0):02d}:{t.split(':')[1]}"
        title = html.unescape((re.search(r"<title>(.*?)(?:\s*[–|-]\s*Community Music Center)?</title>", pg.text, re.S) or [None, ""])[1]).strip()
        title = re.sub(r"\s*[-–|]\s*(San Francisco )?Community Music Center$", "", title)
        loc = (re.search(r"Location \| ([^|]{3,80})", x) or [None, "CMC Concert Hall"])[1].strip()
        cost = (re.search(r"Cost: ([^|]{1,60})", x) or [None, ""])[1].strip()
        desc = (re.search(r'<meta[^>]+(?:property="og:description"|name="description")[^>]+content="([^"]*)"', pg.text) or [None, ""])[1]
        sents = [x for x in re.split(r"(?<=[.!?])\s+", html.unescape(desc)) if re.search(r"[.!?]$", x)]  # whole sentences only
        note = " ".join(sents[:2])[:240]
        at_cmc = "CMC" in loc or "Community Music Center" in loc
        lat, lng = (37.7588, -122.4177) if at_cmc else geocode(loc + ", San Francisco")
        if not in_bbox(lat, lng):
            continue
        out.append(dict(id=f"cmc-{url.rstrip('/').split('/')[-1][:50]}-{d.isoformat()}", kind="event", source="Community Music Center",
                        name=title, area="Community Music Center, 544 Capp St" if at_cmc else loc, lat=lat, lng=lng, indoor=True,
                        exp=0, off=0, free=("free" in cost.lower()) or None, start=local_iso(d, hm(t1, ap1)),
                        end=local_iso(d, hm(t2, ap2)) if t2 else None, note=(note + (f" {cost}." if cost else "")).strip(), url=url,
                        tags=["music"]))
        time.sleep(0.3)
    log(f"  {len(out)} events")
    return out


VENUE_PAGES = ROOT / "data" / "venue_pages.json"
GENRE_TAG = [("jazz", r"(?i)jazz|soul|funk|blues"), ("classical", r"(?i)classical|chamber|opera"), ("world", r"(?i)latin|world|afro|reggae|cumbia"),
             ("songs", r"(?i)rock|indie|folk|americana|country|punk|pop|singer|alt")]


def venue_feed(v: dict, slug: str, start: date, end: date) -> list[dict]:
    """A venue's own event feed: Squarespace (/events?format=json) or The Events Calendar (WordPress)."""
    base = dict(kind="event", source=v["name"], area=v["area"], lat=v["lat"], lng=v["lng"], indoor=True, exp=0, off=0,
                bookAhead=bool(v.get("bookAhead")))
    out = []
    if v["kind"] == "tribe":
        site = re.match(r"https?://[^/]+", v["url"]).group(0)
        for e in scrape_tribe(site, v["name"], slug, start, end, default_geo=(v["lat"], v["lng"])):
            e.update({k: base[k] for k in ("area", "lat", "lng", "indoor", "exp", "bookAhead")})
            e.pop("_family", None); e.pop("_cats", None)
            out.append(e)
        return out
    r = get(v["url"].split("?")[0] + "?format=json")
    if not r:
        return out
    site = re.match(r"https?://[^/]+", v["url"]).group(0)
    for it in r.json().get("upcoming") or r.json().get("items") or []:
        try:
            st = datetime.fromtimestamp(it["startDate"] / 1000, TZ)
            en = datetime.fromtimestamp(it["endDate"] / 1000, TZ) if it.get("endDate") else None
        except (KeyError, TypeError, ValueError):
            continue
        if not (start <= st.date() <= end):
            continue
        name = html.unescape(it.get("title") or "").strip()
        text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", (it.get("excerpt") or "") + " " + (it.get("body") or "")[:3000])).split())
        note = " ".join(re.split(r"(?<=[.!?])\s+", text)[:2])[:240] or f"At {v['name']}."
        out.append(dict(base, id=f"vp-{slug}-{re.sub(r'[^a-z0-9]+', '-', name.lower())[:40]}-{st.date().isoformat()}",
                        name=name, start=st.isoformat(), end=en.isoformat() if en and en > st else None,
                        free=True if re.search(r"(?i)\bfree\b", text) else None, note=note, url=site + (it.get("fullUrl") or "")))
    return out


def scrape_venue_pages(start: date, end: date) -> list[dict]:
    if not VENUE_PAGES.exists():
        return []
    out = []
    for v in json.loads(VENUE_PAGES.read_text()).get("venues", []):
        n0 = len(out)
        slug = re.sub(r"[^a-z0-9]+", "-", v["name"].lower()).strip("-")[:20]
        if v["kind"] in ("squarespace", "tribe"):  # small neighborhood venues: talks, readings, screenings
            out += venue_feed(v, slug, start, end)
            log(f"{v['name']}: {len(out) - n0} events")
            time.sleep(0.5)
            continue
        r = get(v["url"])
        if not r:
            continue
        base = dict(kind="event", source=v["name"], area=v["area"], lat=v["lat"], lng=v["lng"], indoor=True, exp=0, off=0, bookAhead=True)
        if v["kind"] == "jsonld":
            for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', r.text, re.S):
                try:
                    j = json.loads(block)
                except json.JSONDecodeError:
                    continue
                for it in (j if isinstance(j, list) else j.get("@graph", [j]) if isinstance(j, dict) else []):
                    if not (isinstance(it, dict) and "Event" in str(it.get("@type")) and it.get("startDate")):
                        continue
                    try:
                        st = datetime.fromisoformat(it["startDate"].replace("Z", "+00:00")).astimezone(TZ)
                    except ValueError:
                        continue
                    if not (start <= st.date() <= end) or "Cancel" in str(it.get("eventStatus", "")):
                        continue
                    name = html.unescape(it.get("name", "")).strip()
                    perf = [p.get("name") for p in (it.get("performer") or []) if isinstance(p, dict) and p.get("name") and p.get("name") != name] if isinstance(it.get("performer"), list) else []
                    note = f"Live at {v['name']}" + (f", with {', '.join(perf[:3])}" if perf else "") + "."
                    out.append(dict(base, id=f"vp-{re.sub(r'[^a-z0-9]+', '-', v['name'].lower())[:20]}-{re.sub(r'[^a-z0-9]+', '-', name.lower())[:40]}-{st.date().isoformat()}",
                                    name=name, start=st.isoformat(), end=None, free=False, note=note, url=it.get("url") or v["url"], tags=["music"]))
        elif v["kind"] == "seetickets":
            for card in r.text.split("seetickets-list-event-container")[1:]:
                f = {}
                for k, x in re.findall(r'<(?:p|div|span|h\d)[^>]+class="([^"]+)"[^>]*>(.*?)</(?:p|div|span|h\d)>', card, re.S):
                    key = k.split()[-1].replace("event-", "")  # GAMH uses event-title / event-date
                    f.setdefault(key, " ".join(html.unescape(re.sub(r"<[^>]+>", " ", x)).split()))
                if f.get("see-showtime") and "Show at" not in f.get("doortime-showtime", ""):
                    f["doortime-showtime"] = (f.get("doortime-showtime", "").replace("Event Doortime:", "Doors at") + " / Show at " + f["see-showtime"]).strip(" /")
                link = (re.search(r'href="(https://wl\.seetickets\.us/[^"]+)"', card) or [None, v["url"]])[1]
                m = re.match(r"\w{3} (\w{3}) (\d{1,2})", f.get("date", ""))
                if not m or not f.get("title"):
                    continue
                f["title"] = re.sub(r"^\*?RESCHEDULED\*?\s*", "", f["title"])
                mon, dd = m.groups()
                try:
                    d = date(start.year, {k[:3]: x for k, x in MONTHS.items()}[mon.lower()], int(dd))
                except (KeyError, ValueError):
                    continue
                if d < start - timedelta(days=60):
                    d = date(d.year + 1, d.month, d.day)
                if not (start <= d <= end):
                    continue
                show = re.search(r"Show at (\d{1,2}):(\d\d)\s*([AP])M", f.get("doortime-showtime", ""), re.I)
                h = (int(show.group(1)) % 12 + (12 if show.group(3).upper() == "P" else 0), int(show.group(2))) if show else (20, 0)
                genre = f.get("genre", "")
                tags = ["music"] + [t for t, rx in GENRE_TAG if re.search(rx, genre)]
                sup = f.get("supporting-talent", "").replace("Supporting Talent:", "").strip() or f.get("subtitle", "").replace("with ", "", 1)
                price = f.get("fs-12", "")
                note = " ".join(x for x in [f"{genre} at {v['name']}." if genre else f"Live at {v['name']}.",
                                            f"With {sup}." if sup else "", f.get("doortime-showtime", "") + ".",
                                            price + "." if "$" in price or "Ages" in price else ""] if x).strip()
                out.append(dict(base, id=f"vp-{re.sub(r'[^a-z0-9]+', '-', v['name'].lower())[:20]}-{re.sub(r'[^a-z0-9]+', '-', f['title'].lower())[:40]}-{d.isoformat()}",
                                name=f["title"], start=local_iso(d, f"{h[0]:02d}:{h[1]:02d}"), end=None, free=False, note=note, url=link, tags=tags))
        out[n0:] = [e for e in out[n0:] if not re.search(r"(?i)private event|\bpass(es)?\b|show ticket|gift card|parking|add-?on", e["name"])]
        log(f"{v['name']}: {len(out) - n0} shows")
        time.sleep(0.5)
    return out


def build_talks_concerts(today: date, far: date):
    log("— Talks & concerts edition —")
    feeds = scrape_calendars(today, far, "talks-concerts")
    jccsf = scrape_tribe("https://www.jccsf.org", "JCCSF Arts & Ideas", "jccsf", today, far, default_geo=(37.7870, -122.4466))
    ybg = [e for e in scrape_ybg(today, far) if not re.search(r"(?i)children|hoopla|kids|outdoors with", e["name"] + " " + e["area"])]
    ml = scrape_mission_local(today, far) + scrape_galeria(today, far)
    # scrape_tribe marks everything outdoors (right for family street fairs); talks, shows and openings are indoors
    # unless the place says otherwise. YBG keeps its own value: its stages are on the lawn.
    for e in jccsf + ml:
        if not OUTDOOR_PLACE.search(e["name"] + " " + e["area"]):
            e.update(indoor=True, exp=0)
    watch = watch_venues(today, far, edition="talks-concerts") + scrape_commonwealth(today, far) + scrape_independent(today, far)
    # Red Poppy Art House (24th St): its own calendar feed of small concerts and performances, plus MAPP nights it hosts
    rp = scrape_tribe("https://www.redpoppyarthouse.org", "Red Poppy Art House", "rpah", today, far, default_geo=(37.7544, -122.4146))
    for e in rp:
        e.pop("_family", None); e.pop("_cats", None)
        e.update(area="Red Poppy Art House, 2698 Folsom St", indoor=True, exp=0, bookAhead=True)
        if not e.get("note") or e["note"].lower().startswith("advance tickets"):
            e["note"] = "An intimate performance at Red Poppy Art House, a small Mission arts space; advance tickets recommended."
    watch += rp + mapp_events(today, far)
    watch += scrape_venue_pages(today, far)
    watch += [e for e in scrape_cmc(today, far) if not re.search(r"(?i)halloween|costume|children|gala", e["name"])]
    if os.environ.get("ANTHROPIC_API_KEY"):
        loose = curate(jccsf + ml, prompt=CT_PROMPT)
    else:
        loose = [e for e in jccsf + ml if CT_WORDS.search(e["name"] + " " + (e.get("note") or "")) and not FAMILY_WORDS.search(e["name"])
                 and not CT_SKIP.search(e["name"])]
    manual = []
    if MANUAL_CT.exists():
        manual = [e for e in json.loads(MANUAL_CT.read_text()).get("events", [])
                  if (e.get("end") or e.get("start") or "")[:10] >= today.isoformat()]
    social = [e for e in feeds + watch + ybg + loose if SOCIAL_NAME.search(e["name"]) or SOCIAL_NOTE.search(e.get("note") or "")]
    if social:
        log(f"Dropped {len(social)} social events: " + "; ".join(e["name"][:50] for e in social))
    feeds, watch, ybg, loose = ([e for e in x if e not in social] for x in (feeds, watch, ybg, loose))
    events = merge(manual + load_suggestions("talks-concerts", today) + feeds + watch + ybg + loose)
    for e in events:
        e["indoor"] = bool(e.get("indoor", True))
    OUT_CT.write_text(json.dumps({
        "generated": datetime.now(TZ).isoformat(timespec="minutes"),
        "coverage": f"{today:%b %-d}–{far:%b %-d}",
        "sources": ["Calendar feeds (Luma, SPUR and others)", "Commonwealth Club", "The Independent", "Red Poppy Art House", "Community Music Center", "The Fillmore", "The Chapel", "Great American Music Hall", "MAPP", "JCCSF Arts & Ideas", "Yerba Buena Gardens Festival", "Mission Local",
                    "Galería de la Raza", "watchlist venues", "manual"],
        "events": events}, indent=1, ensure_ascii=False) + "\n")
    write_specials_ics(events, ICS_CT, "Talks & concerts: one-offs")
    log(f"Wrote {len(events)} events to {OUT_CT.name}")


# ---------------------------------------------------------------- visitor suggestions (Google Form → Sheet)
def load_suggestions(edition: str, today: date) -> list[dict]:
    """Approved rows from the suggestions Sheet, published as CSV (repo variable SUGGESTIONS_CSV).
    A row counts when its Approved column says yes; "Which page" picks family, talks-concerts or both."""
    url = os.environ.get("SUGGESTIONS_CSV", "").strip()
    if not url:
        return []
    r = get(url)
    if not r:
        return []
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]
    def col(*words):
        for i, h in enumerate(head):
            if any(w in h for w in words):
                return i
        return None
    C = dict(ok=col("approved"), name=col("event name", "name of the event"), page=col("which page", "category"), date=col("date"),
             start=col("start time"), end=col("end time"), place=col("venue", "where"), addr=col("address"), url=col("link", "url"),
             free=col("free"), indoor=col("indoors", "indoor"), note=col("description", "what is it"))
    def cell(row, k):
        i = C[k]
        return row[i].strip() if i is not None and i < len(row) else ""
    def parse_day(t):
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                return datetime.strptime(t, fmt).date()
            except ValueError:
                pass
        return None
    def parse_time(t):
        for fmt in ("%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M", "%I %p"):
            try:
                return datetime.strptime(t.upper(), fmt).time()
            except ValueError:
                pass
        return None
    out = []
    for row in rows[1:]:
        if not re.match(r"(?i)\s*(y|yes|approved|ok|✓|x)\s*$", cell(row, "ok")):
            continue
        page = cell(row, "page").lower()
        if not page:
            log(f"  suggestion skipped, no page chosen: {cell(row, 'name')}")
            continue
        if not ("both" in page or ("family" in page) == (edition == "family")):
            continue
        name, day, t0 = cell(row, "name"), parse_day(cell(row, "date")), parse_time(cell(row, "start"))
        if not (name and day and t0) or day < today:
            continue
        t1 = parse_time(cell(row, "end"))
        start = datetime.combine(day, t0, TZ)
        end = datetime.combine(day, t1, TZ) if t1 and t1 > t0 else None
        place, addr = cell(row, "place"), cell(row, "addr")
        lat, lng = geocode(addr or place)
        if lat is None and place and addr:
            lat, lng = geocode(place)
        if lat is None:
            log(f"  suggestion skipped, couldn't find the place: {name} @ {addr or place}")
            continue
        said = cell(row, "indoor")  # the Form may not ask; then guess from the name and place
        indoor = not re.search(r"(?i)outdoor", said) if said else not OUTDOOR_PLACE.search(f"{name} {place} {addr}")
        e = dict(id=f"sug-{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:40]}-{day.isoformat()}", kind="event", source="suggested",
                 name=name, area=", ".join(x for x in (place, addr) if x) or addr, lat=lat, lng=lng,
                 start=start.isoformat(), indoor=indoor, exp=0 if indoor else 0.6, off=0,
                 free=bool(re.match(r"(?i)\s*yes", cell(row, "free"))), note=cell(row, "note"), url=cell(row, "url"))
        if end:
            e["end"] = end.isoformat()
        out.append(e)
    log(f"Suggestions ({edition}): {len(out)} approved")
    return out


OVERRIDES = ROOT / "data" / "overrides.json"

def apply_overrides():
    """Hand fixes from data/overrides.json (match a name, set fields), applied to both pages' events."""
    if not OVERRIDES.exists():
        return
    rules = [(re.compile(o["match"], re.I), o.get("set", {})) for o in json.loads(OVERRIDES.read_text()).get("overrides", [])]
    n = 0
    for path in (OUT, OUT_CT):
        data = json.loads(path.read_text())
        for e in data["events"]:
            for rx, fields in rules:
                if rx.search(e.get("name") or ""):
                    e.update(fields); n += 1
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    log(f"Applied {n} hand fixes from {OVERRIDES.name}")

def _name_words(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9áéíóúñ]+", name.lower()) if len(w) >= 4} - {"with", "from", "night", "festival", "gardens", "yerba", "buena", "free"}

def crosslink():
    """Tag events that appear on both pages: same start time, and one name contains the other
    or they share a name word at the same place. Each gets also={page, id} pointing at its twin."""
    fam, ct = json.loads(OUT.read_text()), json.loads(OUT_CT.read_text())
    norm = lambda e: re.sub(r"\W+", " ", (e.get("name") or "").lower()).strip()
    for e in fam["events"] + ct["events"]:
        e.pop("also", None)
    by_start = {}
    for e in fam["events"]:
        if e.get("kind") != "impact" and e.get("start") and not e.get("rangeStart"):
            by_start.setdefault(e["start"][:16], []).append(e)
    n = 0
    for c in ct["events"]:
        for f in by_start.get((c.get("start") or "")[:16], []):
            a, b = norm(c), norm(f)
            same = a in b or b in a
            if not same and _name_words(c["name"]) & _name_words(f["name"]):
                try:
                    same = math.hypot(c["lat"] - f["lat"], (c["lng"] - f["lng"]) * 0.79) * 69 < 0.25
                except (KeyError, TypeError):
                    pass
            if same:
                c["also"] = {"page": "index.html", "id": f["id"]}
                f["also"] = {"page": "talks-concerts.html", "id": c["id"]}
                n += 1
                break
    for path, data in ((OUT, fam), (OUT_CT, ct)):
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    log(f"Cross-linked {n} events that appear on both pages")


SHARE_DIR = ROOT / "e"

def share_name(prefix: str, event_id: str) -> str:
    # must match sharePage() in the pages
    return prefix + "-" + re.sub(r"[^A-Za-z0-9_-]+", "-", event_id)[:90] + ".html"

def write_share_pages():
    """One tiny page per upcoming event (e/f-<id>.html, e/t-<id>.html) so links shared by text or chat
    show the event's name, time and place in the preview; the page then opens the event on the site."""
    import shutil
    shutil.rmtree(SHARE_DIR, ignore_errors=True)
    SHARE_DIR.mkdir()
    now, n = datetime.now(TZ), 0
    for path, prefix, page, site in ((OUT, "f", "index.html", "Family outings"), (OUT_CT, "t", "talks-concerts.html", "Talks & concerts")):
        for e in json.loads(path.read_text()).get("events", []):
            if e.get("kind") == "impact" or not e.get("start") or e.get("rangeStart"):
                continue
            st = datetime.fromisoformat(e["start"])
            if st < now - timedelta(hours=6):
                continue
            when = f"{st:%a, %b} {st.day}, " + (f"{st.hour % 12 or 12}" + (f":{st.minute:02d}" if st.minute else "") + (" am" if st.hour < 12 else " pm"))
            place = (e.get("area") or "").split(",")[0]
            title = f"{e['name']} · {when}"
            desc = " · ".join(x for x in [place, "free" if e.get("free") else "", (e.get("note") or "").replace(" (Picked by Local Kiddo SF.)", "")] if x)[:300]
            target = f"../{page}#ev={requests.utils.quote(e['id'], safe='')}"
            q = lambda x: html.escape(x, quote=True)
            (SHARE_DIR / share_name(prefix, e["id"])).write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{q(title)}</title>
<meta name="description" content="{q(desc)}"><meta property="og:title" content="{q(title)}"><meta property="og:description" content="{q(desc)}">
<meta property="og:type" content="website"><meta property="og:site_name" content="{q(site)} · SF"><meta name="twitter:card" content="summary">
<meta http-equiv="refresh" content="0; url={q(target)}"><script>location.replace({json.dumps(target)})</script></head>
<body style="font-family:system-ui,sans-serif;padding:24px"><p><b>{q(e['name'])}</b><br>{q(when)} · {q(place)}</p><p><a href="{q(target)}">Open the event</a></p></body></html>
""")
            n += 1
    log(f"Wrote {n} share pages to {SHARE_DIR.name}/")

# ---------------------------------------------------------------- one broken source shouldn't stop the refresh
FAILED: list[str] = []

def _isolated(fn, empty):
    """Run fn; if it crashes, log the traceback, note it in FAILED and return `empty` so the refresh carries on."""
    def run(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            log(f"!! {fn.__name__} failed, skipping it this run: {type(e).__name__}: {e}")
            log(traceback.format_exc())
            FAILED.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            return empty() if callable(empty) else empty
    run.__name__ = fn.__name__
    return run

for _name in [n for n in list(globals()) if n.startswith("scrape_")] + [
        "msw_events", "mapp_events", "watch_venues", "load_suggestions"]:
    globals()[_name] = _isolated(globals()[_name], list)
build_talks_concerts = _isolated(build_talks_concerts, None)
crosslink = _isolated(crosslink, None)
apply_overrides = _isolated(apply_overrides, None)
write_share_pages = _isolated(write_share_pages, None)

def report_failures():
    if not FAILED:
        return
    log(f"{len(FAILED)} source(s) failed and were skipped:\n  " + "\n  ".join(FAILED))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:  # shows on the run's page in GitHub Actions
        with open(summary, "a") as fh:
            fh.write("### ⚠️ Sources skipped this run\n\n" + "".join(f"- `{x}`\n" for x in FAILED)
                     + "\nEverything else was refreshed. These sources keep no events until the next run that works.\n")


if __name__ == "__main__":
    main()
    report_failures()
