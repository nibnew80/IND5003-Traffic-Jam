import csv
import os
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

LTA_API_KEY = os.environ["LTA_API_KEY"]

# LTA DataMall endpoints. LTA has used both a `TrafficSpeedBandsv2` path and a
# newer `v3/TrafficSpeedBands` path for the speed bands dataset over time —
# if either URL below 404s, check the current DataMall API docs and swap the
# path here.
SPEED_BANDS_URL = "https://datamall2.mytransport.sg/ltaodataservice/v4/TrafficSpeedBands"
EST_TRAVEL_TIMES_URL = "https://datamall2.mytransport.sg/ltaodataservice/EstTravelTimes"

# Case-insensitive substrings matched against each record's text fields, to
# keep only rows relevant to our two crossings (Woodlands Causeway, Tuas
# Second Link). Check the "No rows matched" / matched-count log lines on the
# first real run — Estimated Travel Times in particular only covers named
# expressways, so it may or may not carry rows for the checkpoint approach
# roads at all; if it logs zero matches every run, that confirms it doesn't
# and the speed bands file is the one to rely on.
ROAD_KEYWORDS = ["woodlands causeway", "woodlands crossing", "tuas checkpoint viaduct"]

HEADERS = {"AccountKey": LTA_API_KEY, "accept": "application/json"}
TIMEOUT = 30
SGT = timezone(timedelta(hours=8))

# LOOP_COUNT=1 (default) behaves like a single run. The workflow sets
# LOOP_COUNT=71, LOOP_INTERVAL=300 to mirror the image scraper's ~6-hour,
# 5-minute-cadence loop.
LOOP_COUNT = int(os.environ.get("LOOP_COUNT", "1"))
LOOP_INTERVAL = int(os.environ.get("LOOP_INTERVAL", "300"))

SPEED_BANDS_CSV = Path("data/speed_bands.csv")
SPEED_BANDS_FIELDS = [
    "timestamp",
    "time_bucket_5min",
    "link_id",
    "road_name",
    "road_category",
    "speed_band",
    "min_speed",
    "max_speed",
]

TRAVEL_TIMES_CSV = Path("data/estimated_travel_times.csv")
TRAVEL_TIMES_FIELDS = [
    "timestamp",
    "time_bucket_5min",
    "name",
    "direction",
    "far_end_point",
    "start_point",
    "end_point",
    "est_time_min",
]

# Commit+push every N iterations (12 * 5min ~= hourly) instead of every
# iteration, so git history isn't flooded with hundreds of tiny commits a
# day, while still checkpointing often enough that a crash mid-run loses at
# most about an hour of rows rather than the whole ~6-hour job.
COMMIT_EVERY = 12


def floor_to_5min(dt):
    """Round a datetime down to the start of its 5-minute bucket.

    This is the join key for lining these rows up with the image scraper's
    output later: that scraper's own loop timing drifts over a run (each
    iteration takes a little longer than 5 minutes once network calls are
    counted), so exact string-matching two independently-timed loops'
    timestamps isn't reliable — flooring both sides to the same 5-minute
    grid is. When you build the analysis step, parse each image filename's
    timestamp the same way (floor to 5 minutes) before joining against
    `time_bucket_5min` here.
    """
    floored_minute = (dt.minute // 5) * 5
    return dt.replace(minute=floored_minute, second=0, microsecond=0)


def fetch_all(url):
    """LTA DataMall paginates datasets at 500 records/page via $skip."""
    records = []
    skip = 0
    while True:
        resp = requests.get(url, headers=HEADERS, params={"$skip": skip}, timeout=TIMEOUT)
        resp.raise_for_status()
        batch = resp.json().get("value", [])
        if not batch:
            break
        records.extend(batch)
        skip += 500
    return records


def matches_keywords(record, *fields):
    text = " ".join(str(record.get(f) or "") for f in fields).lower()
    return any(kw in text for kw in ROAD_KEYWORDS)


def ensure_csv(path, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with open(path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()


def collect_speed_bands(timestamp, time_bucket):
    try:
        records = fetch_all(SPEED_BANDS_URL)
    except Exception as e:
        print(f"[{timestamp}] Speed bands request failed: {e}")
        return 0

    matched = [r for r in records if matches_keywords(r, "RoadName")]
    if not matched:
        print(f"[{timestamp}] Speed bands: no rows matched {ROAD_KEYWORDS} out of {len(records)} fetched.")
        return 0

    ensure_csv(SPEED_BANDS_CSV, SPEED_BANDS_FIELDS)
    with open(SPEED_BANDS_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SPEED_BANDS_FIELDS)
        for r in matched:
            writer.writerow(
                {
                    "timestamp": timestamp,
                    "time_bucket_5min": time_bucket,
                    "link_id": r.get("LinkID"),
                    "road_name": r.get("RoadName"),
                    "road_category": r.get("RoadCategory"),
                    "speed_band": r.get("SpeedBand"),
                    "min_speed": r.get("MinimumSpeed"),
                    "max_speed": r.get("MaximumSpeed"),
                }
            )
    print(f"[{timestamp}] Speed bands: logged {len(matched)} matched rows.")
    return len(matched)


def collect_travel_times(timestamp, time_bucket):
    try:
        records = fetch_all(EST_TRAVEL_TIMES_URL)
    except Exception as e:
        print(f"[{timestamp}] Estimated travel times request failed: {e}")
        return 0

    matched = [r for r in records if matches_keywords(r, "Name", "FarEndPoint", "StartPoint", "EndPoint")]
    if not matched:
        print(f"[{timestamp}] Travel times: no rows matched {ROAD_KEYWORDS} out of {len(records)} fetched.")
        return 0

    ensure_csv(TRAVEL_TIMES_CSV, TRAVEL_TIMES_FIELDS)
    with open(TRAVEL_TIMES_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRAVEL_TIMES_FIELDS)
        for r in matched:
            writer.writerow(
                {
                    "timestamp": timestamp,
                    "time_bucket_5min": time_bucket,
                    "name": r.get("Name"),
                    "direction": r.get("Direction"),
                    "far_end_point": r.get("FarEndPoint"),
                    "start_point": r.get("StartPoint"),
                    "end_point": r.get("EndPoint"),
                    "est_time_min": r.get("EstTime"),
                }
            )
    print(f"[{timestamp}] Travel times: logged {len(matched)} matched rows.")
    return len(matched)


def collect_once():
    now = datetime.now(SGT)
    timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    time_bucket = floor_to_5min(now).strftime("%Y-%m-%d_%H-%M-%S")
    collect_speed_bands(timestamp, time_bucket)
    collect_travel_times(timestamp, time_bucket)


def git_commit_and_push(message):
    try:
        subprocess.run(["git", "add", str(SPEED_BANDS_CSV), str(TRAVEL_TIMES_CSV)], check=True)
        result = subprocess.run(["git", "commit", "-m", message], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Nothing new to commit ({(result.stdout or result.stderr).strip()})")
            return
        subprocess.run(["git", "push"], check=True)
        print(f"Committed and pushed: {message}")
    except subprocess.CalledProcessError as e:
        print(f"Git commit/push failed: {e}")


if __name__ == "__main__":
    for i in range(LOOP_COUNT):
        collect_once()
        if (i + 1) % COMMIT_EVERY == 0 or i == LOOP_COUNT - 1:
            git_commit_and_push(
                f"Update speed band / travel time data ({datetime.now(SGT).strftime('%Y-%m-%d %H:%M')} SGT)"
            )
        if i < LOOP_COUNT - 1:
            time.sleep(LOOP_INTERVAL)
