    )

    suspicious_hits = sum(
        1
        for word in suspicious_words
        if word in normalized_generated
    )

    if suspicious_hits >= 4:
        raise RuntimeError(
            "English-output safety gate failed: "
            "generated ViralSpawnTV text appears to be "
            "non-English. Upload stopped."
        )

    # --------------------------------------------------------
    # 6. Generate captions from REAL timestamps
    # --------------------------------------------------------

    if plan.get("english_caption_segments"):
        captions = [
            {
                "start": float(item["start"]),
                "end": float(item["end"]),
                "text": clean_text(
                    item["text"]
                ).upper(),
            }
            for item in plan[
                "english_caption_segments"
            ]
        ]

        (
            WORK /
            "v4_captions.json"
        ).write_text(
            json.dumps(
                captions,
                indent=2
            ),
            encoding="utf-8"
        )

        print(
            f"English caption chunks: "
            f"{len(captions)}"
        )

    else:
        # Fallback for an English source or if the model
        # returns no translated caption segments.
        captions = create_real_captions(
            segments,
            float(
                plan["segment_start"]
            ),
            float(
                plan["segment_end"]
            ),
        )

    # --------------------------------------------------------
    # 7. Generate context-sensitive AI narration
    # --------------------------------------------------------

    beats = generate_voices(
        client,
        plan
    )

    # --------------------------------------------------------
    # 7B. Remove captions that compete with narration
    # --------------------------------------------------------

    captions = suppress_captions_during_narration(
        captions,
        beats
    )

    # --------------------------------------------------------
    # 8. Render
    # --------------------------------------------------------

    render(
        clip,
        plan,
        beats,
        captions
    )

    # --------------------------------------------------------
    # 8B. V5.5 cold-open + branded outro
    # --------------------------------------------------------

    add_short_brand_bookends()

    # --------------------------------------------------------
    # 9. Save metadata for future YouTube uploader
    # --------------------------------------------------------

    save_metadata(
        clip,
        plan
    )

    print("\n" + "=" * 65)
    print("VIRALSPAWNTV V4 COMPLETE")
    print("=" * 65)

    print(
        "\nFinished Short:"
    )

    print(
        "ViralSpawnTV_Short_V4.mp4"
    )

    print(
        "\nV4 gaming render complete."
    )

    print(
        "\nThe GitHub workflow may now pass this file "
        "to youtube_upload.py for PRIVATE upload only."
    )

