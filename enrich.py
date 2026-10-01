"""Enrichment script for hot leads using the Places API (New) Details endpoint.

Takes place_ids (from hot_leads.csv, either as arguments or via --from-csv)
and pulls the full detail record for each business: contact info, hours,
reviews, photos, editorial summary, and accessibility/parking/payment/amenity
attributes. Results are written to enriched/<business-slug>/.

This calls the Enterprise + Atmosphere tier of the Places Details API
(reviews, photos) intentionally, so a hard cap of 10 detail calls per run is
enforced regardless of how many place_ids are passed in.
"""
import argparse
import csv
import glob
import io
import json
import os
import re
import sys

import requests
from dotenv import load_dotenv
from PIL import Image

from prospecting import load_api_key

DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
PHOTO_MEDIA_URL = "https://places.googleapis.com/v1/{photo_name}/media"

DETAIL_FIELD_MASK = ",".join(
    [
        "id",
        "displayName",
        "formattedAddress",
        "nationalPhoneNumber",
        "websiteUri",
        "primaryTypeDisplayName",
        "primaryType",
        "types",
        "rating",
        "userRatingCount",
        "regularOpeningHours",
        "reviews",
        "photos",
        "editorialSummary",
        "accessibilityOptions",
        "parkingOptions",
        "paymentOptions",
        "outdoorSeating",
        "liveMusic",
        "menuForChildren",
        "servesCoffee",
        "servesCocktails",
        "goodForChildren",
        "goodForGroups",
        "goodForWatchingSports",
        "allowsDogs",
        "restroom",
        "delivery",
        "dineIn",
        "takeout",
        "curbsidePickup",
        "reservable",
    ]
)

AMENITY_FIELDS = [
    "outdoorSeating",
    "liveMusic",
    "menuForChildren",
    "servesCoffee",
    "servesCocktails",
    "goodForChildren",
    "goodForGroups",
    "goodForWatchingSports",
    "allowsDogs",
    "restroom",
    "delivery",
    "dineIn",
    "takeout",
    "curbsidePickup",
    "reservable",
]

MAX_DETAIL_CALLS_PER_RUN = 10
MAX_PHOTOS_PER_BUSINESS = 6
MAX_PHOTO_WIDTH_PX = 1600


class DetailCallBudgetExceeded(Exception):
    pass


class DetailCallCounter:
    def __init__(self, max_calls):
        self.max_calls = max_calls
        self.count = 0

    def use(self):
        if self.count >= self.max_calls:
            raise DetailCallBudgetExceeded(
                f"Refusing to exceed the hard cap of {self.max_calls} detail calls per run."
            )
        self.count += 1
        print(f"[detail call {self.count}/{self.max_calls}]")


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "business"


def fetch_details(place_id, api_key, counter):
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": DETAIL_FIELD_MASK,
    }
    counter.use()
    response = requests.get(
        DETAILS_URL.format(place_id=place_id), headers=headers, timeout=30
    )
    response.raise_for_status()
    return response.json()


def download_photo(photo_name, api_key):
    response = requests.get(
        PHOTO_MEDIA_URL.format(photo_name=photo_name),
        params={"key": api_key, "maxWidthPx": MAX_PHOTO_WIDTH_PX * 2},
        timeout=30,
    )
    response.raise_for_status()
    return response.content


def save_photo(raw_bytes, dest_path):
    image = Image.open(io.BytesIO(raw_bytes))
    if image.mode != "RGB":
        image = image.convert("RGB")
    if image.width > MAX_PHOTO_WIDTH_PX:
        ratio = MAX_PHOTO_WIDTH_PX / image.width
        new_size = (MAX_PHOTO_WIDTH_PX, round(image.height * ratio))
        image = image.resize(new_size, Image.LANCZOS)
    image.save(dest_path, "JPEG", quality=85)
    return image.width, image.height


def build_amenities(place):
    return {field: place[field] for field in AMENITY_FIELDS if field in place}


def build_reviews(place):
    reviews = []
    for review in place.get("reviews", []):
        reviews.append(
            {
                "author": review.get("authorAttribution", {}).get("displayName", ""),
                "rating": review.get("rating"),
                "text": review.get("text", {}).get("text", ""),
                "relative_time": review.get("relativePublishTimeDescription", ""),
                "publish_time": review.get("publishTime", ""),
            }
        )
    return reviews


