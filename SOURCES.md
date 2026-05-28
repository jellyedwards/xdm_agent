# Sources catalogue

Each row is a candidate source for the curator to hunt across. The registry in `src/xdm_agent/tools/sources/registry.py` encodes this data structurally; this doc is the human-readable reference.

## Legend

- **API**: has a JSON API we can query
- **Scrape**: must be HTML-scraped
- **Feed**: has RSS/Atom or "image of the day" page to watch
- **Hotlink OK**: source CDN serves images without referer check
- **License default**: what we can assume without per-image lookup
- **Attribution**: how attribution must be rendered

## Stock — clean APIs, generous licences

| Source | Type | Hotlink | License default | Attribution | Env key | Notes |
|---|---|---|---|---|---|---|
| Unsplash | API | ✓ | Unsplash License (free use, no model release) | "Photo by {creator} on Unsplash" | `UNSPLASH_ACCESS_KEY` | Excellent search; download tracking endpoint exists |
| Pexels | API | ✓ | Pexels License (free) | "Photo by {creator} from Pexels" | `PEXELS_API_KEY` | Good search; per-page paging |
| Pixabay | API | ✓ | Pixabay License (free, similar to CC0) | "Image by {creator} from Pixabay" | `PIXABAY_API_KEY` | High volume, variable quality |
| Freepik | API | ✓ | Mixed — must check per-image flag | "Image by {creator} on Freepik" | `FREEPIK_API_KEY` | Includes premium; filter to free |

## Museum / institutional — public domain or CC

| Source | Type | Hotlink | License default | Attribution | Notes |
|---|---|---|---|---|---|
| Met Museum | API | ✓ | Public Domain (OA collection only) | "{title}, {creator}, The Met" | OA flag in API; respect it |
| Smithsonian | API | ✓ | CC0 / Public Domain (subset) | "{title}, Smithsonian" | Open Access subset only |
| Europeana | API | ✓ | Per-item, normalised to CC types | Europeana's `dataProvider` field | Filter by `REUSABILITY=open` |
| NASA Images | API | ✓ | Public domain | "{title} / NASA" | Hubble, JWST, microscopy, etc. |
| NASA APOD | Feed | ✓ | Public domain | "{title} — Astronomy Picture of the Day" | Daily feed, great signal |
| Wikimedia Commons | API | ✓ | Per-item, mostly CC-BY-SA | Wikimedia template | Huge surface; needs licence parsing |

## Web search — gateways to everything else

| Source | Type | Notes |
|---|---|---|
| Google CSE | API | Image search via Custom Search; configured CSE handles SafeSearch |
| Vertex AI Search | API | Google's grounding-ready search; gives us licence-respectful image search with Google Cloud lineage (good challenge story) |
| SerpAPI | API | Fallback for Google Images-style queries; per-result attribution |
| Brave Search | API | Independent index; helps discover sources Google deprioritises |

For web search results, RightsAgent must do per-page licence detection — these don't carry licence in the search response.

## Competition archives — the highest-quality material

| Source | Type | Notes |
|---|---|---|
| Evident IOTY (Olympus Image of the Year) | Scrape | Already implemented in `xdm_server/ingest_ioty.py` — port the manifest + scrape pattern. Editorial/personal use; hot-link from CDN |
| Nikon Small World | Scrape | Annual photomicrography competition; hot-link from `nikonsmallworld.com` CDN; check robots |
| Wellcome Image Awards | Scrape | Mix of biomedical and editorial; varied licensing — careful lookup needed |
| EPSON Pano Awards | Scrape | Landscape/architectural niche |
| ImagenNation / Astronomy POTY | Scrape | Astrophotography |
| WMM (Wildlife Photographer of the Year) | Scrape | High-quality but tight licensing — surface as inspiration only, no hotlinks unless cleared |

The scraper pattern (per IOTY) is: hand-maintained year landings, parse photo cards into `(year, category, photographer, affiliation, title, image_url)`, store the CDN URL not a re-host, render attribution exactly per source convention.

## Feeds — daily image streams

| Source | URL hint | Notes |
|---|---|---|
| NASA APOD | apod.nasa.gov/apod/ | One image/day, RSS-style |
| NIH NLM Image of the Week | nlm.nih.gov | Biomedical |
| Hubble Picture of the Week | esahubble.org | Weekly |
| JWST Image of the Day | webbtelescope.org | Frequent |
| The Atlantic — In Focus | theatlantic.com/photo/ | Editorial-quality photo essays |

The SchedulerAgent prioritises feeds for "what's new" hunts — they're cheap and almost always have something fresh.

## Tactics × Sources matrix

Different tactics call different sources:

| Tactic | Stock | Museum | Web search | Competition | Feeds |
|---|---|---|---|---|---|
| `prizewinner_archive` | – | – | – | **★** | – |
| `feature_term_query` | ★ | ★ | ★ | ★ | – |
| `adjacent_theme` | ★ | ★ | ★ | – | – |
| `colour_family_query` | ★ | – | ★ | – | – |
| `vintage_archive` | – | **★** | – | – | – |
| `negative_space_probe` | ★ | ★ | ★ | – | – |
| `creator_pursuit` | ★ | – | ★ | ★ | – |
| `temporal_recency` | – | – | – | – | **★** |

★ = primary mapping. ScoutAgent uses this matrix + per-mindset tactic preferences to compose a hunt plan.
