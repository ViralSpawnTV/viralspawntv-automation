import subprocess
import sys
from pathlib import Path


def run(name):
    print("\n" + "=" * 72)
    print("RUNNING:", name)
    print("=" * 72)

    subprocess.run(
        [sys.executable, name],
        check=True
    )


def main():

    Path(
        "work/longform"
    ).mkdir(
        parents=True,
        exist_ok=True
    )

    # --------------------------------------------------
    # Discovery
    # --------------------------------------------------

    run(
        "kick_game_discovery.py"
    )

    # --------------------------------------------------
    # Long-form candidate ranking
    # --------------------------------------------------

    run(
        "longform_ranker.py"
    )

    # --------------------------------------------------
    # Acquire selected source clips
    # --------------------------------------------------

    run(
        "longform_collect.py"
    )

    # --------------------------------------------------
    # Existing source safety/content gate
    # --------------------------------------------------

    run(
        "longform_source_gate_v1_1.py"
    )

    # --------------------------------------------------
    # V1.4 production
    #
    # Continuous gameplay
    # No black narration screens
    # Freeze detection
    # Visual activity detection
    # --------------------------------------------------

    run(
        "longform_production_v1_4.py"
    )

    # --------------------------------------------------
    # V1.4 public YouTube upload
    #
    # Upload will refuse publication unless the
    # V1.4 motion + visual activity gates passed.
    # --------------------------------------------------

    run(
        "longform_upload_v1_4.py"
    )


if __name__ == "__main__":

    try:

        main()

    except Exception as e:

        print(
            "LONGFORM V1.4 PIPELINE FAILED:",
            e
        )

        sys.exit(1)
