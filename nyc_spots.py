#!/usr/bin/env python3
"""
nyc_spots.py - turn your TikTok saves and likes into a map of NYC bars and restaurants.

Monthly routine:
  1. Download your TikTok data as JSON and unzip it.
  2. python nyc_spots.py path/to/user_data_tiktok.json
  3. Import the new spots_<date>.csv into Google My Maps as a new layer.

Needs two keys as environment variables:
  ANTHROPIC_API_KEY   - from console.anthropic.com
  GOOGLE_MAPS_API_KEY - Google Cloud key with "Places API (New)" enabled

Start with:  python nyc_spots.py export.json --dry-run
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing package. Run:  pip install requests")

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
STATE_FILE = Path("spots_state.json")   # remembers what was already processed
BATCH_SIZE = 20                         # captions per Claude call
NYC_CENTER = (40.7306, -73.9866)
NYC_BOX = (40.49, 40.92, -74.27, -73.68)  # lat min, lat max, lng min, lng max
CONFIRM_OVER = 300                      # ask before processing more new videos than this

FOOD_TYPES = {"restaurant", "bar", "cafe", "bakery", "dessert", "food stall"}

PRICE = {"PRICE_LEVEL_INEXPENSIVE": "$", "PRICE_LEVEL_MODERATE": "$$",
         "PRICE_LEVEL_EXPENSIVE": "$$$", "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$"}


# ---------------------------------------------------------------------------
# 1. Read the TikTok export
# ---------------------------------------------------------------------------
LINK_KEYS = {"link", "videolink", "video_link", "url", "href"}
DATE_KEYS = {"date", "timestamp", "time"}
VIDEO_ID_RE = re.compile(r"(?:/video/|/v/|share/video/)(\d{8,})")


def walk(node, path=()):
    """Yield (path, dict) for every dict in the JSON, so we don't depend on exact key names."""
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from walk(v, path + (str(k),))
    elif isinstance(node, list):
        for item in node:
            yield from walk(item, path)


def classify_section(path):
    """'saved' for Favorite Videos, 'liked' for Like List, None for anything else."""
    for seg in reversed(path):
        s = seg.lower().replace(" ", "").replace("_", "")
        if "favoritevideo" in s or "favouritevideo" in s:
            return "saved"
        if "likelist" in s or "likedvideo" in s:
            return "liked"
    return None


def parse_date(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v).date()
    s = str(v).strip()
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(s[:n], fmt).date()
        except ValueError:
            pass
    return None


def load_tiktok_export(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        sys.exit("That file isn't JSON. Request your TikTok data again and pick JSON, not TXT.")
    items = {}
    for p, d in walk(data):
        link, when = None, None
        for k, v in d.items():
            kl = k.lower()
            if kl in LINK_KEYS and isinstance(v, str) and "tiktok" in v:
                link = v.strip()
            elif kl in DATE_KEYS:
                when = v
        if not link:
            continue
        m = VIDEO_ID_RE.search(link)
        section = classify_section(p)
        if not m or not section:
            continue
        vid = m.group(1)
        if vid in items:
            if section == "saved":
                items[vid]["source"] = "saved"   # saved beats liked
            continue
        d_ = parse_date(when)
        items[vid] = {"id": vid, "link": link, "source": section,
                      "date": d_.isoformat() if d_ else ""}
    return list(items.values())


# ---------------------------------------------------------------------------
# 2. Get each video's caption, creator and location tag (from the video page)
# ---------------------------------------------------------------------------
DATA_RE = re.compile(r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)


def _unescape(s):
    try:
        return json.loads('"' + s + '"')
    except ValueError:
        return s


def parse_video_page(html):
    """Returns {"caption", "author", "location_tag"} or None if the video isn't there."""
    m = DATA_RE.search(html)
    if m:
        try:
            data = json.loads(m.group(1))
            item = data["__DEFAULT_SCOPE__"]["webapp.video-detail"]["itemInfo"]["itemStruct"]
            poi = item.get("poi") or {}
            tag = ", ".join(str(x) for x in (poi.get("name"), poi.get("address"),
                                              poi.get("city")) if x)
            return {"caption": item.get("desc") or "",
                    "author": (item.get("author") or {}).get("uniqueId", ""),
                    "location_tag": tag}
        except (ValueError, KeyError, TypeError):
            pass
    # fallback: pull the fields straight out of the page text
    d = re.search(r'"desc"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    u = re.search(r'"uniqueId"\s*:\s*"([^"]+)"', html)
    if not d and not u:
        return None
    p = re.search(r'"poi"\s*:\s*\{[^{}]*?"name"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    return {"caption": _unescape(d.group(1)) if d else "",
            "author": u.group(1) if u else "",
            "location_tag": _unescape(p.group(1)) if p else ""}


