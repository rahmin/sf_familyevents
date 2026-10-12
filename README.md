# Where should we go today?

A one-page guide to family outings within a 30-minute drive of any Bay Area starting point. Places and events are ranked by how the weather will feel at each spot, using the live National Weather Service forecast.

## What's in this repo

| Path | What it is |
| --- | --- |
| `index.html` | The whole site: one self-contained page. |
| `events.json` | This week's events. Rebuilt automatically twice a week. |
| `data/manual.json` | Events you add by hand. Kept until they end. |
| `specials.ics` | A calendar feed of one-off events, rebuilt with each refresh. |
| `data/watchlist.json` | Venue websites without a calendar feed, read by Claude each week. Add a venue by copying an entry. |
| `data/venues.json` | Known venues for listings that only name a neighborhood, matched by the event's link. |
| `data/geocache.json` | Saved address lookups, so the refresh doesn't repeat them. |
| `scripts/refresh_events.py` | The script that rebuilds `events.json`. |
| `.github/workflows/refresh-and-deploy.yml` | Runs the refresh on a schedule and publishes the site. |

## Setup (about 10 minutes)

1. **Create a repository** on GitHub. Public repositories get GitHub Pages for free. Upload everything in this folder, keeping the folder structure, including the hidden `.github` folder. If you use the web uploader and `.github` doesn't come through, create `.github/workflows/refresh-and-deploy.yml` with **Add file → Create new file** and paste its contents.
2. **Turn on Pages:** Settings → Pages → under "Build and deployment," set **Source** to **GitHub Actions**.
3. **Add your contact email (recommended):** Settings → Secrets and variables → Actions → **Variables** tab → New variable named `CONTACT_EMAIL`. The refresh script includes it in its requests, so the sites it reads and OpenStreetMap's geocoder know who to contact.
4. **Optional, add Claude curation:** On the **Secrets** tab, add `ANTHROPIC_API_KEY` with a key from console.anthropic.com. With a key, the refresh also reads Funcheap and has Claude drop anything unsuitable for kids under 5 and estimate each location's wind and warmth. It costs pennies per run. To use a different model, add a variable named `CLAUDE_MODEL`; the default is `claude-haiku-5-5`.
5. **First run:** Actions tab → **Refresh events and deploy** → **Run workflow**. When it finishes, the site is at `https://<your-username>.github.io/<repo-name>/`.

## How the refresh works