def build_notes(place, downloaded_photo_count):
    missing = []

    if not place.get("websiteUri"):
        missing.append("No website on file — needs a domain/hosting plan.")
    if not place.get("nationalPhoneNumber"):
        missing.append("No phone number on file — confirm directly with the business.")
    if not place.get("regularOpeningHours"):
        missing.append("No hours listed — confirm hours directly with the owner.")
    if not place.get("editorialSummary", {}).get("text"):
        missing.append("No Google editorial summary — write custom description copy.")
    if not place.get("reviews"):
        missing.append("No reviews returned — consider screenshotting reviews manually.")
    if downloaded_photo_count < MAX_PHOTOS_PER_BUSINESS:
        missing.append(
            f"Only {downloaded_photo_count} Google photo(s) available — "
            "source additional photos (site visit, owner-provided, or a shoot) "
            f"to reach {MAX_PHOTOS_PER_BUSINESS}."
        )
    if not place.get("accessibilityOptions"):
        missing.append("No accessibility data from Google — verify on-site (parking, entrance, restroom, seating).")
    if not place.get("parkingOptions"):
        missing.append("No parking data from Google — confirm parking options on-site.")
    if not place.get("paymentOptions"):
        missing.append("No payment method data from Google — confirm accepted payment types.")

    lines = ["# Manual follow-up notes", ""]
    if missing:
        lines.append("Missing / needs manual sourcing:")
        lines.extend(f"- {item}" for item in missing)
    else:
        lines.append("Nothing missing — full data set came back from Google.")
    lines.append("")
    return "\n".join(lines)


