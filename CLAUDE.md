# sf_familyevents — project guide for Claude Code

Bay Area outing finder, hosted on GitHub Pages at https://rahmin.github.io/sf_familyevents/.
Static HTML pages + JSON data, rebuilt twice a week by a Python script in GitHub Actions. No build step, no framework.

## The pages

| File | What it is |
|---|---|
| `index.html` | **Family outings** page ("Where should we go today?"). Two tabs: *Outings & events* (≈20 hand-curated places + kid/family events) and *🛝 Playgrounds* (135 SF Rec & Park playgrounds, embedded as `const PLAYGROUNDS=[…]`). |
| `talks-concerts.html` | **Talks & concerts** page ("Go hear something good."; ticket icon, SF skyline). Same engine, no places, its own tags. Opens on This month · Anytime. |

Both GitHub pages share almost all of their JavaScript. **A change to shared behavior (cards, filters, map, likes, venues, compact controls) usually needs to be made in both `index.html` and `talks-concerts.html`**. Page-specific differences: `PLACES`/`PLAYGROUNDS` (family only), `TAGS` list, default `S.tod`/`S.kind`, `WIN` time windows, kid-focus scoring (family only), localStorage keys (`fdo*` vs `fdo-ct*`), data file names.

### Key client concepts (in each page's main `<script>`)
- `S` — UI state, saved to localStorage (`fdo` / `fdo-ct`). `PREFS` — likes, hides, follows, saved ("Interested"), learned weights (`fdo-prefs` / `fdo-ct-prefs`).
- `items()` builds the ranked list: weather score per spot (`condFor`, exposure `exp`, warmth offset `off`; indoors is a flat 0.58 so a good outdoor day can win) + one-off boost (+0.30, library one-offs +0.10) / regular-program penalty (−0.12) / places (−0.10) + kid focus (family: +0.15 for a kid word or a family-only source in `KIDSRC`, no penalty otherwise) + interests (+0.2) + learned prefs, then filters.
- Nearness: `walkMin()` (straight line × 1.3, 3 mph) sits beside `driveMin()`; events get up to +0.3 (`near`) for a short walk (full within 12 min ≈ 6 blocks, none past 25). Cards say "🚶 N min walk" within 15 min (`distText`). The drive filter has a **Walking distance** option stored as `S.drive=0` (`inRange()`).
- `rankRows()` sorts by score (ties: sooner, then closer) and keeps the top 10 varied: at most 2 events per venue and 3 library events; extras move just below the top 10.
- Talks & concerts differs: indoors scores 0.75 (most of the page is indoors), and events get up to +0.15 for being soon (fading over 14 days) so a month view isn't one big tie.
- `render()` draws list, map (numbered top-10 pins), Coming up, On the horizon, preferences, venues, Interested.
- A second `<script>` at the end of the body builds the **compact controls** (From ✎, When/Time/Drive pills, Filters sheet, weather one-liner, 7-day weather strip `#wkstrip` from `DAYS`: tap a day to view it) by moving existing controls; it wraps `render()` to keep the pills in sync.
  It also builds the **tools row** under the intro (`nav.tools`): 📌 Interested (count; hidden when empty), ⭐ Venues, 👍 Your taste, 📅 Add to calendar, ➕ Suggest an event. Each button moves that section's body (`savedbody`, `venuesbody`, `prefsbody`, `icsfoot`) into one shared sheet (`#msheet`) and back on close; `openVenue` is wrapped to close it first.