def fetch_caption(item, session):
    try:
        r = session.get(f"https://www.tiktok.com/@/video/{item['id']}", timeout=20)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    return parse_video_page(r.text)


# ---------------------------------------------------------------------------
# 3. Claude: is it a place? which one?
# ---------------------------------------------------------------------------
SYSTEM = ("You extract bar and restaurant recommendations from TikTok captions "
          "for someone who lives in New York City. You never invent place names.")

INSTRUCTIONS = """For each TikTok video below, find any specific food or drink places it recommends
(restaurants, bars, cafes, bakeries, dessert spots, food stalls).

Call the record_places tool with one entry per video, in this shape:
[{"id": "<video id>",
  "places": [{"name": "<exact place name>",
              "neighborhood": "<e.g. West Village, Williamsburg, or null>",
              "in_nyc": true | false | null,
              "type": "restaurant" | "bar" | "cafe" | "bakery" | "dessert" | "food stall",
              "vibe": "<2-4 words, e.g. date night, rooftop, cheap eats>",
              "confidence": "high" | "medium" | "low"}],
  "maybe_place": true | false}]

Rules:
- Food and drink places ONLY. Theaters, cinemas, museums, shops, salons, gyms, hotels,
  parks and other non-food places are never included, even if they're in NYC.
- "location_tag" is the place the creator tagged on the video. It's the most reliable clue:
  if it's a food or drink place, include it.
- Otherwise only include a place if its name is actually in the caption, the location tag
  or the creator name. Never guess.
- in_nyc: true if the caption says NYC or a NYC neighborhood/borough, false if it names
  another city, null if unclear.
- If the video seems to be about a food/drink spot but the name isn't written anywhere,
  return "places": [] and "maybe_place": true.
- Videos that aren't about food or drink places: "places": [], "maybe_place": false.

Videos:
"""


def parse_json_array(text):
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("No JSON array in Claude's reply")
    return json.loads(text[start:end + 1])


# Tool use: Claude fills in this form, so the answer always comes back as valid data
PLACES_TOOL = {
    "name": "record_places",
    "description": "Record the food and drink places found in each video.",
    "input_schema": {
        "type": "object",
        "properties": {"videos": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "places": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "neighborhood": {"type": ["string", "null"]},
                        "in_nyc": {"type": ["boolean", "null"]},
                        "type": {"type": "string", "enum": sorted(FOOD_TYPES)},
                        "vibe": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]}},
                    "required": ["name", "type", "confidence"]}},
                "maybe_place": {"type": "boolean"}},
            "required": ["id", "places", "maybe_place"]}}},
        "required": ["videos"]}}


def _split(batch, api_key):
    mid = len(batch) // 2
    return call_claude(batch[:mid], api_key) + call_claude(batch[mid:], api_key)


