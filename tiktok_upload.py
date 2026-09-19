import json
import os
import sys
import time
from pathlib import Path

import requests


WORKER_TOKEN_URL = (
    "https://viralspawntv-tiktok.srwalker3333.workers.dev"
    "/automation/token"
)

CREATOR_INFO_URL = (
    "https://open.tiktokapis.com"
    "/v2/post/publish/creator_info/query/"
)

DIRECT_POST_URL = (
    "https://open.tiktokapis.com"
    "/v2/post/publish/video/init/"
)

STATUS_URL = (
    "https://open.tiktokapis.com"
    "/v2/post/publish/status/fetch/"
)

DEFAULT_VIDEO = Path(
    "work/production/ViralSpawnTV_Short_V4.mp4"
)

DEFAULT_METADATA = Path(
    "work/production/ViralSpawnTV_V4_metadata.json"
)

RESULT_FILE = Path(
    "work/production/tiktok_upload_result.json"
)


def fail(message, details=None):
    print(f"TIKTOK UPLOAD FAILED: {message}")

    if details is not None:
        print(json.dumps(details, indent=2))

    sys.exit(1)


def get_access_token():
    secret = os.environ.get(
        "TIKTOK_AUTOMATION_SECRET"
    )

    if not secret:
        fail(
            "Missing TIKTOK_AUTOMATION_SECRET."
        )

    response = requests.post(
        WORKER_TOKEN_URL,
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )

    if response.status_code != 200:
        fail(
            f"Token bridge returned HTTP "
            f"{response.status_code}."
        )

    data = response.json()

    token = data.get("access_token")

    if not token:
        fail(
            "Token bridge returned no access token."
        )

    print(
        "TikTok access token obtained securely."
    )

    return token


def tiktok_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type":
            "application/json; charset=UTF-8",
    }


def query_creator_info(token):
    print("Querying TikTok creator info...")

    response = requests.post(
        CREATOR_INFO_URL,
        headers=tiktok_headers(token),
        timeout=30,
    )

    try:
        payload = response.json()
    except Exception:
        fail(
            "Creator info returned non-JSON response."
        )

    error = payload.get("error", {})

    if (
        response.status_code != 200
        or error.get("code") != "ok"
    ):
        fail(
            "TikTok creator info request failed.",
            payload,
        )

    creator = payload.get("data", {})

    print(
        "TikTok creator:",
        creator.get(
            "creator_username",
            "unknown"
        )
    )

    privacy_options = creator.get(
        "privacy_level_options",
        []
    )

    print(
        "Available privacy levels:",
        ", ".join(privacy_options)
    )

    return creator


