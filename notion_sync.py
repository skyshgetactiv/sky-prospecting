"""Push hot leads into the "Website Prospects" Notion database.

Rows are matched on place_id:
- no row with that place_id  -> create one, Status = Untouched
- a row whose Status is Untouched -> refresh its fields (Status left as is)
- a row with any other Status (or duplicates where any has moved on) -> left alone

Needs NOTION_TOKEN in .env, and the integration must be connected to the database
(database ... menu > Connections).
"""
import csv
import os
import re
import sys
import time

import requests

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"  # first version with data sources
DATA_SOURCE_ID = "409a5551-12bd-431f-8d9a-59ccb8738ef9"

UNTOUCHED = "Untouched"
WEB_PRESENCE = {"no_website": "None", "weak_website": "Social only", "has_website": "Has site"}

# (pattern, Type option). Google's primary type is checked first, then the search
# category, so a tattoo studio Google files as "Store" still lands on Tattoo.
TYPE_RULES = [
    (r"tattoo", "Tattoo"),
    (r"barber", "Barbershop"),
    (r"nail", "Nail salon"),
    (r"beauty|salon|hair|spa\b|lash|brow|wax", "Beauty"),
]


class NotionError(Exception):
    pass


def map_type(google_type, search_type):
    for text in (google_type, search_type):
        for pattern, option in TYPE_RULES:
            if text and re.search(pattern, text, re.I):
                return option
    return "Other"


def _text(value):
    return {"rich_text": [{"text": {"content": str(value)[:2000]}}]} if value else {"rich_text": []}


def _number(value):
    try:
        return {"number": float(value)} if value not in ("", None) else {"number": None}
    except (TypeError, ValueError):
        return {"number": None}


def lead_properties(lead):
    """Notion properties for a hot-lead row (everything except Status)."""
    return {
        "Business": {"title": [{"text": {"content": lead["name"] or lead["place_id"]}}]},
        "Phone": {"phone_number": lead["phone"] or None},
        "Address": _text(lead["address"]),
        "Rating": _number(lead["rating"]),
        "Reviews": _number(lead["rating_count"]),
        "Web presence": {"select": {"name": WEB_PRESENCE[lead["bucket"]]}},
        "Type": {"select": {"name": map_type(lead.get("type"), lead.get("search_type"))}},
        "place_id": _text(lead["place_id"]),
    }


class NotionClient:
    def __init__(self, token, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })

    def request(self, method, path, body=None, retries=4):
        for attempt in range(retries + 1):
            resp = self.session.request(method, f"{NOTION_API}{path}", json=body, timeout=30)
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < retries:
                    time.sleep(float(resp.headers.get("Retry-After", 2 ** attempt)))
                    continue
            if not resp.ok:
                try:
                    detail = resp.json().get("message", resp.text)
                except ValueError:
                    detail = resp.text
                raise NotionError(f"{method} {path} -> {resp.status_code}: {detail}")
            return resp.json()
        raise NotionError(f"{method} {path} kept failing after {retries} retries")

    def existing_rows(self, data_source_id):
        """place_id -> [(page_id, status)] for every row in the data source."""
        rows, cursor = {}, None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = self.request("POST", f"/data_sources/{data_source_id}/query", body)
            for page in data.get("results", []):
                props = page.get("properties", {})
                pid = "".join(t.get("plain_text", "") for t in props.get("place_id", {}).get("rich_text", [])).strip()
                if not pid:
                    continue
                status = ((props.get("Status") or {}).get("select") or {}).get("name")
                rows.setdefault(pid, []).append((page["id"], status))
            if not data.get("has_more"):
                return rows
            cursor = data.get("next_cursor")

    def create_row(self, data_source_id, properties):
        body = {"parent": {"type": "data_source_id", "data_source_id": data_source_id}, "properties": properties}
        return self.request("POST", "/pages", body)

    def update_row(self, page_id, properties):
        return self.request("PATCH", f"/pages/{page_id}", {"properties": properties})


def sync_hot_leads(leads, token, data_source_id=DATA_SOURCE_ID, session=None):
    """Returns {"created": [...], "updated": [...], "skipped": [(name, reason)]}."""
    client = NotionClient(token, session)
    existing = client.existing_rows(data_source_id)
    result = {"created": [], "updated": [], "skipped": []}
    seen = set()
    for lead in leads:
        pid = lead.get("place_id")
        if not pid:
            result["skipped"].append((lead.get("name", "?"), "no place_id"))
            continue
        if pid in seen:
            result["skipped"].append((lead["name"], "place_id repeated in this batch"))
            continue
        seen.add(pid)
        matches = existing.get(pid, [])
        props = lead_properties(lead)
        if not matches:
            client.create_row(data_source_id, {**props, "Status": {"select": {"name": UNTOUCHED}}})
            result["created"].append(lead["name"])
            continue
        moved_on = [s for _, s in matches if s != UNTOUCHED]
        if moved_on:
            result["skipped"].append((lead["name"], f"Status is {moved_on[0] or 'empty'}"))
            continue
        for page_id, _ in matches:
            client.update_row(page_id, props)
        result["updated"].append(lead["name"])
    return result


def load_token():
    return os.environ.get("NOTION_TOKEN", "").strip()


def print_result(result):
    print(f"  created: {len(result['created'])}")
    for name in result["created"]:
        print(f"    + {name}")
    print(f"  refreshed (still Untouched): {len(result['updated'])}")
    print(f"  left alone: {len(result['skipped'])}")
    for name, reason in result["skipped"]:
        print(f"    - {name}: {reason}")


def main():
    """Push an existing hot_leads CSV: python notion_sync.py output/hot_leads_latest.csv"""
    from dotenv import load_dotenv
    load_dotenv()
    if len(sys.argv) != 2:
        sys.exit("usage: python notion_sync.py <hot_leads.csv>")
    token = load_token()
    if not token:
        sys.exit("NOTION_TOKEN is not set. Add it to your local .env file.")
    with open(sys.argv[1], newline="", encoding="utf-8") as f:
        leads = list(csv.DictReader(f))
    try:
        print_result(sync_hot_leads(leads, token))
    except (NotionError, requests.RequestException) as exc:
        sys.exit(f"Notion push failed: {exc}")


if __name__ == "__main__":
    main()
