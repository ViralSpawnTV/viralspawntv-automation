[README.md](https://github.com/user-attachments/files/33060694/README.md)
# ViralSpawnTV: 100 local clips, small paid shortlist

## Install in this order

1. Download and extract this ZIP on your computer.
2. In the ROOT of ViralSpawnTV/viralspawntv-automation, replace these six existing files with the full files from this ZIP:
   - kick_game_discovery.py
   - active_firefight_prescreener.py
   - viral_prescreener.py
   - v12_1_pipeline.py
   - production_test.py
   - action_segment_gate.py (add it if it is missing)
3. Add these three NEW files to the same repository root:
   - firefight_cache.py
   - ai_budget.py
   - budgeted_pipeline.py
4. Open your EXISTING Shorts workflow under .github/workflows/. Replace its ENTIRE contents with the contents of ViralSpawnTV_Shorts_Budget_Screen.yml. Keep the existing workflow filename; do not create a second scheduled Shorts workflow.
5. Commit all files to main before running. Keep music_rotation.py, music_manifest.json, your MP3s, history.json, and the other pipeline scripts. The workflow retains your current MUSIC_DIR: assets/music/assets/music.
6. GitHub > Actions > your existing ViralSpawnTV Shorts workflow > Run workflow > main > Run workflow.
7. Review the log. Expected messages include FREE SCREEN, LOCAL SCREEN, PAID SCREEN PASS/REJECT, EARLY STOP when four pass, and SHARED AI BUDGET.
8. If the run fails, download the viralspawntv-v12-1-production diagnostic artifact and share its work/firefight_prescreen/screening_report.json and work/ai_budget.json. Do not repeatedly rerun a failing job before checking its reason.

README.md and VALIDATION.md are instructions, not required repository uploads. All Python files are complete replacements/additions, not snippets.

## What changes

- Discovery targets 100 unique eligible shooter clips. More pages and recent-date searches widen the pool; creator/game publishing limits are retained. Availability, history and network access can still leave fewer than 100.
- Up to 100 uncached shooter sources are screened locally with FFmpeg/Python. This has no OpenAI charge but uses GitHub runner time. Four download workers fetch a preview once per clip, capped at the first 120 seconds; frame extraction reuses that local preview.
- Motion selects a promising 35-second window. It does not prove gunfire or guarantee exclusion of every menu. Five visual samples from that window go to paid review only for a small shortlist.
- Paid firefight review runs in batches of four, with at most 12 clips reviewed. It stops after four confirmed sources are available. A source must have direct weapon engagement in at least two samples and score at least 60. It no longer has to show sustained combat throughout its entire original duration.
- No rejected or motion-only source is backfilled into the approved pool. The exact selected edit still faces the mandatory strict action_segment_gate.py check before narration and rendering.
- Paid source approvals are cached for 24 hours; valid AI rejections for 72 hours. Expired entries are reconsidered. A changed source URL invalidates the verdict. Network failures, incomplete frames, API failures and malformed verdicts are not cached as rejections.
- The cache is captured before workflow Git resets and merged back to main, including on failed runs. It does not replace published-clip history or existing rejection history.
- Later payoff/story screening reviews one window per approved source, at most four windows, and skips preliminary paid audio transcription. Existing selected-source audio and music checks still run. At most two sources proceed to expensive acquisition/render attempts.
- Music rotation and one short narration hook remain enabled.

## Shared API limits per production run

The workflow MUST launch `python budgeted_pipeline.py`. Running `python v12_1_pipeline.py` directly bypasses the shared API protection.

The launcher installs protection into the pipeline and its Python subprocesses. Supported synchronous OpenAI Responses, audio transcription and speech requests share one ledger. SDK automatic retries are disabled. Every attempted request reserves its allowance BEFORE contacting OpenAI; failed calls do not refund the reservation.

Default upper limits in ai_budget.py:

| Resource | Limit |
| --- | ---: |
| All supported API requests combined | 28 |
| Responses requests | 18 |
| Image inputs across Responses requests | 240 |
| Output tokens reserved across Responses requests | 72,000 |
| Output tokens per Responses request | 6,000, or the caller's smaller limit |
| Text input bytes across Responses requests | 800,000 |
| WAV audio submitted for transcription | 300 seconds |
| Speech input characters | 800 |

A reached limit stops further paid requests for that run. Clips deferred because the shared budget was exhausted are not added to permanent rejection history. A run can skip publishing when no candidate passes or the budget is exhausted. These are workload limits, not a fixed dollar ceiling; model prices and actual usage determine the bill. Existing scripts use their existing models. The ledger records model names, reservations and returned token usage where the SDK exposes it; audio duration/character reservations remain visible even without returned token usage.

Preliminary visual-only screening trades some audio context for lower cost. The local motion heuristic and sampled AI checks can miss fights or misclassify them. No code can promise that every run finds an acceptable gunfight. This patch was tested without live paid AI calls; the next workflow run is the live validation.
