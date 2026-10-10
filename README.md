# Where should we go today?

A one-page guide to family outings within a 30-minute drive of any Bay Area starting point. Places and events are ranked by how the weather will feel at each spot, using the live National Weather Service forecast.

## What's in this repo

| Path | What it is |
| --- | --- |
| `index.html` | The whole site: one self-contained page. |
| `events.json` | This week's events. Rebuilt automatically twice a week. |
| `data/manual.json` | Events you add by hand. Kept until they end. |
| `specials.ics` | A calendar feed of one-off events, rebuilt with each refresh. |
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

Every Thursday and Saturday at 7 am Pacific, the workflow:

- reads **SF Public Library** program pages and keeps those for babies, toddlers, preschoolers and families, skipping days the library has announced it's closed;
- reads **Cal Academy**'s daily calendar for penguin feedings, reef dives, the puppet show, storytime and the stingray program, skipping members-only Sunday mornings;
- reads **SF Children & Nature**'s events feed for its "for tots" events;
- with an API key, reads **Funcheap**'s kids and families listings and has Claude filter them;
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
