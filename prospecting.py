"""Miami business prospecting scraper using the Places API (New) Text Search endpoint.

Finds businesses by type + zip code, buckets them by website quality, and
writes CSV reports highlighting hot leads (no website / weak website, but
well-reviewed).
"""
import argparse
import csv
import os
import shutil
import sys
from datetime import datetime

import requests
from dotenv import load_dotenv

import config
import notion_sync

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.websiteUri",
        "places.primaryTypeDisplayName",
        "places.rating",
        "places.userRatingCount",
        "nextPageToken",
    ]
)

WEAK_WEBSITE_MARKERS = [
    "facebook.com",
    "instagram.com",
    "linktr.ee",
    "linktree",
    "wixsite.com",
    "business.site",
    "godaddysites.com",
]

HOT_LEAD_MIN_RATING = 4.5
HOT_LEAD_MIN_RATING_COUNT = 50


class ApiCallBudgetExceeded(Exception):
    pass


class ApiCallCounter:
    def __init__(self, max_calls):
        self.max_calls = max_calls
        self.count = 0

    def use(self):
        if self.count >= self.max_calls:
            raise ApiCallBudgetExceeded(
                f"Refusing to exceed the hard cap of {self.max_calls} API calls per run."
            )
        self.count += 1
        print(f"[api call {self.count}/{self.max_calls}]")


def load_api_key():
    load_dotenv()
    api_key = os.environ.get("GOOGLE_PLACES_KEY")
    if not api_key:
        print(
            "GOOGLE_PLACES_KEY is not set. Add it to your local .env file.",
            file=sys.stderr,
        )
        sys.exit(1)
    return api_key


def search_text(query, api_key, counter, page_token=None):
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    body = {"textQuery": query}
    if page_token:
        body["pageToken"] = page_token

    counter.use()
    response = requests.post(SEARCH_URL, headers=headers, json=body, timeout=30)
    response.raise_for_status()
    return response.json()


def classify_website(website_uri):
    if not website_uri:
        return "no_website"
    lowered = website_uri.lower()
    for marker in WEAK_WEBSITE_MARKERS:
        if marker in lowered:
            return "weak_website"
    return "has_website"


def score_priority(bucket, rating_count):
    if bucket == "no_website" and rating_count >= 200:
        return 1
    if bucket == "no_website" and rating_count < 200:
        return 2
    if bucket == "weak_website" and rating_count >= 200:
        return 2
    return 3


def run_scrape(api_key, max_pages_per_query, max_api_calls):
    counter = ApiCallCounter(max_api_calls)
    results_by_id = {}

    budget_exhausted = False
    for business_type in config.BUSINESS_TYPES:
        if budget_exhausted:
            break
        for zip_code in config.ZIP_CODES:
            if budget_exhausted:
                break
            query = f"{business_type} in {zip_code}"
            page_token = None
            for page_num in range(max_pages_per_query):
                try:
                    data = search_text(query, api_key, counter, page_token)
                except ApiCallBudgetExceeded as exc:
                    print(str(exc))
                    budget_exhausted = True
                    break

                for place in data.get("places", []):
                    place_id = place.get("id")
                    if not place_id or place_id in results_by_id:
                        continue
                    results_by_id[place_id] = {
                        "place": place,
                        "query": query,
                        "business_type": business_type,
                    }

                page_token = data.get("nextPageToken")
                if not page_token:
                    break

    return results_by_id, counter.count


def build_rows(results_by_id):
    buckets = {"no_website": [], "weak_website": [], "has_website": []}

    for entry in results_by_id.values():
        place = entry["place"]
        name = place.get("displayName", {}).get("text", "")
        address = place.get("formattedAddress", "")
        phone = place.get("nationalPhoneNumber", "")
        website = place.get("websiteUri", "")
        place_type = place.get("primaryTypeDisplayName", {}).get("text", "") or entry["business_type"]
        rating = place.get("rating", "")
        rating_count = place.get("userRatingCount", 0)

        bucket = classify_website(website)

        row = {
            "place_id": entry["place"].get("id", ""),
            "name": name,
            "phone": phone,
            "address": address,
            "type": place_type,
            "website": website,
            "bucket": bucket,
            "rating": rating,
            "rating_count": rating_count,
            "search_type": entry["business_type"],  # not written to CSV; used for the Notion Type
        }
        buckets[bucket].append(row)

    return buckets


