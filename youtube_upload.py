import json
import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


VIDEO_PATH = Path("work/production/ViralSpawnTV_Short_V4.mp4")
METADATA_PATH = Path("work/production/ViralSpawnTV_V4_metadata.json")
MUSIC_GATE_PATH = Path("work/music_gate/music_gate_result.json")
RESULT_PATH = Path("work/production/youtube_upload_result.json")

PRIVACY_STATUS = "public"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def require_music_gate():
    if not MUSIC_GATE_PATH.exists():
        raise RuntimeError(
            "Public upload blocked: music gate result is missing."
        )

    result = json.loads(
        MUSIC_GATE_PATH.read_text(encoding="utf-8")
    )

    if result.get("passed") is not True:
        raise RuntimeError(
            "Public upload blocked: source did not pass music screening."
        )


def credentials_from_secret():
    raw = os.environ.get("YOUTUBE_TOKEN_JSON")

    if not raw:
        raise RuntimeError("YOUTUBE_TOKEN_JSON secret is missing.")

    info = json.loads(raw)
    creds = Credentials.from_authorized_user_info(info, SCOPES)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds.valid:
        raise RuntimeError("YouTube OAuth credentials are not valid.")

    return creds


def main():
    require_music_gate()

    if PRIVACY_STATUS != "public":
        raise RuntimeError(
            "This V8 uploader is expected to publish PUBLIC."
        )

    if not VIDEO_PATH.exists():
        raise RuntimeError(f"Missing finished video: {VIDEO_PATH}")

    if not METADATA_PATH.exists():
        raise RuntimeError(f"Missing metadata: {METADATA_PATH}")

    metadata = json.loads(
        METADATA_PATH.read_text(encoding="utf-8")
    )

    title = str(metadata.get("title") or "ViralSpawnTV Gaming Short")[:100]
    description = str(metadata.get("description") or "")[:5000]

    youtube = build(
        "youtube",
        "v3",
        credentials=credentials_from_secret(),
    )

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "20",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(VIDEO_PATH),
        mimetype="video/mp4",
        resumable=True,
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None

    while response is None:
        status, response = request.next_chunk()

        if status:
            print(
                f"YouTube upload progress: "
                f"{int(status.progress() * 100)}%"
            )

    result = {
        "success": True,
        "video_id": response["id"],
        "privacy_status": "public",
        "title": title,
        "music_gate_passed": True,
    }

    RESULT_PATH.write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"PUBLIC YOUTUBE UPLOAD FAILED: {exc}")
        sys.exit(1)