def call_claude(batch, api_key):
    videos = [{"id": v["id"], "creator": v.get("author", ""), "caption": v.get("caption", ""),
               "location_tag": v.get("location_tag", "")} for v in batch]
    body = {"model": CLAUDE_MODEL, "max_tokens": 8192, "system": SYSTEM,
            "tools": [PLACES_TOOL],
            "tool_choice": {"type": "tool", "name": "record_places"},
            "messages": [{"role": "user",
                          "content": INSTRUCTIONS + json.dumps(videos, ensure_ascii=False, indent=1)}]}
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    for attempt in range(5):
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers,
                          json=body, timeout=180)
        if r.status_code in (429, 500, 529):
            time.sleep(2 ** attempt * 3)
            continue
        if r.status_code != 200:
            sys.exit(f"Claude API error {r.status_code}: {r.text[:300]}")
        j = r.json()
        if j.get("stop_reason") == "max_tokens" and len(batch) > 1:
            return _split(batch, api_key)          # answer got cut off: do half at a time
        for block in j.get("content", []):
            if block.get("type") == "tool_use":
                vids = (block.get("input") or {}).get("videos")
                if isinstance(vids, list):
                    return vids
        text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
        try:
            return parse_json_array(text)
        except ValueError:
            if len(batch) > 1:
                return _split(batch, api_key)
            print(f"  Couldn't read Claude's answer for video {batch[0]['id']}; skipping it.")
            return []
    sys.exit("Claude API kept returning busy errors. Try again later; progress is saved.")


# ---------------------------------------------------------------------------
# 4. Google Places: exact address + Maps link
# ---------------------------------------------------------------------------
FIELDS = ("places.id,places.displayName,places.formattedAddress,places.location,"
          "places.googleMapsUri,places.primaryTypeDisplayName,places.rating,"
          "places.priceLevel,places.businessStatus")


def in_nyc_box(lat, lng):
    return NYC_BOX[0] <= lat <= NYC_BOX[1] and NYC_BOX[2] <= lng <= NYC_BOX[3]


def find_place(place, api_key, session):
    query = ", ".join(x for x in (place.get("name"), place.get("neighborhood"),
                                  "New York, NY") if x)
    body = {"textQuery": query,
            "locationBias": {"circle": {"center": {"latitude": NYC_CENTER[0],
                                                   "longitude": NYC_CENTER[1]},
                                        "radius": 30000.0}}}
    r = session.post("https://places.googleapis.com/v1/places:searchText",
                     headers={"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELDS},
                     json=body, timeout=20)
    if r.status_code != 200:
        sys.exit(f"Google Places error {r.status_code}: {r.text[:300]}")
    results = r.json().get("places", [])
    if not results:
        return None
    p = results[0]
    loc = p.get("location", {})
    return {"place_id": p.get("id"),
            "maps_name": p.get("displayName", {}).get("text", ""),
            "address": p.get("formattedAddress", ""),
            "lat": loc.get("latitude"), "lng": loc.get("longitude"),
            "maps_url": p.get("googleMapsUri", ""),
            "google_type": p.get("primaryTypeDisplayName", {}).get("text", ""),
            "rating": p.get("rating", ""),
            "price": PRICE.get(p.get("priceLevel", ""), ""),
            "status": p.get("businessStatus", "")}


# ---------------------------------------------------------------------------
# State (so each month only new videos cost anything)
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"videos": {}, "places": {}}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
SPOT_COLS = ["Name", "Type", "Vibe", "Neighborhood", "Address", "Latitude", "Longitude",
             "Rating", "Price", "Saved or liked", "Date", "TikTok", "Google Maps"]
REVIEW_COLS = ["Reason", "Place name (from caption)", "Caption", "Creator", "Saved or liked",
               "Date", "TikTok", "Google Maps"]


