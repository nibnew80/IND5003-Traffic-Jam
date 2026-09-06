import io, os, time, requests
from datetime import datetime, timezone, timedelta
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

CAMERA_IDS = ["2701", "2702", "4703", "4712"]
LTA_URL = "https://datamall2.mytransport.sg/ltaodataservice/Traffic-Imagesv2"
LTA_API_KEY = os.environ["LTA_API_KEY"]
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]

# Loop settings: LOOP_COUNT=1 (default) behaves like the original single run.
# In the hourly workflow set LOOP_COUNT=11 and LOOP_INTERVAL=300 (5 min).
LOOP_COUNT = int(os.environ.get("LOOP_COUNT", "1"))
LOOP_INTERVAL = int(os.environ.get("LOOP_INTERVAL", "300"))
TIMEOUT = 30

SGT = timezone(timedelta(hours=8))
HEADERS = {"AccountKey": LTA_API_KEY, "accept": "application/json"}

creds = Credentials(
    None,
    refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
    client_id=os.environ["GDRIVE_CLIENT_ID"],
    client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
    token_uri="https://oauth2.googleapis.com/token",
)
drive = build("drive", "v3", credentials=creds, cache_discovery=False)

last_link = {}  # cam_id -> ImageLink of the last frame we uploaded


def scrape_once():
    timestamp = datetime.now(SGT).strftime("%Y-%m-%d_%H-%M-%S")

    try:
        resp = requests.get(LTA_URL, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        cameras = {c["CameraID"]: c for c in resp.json()["value"]}
    except Exception as e:
        print(f"[{timestamp}] LTA request failed: {e}")
        return

    for cam_id in CAMERA_IDS:
        cam = cameras.get(cam_id)
        if not cam:
            print(f"[{timestamp}] Camera {cam_id} missing from response, skipping.")
            continue

        link = cam["ImageLink"]
        if last_link.get(cam_id) == link:
            print(f"[{timestamp}] Camera {cam_id} unchanged, skipping.")
            continue

        try:
            img = requests.get(link, timeout=TIMEOUT)
            img.raise_for_status()
            filename = f"cam{cam_id}_{timestamp}.jpg"
            media = MediaIoBaseUpload(io.BytesIO(img.content), mimetype="image/jpeg")
            created = drive.files().create(
                body={"name": filename, "parents": [DRIVE_FOLDER_ID]},
                media_body=media,
                fields="id",
            ).execute()
            last_link[cam_id] = link
            print(f"[{timestamp}] Uploaded {filename} (ID: {created.get('id')})")
        except Exception as e:
            print(f"[{timestamp}] Camera {cam_id} failed: {e}")


if __name__ == "__main__":
    for i in range(LOOP_COUNT):
        scrape_once()
        if i < LOOP_COUNT - 1:
            time.sleep(LOOP_INTERVAL)
