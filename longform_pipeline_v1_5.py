import subprocess
import sys


def run(script):
    print("\n" + "=" * 70)
    print(f"RUNNING: {script}")
    print("=" * 70 + "\n")

    result = subprocess.run(
        [sys.executable, script]
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"{script} failed with "
            f"exit code {result.returncode}"
        )

    print("\n" + "=" * 70)
    print(f"COMPLETED: {script}")
    print("=" * 70 + "\n")


def main():

    # ---------------------------------------------------------
    # 1. Discover current gaming clips
    # ---------------------------------------------------------

    run(
        "kick_game_discovery.py"
    )

    # ---------------------------------------------------------
    # 2. Rank candidates for long-form use
    # ---------------------------------------------------------

    run(
        "longform_ranker.py"
    )

    # ---------------------------------------------------------
    # 3. Acquire candidate source clips
    # ---------------------------------------------------------

    run(
        "longform_collect.py"
    )

    # ---------------------------------------------------------
    # 4. Existing source/content screening
    # ---------------------------------------------------------

    run(
        "longform_source_gate_v1_1.py"
    )

    # ---------------------------------------------------------
    # 5. V1.5 production
    #
    # Production performs:
    # - source quality screening
    # - V1.4 motion/activity screening
    # - narration/game-audio mixing
    # - ViralSpawnTV intro
    # - ViralSpawnTV outro
    # - final quality/audio validation
    # ---------------------------------------------------------

    run(
        "longform_production_v1_5.py"
    )

    # ---------------------------------------------------------
    # 6. Upload only after V1.5 passes every required gate
    # ---------------------------------------------------------

    run(
        "longform_upload_v1_5.py"
    )

    print(
        "\n"
        "VIRALSPAWNTV LONG-FORM V1.5 "
        "PIPELINE COMPLETE"
    )


if __name__ == "__main__":

    try:
        main()

    except Exception as exc:

        print(
            "\n"
            "VIRALSPAWNTV LONG-FORM V1.5 "
            "PIPELINE FAILED:",
            exc,
        )

        sys.exit(1)