Regular programs (library storytimes, museum shows) are gathered one week ahead. One-off and ticketed events (MAPP, the Mission Science Workshop's Saturdays, Yerba Buena, Mission Local, and the watchlist venues) are gathered three months ahead, so they appear in the page's "On the horizon" section and in `specials.ics`. To change how far ahead it looks, edit `DAYS_AHEAD` and `HORIZON_DAYS` near the top of `scripts/refresh_events.py`.

Every Thursday and Saturday at 7 am Pacific, the workflow:

- reads **SF Public Library** program pages and keeps those for babies, toddlers, preschoolers and families, skipping days the library has announced it's closed;
- reads **Cal Academy**'s daily calendar for penguin feedings, reef dives, the puppet show, storytime and the stingray program, skipping members-only Sunday mornings;
- reads **SF Children & Nature**'s events feed for its "for tots" events;
- adds the **Mission Science Workshop**'s free drop-in days from the schedule on its Programs page: first Saturday in Bayview, second Saturday in the Mission, third Saturday and Tuesday afternoons in the Excelsior, during the school year;
- adds **MAPP** (Mission Arts & Performance Project) nights, the first Saturday of every even month;
- reads **SFMOMA**'s events page and the **Oakland Museum of California**'s calendar feed, keeping family programs;
- reads the **Yerba Buena Gardens Festival** calendar (free outdoor music, dance and kids' shows, May through October);
- reads **Mission Local**'s neighborhood calendar and **Galería de la Raza**'s calendar feed, keeping family events;
- reads the latest public issues of the **Local Kiddo SF** newsletter (localkiddo.com), taking each pick's date, time, place and link, crediting the newsletter, and skipping library and Cal Academy listings that come in directly;
- with an API key, reads **Funcheap**'s kids and families listings and has Claude filter them;
- with an API key, reads the venue websites listed in `data/watchlist.json` (Hawk Creek Farm, ODC, SF Ballet's family shows, Oakland Ballet, SF Symphony's family concerts and SFJAZZ's family matinees, MoAD, and the Mission's Brava Theater, Community Music Center, Precita Eyes and Loco Bloco) and has Claude pull out upcoming family events;
- adds anything in `data/manual.json` that hasn't ended yet;
- writes `specials.ics`, a calendar feed of one-off events you can subscribe to;
- commits the new files and republishes the site.

You can also run it any time from the Actions tab, or locally with `pip install requests` then `python scripts/refresh_events.py`.

## Adding events by hand

Edit `data/manual.json` on GitHub and commit. The site redeploys on its own. Each event needs an `id`, `name`, `area`, `lat`, `lng`, `start` and `url`. The rest is optional:

- `end`: an ISO time like `2026-10-17T15:00:00-07:00`. If you leave it out, the page assumes two hours.
- `indoor`: `true` or `false`.
- `exp`: wind exposure from 0 (sheltered) to 1 (open bayfront).
- `off`: degrees warmer (+) or cooler (−) than downtown SF.
- `free`: `true` or `false`.
- `note`: one sentence for parents.
- `recurring`: `true` for regular programs, so the "Hide recurring" toggle can hide them. Events that repeat on more than one day, and library storytimes, are detected automatically.

The Exploratorium and 510 Families block automated readers, so add their events this way. Their newsletters are an easy way to hear about them.

Multi-day exhibits can use `rangeStart`, `rangeEnd` and `rangeLabel` instead of times.

To warn about something that affects nearby places, such as an air show snarling traffic, add an entry with `"kind": "impact"`, a `title`, `start`, `end`, and an `impacts` list of place or event ids.

## Things to know

- **Paused schedules:** GitHub pauses scheduled workflows in public repositories after 60 days without activity. If events stop updating, open the Actions tab and re-enable the workflow.
- **Approximate starting points:** Addresses are looked up with OpenStreetMap's free geocoder, and ZIP codes use a built-in list of Bay Area ZIP centers. Starting points are stored only in each visitor's browser.
- **Estimated drive times:** They're based on straight-line distance with light traffic, calibrated against typical drive times from the Mission District. They're usually within a few minutes, but not traffic-aware.
- **Weather adjustments:** The forecast is for downtown San Francisco. Each place's wind exposure and warmth adjustment is an estimate of its usual microclimate.


## Talks & concerts page

`talks-concerts.html` is a second page for grown-up evenings out: concerts, talks, readings, screenings and openings. It uses the same engine as the family page, with its own data (`talks-concerts.json`), its own calendar feed (`talks-concerts.ics`), evening-first time windows, music and ideas tags, and its own likes and settings. The two pages link to each other at the top.

The weekly refresh builds it from:
- the calendar feeds in `data/calendars.json` (any Luma calendar page or .ics feed; The Fold and SPUR to start);
- Commonwealth Club's events page (in-person San Francisco talks), The Independent's show calendar, Red Poppy Art House's calendar feed, Community Music Center's event pages, and MAPP nights;
- concert venue pages in `data/venue_pages.json`, read directly (The Fillmore, The Chapel, Great American Music Hall to start);
- JCCSF Arts & Ideas and Galería de la Raza's calendar feeds, Mission Local's calendar (adult events), and Yerba Buena Gardens' concerts;
- watchlist venues marked `"edition": "talks-concerts"` (City Arts & Lectures, Long Now, The Freight, Gray Area, The Warfield, August Hall, Bottom of the Hill, Yoshi's), which need `ANTHROPIC_API_KEY`;
- anything in `data/manual-talks-concerts.json`.

To follow another venue or organizer that uses Luma, add its Luma page link to `data/calendars.json`.