def load_title():
    title = (
        "Gaming chaos escalated fast "
        "#gaming #viralspawntv"
    )

    if not DEFAULT_METADATA.exists():
        return title

    try:
        metadata = json.loads(
            DEFAULT_METADATA.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return title

    possible_fields = [
        "title",
        "youtube_title",
        "short_title",
    ]

    for field in possible_fields:
        value = metadata.get(field)

        if isinstance(value, str) and value.strip():
            title = value.strip()
            break

    if "#gaming" not in title.lower():
        title += " #gaming"

    if "#viralspawntv" not in title.lower():
        title += " #ViralSpawnTV"

    # TikTok supports much more, but keeping
    # our automated caption conservative.
    return title[:500]


def initialize_direct_post(
    token,
    video_path,
    creator,
):
    video_size = video_path.stat().st_size

    if video_size <= 0:
        fail("Video file is empty.")

    max_duration = creator.get(
        "max_video_post_duration_sec"
    )

    privacy_options = creator.get(
        "privacy_level_options",
        []
    )

    # Sandbox / unaudited test:
    # intentionally private.
    if "SELF_ONLY" not in privacy_options:
        fail(
            "TikTok did not offer SELF_ONLY "
            "as an available privacy level.",
            {
                "privacy_level_options":
                    privacy_options
            },
        )

    title = load_title()

    # ViralSpawnTV Shorts should normally be
    # comfortably under TikTok's single-upload
    # practical limits. For <=64 MB we send
    # the entire file as one chunk.
    #
    # For larger files we use 10 MB chunks.
    if video_size <= 64 * 1024 * 1024:
        chunk_size = video_size
        total_chunks = 1
    else:
        chunk_size = 10 * 1024 * 1024
        total_chunks = video_size // chunk_size

        if total_chunks < 1:
            total_chunks = 1

    body = {
        "post_info": {
            "title": title,
            "privacy_level": "SELF_ONLY",
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
            "video_cover_timestamp_ms": 1000,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        },
    }

    print("Initializing TikTok Direct Post...")
    print(f"Video size: {video_size:,} bytes")
    print(f"Caption: {title}")
    print("Privacy: SELF_ONLY")

    response = requests.post(
        DIRECT_POST_URL,
        headers=tiktok_headers(token),
        json=body,
        timeout=30,
    )

    try:
        payload = response.json()
    except Exception:
        fail(
            "TikTok Direct Post initialization "
            "returned non-JSON response."
        )

    error = payload.get("error", {})

    if (
        response.status_code != 200
        or error.get("code") != "ok"
    ):
        fail(
            "TikTok Direct Post initialization "
            "failed.",
            payload,
        )

    data = payload.get("data", {})

    publish_id = data.get("publish_id")
    upload_url = data.get("upload_url")

    if not publish_id or not upload_url:
        fail(
            "TikTok did not return publish_id "
            "and upload_url.",
            payload,
        )

    print(
        "TikTok Direct Post initialized."
    )

    return (
        publish_id,
        upload_url,
        chunk_size,
    )


def upload_video(
    video_path,
    upload_url,
    chunk_size,
):
    total_size = video_path.stat().st_size

    print("Uploading video to TikTok...")

    with video_path.open("rb") as video:
        start = 0

        while start < total_size:
            remaining = total_size - start

            # TikTok permits the final chunk
            # to contain the trailing bytes.
            current_size = min(
                chunk_size,
                remaining,
            )

            data = video.read(current_size)

            if not data:
                fail(
                    "Unexpected end of video file."
                )

            end = start + len(data) - 1

            headers = {
                "Content-Type": "video/mp4",
                "Content-Length": str(len(data)),
                "Content-Range":
                    f"bytes {start}-{end}/"
                    f"{total_size}",
            }

            response = requests.put(
                upload_url,
                headers=headers,
                data=data,
                timeout=180,
            )

            if response.status_code not in (
                200,
                201,
                206,
            ):
                fail(
                    "TikTok video transfer failed.",
                    {
                        "http_status":
                            response.status_code,
                        "response":
                            response.text[:1000],
                    },
                )

            print(
                f"Uploaded bytes "
                f"{start:,}-{end:,} "
                f"of {total_size:,}"
            )

            start = end + 1

    print("Video transfer complete.")


def fetch_status(token, publish_id):
    response = requests.post(
        STATUS_URL,
        headers=tiktok_headers(token),
        json={
            "publish_id": publish_id
        },
        timeout=30,
    )

    try:
        payload = response.json()
    except Exception:
        return {
            "http_status":
                response.status_code,
            "raw":
                response.text[:1000],
        }

    return payload


def wait_for_status(
    token,
    publish_id,
):
    print(
        "Waiting for TikTok processing..."
    )

    last_status = None

    for attempt in range(12):
        time.sleep(5)

        payload = fetch_status(
            token,
            publish_id,
        )

        data = payload.get("data", {})

        status = data.get(
            "status",
            "UNKNOWN"
        )

        last_status = payload

        print(
            f"TikTok status "
            f"({attempt + 1}/12): {status}"
        )

        if status in (
            "PUBLISH_COMPLETE",
            "SEND_TO_USER_INBOX",
        ):
            return payload

        if status in (
            "FAILED",
            "PUBLISH_FAILED",
        ):
            fail(
                "TikTok reported publishing failure.",
                payload,
            )

    return last_status


def main():
    video_path = Path(
        os.environ.get(
            "TIKTOK_VIDEO_PATH",
            str(DEFAULT_VIDEO),
        )
    )

    if not video_path.exists():
        fail(
            f"Video not found: {video_path}"
        )

    RESULT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    token = get_access_token()

    creator = query_creator_info(token)

    publish_id, upload_url, chunk_size = (
        initialize_direct_post(
            token,
            video_path,
            creator,
        )
    )

    upload_video(
        video_path,
        upload_url,
        chunk_size,
    )

    status_payload = wait_for_status(
        token,
        publish_id,
    )

    result = {
        "platform": "tiktok",
        "success": True,
        "publish_id": publish_id,
        "privacy_level": "SELF_ONLY",
        "video_path": str(video_path),
        "status_response": status_payload,
    }

    RESULT_FILE.write_text(
        json.dumps(
            result,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print("================================")
    print("TIKTOK TEST UPLOAD COMPLETED")
    print("================================")
    print(f"Publish ID: {publish_id}")
    print("Privacy: SELF_ONLY")
    print(
        f"Result: {RESULT_FILE}"
    )


if __name__ == "__main__":
    main()
