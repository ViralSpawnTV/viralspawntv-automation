import base64
import json
import math
import os
import pathlib
import re
import subprocess
import textwrap

from openai import OpenAI
from playwright.sync_api import sync_playwright


# ============================================================
# SETTINGS
# ============================================================

ROOT = pathlib.Path(__file__).resolve().parent
WORK = ROOT / "work" / "production"
FRAMES = WORK / "frames"
VOICES = WORK / "voices"
TEXTS = WORK / "texts"

for folder in [WORK, FRAMES, VOICES, TEXTS]:
    folder.mkdir(parents=True, exist_ok=True)

CHANNEL = "unknown"
CREATOR = "Unknown Creator"

KICK_WORK = ROOT / "work" / "kick_gaming"
KICK_VIDEO = KICK_WORK / "selected_kick_gaming_source.mp4"
KICK_RESULT = KICK_WORK / "acquisition_result.json"

FINAL_VIDEO = WORK / "ViralSpawnTV_Short_V4.mp4"

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# V5.1 SHORTS BRAND BOOKENDS
INTRO_IMAGE = ROOT / "viralspawntv_intro.png"
OUTRO_IMAGE = ROOT / "viralspawntv_outro.png"
OUTRO_CTA = "FOLLOW VIRALSPAWNTV FOR DAILY GAMING CLIPS"
NEON_FRAME = ROOT / "viralspawntv_neon_frame_overlay.png"
INTRO_SECONDS = 0.0
OUTRO_SECONDS = 1.0
CORE_VIDEO = WORK / "ViralSpawnTV_Short_V4_core.mp4"
INTRO_VIDEO = WORK / "ViralSpawnTV_Short_V5_1_intro.mp4"
OUTRO_VIDEO = WORK / "ViralSpawnTV_Short_V5_1_outro.mp4"


# ============================================================
# BASIC HELPERS
# ============================================================

def run(command):

    print("\nRUNNING:")
    print(" ".join(str(x) for x in command))

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("\nSTDERR:")
        print(result.stderr[-15000:])
        raise RuntimeError("Command failed.")

    return result


def duration(path):

    result = run([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])

    return float(result.stdout.strip())


def encode_image(path):

    return base64.b64encode(
        path.read_bytes()
    ).decode("utf-8")


def clean_text(text):

    text = str(text)
