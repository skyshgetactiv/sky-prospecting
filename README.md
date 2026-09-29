# Sky Prospecting

A Miami business prospecting scraper built on the Places API (New) Text Search
endpoint. It searches for combinations of business type + zip code, buckets
results by website quality, and writes CSV reports to help identify
"hot leads" — well-reviewed businesses with no website or only a weak one
(Facebook/Instagram page, Linktree, etc.).

## Setup

1. Install dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Add your Google Places API key to `.env` (already gitignored):

   ```
   GOOGLE_PLACES_KEY=your_key_here
   NOTION_TOKEN=your_notion_integration_secret
   ```

   `NOTION_TOKEN` is an internal integration secret (notion.so/profile/integrations).
   Connect that integration to the "Website Prospects" database (database ... menu >
   Connections) or every Notion call returns 404.

3. Edit `config.py` to adjust the list of business types, Miami zip codes,
   pagination limit, and the API call cap.

## Usage

```
python prospecting.py [--limit 20] [--max-pages 2] [--max-calls 60] [--no-notion]
```

- `--limit`: max rows written to `hot_leads.csv` (default 20).
- `--max-pages`: max result pages fetched per query (default from `config.py`).
- `--max-calls`: hard cap on total API calls for the run (default from
  `config.py`, 60). The script refuses to exceed this and will stop issuing
  new requests once the cap is hit, writing out whatever results it has
  collected so far.

- `--no-notion`: skip the Notion push (no token needed).

Each run prints a running API call count and a final summary of how many
places landed in each bucket.

## Output

Four timestamped CSVs are written to `output/` (gitignored) per run:

- `no_website_<timestamp>.csv`
- `weak_website_<timestamp>.csv`
- `has_website_<timestamp>.csv`
- `hot_leads_<timestamp>.csv`

`hot_leads.csv` only includes places with a rating >= 4.5, at least 50
ratings, and a bucket of `no_website` or `weak_website`. It's sorted by
rating count descending, and includes a `priority` column (1 = best lead):

- **1**: no website, 200+ ratings
- **2**: no website under 200 ratings, or weak website with 200+ ratings
- **3**: everything else that still qualifies as a hot lead

## Notion sync

After the CSVs are written, the run's hot leads are pushed to the "Website Prospects"
Notion database (`notion_sync.py`), matched on `place_id`:

- **New place_id**: a row is created with Status = Untouched.
- **Existing row, Status Untouched**: its fields are refreshed; Status is left alone.
- **Existing row, any other Status (including empty)**: left untouched.

| CSV | Notion |
|---|---|
| name | Business |
| phone | Phone |
| address | Address |
| rating | Rating |
| rating_count | Reviews |
| bucket | Web presence (`no_website` → None, `weak_website` → Social only) |
| type (then the search category) | Type (Tattoo / Barbershop / Nail salon / Beauty / Other) |
| place_id | place_id |

The token is checked before any Google calls are made. If the push fails, the CSVs are
already written; retry just the push with `python notion_sync.py output/hot_leads_latest.csv`.

## Notes

- Results are deduplicated by place ID across every type/zip query.
- The `X-Goog-FieldMask` header is restricted to only the fields this tool
  needs, to keep API costs down.
- Because business types x zip codes can generate far more queries than the
  60-call cap allows, a single run will typically stop partway through the
  full combination list. Re-run with a higher `--max-calls` (mind your Google
  Cloud billing) or narrow `config.py` to cover fewer combinations per run.
