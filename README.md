# LAIG Lane 2 Federal Sales Roadmap Dashboard

Interactive federal contractor pipeline intelligence dashboard built by Limitless AI Growth.

## Deploy Options

### Option A — Netlify Drop (30 seconds, no account needed)
1. Go to https://netlify.com/drop
2. Drag this entire folder onto the page
3. You get a public URL instantly (e.g. `random-name.netlify.app`)
4. Optional: rename it to `laig-dashboard.netlify.app` after creating a free account

### Option B — GitHub Pages (free, permanent, brandable URL)
1. Create a free account at https://github.com
2. Create a new repository (e.g. `laig-lane2-dashboard`) — set it to **Public**
3. Upload `index.html` and `.nojekyll` to the repo
4. Go to Settings → Pages → Source: Deploy from branch → Branch: main → / (root)
5. Your URL: `https://yourusername.github.io/laig-lane2-dashboard`

## Files
- `index.html` — The complete self-contained dashboard (HTML + CSS + JS, no dependencies needed)
- `.nojekyll` — Tells GitHub Pages to serve the file as-is
- `README.md` — This file

## Data
All 7 DOT federal forecast opportunities are embedded in the JavaScript at the bottom of index.html.
To swap to a new agency, find the `ALL_OPPS` array and replace the opportunity objects.

## Dashboard Views
1. Overview — KPIs + 4 charts
2. Priority Table — All opportunities ranked by positioning urgency
3. Timeline — Solicitation roadmap FY2026–2027
4. By Department — Filterable card grid
5. NAICS / Type — Code breakdown
6. Recompete Watch — Competitive intelligence view

## Priority Logic
- Position Now: sol date 60–90 days, in market research phase
- Monitor: 90–180 days out
- Recompete Watch: recompete with known incumbent, 180+ days
- Too Early: 180+ days, new requirement
- Ineligible: < 60 days from solicitation (automatically excluded)

## Contact
limitlessaigrowth.com