def write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def spot_row(s):
    return {"Name": s["maps_name"] or s["name"], "Type": s["type"], "Vibe": s["vibe"],
            "Neighborhood": s["neighborhood"], "Address": s["address"],
            "Latitude": s["lat"], "Longitude": s["lng"], "Rating": s["rating"],
            "Price": s["price"], "Saved or liked": s["source"], "Date": s["date"],
            "TikTok": s["tiktok"], "Google Maps": s["maps_url"]}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="TikTok saves/likes -> NYC spots map")
    ap.add_argument("export", help="TikTok export JSON (user_data_tiktok.json)")
    ap.add_argument("--dry-run", action="store_true",
                    help="only read the export and show counts; no API calls")
    ap.add_argument("--saved-only", action="store_true", help="skip liked videos")
    ap.add_argument("--since", help="only videos saved/liked on or after YYYY-MM-DD")
    ap.add_argument("--limit", type=int, help="process at most N new videos (good for a test)")
    ap.add_argument("--yes", action="store_true", help="don't ask before big runs")
    args = ap.parse_args()

    items = load_tiktok_export(args.export)
    if args.saved_only:
        items = [i for i in items if i["source"] == "saved"]
    if args.since:
        since = date.fromisoformat(args.since)
        items = [i for i in items if i["date"] and date.fromisoformat(i["date"]) >= since]
    # saved first, then liked; newest first within each
    items.sort(key=lambda i: i["date"] or "", reverse=True)
    items.sort(key=lambda i: i["source"] != "saved")

    state = load_state()
    # videos read by the first version (blank captions) get redone automatically
    for v in state["videos"].values():
        if "location_tag" not in v and v.get("stage") == "done" and not v.get("caption"):
            for k in ("stage", "caption", "author", "extracted"):
                v.pop(k, None)
    new = [i for i in items if state["videos"].get(i["id"], {}).get("stage") != "done"]
    if args.limit:
        new = new[:args.limit]

    n_saved = sum(i["source"] == "saved" for i in items)
    print(f"Export: {n_saved} saved + {len(items) - n_saved} liked videos "
          f"(after filters). Not processed yet: {len(new)}.")
    if args.dry_run:
        dates = sorted(i["date"] for i in items if i["date"])
        if dates:
            print(f"Date range: {dates[0]} to {dates[-1]}")
        for i in new[:5]:
            print(f"  {i['source']:5}  {i['date'] or '?':10}  {i['link']}")
        print("Dry run only - nothing was sent anywhere.")
        return
    if not new:
        state["places"] = {k: s for k, s in state["places"].items()
                           if s.get("type") in FOOD_TYPES}
        save_state(state)
        write_csv("all_spots.csv", SPOT_COLS, [spot_row(s) for s in state["places"].values()])
        print(f"Nothing new since last run. all_spots.csv has {len(state['places'])} spots.")
        return

    claude_key = os.environ.get("ANTHROPIC_API_KEY")
    google_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not claude_key or not google_key:
        sys.exit("Set ANTHROPIC_API_KEY and GOOGLE_MAPS_API_KEY first (see README).")
    if len(new) > CONFIRM_OVER and not args.yes:
        ans = input(f"About to process {len(new)} videos. Continue? [y/N] ")
        if ans.strip().lower() != "y":
            sys.exit("Stopped. Tip: use --since or --saved-only to narrow it down.")

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (nyc_spots personal script)"
    for i in new:
        state["videos"].setdefault(i["id"], {}).update(i)
    vids = [state["videos"][i["id"]] for i in new]

    # Step 2: captions
    todo = [v for v in vids if "caption" not in v and v.get("stage") != "no_caption"]
    failed_in_a_row = []
    for n, v in enumerate(todo, 1):
        cap = fetch_caption(v, session)
        if cap is None:
            failed_in_a_row.append(v)
            if len(failed_in_a_row) >= 10:
                print("TikTok stopped answering (probably rate-limited). Progress is saved;"
                      " run the same command again in an hour to continue.")
                failed_in_a_row = []   # don't mark these as unreadable
                break
        else:
            for f in failed_in_a_row:   # isolated failures = deleted/private videos
                f["stage"] = "no_caption"
            failed_in_a_row = []
            v.update(cap)
        if n % 25 == 0 or n == len(todo):
            print(f"Captions: {n}/{len(todo)}")
            save_state(state)
        time.sleep(0.6)
    for f in failed_in_a_row:
        f["stage"] = "no_caption"
    save_state(state)

    # Step 3: Claude
    todo = [v for v in vids if "caption" in v and "extracted" not in v]
    for b in range(0, len(todo), BATCH_SIZE):
        batch = todo[b:b + BATCH_SIZE]
        result = {str(r.get("id")): r for r in call_claude(batch, claude_key)}
        for v in batch:
            r = result.get(v["id"], {"places": [], "maybe_place": False})
            v["extracted"] = {"places": r.get("places") or [],
                              "maybe_place": bool(r.get("maybe_place"))}
        save_state(state)
        print(f"Claude: {min(b + BATCH_SIZE, len(todo))}/{len(todo)} videos read")

    # Step 4: Google Places
    new_spots, review = [], []
    for v in vids:
        base = {"Saved or liked": v["source"], "Date": v.get("date", ""), "TikTok": v["link"],
                "Caption": (v.get("caption") or "")[:300], "Creator": v.get("author", "")}
        if "caption" not in v and v.get("stage") != "no_caption":
            continue  # not fetched yet (TikTok paused us) - next run picks it up
        if v.get("stage") == "no_caption":
            if v["source"] == "saved":
                review.append({**base, "Reason": "Couldn't read caption (deleted or private?)"})
            v["stage"] = "done"
            continue
        ex = v.get("extracted", {})
        if not ex.get("places") and ex.get("maybe_place"):
            review.append({**base, "Reason": "Food/bar video, name not in caption"})
        for pl in ex.get("places", []):
            if pl.get("in_nyc") is False or pl.get("type") not in FOOD_TYPES:
                continue
            if "lookup" not in pl:
                pl["lookup"] = find_place(pl, google_key, session)
                time.sleep(0.1)
            lk = pl["lookup"]
            name = pl.get("name", "")
            if not lk or lk["lat"] is None or not in_nyc_box(lk["lat"], lk["lng"]):
                if pl.get("in_nyc"):
                    review.append({**base, "Reason": "Not found on Google Maps",
                                   "Place name (from caption)": name})
                continue
            if lk["status"] == "CLOSED_PERMANENTLY":
                review.append({**base, "Reason": "Permanently closed",
                               "Place name (from caption)": name, "Google Maps": lk["maps_url"]})
                continue
            if pl.get("confidence") == "low":
                review.append({**base, "Reason": "Low confidence - double check",
                               "Place name (from caption)": name, "Google Maps": lk["maps_url"]})
                continue
            if lk["place_id"] in state["places"]:
                continue  # already on your map
            spot = {**lk, "name": name, "type": pl.get("type", ""), "vibe": pl.get("vibe", ""),
                    "neighborhood": pl.get("neighborhood") or "", "source": v["source"],
                    "date": v.get("date", ""), "tiktok": v["link"]}
            state["places"][lk["place_id"]] = spot
            new_spots.append(spot)
        v["stage"] = "done"
    # drop anything that isn't food/drink (e.g. a theater saved by an older version)
    state["places"] = {k: s for k, s in state["places"].items() if s.get("type") in FOOD_TYPES}
    save_state(state)

    stamp = date.today().isoformat()
    if new_spots:
        write_csv(f"spots_{stamp}.csv", SPOT_COLS, [spot_row(s) for s in new_spots])
    if review:
        write_csv(f"needs_review_{stamp}.csv", REVIEW_COLS, review)
    write_csv("all_spots.csv", SPOT_COLS, [spot_row(s) for s in state["places"].values()])

    print(f"\nDone. {len(new_spots)} new NYC spots -> spots_{stamp}.csv"
          if new_spots else "\nDone. No new NYC spots this time.")
    if review:
        print(f"{len(review)} to check by hand -> needs_review_{stamp}.csv")
    print(f"Everything so far ({len(state['places'])} spots) -> all_spots.csv")


if __name__ == "__main__":
    main()
