import json, os, sys
from pathlib import Path
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT=Path("work/longform")
META=ROOT/"ViralSpawnTV_Longform_V1_metadata.json"
RESULT=ROOT/"youtube_longform_upload_result.json"

def main():
    m=json.loads(META.read_text(encoding="utf-8"))
    token=json.loads(os.environ["YOUTUBE_TOKEN_JSON"])
    creds=Credentials.from_authorized_user_info(token)
    youtube=build("youtube","v3",credentials=creds)

    body={"snippet":{"title":m["title"],"description":m["description"],
                     "categoryId":"20"},
          "status":{"privacyStatus":"public","selfDeclaredMadeForKids":False}}
    media=MediaFileUpload(m["video_path"],mimetype="video/mp4",resumable=True)
    req=youtube.videos().insert(part="snippet,status",body=body,media_body=media)
    response=None
    while response is None:
        _,response=req.next_chunk()

    out={"success":True,"video_id":response["id"],"privacy_status":"public",
         "title":m["title"]}
    RESULT.write_text(json.dumps(out,indent=2),encoding="utf-8")
    print("PUBLIC LONG-FORM UPLOAD:",response["id"])

if __name__=="__main__":
    try: main()
    except Exception as e:
        print("LONGFORM UPLOAD FAILED:",e); sys.exit(1)