def enrich_place(place_id, api_key, counter, used_slugs):
    place = fetch_details(place_id, api_key, counter)

    name = place.get("displayName", {}).get("text", place_id)
    slug = slugify(name)
    if slug in used_slugs:
        slug = f"{slug}-{place_id[-6:].lower()}"
    used_slugs.add(slug)

    business_dir = os.path.join("enriched", slug)
    photos_dir = os.path.join(business_dir, "photos")
    os.makedirs(photos_dir, exist_ok=True)

    photos_meta = []
    for i, photo in enumerate(place.get("photos", [])[:MAX_PHOTOS_PER_BUSINESS], start=1):
        photo_name = photo.get("name")
        if not photo_name:
            continue
        try:
            raw_bytes = download_photo(photo_name, api_key)
            filename = f"{i:02d}.jpg"
            width, height = save_photo(raw_bytes, os.path.join(photos_dir, filename))
            photos_meta.append(
                {
                    "index": i,
                    "file": f"photos/{filename}",
                    "width": width,
                    "height": height,
                }
            )
        except (requests.RequestException, OSError) as exc:
            print(f"  ! failed to download photo {i} for {name}: {exc}")

    accessibility = place.get("accessibilityOptions", {})
    parking = place.get("parkingOptions", {})
    payment = place.get("paymentOptions", {})
    amenities = build_amenities(place)
    reviews = build_reviews(place)
    hours = place.get("regularOpeningHours")

    amenity_attribute_count = (
        len(accessibility) + len(parking) + len(payment) + len(amenities)
    )

    data = {
        "place_id": place.get("id", place_id),
        "name": name,
        "address": place.get("formattedAddress", ""),
        "phone": place.get("nationalPhoneNumber", ""),
        "website": place.get("websiteUri", ""),
        "primary_type": place.get("primaryTypeDisplayName", {}).get("text", ""),
        "primary_type_id": place.get("primaryType", ""),
        "types": place.get("types", []),
        "rating": place.get("rating"),
        "rating_count": place.get("userRatingCount"),
        "editorial_summary": place.get("editorialSummary", {}).get("text", ""),
        "hours": hours,
        "reviews": reviews,
        "accessibility": accessibility,
        "parking": parking,
        "payment": payment,
        "amenities": amenities,
        "photos": photos_meta,
        "assets_summary": {
            "photo_count": len(photos_meta),
            "review_count": len(reviews),
            "has_hours": hours is not None,
            "amenity_attribute_count": amenity_attribute_count,
        },
    }

    with open(os.path.join(business_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    with open(os.path.join(business_dir, "notes.md"), "w", encoding="utf-8") as f:
        f.write(build_notes(place, len(photos_meta)))

    print(f"\n=== {name} ({slug}) ===")
    print(f"  address: {data['address'] or '(none)'}")
    print(f"  phone: {data['phone'] or '(none)'}")
    print(f"  website: {data['website'] or '(none)'}")
    print(f"  rating: {data['rating']} ({data['rating_count']} reviews)")
    print(f"  hours retrieved: {data['assets_summary']['has_hours']}")
    print(f"  reviews retrieved: {data['assets_summary']['review_count']}")
    print(f"  photos downloaded: {data['assets_summary']['photo_count']}")
    print(f"  amenity attributes returned: {data['assets_summary']['amenity_attribute_count']}")
    print(f"  wrote: {business_dir}/data.json, notes.md, photos/")

    return data


def find_latest_hot_leads_csv(directory="output"):
    candidates = [
        path
        for path in glob.glob(os.path.join(directory, "hot_leads_*.csv"))
        if os.path.basename(path) != "hot_leads_latest.csv"
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def load_place_ids_from_csv(path, top_n):
    place_ids = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "place_id" not in (reader.fieldnames or []):
            print(
                f"'{path}' has no place_id column. Re-run prospecting.py to regenerate it.",
                file=sys.stderr,
            )
            sys.exit(1)
        for row in reader:
            place_id = row.get("place_id", "").strip()
            if place_id:
                place_ids.append(place_id)
            if len(place_ids) >= top_n:
                break
    return place_ids


def main():
    parser = argparse.ArgumentParser(
        description="Enrich hot leads via the Places API Details endpoint."
    )
    parser.add_argument(
        "place_ids",
        nargs="*",
        help="One or more Places API place_ids to enrich.",
    )
    parser.add_argument(
        "--from-csv",
        nargs="?",
        const="__AUTO__",
        default=None,
        metavar="PATH",
        help=(
            "Read place_ids from a hot_leads.csv-style file instead of passing them "
            "as arguments. If PATH is omitted, uses the newest output/hot_leads_*.csv."
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        default=MAX_DETAIL_CALLS_PER_RUN,
        help=f"With --from-csv, how many top rows to read (default: {MAX_DETAIL_CALLS_PER_RUN}).",
    )
    args = parser.parse_args()

    if args.place_ids and args.from_csv:
        print("Pass place_ids as arguments or use --from-csv, not both.", file=sys.stderr)
        sys.exit(1)

    if args.from_csv:
        csv_path = args.from_csv
        if csv_path == "__AUTO__":
            csv_path = find_latest_hot_leads_csv()
            if not csv_path:
                print(
                    "No output/hot_leads_*.csv files found. Run prospecting.py first "
                    "or pass --from-csv PATH.",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(f"Using latest hot leads file: {csv_path}")
        place_ids = load_place_ids_from_csv(csv_path, args.top)
    else:
        place_ids = args.place_ids

    # de-dup, preserve order
    seen = set()
    deduped = []
    for pid in place_ids:
        if pid not in seen:
            seen.add(pid)
            deduped.append(pid)
    place_ids = deduped

    if not place_ids:
        print("No place_ids given. Pass some as arguments or use --from-csv.", file=sys.stderr)
        sys.exit(1)

    if len(place_ids) > MAX_DETAIL_CALLS_PER_RUN:
        print(
            f"{len(place_ids)} place_ids given, but the hard cap is "
            f"{MAX_DETAIL_CALLS_PER_RUN} detail calls per run. "
            f"Only the first {MAX_DETAIL_CALLS_PER_RUN} will be enriched."
        )
        place_ids = place_ids[:MAX_DETAIL_CALLS_PER_RUN]

    load_dotenv()
    api_key = load_api_key()

    os.makedirs("enriched", exist_ok=True)

    counter = DetailCallCounter(MAX_DETAIL_CALLS_PER_RUN)
    used_slugs = set()
    enriched = []

    for place_id in place_ids:
        try:
            enriched.append(enrich_place(place_id, api_key, counter, used_slugs))
        except DetailCallBudgetExceeded as exc:
            print(str(exc))
            break
        except requests.RequestException as exc:
            print(f"! failed to fetch {place_id}: {exc}", file=sys.stderr)

    print()
    print("=== Summary ===")
    print(f"Detail calls used: {counter.count}/{MAX_DETAIL_CALLS_PER_RUN}")
    print(f"Businesses enriched: {len(enriched)}")
    for data in enriched:
        print(f"  - {data['name']}: {data['assets_summary']}")


if __name__ == "__main__":
    main()
