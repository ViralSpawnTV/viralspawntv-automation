import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


ROOT = Path("work/longform")

VIDEO = (
    ROOT /
    "ViralSpawnTV_Longform_V1_5.mp4"
)

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
    """
    Load permanent long-form source history.

    Missing history is normal on the first successful
    long-form upload.
    """

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
    """
    Normalize one production source record into the
    permanent history format.
    """

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

    rights_status = str(
        source.get("rights_status")
        or "unverified"
    ).strip()

    # We need at least one permanent identifier.
    if not clip_id and not clip_url:
        return None

    return {
        "clip_id": clip_id,
        "clip_url": clip_url,
        "channel": channel,
        "game": game,
        "rights_status": rights_status,
    }


def extract_used_sources(meta):
    """
    Extract ONLY source clips that production confirms were
    actually used in the final episode.

    V1.5 production currently writes these to "source_clips".

    Other names remain supported for compatibility with future
    production versions.
    """

    possible_fields = [
        "source_clips",
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

        clip_id = row[
            "clip_id"
        ]

        clip_url = row[
            "clip_url"
        ]

        # Prevent duplicate entries inside the same episode.
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


def verify_branding(meta):
    """
    V1.5 production currently records the actual branding
    asset paths as:

        branding_intro
        branding_outro

    Older/newer metadata may instead contain boolean
    branded_intro / branded_outro fields.

    Either representation is accepted, but both intro and
    outro must be explicitly confirmed.
    """

    intro_value = meta.get(
        "branding_intro"
    )

    outro_value = meta.get(
        "branding_outro"
    )

    intro_confirmed = bool(
        intro_value
    ) or bool(
        meta.get(
            "branded_intro",
            False,
        )
    )

    outro_confirmed = bool(
        outro_value
    ) or bool(
        meta.get(
            "branded_outro",
            False,
        )
    )

    if not intro_confirmed:
        fail(
            "ViralSpawnTV branded intro "
            "was not confirmed by V1.5 metadata."
        )

    if not outro_confirmed:
        fail(
            "ViralSpawnTV branded outro "
            "was not confirmed by V1.5 metadata."
        )

    return {
        "intro_confirmed":
            True,
        "outro_confirmed":
            True,
        "intro_metadata":
            intro_value,
        "outro_metadata":
            outro_value,
    }


def save_successful_longform_history(
    used_sources,
    source_field,
    video_id,
    title,
):
    """
    Permanently record every source clip actually used in the
    successfully published episode.

    IMPORTANT:
    This function is called only AFTER YouTube returns a valid
    video ID.

    Failed production attempts and failed uploads therefore do
    not burn clips.
    """

    if not used_sources:
        fail(
            "Cannot update long-form history because "
            "no used sources were supplied."
        )

    history = (
        load_longform_history()
    )

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

                # "channel" always means SOURCE creator.
                "channel":
                    source[
                        "channel"
                    ],

                "game":
                    source[
                        "game"
                    ],

                "rights_status":
                    source[
                        "rights_status"
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
            f"V1.5 video does not exist: "
            f"{VIDEO}"
        )

    if VIDEO.stat().st_size <= 0:
        fail(
            "V1.5 video file is empty."
        )

    if not METADATA.exists():
        fail(
            f"V1.5 metadata does not exist: "
            f"{METADATA}"
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
    # ---------------------------------------------------------

    metadata_version = str(
        meta.get("version")
        or ""
    ).strip()

    if metadata_version not in {
        "1.5",
        "1.6-seo",
    }:
        fail(
            "Uploader expected compatible V1.5/V1.6 SEO metadata. "
            f"Received: {metadata_version!r}"
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
        False,
    ):
        fail(
            "V1.5 motion gate did not pass."
        )

    if not meta.get(
        "visual_activity_gate_passed",
        False,
    ):
        fail(
            "V1.5 visual activity gate "
            "did not pass."
        )

    if not meta.get(
        "quality_gate_passed",
        False,
    ):
        fail(
            "V1.5 quality gate did not pass."
        )

    if not meta.get(
        "audio_continuity_gate_passed",
        False,
    ):
        fail(
            "V1.5 audio continuity gate "
            "did not pass."
        )

    if not meta.get(
        "continuous_gameplay",
        False,
    ):
        fail(
            "Continuous gameplay requirement "
            "was not confirmed."
        )

    if meta.get(
        "narration_cards",
        True,
    ):
        fail(
            "Narration cards are not allowed."
        )

    # ---------------------------------------------------------
    # Branding compatibility
    # ---------------------------------------------------------

    branding = verify_branding(
        meta
    )

    # ---------------------------------------------------------
    # Duration protection
    # ---------------------------------------------------------

    duration = float(
        meta.get(
            "duration_seconds",
            0,
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
    # Verify exact USED sources BEFORE uploading
    #
    # Production's "source_clips" contains only clips that
    # actually passed the gates and were added to the episode.
    # ---------------------------------------------------------

    (
        used_sources,
        source_field,
    ) = extract_used_sources(
        meta
    )

    if not used_sources:
        fail(
            "V1.5 metadata does not contain "
            "identifiable source clips actually "
            "used in the final episode. "
            "Refusing upload until permanent "
            "history tracking can be guaranteed."
        )

    print(
        "\nV1.5 USED-SOURCE VERIFICATION"
    )

    print(
        f"Metadata field: "
        f"{source_field}"
    )

    print(
        f"Sources actually used: "
        f"{len(used_sources)}"
    )

    for i, source in enumerate(
        used_sources,
        1,
    ):
        print(
            f"{i}. "
            f"{source.get('clip_id', '')} | "
            f"{source.get('game', '')} | "
            f"{source.get('channel', '')}"
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
                "https://www.googleapis.com/"
                "auth/youtube.upload"
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
    # YouTube metadata
    # ---------------------------------------------------------

    title = str(
        meta.get("title")
        or
        "ViralSpawnTV Gaming Compilation"
    ).strip()[:100]

    description = str(
        meta.get("description")
        or
        "Gaming moments from ViralSpawnTV."
    ).strip()

    raw_tags = meta.get(
        "tags",
        [],
    )

    tags = []
    seen_tags = set()

    if isinstance(raw_tags, list):
        for item in raw_tags:
            tag = str(item).strip()

            if not tag:
                continue

            key = tag.casefold()

            if key in seen_tags:
                continue

            seen_tags.add(key)
            tags.append(tag[:100])

            if len(tags) >= 15:
                break

    snippet = {
        "title":
            title,

        "description":
            description,

        "categoryId":
            "20",
    }

    if tags:
        snippet["tags"] = tags

    body = {
        "snippet": snippet,

        "status": {
            "privacyStatus":
                "public",

            "selfDeclaredMadeForKids":
                False,
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

    request = (
        youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )
    )

    response = None

    while response is None:
        _, response = (
            request.next_chunk()
        )

    video_id = response.get(
        "id"
    )

    if not video_id:
        fail(
            "YouTube upload completed without "
            "returning a video ID."
        )

    print(
        "\nYouTube returned video ID: "
        f"{video_id}"
    )

    # ---------------------------------------------------------
    # PERMANENT LONG-FORM HISTORY
    #
    # This occurs ONLY after YouTube returns a valid ID.
    # ---------------------------------------------------------

    history_result = (
        save_successful_longform_history(
            used_sources=
                used_sources,

            source_field=
                source_field,

            video_id=
                video_id,

            title=
                title,
        )
    )

    # ---------------------------------------------------------
    # Upload result
    # ---------------------------------------------------------

    result = {
        "version":
            "1.6-seo",

        "status":
            "UPLOADED",

        "production_metadata_version":
            metadata_version,

        "seo_version":
            meta.get("seo_version"),

        "primary_search_phrase":
            meta.get("primary_search_phrase", ""),

        "secondary_search_phrases":
            meta.get("secondary_search_phrases", []),

        "tags":
            tags,

        "tag_count":
            len(tags),

        "thumbnail_text":
            meta.get("thumbnail_text", ""),

        "video_id":
            video_id,

        "title":
            title,

        "privacy_status":
            "public",

        "duration_seconds":
            duration,

        "quality_gate_passed":
            True,

        "audio_continuity_gate_passed":
            True,

        "motion_gate_passed":
            True,

        "visual_activity_gate_passed":
            True,

        "branded_intro":
            branding[
                "intro_confirmed"
            ],

        "branded_outro":
            branding[
                "outro_confirmed"
            ],

        "longform_history_updated":
            True,

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
