import csv
import math
import os
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

LTA_API_KEY = os.environ["LTA_API_KEY"]

# LTA DataMall endpoints. `v3/TrafficSpeedBands` 404'd on 2026-09-14;
# `TrafficSpeedBandsv2` worked then. `v4/TrafficSpeedBands` is the path given
# in LTA's official DataMall API User Guide and was confirmed working
# against a live key on 2026-10-08 — using that going forward.
SPEED_BANDS_URL = "https://datamall2.mytransport.sg/ltaodataservice/v4/TrafficSpeedBands"
EST_TRAVEL_TIMES_URL = "https://datamall2.mytransport.sg/ltaodataservice/EstTravelTimes"

# --- Camera coordinates ---------------------------------------------------
# 4712 confirmed by the team (looked up externally) on 2026-10-01.
# 2701/2702/4703 are centroids of their own already-trusted matched clusters
# (those three never showed the over-match problem 4712 did) — not
# independently confirmed camera positions, but validated: every point in
# each of those clusters sits within ~400m of its own centroid, so this is a
# safe stand-in until real coordinates are available.
# 4713 added 2026-10-08 — sits 404m from 4703 and 1073m from 4712, close
# enough to 4703 that their 500m radii overlap somewhat, but camera_ref_by_
# distance() always picks whichever is nearer per segment, so this splits
# sensibly rather than double-counting.
CAMERA_COORDS = {
    "2701": (1.451511, 103.769569),
    "2702": (1.444504, 103.767372),
    "4703": (1.350168, 103.634076),
    "4712": (1.341244, 103.643913),
    "4713": (1.347645829, 103.6366955),
}
# Only keep a Speed Bands row if it's within this distance of its camera.
# Validated on 2026-10-01: every point in the three trusted clusters above
# is within 400m of its centroid, so 500m has margin without being so wide
# it lets in another road's segments.
MAX_DISTANCE_KM = 0.5

# --- Direction -------------------------------------------------------------
# Some cameras (e.g. 2702, a wide checkpoint shot) show both directions of
# traffic in one frame, so a single blended ground-truth number per camera
# would wash out a jam happening on only one side. Each Speed Bands LinkID
# is itself already one direction only (LTA splits each carriageway into its
# own LinkID) — we just weren't labeling which. To label it without assuming
# a fixed compass axis (checked on 2026-10-08: Woodlands' two cameras differ
# mainly in latitude, Tuas' differ in both lat AND lon — the corridors aren't
# oriented the same way, so "latitude increasing = towards Malaysia" would
# only work for one of the two crossings), each crossing gets a border-side
# anchor point: whichever of its own cameras sits literally on/at the border
# crossing. A segment's direction is then whichever of its start/end point is
# closer to that anchor — closer at the end = heading toward Malaysia,
# closer at the start = heading into Singapore.
BORDER_ANCHOR = {
    "2701": CAMERA_COORDS["2701"],  # Woodlands Causeway bridge itself
    "2702": CAMERA_COORDS["2701"],
    "4703": CAMERA_COORDS["4703"],  # Tuas Second Crossing bridge itself
    "4712": CAMERA_COORDS["4703"],
    "4713": CAMERA_COORDS["4703"],
}

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
#
# Attributed to all three Tuas-side cameras (4703, 4712, 4713) as of
# 2026-10-08 — this dataset has no coordinates to tell us which camera's
# exact stretch it describes, and all three sit along the same AYE-to-Tuas-
# Checkpoint corridor this data covers, so none of them has a better claim
# to it than the others. Each matched record gets written out once per
# camera in the list below (same data, same direction_label — that's
# computed from the record's own text, not from which camera it's attributed
# to, so it stays correct for every duplicate).
TRAVEL_ROAD_CAMERA_MAP = {
    "tuas checkpoint": ["4703", "4712", "4713"],
    # Untested as of 2026-10-08 — added to check whether Travel Times covers
    # BKE/Woodlands the same way it covers AYE/Tuas. If the next run logs 0
    # matches for this, that's a real "no coverage" confirmation (unlike our
    # earlier assumption, which was never actually tested against this exact
    # phrase). If it does match, check what RoadName/Name comes back — update
    # the direction_label logic below to match on that road's actual segment
    # count/pattern too, the way "TUAS CHECKPOINT" is handled.
    "woodlands checkpoint": ["2701", "2702"],
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
    "direction",  # "to_malaysia" or "to_singapore" — see BORDER_ANCHOR above
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
    "direction",  # LTA's raw code (1 or 2)
    "direction_label",  # decoded from real data on 2026-10-08: direction=1
                         # rows always have far_end_point=TUAS CHECKPOINT
                         # (heading toward it) and direction=2 rows have
                         # far_end_point=CITY (heading away, into Singapore)
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


def camera_refs_for(record, road_map, *fields):
    """Return the list of camera_ids this record's text matched (may be
    more than one, e.g. several cameras sharing one corridor), or []."""
    text = " ".join(str(record.get(f) or "") for f in fields).lower()
    for keyword, camera_ids in road_map.items():
        if keyword in text:
            return camera_ids
    return []


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


def direction_for(cam, start, end):
    """"to_malaysia" if this segment's end is closer to the border crossing
    than its start, else "to_singapore". See BORDER_ANCHOR above."""
    anchor = BORDER_ANCHOR[cam]
    return "to_malaysia" if haversine_km(anchor, end) < haversine_km(anchor, start) else "to_singapore"


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
            start = (float(r["StartLat"]), float(r["StartLon"]))
            end_lat, end_lon = r.get("EndLat"), r.get("EndLon")
            direction = direction_for(cam, start, (float(end_lat), float(end_lon))) if end_lat and end_lon else ""
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
                    "direction": direction,
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

    # Each matched record maps to a *list* of cameras (see TRAVEL_ROAD_CAMERA_MAP)
    # — one row gets written per camera in that list, not one row total.
    matched = [
        (r, cams)
        for r in records
        if (cams := camera_refs_for(r, TRAVEL_ROAD_CAMERA_MAP, "Name", "FarEndPoint", "StartPoint", "EndPoint"))
    ]
    if not matched:
        print(f"[{timestamp}] Travel times: no rows matched {list(TRAVEL_ROAD_CAMERA_MAP)} out of {len(records)} fetched.")
        return 0

    ensure_csv(TRAVEL_TIMES_CSV, TRAVEL_TIMES_FIELDS)
    rows_written = 0
    with open(TRAVEL_TIMES_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRAVEL_TIMES_FIELDS)
        for r, cams in matched:
            far_end = (r.get("FarEndPoint") or "").upper()
            if "TUAS CHECKPOINT" in far_end or "WOODLANDS CHECKPOINT" in far_end:
                direction_label = "to_malaysia"
            elif "CITY" in far_end:
                direction_label = "to_singapore"
            else:
                direction_label = ""  # unseen pattern — leave for manual review
            for cam in cams:
                writer.writerow(
                    {
                        "timestamp": timestamp,
                        "time_bucket_5min": time_bucket,
                        "camera_ref": cam,
                        "name": r.get("Name"),
                        "direction": r.get("Direction"),
                        "direction_label": direction_label,
                        "far_end_point": r.get("FarEndPoint"),
                        "start_point": r.get("StartPoint"),
                        "end_point": r.get("EndPoint"),
                        "est_time_min": r.get("EstTime"),
                    }
                )
                rows_written += 1
    print(f"[{timestamp}] Travel times: logged {rows_written} matched rows ({len(matched)} unique records x cameras).")
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
