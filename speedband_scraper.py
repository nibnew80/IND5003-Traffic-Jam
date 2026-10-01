import csv
import math
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

CAMERA_COORDS = {
    "2701": (1.451511, 103.769569),
    "2702": (1.444504, 103.767372),
    "4703": (1.350168, 103.634076),
    "4712": (1.341244, 103.643913),
}

# Only keep a Speed Bands row if it's within this distance of its camera.
# Validated on 2026-10-01: every point in the three trusted clusters above
# is within 400m of its centroid, so 500m has margin without being so wide
# it lets in another road's segments.
MAX_DISTANCE_KM = 0.5

# Speed Bands is filtered purely by distance now (see camera_ref_by_distance
# below) — no road-name keyword involved. We tried keyword matching three
# times ("tuas", then "tuas avenue 8", then "ayer rajah expressway") and each
# one turned out to be shared by unrelated roads somewhere else on the
# island (Tuas is an entire industrial district; "aye"/"ayer" is also a
# common Malay word in street names, e.g. Kreta Ayer in Chinatown, ~15km
# away). Since Speed Bands rows carry real coordinates, distance from the
# camera is a more direct and reliable filter than guessing road names.

# Text filter for Estimated Travel Times. This dataset has no lat/lon, so
# MAX_DISTANCE_KM can't apply here — keep this list precise instead. "aye"
# was tried and removed on 2026-10-01: it matched other expressways' segments
# merely because their FarEndPoint/StartPoint/EndPoint text mentioned an AYE
# interchange (e.g. a PIE segment near Changi, ~40km from Tuas, whose
# far_end_point just says "PIE/AYE INTERCHANGE"). "tuas checkpoint" alone
# already captures the real AYE-approaching-Tuas segments cleanly (confirmed
# clean in the original test before "aye" was added) and nothing else has
# ever matched here for Woodlands in any test so far.
TRAVEL_ROAD_CAMERA_MAP = {
    "tuas checkpoint": "4712",
}


#ROAD_CAMERA_MAP = {
 #   "woodlands causeway": "2701",
  #  "causeway": "2702",  # tentative — verify via lat/lon once collected
   # "tuas second crossing": "4703",
    #"aye": "4712",
    #"tuas checkpoint": "4712",
#}

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


def haversine_km(a, b):
    """Great-circle distance in km between two (lat, lon) points."""
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    x = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


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


def camera_ref_for(record, road_map, *fields):
    """Return the camera_id this record's text matched, or None."""
    text = " ".join(str(record.get(f) or "") for f in fields).lower()
    for keyword, camera_id in road_map.items():
        if keyword in text:
            return camera_id
    return None


def camera_ref_by_distance(lat, lon):
    """Return whichever camera this point is within MAX_DISTANCE_KM of
    (the nearest one, if more than one radius somehow overlapped), or None.
    """
    best_cam, best_dist = None, None
    for cam, coord in CAMERA_COORDS.items():
        d = haversine_km(coord, (lat, lon))
        if d <= MAX_DISTANCE_KM and (best_dist is None or d < best_dist):
            best_cam, best_dist = cam, d
    return best_cam


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

    matched = []
    for r in records:
        lat, lon = r.get("StartLat"), r.get("StartLon")
        if not lat or not lon:
            continue
        cam = camera_ref_by_distance(float(lat), float(lon))
        if cam is not None:
            matched.append((r, cam))

if not matched:
        print(f"[{timestamp}] Speed bands: no rows within {MAX_DISTANCE_KM}km of any camera, out of {len(records)} fetched.")
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

    matched = [
        (r, camera_ref_for(r, TRAVEL_ROAD_CAMERA_MAP, "Name", "FarEndPoint", "StartPoint", "EndPoint"))
        for r in records
    ]
    matched = [(r, cam) for r, cam in matched if cam is not None]
    if not matched:
        print(f"[{timestamp}] Travel times: no rows matched {list(TRAVEL_ROAD_CAMERA_MAP)} out of {len(records)} fetched.")
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
