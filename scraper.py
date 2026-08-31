import requests, io, os 

from datetime import datetime, timezone, timedelta 

from google.oauth2.credentials import Credentials

from googleapiclient.discovery import build 

from googleapiclient.http import MediaIoBaseUpload 


CAMERA_IDS = ["2701", "2702", "4703", "4712"]  

LTA_API_KEY = os.environ["LTA_API_KEY"] 

DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"] 

  
headers = {"AccountKey": LTA_API_KEY, "accept": "application/json"} 

resp_raw = requests.get(
    "https://datamall2.mytransport.sg/ltaodataservice/Traffic-Imagesv2",
    headers=headers
)
print("Status code:", resp_raw.status_code)
print("Response text:", resp_raw.text[:300])
resp = resp_raw.json()
  

all_cameras = {c["CameraID"]: c for c in resp["value"]} 

  

from google.oauth2.credentials import Credentials

creds = Credentials(
    None,
    refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
    client_id=os.environ["GDRIVE_CLIENT_ID"],
    client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
    token_uri="https://oauth2.googleapis.com/token",
)

drive_service = build("drive", "v3", credentials=creds) 

  

sgt = timezone(timedelta(hours=8)) 

now = datetime.now(sgt) 

timestamp = now.strftime('%Y-%m-%d_%H-%M-%S') 

  

for cam_id in CAMERA_IDS: 

    camera = all_cameras.get(cam_id) 

    if not camera: 

        print(f"Camera {cam_id} not found in API response, skipping.") 

        continue 

  

    img_data = requests.get(camera["ImageLink"]).content 

    filename = f"cam{cam_id}_{timestamp}.jpg" 

  

    file_metadata = { 

        "name": filename, 

        "parents": [DRIVE_FOLDER_ID] 

    } 

    media = MediaIoBaseUpload(io.BytesIO(img_data), mimetype="image/jpeg") 

  

    uploaded_file = drive_service.files().create( 

        body=file_metadata, media_body=media, fields="id" 

    ).execute() 

  

    print(f"Uploaded: {filename} (ID: {uploaded_file.get('id')})") 
