# ViralSpawnTV Automation

A starter automation for producing transformative vertical commentary Shorts from **authorized source feeds**.

## What it does
Runs up to 10 times per day, checks configured sources, rejects already-processed clips, drafts original commentary/title/description with OpenAI, generates a consistent American-English voiceover, renders a 1080x1920 MP4 with FFmpeg, and can upload it through the YouTube Data API.

## Safety defaults
- `AUTO_UPLOAD` defaults to `false` so the first renders can be reviewed.
- YouTube `publish_mode` defaults to `private`.
- Discovery only accepts sources explicitly marked `reuse_authorized: true`.
- API keys/tokens are never committed.

## Setup
1. Upload this project to the GitHub repository.
2. Copy `config.example.json` to `config.json` and configure feeds/endpoints for clips you have permission to reuse. Commit `config.json` (do not put credentials in it).
3. In GitHub repository Settings → Secrets and variables → Actions, create `OPENAI_API_KEY` and later `YOUTUBE_TOKEN_JSON`.
4. Run the workflow manually first. Keep `AUTO_UPLOAD` false while validating voice, captions, source audio, and pacing.
5. After YouTube OAuth is configured and test uploads are correct, create repository variable `AUTO_UPLOAD=true`. Keep `publish_mode` as `private` until final QC is satisfactory; switch to `public` or scheduled publishing only after validation.

## Source adapter
The included `json` source adapter expects an endpoint shaped roughly as `{ "clips": [{"download_url":"...", "title":"..."}] }`. This intentionally does not scrape/download arbitrary YouTube/Twitch videos. Add official APIs or creator-provided feeds where you have the needed rights/authorization.

## YouTube OAuth
YouTube uploads require user OAuth consent. Generate an authorized-user token with the `youtube.upload` scope, then store the resulting token JSON as the `YOUTUBE_TOKEN_JSON` GitHub Actions secret. Never commit client secrets or refresh tokens.

## Important
Ten workflow runs do not mean ten forced uploads. If there is no new authorized candidate, the run exits without publishing. This protects quality and reduces duplicate/reuse problems.
