
                normalized_id = normalize_clip_id(clip_id)
                normalized_url = normalize_clip_url(url)

                if normalized_id in seen_ids or normalized_url in seen_urls:
                    continue

                # Filter published Shorts BEFORE they enter the candidate pool.
                if (
                    normalized_id in used
                    or normalized_id in published_ids
                    or normalized_url in published_urls
                ):
                    skipped_published += 1
                    continue

                # Filter permanently rejected Shorts BEFORE candidate creation.
                if (
                    normalized_id in shorts_rejected_ids
                    or normalized_url in shorts_rejected_urls
                ):
                    skipped_shorts_rejected += 1
                    continue

                if creator_counts.get(channel, 0) >= MAX_PUBLIC_UPLOADS_PER_CREATOR_24H:
                    continue

                if LONGFORM_MODE and (
                    normalized_id in longform_used_ids
                    or normalized_url in longform_used_urls
                ):
                    skipped_longform_used += 1
                    continue

                if LONGFORM_MODE and (
                    normalized_id in rejected_ids
                    or normalized_url in rejected_urls
                ):
                    skipped_longform_rejected += 1
                    continue

                rows.append({
                    "platform": "kick",
                    "game": game,
                    "category_slug": slug,
                    "channel": channel,
                    "clip_id": clip_id,
                    "clip_url": url,
                    "category_position": position,
                })

                seen_ids.add(normalized_id)
                seen_urls.add(normalized_url)

                # Limit only AFTER filtering old/rejected clips.
                if len(rows) >= MAX_CLIPS_PER_GAME:
                    break

            if rows:
                per_game[game] = rows

        browser.close()

    candidates = interleave(per_game)

    manifest = {
        "version": 11,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "creator_cap_24h": MAX_PUBLIC_UPLOADS_PER_CREATOR_24H,
        "game_cap_24h": MAX_PUBLIC_UPLOADS_PER_GAME_24H,
        "max_clips_per_game": MAX_CLIPS_PER_GAME,
        "max_total_candidates": MAX_TOTAL_CANDIDATES,
        "games_with_candidates": list(per_game.keys()),
        "candidate_count": len(candidates),
        "fresh_candidate_target": MAX_TOTAL_CANDIDATES,
        "fresh_target_reached": len(candidates) >= MAX_TOTAL_CANDIDATES,
        "skipped_published_shorts": skipped_published,
        "skipped_rejected_shorts": skipped_shorts_rejected,
        "skipped_longform_history": skipped_longform_used,
        "skipped_longform_rejected": skipped_longform_rejected,
        "candidates": candidates,
    }

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print()
    print(f"Games with candidates: {len(per_game)}")
    print(f"Fresh batch candidate count: {len(candidates)}")
    print(f"Skipped already-published Shorts during discovery: {skipped_published}")
    print(f"Skipped permanently rejected Shorts during discovery: {skipped_shorts_rejected}")

    if LONGFORM_MODE:
        print(f"Skipped previously used long-form clips: {skipped_longform_used}")
        print(f"Skipped permanently rejected long-form clips: {skipped_longform_rejected}")

    if len(candidates) < MAX_TOTAL_CANDIDATES:
        print(
            "NOTICE: Kick exposed fewer fresh eligible clips than the "
            "requested target. Continuing with every fresh clip found."
        )

    if not candidates:
        raise RuntimeError(
            "V11 discovery found no eligible fresh game-category clips."
        )

    print("V11 batch discovery complete.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"V11 DISCOVERY FAILED: {exc}")
        sys.exit(1)
