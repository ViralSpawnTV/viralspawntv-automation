import json
import os
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


ROOT = Path("work/longform")

VIDEO = ROOT / "ViralSpawnTV_Longform_V1_5.mp4"

METADATA = (
    ROOT /
    "ViralSpawnTV_Longform_V1_5_metadata.json"
)

RESULT = (
    ROOT /
    "youtube_longform_upload_result_v1_5.json"
)


def fail(message):
    raise RuntimeError(message)


def main():

    # ---------------------------------------------------------
    # Verify files exist
    # ---------------------------------------------------------

    if not VIDEO.exists():
        fail(
            f"V1.5 video does not exist: {VIDEO}"
        )

    if VIDEO.stat().st_size <= 0:
        fail(
            "V1.5 video file is empty."
        )

    if not METADATA.exists():
        fail(
            f"V1.5 metadata does not exist: {METADATA}"
        )

    # ---------------------------------------------------------
    # Load V1.5 production metadata
    # ---------------------------------------------------------

    meta = json.loads(
        METADATA.read_text(
            encoding="utf-8"
        )
    )

    # ---------------------------------------------------------
    # HARD V1.5 SAFETY CHECKS
    #
    # Nothing reaches YouTube unless production explicitly
    # confirms every required gate.
    # ---------------------------------------------------------

    if str(meta.get("version")) != "1.5":
        fail(
            "Uploader expected V1.5 metadata."
        )

    if (
        meta.get("publish_status")
        != "READY_FOR_UPLOAD"
    ):
        fail(
            "V1.5 production did not mark "
            "this episode READY_FOR_UPLOAD."
        )

    if not meta.get(
        "motion_gate_passed",
        False
    ):
        fail(
            "V1.5 motion gate did not pass."
        )

    if not meta.get(
        "visual_activity_gate_passed",
        False
    ):
        fail(
            "V1.5 visual activity gate "
            "did not pass."
        )

    if not meta.get(
        "quality_gate_passed",
        False
    ):
        fail(
            "V1.5 quality gate did not pass."
        )

    if not meta.get(
        "audio_continuity_gate_passed",
        False
    ):
        fail(
            "V1.5 audio continuity gate "
            "did not pass."
        )

    if not meta.get(
        "continuous_gameplay",
        False
    ):
        fail(
            "Continuous gameplay requirement "
            "was not confirmed."
        )

    if meta.get(
        "narration_cards",
        True
    ):
        fail(
            "Narration cards are not allowed."
        )

    # ---------------------------------------------------------
    # Branding checks
    # ---------------------------------------------------------

    if not meta.get(
        "branded_intro",
        False
    ):
        fail(
            "ViralSpawnTV branded intro "
            "was not confirmed."
        )

    if not meta.get(
        "branded_outro",
        False
    ):
        fail(
            "ViralSpawnTV branded outro "
            "was not confirmed."
        )

    # ---------------------------------------------------------
    # Duration protection
    # ---------------------------------------------------------

    duration = float(
        meta.get(
            "duration_seconds",
            0
        )
        or 0
    )

    if duration < 150:
        fail(
            f"Episode is only "
            f"{duration / 60:.2f} minutes. "
            "Refusing upload."
        )

    # ---------------------------------------------------------
    # YouTube token
    # ---------------------------------------------------------

    token_json = os.environ.get(
        "YOUTUBE_TOKEN_JSON"
    )

    if not token_json:
        fail(
            "YOUTUBE_TOKEN_JSON secret "
            "is missing."
        )

    token_data = json.loads(
        token_json
    )

    creds = Credentials.from_authorized_user_info(
        token_data,
        scopes=[
            "https://www.googleapis.com/auth/youtube.upload"
        ],
    )

    youtube = build(
        "youtube",
        "v3",
        credentials=creds,
        cache_discovery=False,
    )

    # ---------------------------------------------------------
    # Metadata
    # ---------------------------------------------------------

    title = (
        meta.get("title")
        or
        "ViralSpawnTV Gaming Compilation"
    )[:100]

    description = (
        meta.get("description")
        or
        "Gaming moments from ViralSpawnTV."
    )

    description += (
        "\n\n"
        "Subscribe to ViralSpawnTV for more "
        "gaming moments, crazy plays, "
        "reactions and stories."
        "\n\n"
        "#Gaming #ViralSpawnTV"
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

    # ---------------------------------------------------------
    # Upload
    # ---------------------------------------------------------

    media = MediaFileUpload(
        str(VIDEO),
        mimetype="video/mp4",
        chunksize=-1,
        resumable=True,
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None

    while response is None:
        _, response = request.next_chunk()

    video_id = response.get("id")

    if not video_id:
        fail(
            "YouTube upload completed without "
            "returning a video ID."
        )

    result = {
        "version": "1.5",
        "status": "UPLOADED",
        "video_id": video_id,
        "title": title,
        "privacy_status": "public",
        "duration_seconds": duration,
        "quality_gate_passed": True,
        "audio_continuity_gate_passed": True,
        "motion_gate_passed": True,
        "visual_activity_gate_passed": True,
        "branded_intro": True,
        "branded_outro": True,
    }

    RESULT.write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(
        "\nV1.5 YOUTUBE UPLOAD COMPLETE"
    )

    print(
        json.dumps(
            result,
            indent=2,
        )
    )


if __name__ == "__main__":

    try:
        main()

    except Exception as exc:

        print(
            "LONGFORM V1.5 "
            "UPLOAD FAILED:",
            exc,
        )

        sys.exit(1)
