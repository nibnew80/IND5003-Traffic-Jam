import requests, io, os 

from datetime import datetime, timezone, timedelta 

from google.oauth2 import service_account 

from googleapiclient.discovery import build 

from googleapiclient.http import MediaIoBaseUpload 


CAMERA_IDS = ["2701", "2702", "4703", "4712"]  

LTA_API_KEY = os.environ["LTA_API_KEY"] 

DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"] 

  
headers = {"AccountKey": LTA_API_KEY, "accept": "application/json"} 

resp = requests.get( 

    "https://datamall2.mytransport.sg/ltaodataservice/Traffic-Imagesv2", 

    headers=headers 

).json() 

  

all_cameras = {c["CameraID"]: c for c in resp["value"]} 

  

SCOPES = ["https://www.googleapis.com/auth/drive.file"] 

creds = service_account.Credentials.from_service_account_file( 

    "service-account-key.json",  # this file is auto-created by the workflow — leave as is 

    scopes=SCOPES 

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