- Venue view (`openVenue`), Venues directory, follow/hide venue, 📌 Interested list, 👍/👎 learning.
- 📌 Interested panel opens with a **Next 10 days** timeline (`interestedTimeline`): saves from both pages (`allSaved()` reads the other page's prefs from localStorage), one row per day with weather from `DAYS`, blocks on an 8 am–midnight track colored by page (both = split), linking to `<page>#ev=<id>`. The 📌 Interested tool button counts both pages.
- Planning together: 📤 Share on cards and Coming up / On the horizon rows (`shareEvent`: share sheet or copy) links to `<page>#ev=<id>`; `openDeepLink()` (on load and `hashchange`) opens that event's day with filters wide open and selects it. A partner email saved in the Add to calendar panel (`fdo-partner`, both pages) is added as a guest on every Add to calendar link (`gcal`, `calLabel`).

## Data files

| File | Written by | Notes |
|---|---|---|
| `events.json`, `specials.ics` | refresh script | Family events; `specials.ics` = one-off events feed |
| `talks-concerts.json`, `talks-concerts.ics` | refresh script | Talks & concerts |
| `data/manual.json` | hand | Hand-added family events (kept until they end) |
| `data/manual-talks-concerts.json` | hand | Hand-added talks/concerts |
| `data/watchlist.json` | hand | Venue sites Claude reads weekly (needs `ANTHROPIC_API_KEY`); `edition` = family or talks-concerts |
| `data/calendars.json` | hand | iCal feeds: Luma pages (luma.com/name), SPUR, any .ics |
| `data/venue_pages.json` | hand | Venue pages read directly: `jsonld`, `seetickets`, `squarespace` (an /events page, e.g. Medicine for Nightmares) or `tribe` (WordPress Events Calendar, e.g. Artists' Television Access) |
| `data/venues.json` | hand | Venue overrides matched by event link (e.g. Natural Resources) |
| `data/playgrounds.json` | Phase 0 audit | Source copy of the playground list embedded in `index.html` |
| `data/geocache.json` | refresh script | Saved Nominatim lookups |
| Google Sheet (CSV in var `SUGGESTIONS_CSV`) | visitors via Google Form | Suggestions; `load_suggestions()` keeps rows with Approved = yes. Setup and approving: README "Visitor suggestions". Pages link to the Form via `SUGGEST_URL` (empty hides the link). |

Event fields: `id, kind ("event"|"impact"), source, name, area, lat, lng, start, end, rangeStart/rangeEnd/rangeLabel, indoor, exp (0–1), off (°F), free, note, url, tags, recurring, bookAhead, also`.

`also` = `{page, id}` of the same event on the other page, set by `crosslink()` at the end of the refresh (same start time + one name contains the other, or a shared name word at the same place). Cards show it as a plain label ("Also on Talks & concerts" / "Good with kids too"). 📌 Interested on a cross-listed event is mirrored into the other page's prefs.

## Refresh script: `scripts/refresh_events.py`
Run locally: `pip install requests` then `python scripts/refresh_events.py` (≈10 min; network heavy).
`main()` builds the family page, then `build_talks_concerts()` builds the second page, then `crosslink()` tags events on both; `merge()` dedupes (same id, same start+place, or near-identical names on the same day; earlier lists win).

Family sources: SF Public Library (`scrape_sfpl`, with real program descriptions via `sfpl_note`), Cal Academy, SFMOMA, OMCA, Mission Science Workshop & MAPP (rules), YBG, SF Children & Nature, Mission Local, Galería de la Raza, Local Kiddo SF newsletter (`scrape_localkiddo` + `enrich_events`), Funcheap (key), watchlist (key), manual.
Talks & concerts sources: calendar feeds (The Fold, SPUR), JCCSF, Commonwealth Club, The Independent (TicketWeb ajax), Red Poppy Art House, Community Music Center, MAPP, venue pages (Fillmore, The Chapel, GAMH), YBG, Mission Local/Galería (adult filter), watchlist (key), manual.

Env (repo Settings → Secrets and variables → Actions): secret `ANTHROPIC_API_KEY` (enables Funcheap, Claude screening, watchlist), variable `CONTACT_EMAIL` (Nominatim user-agent), optional `CLAUDE_MODEL`, `SUGGESTIONS_CSV`.

Each source (`scrape_*`, `msw_events`, `mapp_events`, `watch_venues`, `load_suggestions`, plus `build_talks_concerts` and `crosslink`) is wrapped by `_isolated()` at the bottom of the script: a crash logs the traceback, that source returns nothing, and the run continues. `report_failures()` lists skipped sources in the log and on the Actions run summary.

Workflow: `.github/workflows/refresh-and-deploy.yml` — Thu + Sat 7 am Pacific, plus manual runs and redeploy on push.

## Working on it
- Preview locally: `python3 -m http.server 8765` then open http://localhost:8765/ and /talks-concerts.html.
- Quick syntax check for a page's scripts: `node -e "const s=require('fs').readFileSync('index.html','utf8');s.split('<script>').slice(1).forEach(p=>new Function(p.split('</script>')[0]));console.log('ok')"`.
- Prefer browser checks (Playwright) at phone widths (320–414 px) for UI changes; dropdowns must stay on screen.
- Be polite to sources: user-agent with contact email, small delays between page fetches, respect robots.txt.
- UI copy: plain, warm, short. Avoid the "X, not Y" / "it's not X, it's Y" construction.

## Backlog / ideas
- Ticketmaster Discovery API key → The Masonic, Regency, Bill Graham, Chase Center, Fox, Greek, UC Theatre, Paramount, Bimbo's.
- "Include online talks" option (SPUR Digital Discourse, Commonwealth livestreams).
- Playgrounds: add non–Rec & Park playgrounds (Salesforce Park, YBG Children's Garden, Tunnel Tops, Mission Bay); equipment checklist (fenced, shade, swings, sand, water) starting with ~30 near the Mission; Oakland Phase 0.
- Rec & Park "A Place to Play" drop-ins citywide (Bernal Heights Rec Center done by hand in `data/manual.json`).
- Per-venue "last checked" dates in the Venues directory.
