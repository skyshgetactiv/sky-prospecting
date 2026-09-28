# Workflow: scrape → pick leads → enrich → site

End-to-end pipeline for turning Miami business search results into a
ready-to-hand-off data package for `site-template-static`.

## 0. One-time setup

```
pip install -r requirements.txt
```

Add your key to `.env` (already gitignored):

```
GOOGLE_PLACES_KEY=your_key_here
```

Edit `config.py` to set the business types and zip codes you want to search.

## 1. Scrape

Run the Text Search scraper. It searches every business type × zip code
combination in `config.py`, buckets results by website quality, and writes
timestamped CSVs to `output/` (gitignored).

```
python prospecting.py --limit 20 --max-pages 1 --max-calls 60
```

- `--limit`: max rows in `hot_leads_<timestamp>.csv` (default 20).
- `--max-pages`: result pages per query (default from `config.py`).
- `--max-calls`: hard cap on Text Search calls this run (default from
  `config.py`).

This is billed at the standard Text Search tier. Mind the call cap if you
widen `config.py`.

## 2. Pick leads

Open `output/hot_leads_<timestamp>.csv`. It's sorted by rating count
descending, with a `priority` column (1 = best: no website, well-reviewed).
Each row has a `place_id` — that's what step 3 needs.

Manually decide which leads to pursue, or just take the top N as-is.

## 3. Enrich

Run the Details-endpoint enrichment script on the leads you picked. This
calls the Enterprise + Atmosphere tier of the Places API (reviews, photos,
accessibility/parking/payment/amenity attributes) — intentionally pricier
than step 1, which is why there's a hard cap of 10 detail calls per run.

Pull the top 10 rows straight from a hot_leads CSV:

```
python enrich.py --from-csv output/hot_leads_20260922_182617.csv --top 10
```

Or enrich specific businesses by place_id:

```
python enrich.py ChIJ...abc ChIJ...def
```

For each business this writes `enriched/<business-slug>/`:

- `data.json` — structured fields (contact info, hours, reviews, editorial
  summary, accessibility/parking/payment/amenities) plus a computed
  `assets_summary` (photo count, review count, whether hours exist, how many
  amenity attributes came back). `site-template-static` reads this summary
  to decide page layout.
- `photos/` — up to 6 Google photos, downloaded and resized to 1600px max
  width, saved as `01.jpg`…`06.jpg`.
- `notes.md` — what's missing from Google's data and needs to be sourced
  manually (photos, hours, accessibility details, etc.).

Review each `notes.md` before handing off — that's the manual work list.

## 4. Hand off to site-template-static

Copy or point `site-template-static` at the `enriched/<business-slug>/`
folders it needs to build from. Each folder is self-contained: `data.json`
for content and layout decisions, `photos/` for images, `notes.md` as the
punch list of anything to fill in by hand before publishing.
