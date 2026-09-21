import json
import os
import sys
from datetime import datetime, timezone
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

LONGFORM_HISTORY = Path(
    "longform_history.json"
)


def fail(message):
    raise RuntimeError(message)


def load_longform_history():
    if not LONGFORM_HISTORY.exists():
        return {
            "version": 1,
            "used_clips": [],
        }

    try:
        data = json.loads(
            LONGFORM_HISTORY.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        return {
            "version": 1,
            "used_clips": [],
        }

    if isinstance(data, list):
        return {
            "version": 1,
            "used_clips": data,
        }

    if not isinstance(data, dict):
        return {
            "version": 1,
            "used_clips": [],
        }

    data.setdefault(
        "version",
        1,
    )

    data.setdefault(
        "used_clips",
        [],
    )

    if not isinstance(
        data.get("used_clips"),
        list,
    ):
        data["used_clips"] = []

    return data


def normalize_source(source):
    if not isinstance(
        source,
        dict,
    ):
        return None

    clip_id = str(
        source.get("clip_id")
        or source.get("id")
        or ""
    ).strip()

    clip_url = str(
        source.get("clip_url")
        or source.get("source_url")
        or source.get("url")
        or source.get("source")
        or ""
    ).strip()

    channel = str(
        source.get("channel")
        or source.get("creator")
        or ""
    ).strip()

    game = str(
        source.get("game")
        or ""
    ).strip()

    if not clip_id and not clip_url:
        return None

    return {
        "clip_id": clip_id,
        "clip_url": clip_url,
        "channel": channel,
        "game": game,
    }


def extract_used_sources(meta):
    """
    Extract ONLY the source clips that production says were
    actually used in the finished episode.

    Several field names are supported so this remains compatible
    with V1.5 metadata variations.

    We intentionally do NOT fall back to screened_sources.json or
    acquired_sources.json because those contain clips that may not
    have appeared in the final episode.
    """

    possible_fields = [
        "used_sources",
        "sources_used",
        "used_clips",
        "clips_used",
        "episode_sources",
        "selected_sources",
    ]

    raw_sources = None
    source_field = None

    for field in possible_fields:
        value = meta.get(field)

        if (
            isinstance(value, list)
            and value
        ):
            raw_sources = value
            source_field = field
            break

    if not raw_sources:
        return [], None

    normalized = []

    seen_ids = set()
    seen_urls = set()

    for source in raw_sources:
        row = normalize_source(
            source
        )

        if not row:
            continue

        clip_id = row["clip_id"]
        clip_url = row["clip_url"]

        if (
            clip_id
            and clip_id in seen_ids
        ):
            continue

        if (
            not clip_id
            and clip_url
            and clip_url in seen_urls
        ):
            continue

        if clip_id:
            seen_ids.add(
                clip_id
            )

        if clip_url:
            seen_urls.add(
                clip_url
            )

        normalized.append(
            row
        )

    return (
        normalized,
        source_field,
    )


def save_successful_longform_history(
    meta,
    video_id,
    title,
):
    """
    Record every source clip actually used in the successfully
    uploaded long-form episode.

    This function is called ONLY after YouTube returns a valid
    video ID.

    Failed production runs and failed uploads therefore do not
    burn source clips.
    """

    used_sources, source_field = (
        extract_used_sources(meta)
    )

    if not used_sources:
        fail(
            "YouTube upload succeeded, but V1.5 "
            "metadata did not contain identifiable "
            "used source clips. Refusing to create "
            "an inaccurate longform history."
        )

    history = load_longform_history()

    existing_ids = set()
    existing_urls = set()

    for item in history.get(
        "used_clips",
        [],
    ):
        if not isinstance(
            item,
            dict,
        ):
            continue

        clip_id = str(
            item.get("clip_id")
            or ""
        ).strip()

        clip_url = str(
            item.get("clip_url")
            or item.get("source")
            or ""
        ).strip()

        if clip_id:
            existing_ids.add(
                clip_id
            )

        if clip_url:
            existing_urls.add(
                clip_url
            )

    uploaded_at = (
        datetime.now(
            timezone.utc
        )
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )

    added = 0
    already_recorded = 0

    for source in used_sources:
        clip_id = source[
            "clip_id"
        ]

        clip_url = source[
            "clip_url"
        ]

        duplicate = False

        if (
            clip_id
            and clip_id in existing_ids
        ):
            duplicate = True

        if (
            clip_url
            and clip_url in existing_urls
        ):
            duplicate = True

        if duplicate:
            already_recorded += 1
            continue

        history[
            "used_clips"
        ].append(
            {
                "clip_id":
                    clip_id,
                "clip_url":
                    clip_url,
                "channel":
                    source[
                        "channel"
                    ],
                "game":
                    source[
                        "game"
                    ],
                "youtube_video_id":
                    video_id,
                "youtube_title":
                    title,
                "privacy_status":
                    "public",
                "uploaded_at":
                    uploaded_at,
            }
        )

        if clip_id:
            existing_ids.add(
                clip_id
            )

        if clip_url:
            existing_urls.add(
                clip_url
            )

        added += 1

    history[
        "version"
    ] = 1

    history[
        "last_successful_upload"
    ] = {
        "youtube_video_id":
            video_id,
        "youtube_title":
            title,
        "uploaded_at":
            uploaded_at,
        "metadata_source_field":
            source_field,
        "episode_source_count":
            len(used_sources),
        "new_history_entries":
            added,
        "already_recorded":
            already_recorded,
    }

    LONGFORM_HISTORY.write_text(
        json.dumps(
            history,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(
        "\nLONG-FORM HISTORY UPDATED"
    )

    print(
        f"Used sources found: "
        f"{len(used_sources)}"
    )

    print(
        f"New permanent history entries: "
        f"{added}"
    )

    print(
        f"Already recorded: "
        f"{already_recorded}"
    )

    print(
        f"History file: "
        f"{LONGFORM_HISTORY}"
    )

    return {
        "source_field":
            source_field,
        "used_source_count":
            len(used_sources),
        "history_entries_added":
            added,
        "already_recorded":
            already_recorded,
    }


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
    # Verify used-source metadata BEFORE upload
    #
    # We need this information for permanent duplicate
    # prevention after the successful upload.
    # ---------------------------------------------------------

    used_sources, source_field = (
        extract_used_sources(meta)
    )

    if not used_sources:
        fail(
            "V1.5 metadata does not contain "
            "identifiable source clips actually "
            "used in the final episode. "
            "Refusing upload until history tracking "
            "can be guaranteed."
        )

    print(
        f"V1.5 metadata contains "
        f"{len(used_sources)} used sources "
        f"from field '{source_field}'."
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

    creds = (
        Credentials.from_authorized_user_info(
            token_data,
            scopes=[
                "https://www.googleapis.com/auth/youtube.upload"
            ],
        )
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

    # ---------------------------------------------------------
    # PERMANENT LONG-FORM HISTORY
    #
    # Only reached after YouTube returned a real video ID.
    # ---------------------------------------------------------

    history_result = (
        save_successful_longform_history(
            meta=meta,
            video_id=video_id,
            title=title,
        )
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
        "longform_history_updated": True,
        "used_source_count":
            history_result[
                "used_source_count"
            ],
        "history_entries_added":
            history_result[
                "history_entries_added"
            ],
        "history_source_field":
            history_result[
                "source_field"
            ],
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
