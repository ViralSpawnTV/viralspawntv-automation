import json, os, sys
from pathlib import Path
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
ROOT=Path("work/longform")
META=ROOT/"ViralSpawnTV_Longform_V1_3_metadata.json"
RESULT=ROOT/"youtube_longform_upload_result_v1_3.json"
def main():
    m=json.loads(META.read_text(encoding="utf-8"))
    if m.get("publish_status")!="READY_FOR_UPLOAD": raise RuntimeError("Not upload-ready.")
    if not m.get("motion_gate_passed"): raise RuntimeError("Motion gate not passed.")
    if m.get("narration_cards") or not m.get("continuous_gameplay"): raise RuntimeError("Visual safety check failed.")
    creds=Credentials.from_authorized_user_info(json.loads(os.environ["YOUTUBE_TOKEN_JSON"]))
    yt=build("youtube","v3",credentials=creds)
    req=yt.videos().insert(part="snippet,status",
      body={"snippet":{"title":m["title"],"description":m["description"],"categoryId":"20"},
            "status":{"privacyStatus":"public","selfDeclaredMadeForKids":False}},
      media_body=MediaFileUpload(m["video_path"],mimetype="video/mp4",resumable=True))
    response=None
    while response is None: _,response=req.next_chunk()
    m["publish_status"]="PUBLIC"; m["youtube_video_id"]=response["id"]
    META.write_text(json.dumps(m,indent=2,ensure_ascii=False),encoding="utf-8")
    RESULT.write_text(json.dumps({"success":True,"video_id":response["id"],
        "privacy_status":"public","title":m["title"]},indent=2),encoding="utf-8")
    print("PUBLIC LONG-FORM V1.3 UPLOAD:",response["id"])
if __name__=="__main__":
    try: main()
    except Exception as e:
        print("LONGFORM V1.3 UPLOAD FAILED:",e);sys.exit(1)
