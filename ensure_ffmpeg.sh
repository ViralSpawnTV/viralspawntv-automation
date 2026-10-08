#!/usr/bin/env bash
set -euo pipefail
usable() {
  command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null || return 1
  local filters encoders
  filters="$(ffmpeg -hide_banner -filters 2>/dev/null)"
  encoders="$(ffmpeg -hide_banner -encoders 2>/dev/null)"
  [[ "$filters" == *drawtext* && "$encoders" == *libx264* ]]
}
if usable; then
  echo 'Using available FFmpeg and FFprobe; no apt installation needed.'
  exit 0
fi
FFMPEG_CACHE_DIR="${HOME}/.cache/viralspawntv-ffmpeg"
mkdir -p "$FFMPEG_CACHE_DIR/bin"
export PATH="$FFMPEG_CACHE_DIR/bin:$PATH"
if ! usable; then
  archive="$FFMPEG_CACHE_DIR/build.tar.xz"
  curl --fail --location --retry 2 --max-time 240 \
    'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n9.0-latest-linux64-gpl-9.0.tar.xz' \
    --output "$archive"
  tar -xJf "$archive" --directory "$FFMPEG_CACHE_DIR/bin" --strip-components=2 \
    --wildcards '*/bin/ffmpeg' '*/bin/ffprobe'
  rm "$archive"
  chmod +x "$FFMPEG_CACHE_DIR/bin/ffmpeg" "$FFMPEG_CACHE_DIR/bin/ffprobe"
fi
usable || { echo 'FFmpeg codec/filter self-test failed.'; exit 1; }
ffmpeg -v error -f lavfi -i 'color=s=64x64:d=0.1' -vf 'drawtext=text=test:fontsize=12' \
  -c:v libx264 -f null -
if [[ -n "${GITHUB_PATH:-}" ]]; then
  echo "$FFMPEG_CACHE_DIR/bin" >> "$GITHUB_PATH"
fi
echo 'Cached FFmpeg/FFprobe ready; no apt update needed.'
