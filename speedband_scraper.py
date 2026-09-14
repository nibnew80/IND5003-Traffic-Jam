import csv
import os
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

LTA_API_KEY = os.environ["LTA_API_KEY"]

# LTA DataMall endpoints. Confirmed working against a live key on 2026-09-14 —
# `v3/TrafficSpeedBands` 404s, `TrafficSpeedBandsv2` is the correct path.
SPEED_BANDS_URL = "https://datamall2.mytransport.sg/ltaodataservice/v4/TrafficSpeedBands"
EST_TRAVEL_TIMES_URL = "https://datamall2.mytransport.sg/ltaodataservice/EstTravelTimes"

# Maps a case-insensitive substring (matched against each record's text
# fields) to the camera it's ground truth for. This both filters (only rows
# matching some key are kept) and tags each row with which camera's vehicle
# counts it should be compared against later.
#
# Confirmed against live data on 2026-09-14:
#   - Speed bands' RoadName for the Tuas crossing is "TUAS SECOND CROSSING"
#     ("tuas" alone is far too broad — Tuas is a whole industrial district
#     with ~90 named roads, e.g. Tuas Avenue 1-20, Tuas South Street 1-15).
#     Woodlands is "WOODLANDS CAUSEWAY" (2701) plus a separately-listed
#     "CAUSEWAY" entry (different LinkID/road_category) tentatively mapped to
#     2702 (checkpoint) — NOT independently confirmed yet, since we haven't
#     been able to search Speed Bands for an explicit "checkpoint" name.
#     Once a run has collected some rows, check the start_lat/start_lon/
#     end_lat/end_lon columns against each camera's known location to verify
#     or correct this mapping.
#   - Estimated Travel Times has no Woodlands/Causeway coverage at all in
#     this data — it only carries "AYE" segments with "TUAS CHECKPOINT" as a
#     waypoint, which is the approach road 4712 (AYE/Tuas Ave 8) watches, so
#     this file will only ever populate for that camera.
#   - "TUAS AVENUE 8" (4712's approach, in Speed Bands) is confirmed real —
#     it showed up in the earlier broad "tuas" test pull.
ROAD_CAMERA_MAP = {
    "woodlands causeway": "2701",
    "causeway": "2702",  # tentative — verify via lat/lon once collected
    "tuas second crossing": "4703",
    "tuas avenue 8": "4712",
    "tuas checkpoint": "4712",
}

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
    "camera_ref",
    "link_id",
    "road_name",
    "road_category",
    "speed_band",
    "min_speed",
    "max_speed",
    "start_lat",
    "start_lon",
    "end_lat",
    "end_lon",
]

TRAVEL_TIMES_CSV = Path("data/estimated_travel_times.csv")
TRAVEL_TIMES_FIELDS = [
    "timestamp",
    "time_bucket_5min",
    "camera_ref",
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


def camera_ref_for(record, *fields):
    """Return the camera_id this record is ground truth for, or None."""
    text = " ".join(str(record.get(f) or "") for f in fields).lower()
    for keyword, camera_id in ROAD_CAMERA_MAP.items():
        if keyword in text:
            return camera_id
    return None


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

    matched = [(r, camera_ref_for(r, "RoadName")) for r in records]
    matched = [(r, cam) for r, cam in matched if cam is not None]
    if not matched:
        print(f"[{timestamp}] Speed bands: no rows matched {list(ROAD_CAMERA_MAP)} out of {len(records)} fetched.")
        return 0

    ensure_csv(SPEED_BANDS_CSV, SPEED_BANDS_FIELDS)
    with open(SPEED_BANDS_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SPEED_BANDS_FIELDS)
        for r, cam in matched:
            writer.writerow(
                {
                    "timestamp": timestamp,
                    "time_bucket_5min": time_bucket,
                    "camera_ref": cam,
                    "link_id": r.get("LinkID"),
                    "road_name": r.get("RoadName"),
                    "road_category": r.get("RoadCategory"),
                    "speed_band": r.get("SpeedBand"),
                    "min_speed": r.get("MinimumSpeed"),
                    "max_speed": r.get("MaximumSpeed"),
                    "start_lat": r.get("StartLat"),
                    "start_lon": r.get("StartLon"),
                    "end_lat": r.get("EndLat"),
                    "end_lon": r.get("EndLon"),
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

    matched = [(r, camera_ref_for(r, "Name", "FarEndPoint", "StartPoint", "EndPoint")) for r in records]
    matched = [(r, cam) for r, cam in matched if cam is not None]
    if not matched:
        print(f"[{timestamp}] Travel times: no rows matched {list(ROAD_CAMERA_MAP)} out of {len(records)} fetched.")
        return 0

    ensure_csv(TRAVEL_TIMES_CSV, TRAVEL_TIMES_FIELDS)
    with open(TRAVEL_TIMES_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRAVEL_TIMES_FIELDS)
        for r, cam in matched:
            writer.writerow(
                {
                    "timestamp": timestamp,
                    "time_bucket_5min": time_bucket,
                    "camera_ref": cam,
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