def build_hot_leads(buckets, limit):
    candidates = []
    for bucket_name in ("no_website", "weak_website"):
        for row in buckets[bucket_name]:
            rating = row["rating"]
            rating_count = row["rating_count"]
            if rating == "" or rating_count == "":
                continue
            if rating >= HOT_LEAD_MIN_RATING and rating_count >= HOT_LEAD_MIN_RATING_COUNT:
                hot_row = dict(row)
                hot_row["priority"] = score_priority(row["bucket"], rating_count)
                hot_row["notes"] = ""
                candidates.append(hot_row)

    candidates.sort(key=lambda r: r["rating_count"], reverse=True)
    return candidates[:limit]


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description="Miami business prospecting scraper.")
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max rows written to hot_leads.csv (default: 20).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=config.MAX_PAGES_PER_QUERY,
        help="Max result pages per query (default from config.py).",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=config.MAX_API_CALLS_PER_RUN,
        help="Hard cap on total API calls per run (default from config.py).",
    )
    parser.add_argument(
        "--no-notion",
        action="store_true",
        help="Skip pushing hot leads to the Notion 'Website Prospects' database.",
    )
    args = parser.parse_args()

    api_key = load_api_key()
    notion_token = notion_sync.load_token()
    if not args.no_notion and not notion_token:
        print(
            "NOTION_TOKEN is not set. Add it to your local .env file, or pass --no-notion.",
            file=sys.stderr,
        )
        sys.exit(1)

    results_by_id, call_count = run_scrape(api_key, args.max_pages, args.max_calls)

    buckets = build_rows(results_by_id)
    hot_leads = build_hot_leads(buckets, args.limit)

    os.makedirs("output", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    bucket_fieldnames = [
        "place_id",
        "name",
        "phone",
        "address",
        "type",
        "website",
        "bucket",
        "rating",
        "rating_count",
    ]
    hot_lead_fieldnames = [
        "place_id",
        "name",
        "phone",
        "address",
        "type",
        "website",
        "bucket",
        "rating",
        "rating_count",
        "priority",
        "notes",
    ]

    for bucket_name in ("no_website", "weak_website", "has_website"):
        path = os.path.join("output", f"{bucket_name}_{timestamp}.csv")
        write_csv(path, buckets[bucket_name], bucket_fieldnames)

    hot_leads_path = os.path.join("output", f"hot_leads_{timestamp}.csv")
    write_csv(hot_leads_path, hot_leads, hot_lead_fieldnames)

    latest_path = os.path.join("output", "hot_leads_latest.csv")
    shutil.copyfile(hot_leads_path, latest_path)

    print()
    print("=== Summary ===")
    print(f"Total API calls used: {call_count}")
    print(f"Unique places found: {len(results_by_id)}")
    for bucket_name in ("no_website", "weak_website", "has_website"):
        print(f"  {bucket_name}: {len(buckets[bucket_name])}")
    print(f"  hot_leads: {len(hot_leads)}")
    print()
    print(f"CSVs written to output/ with timestamp {timestamp}")
    print(f"Latest hot leads copied to {latest_path}")

    if args.no_notion:
        print("Notion push skipped (--no-notion).")
        return
    print()
    print("=== Notion ===")
    try:
        result = notion_sync.sync_hot_leads(hot_leads, notion_token)
    except (notion_sync.NotionError, requests.RequestException) as exc:
        print(f"Notion push failed: {exc}", file=sys.stderr)
        print(f"CSVs are written. Retry the push without re-scraping: python notion_sync.py {hot_leads_path}",
              file=sys.stderr)
        sys.exit(1)
    notion_sync.print_result(result)


if __name__ == "__main__":
    main()
