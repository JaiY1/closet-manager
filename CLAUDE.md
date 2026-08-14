# CLAUDE.md

## Project: Closet Manager (V1 + V2 shopping + V3 AI photo + V4 deploy-prep + V5 auth & observability — complete)

Personal wardrobe app built with Flask + Claude API. Third project in this repo alongside `news-aggregator/` and `nba-tool/`. Follow the same conventions (Flask, SQLite via `database.py`, templates with vanilla JS, "POP" light theme — cream paper, tangerine accent, bold black outlines).

### What's built (V1)
- **Garment catalog** — CRUD with image upload, auto-tagging via Claude Haiku Vision (`vision.py`)
- **Semantic search** — ChromaDB with ONNX all-MiniLM-L6-v2 embeddings (`vector_store.py`). Note: uses `ONNXMiniLM_L6_V2` from chromadb, NOT sentence-transformers (HuggingFace was blocked in the build environment)
- **Multi-agent ask** — Coordinator routes queries to Closet Agent and Style Agent via Claude tool use (`agents.py`)
- **Outfit builder** — select garments, save combos, rate 1–5 stars
- **Outfit calendar** — log what you wore each day; feeds into style profile
- **Style profile** — Claude Sonnet writes a natural language paragraph from your outfits + calendar; prepended to every suggestion prompt as context
- **Outfit suggester** — RAG: semantic search retrieves relevant garments → Claude Haiku picks the outfit

### What's built (V2 — Shopping)
- **Shopper Agent** (`agents.py: shopper_agent`) — live product search via Serper's Google Shopping API (`shopping.py`), not Tavily (switched pre-build — cheaper, structured product data, no scraping needed). Each result labeled "similar" or "different" from the user's established style, with reasoning grounded in their actual style profile + RAG-retrieved wardrobe items
- **Budget filtering** — optional `max_price` narrows both the Serper query and a post-fetch filter (Serper doesn't reliably respect price constraints on its own)
- **Gap detection** (`agents.py: identify_wardrobe_gaps`) — Claude reviews the full wardrobe + style profile, flags 3-5 genuinely thin categories with a ready-to-use search query each. Cached (1hr TTL), invalidated on any garment add/edit/delete via `invalidate_gaps_cache()`
- **Wishlist** — persists saved shopping results (`wishlist_items` table); save/remove from the Shop page
- **Query cache** (`agents.py: _shop_cache`) — identical shopper queries (by query+max_price) skip both the Serper call and the Claude scoring call for 1hr, since Serper's free tier is 2,500 searches/month and dev/testing burns through it fast otherwise
- **Shop UI** (`templates/shop.html`) — intentionally plain/functional styling; flagged for a design pass later, don't over-invest here

### What's built (V3 — AI Photo, powered by Gemini)
Image generation runs on **Google Gemini 2.5 Flash Image** ("nano-banana", `gemini-2.5-flash-image`) via the `google-genai` SDK — Anthropic has no image-generation API, so this is a second vendor. All lives in `imagegen.py`. **Requires `GEMINI_API_KEY` on a billing-enabled Google Cloud project** — the image model is NOT on the free tier (free tier returns `429 limit: 0`). ~$0.04 per generated image, ~8–15s each.

- **Photo import** (`/add/photo`) — drop an outfit selfie → `vision.detect_garments_in_photo` (Claude Haiku, one call) returns each worn garment with attributes + a normalized `[l,t,r,b]` bbox → `app._crop_bbox` pads ~12% and crops → `imagegen.reconstruct_garment` renders each as a clean ghost-mannequin cutout on a solid background → chroma-keyed to a transparent PNG. Returns base64 previews for a **review grid** (`templates/add_photo.html`); nothing is saved until the user confirms via `/add/photo/confirm`, which inserts through the normal `add_garment` + `embed_garment` path. Detection can over-segment (e.g. reads slide stripes as "socks") — the review/uncheck step is the guardrail, so keep it.
- **Chroma-key** (`imagegen.chroma_key_to_png`) — Gemini honors "solid green background" but renders a *muted sage*, not `#00ff00`, so the keyer **samples the actual border median** rather than assuming a fixed key. Backgrounds come back near-uniform (border std ~2); a tight smoothstep ramp (`_TRANSPARENT_T=28`, `_OPAQUE_T=72`) keys them out while leaving the garment opaque, plus a despill pass. Numpy-based, ported from the "Extract Clothing" skill gist — no external skill dependency. Occasional single-corner haze from generation vignetting; the review step catches bad cutouts.
- **Virtual try-on** (`/suggest/tryon`) — on the Ask/Suggest page, "👤 See it on you" renders the user wearing a suggested outfit. One full-body photo per user is stored (`users.body_photo_path`, set via `/settings/body-photo`); `imagegen.tryon` sends it + the garment cutout PNGs to Gemini. Results are cached by `(user, body photo, sorted garment ids)`, persisted to disk so restarts reuse them. **Honest limitation: it's a representative model wearing your clothes, not a perfect likeness** — the UI says as much.

### What's built (V4 — process improvements + deployment prep)
All image-pipeline improvements are behind **feature flags in `config.py`** (env-overridable) so any can be reverted; defaults are the new behavior.
- **Accuracy** — `IDENTITY_PROMPT_V2` (try-on preserves the *specific* person), `BODY_INPUT_MAXPX=1536` (hi-res body ref), `CUTOUT_FIDELITY` (hi-res crop + logo/colour-exact prompt) + `CUTOUT_RETRY` (auto re-roll a hazy cutout), `BODY_PHOTO_GATE` (`vision.check_body_photo` warns if the try-on photo has a hidden face / isn't full-body — advisory, still saves).
- **Cost** — `OPTIN_CUTOUTS` (import shows cheap crops; only reconstruct kept items on confirm), `CACHE_CUTOUTS` (disk cache by input hash — `reconstruct_garment(force=True)` bypasses it for regenerate), `PERSIST_TRYON` (try-on cache on disk), `REMBG_HYBRID` (free local `rembg` bg-removal on the normal `/add` flow — only helps flat product shots, NOT worn selfies). **Cost caps** (`DAILY_USER_CAP=40`, `DAILY_GLOBAL_CAP=200`, `MONTHLY_GLOBAL_CAP=1000`, ~$40/mo ceiling): every real generation is logged to `image_events` and checked via `_cap_blocked()`; cache hits never count or block.
- **Review-grid extras** — per-item "✨ Preview cutout" / "🔄 Regenerate" (`/add/photo/cutout`), paste-to-import (⌘/Ctrl+V).
- **Usage + gallery** — `/usage` meter on Suggest + Import pages; `/tryon/history` gallery of past renders (`tryon_history` table).
- **Upload safety** — `MAX_CONTENT_LENGTH=12MB` + 413 handler; `_safe_ext()` whitelists image extensions (blocks storing `.html`/`.svg`/etc.). Raw Gemini errors → friendly messages via `_friendly_gemini_error()`.
- **PWA** — installable: `static/manifest.json`, `static/sw.js` (served from `/sw.js`, scope `/`, static-only caching — never caches renders/API), on-brand icons, head tags on every page. Camera works on mobile via existing `accept="image/*"` inputs.
- **`DATA_DIR`** (config.py) — all mutable data (DB, ChromaDB, `uploads/`, caches) lives under one dir; defaults to project root (local unchanged), set `DATA_DIR=/data` (mounted volume) in prod so redeploys don't wipe data. `uploads/` moved out of `static/` and auto-migrated on first run; still served at `/static/uploads/...` via the `serve_upload` override route (so no template/URL churn).

### What's built (V5 — real auth, admin tooling, observability)
- **Real per-user auth — live in prod.** Email/password signup+login (PBKDF2-SHA256, `app.py: _hash_password`), "Sign in with Google" (Authlib/OIDC, hidden until `GOOGLE_CLIENT_ID`/`SECRET` are set), forgot/reset password via Resend (logs the link instead of emailing if `RESEND_API_KEY` is unset), and a forced "secure your account" prompt for old name-only accounts (`users.auth_provider == 'legacy'`). The old shared `ACCESS_CODE` login gate is gone — that var now only gates the admin routes below. Google callback disambiguates a display-name collision (`"Name (email-prefix)"`, then a random suffix) instead of 500ing, since unlike the signup form there's no field to fix Google's name before account creation.
- **Photo-import cutouts run in parallel**, not sequentially — `_run_photo_analyze_job`'s eager-cutout loop (the default path) fires up to `_CUTOUT_MAX_WORKERS=4` concurrent Gemini calls (ThreadPoolExecutor) instead of one at a time, since each is I/O-bound. A multi-garment photo now takes roughly as long as *one* Gemini call, not the sum of all of them.
- **"Add from a product link"** (`link_import.py`, new file) — paste a product URL on the add-garment page instead of uploading a photo. Scrapes `og:image`/title/price (schema.org JSON-LD first, then meta tags), has an SSRF guard (rejects private/loopback IPs, re-checked on every redirect hop) since it fetches whatever URL a logged-in user pastes, and runs the fetched photo through the *same* `reconstruct_garment` Gemini isolation as photo-import — the raw scraped image is often a model wearing the whole outfit, not an isolated shot of just this item, so it needs the same treatment. Runs as a background job (`/garments/from-link` + `/garments/from-link/<job_id>/status`), same polling pattern as photo-import.
- **Uploaded/generated images are cached hard by the browser** — `serve_upload()` sets `Cache-Control: private, max-age=31536000, immutable`. Safe because every write goes through `_unique_name()` (timestamp+uuid), so no file under `uploads/` is ever overwritten in place.
- **ChromaDB's ONNX model cache now persists across redeploys.** It used to live in `~/.cache` (the container's ephemeral filesystem on Railway), forcing a fresh ~79MB download on every redeploy; two concurrent first-use requests racing on that download crashed with `FileNotFoundError`. `config.py` now points `HOME` at `DATA_DIR` (skipped under the test suite via `CLOSET_MANAGER_TESTING`, or it'd force a full re-download every test run), and `vector_store.warm_up()` (called once at app startup, alongside `init_db()`) forces the model to load synchronously before any request threads start, closing the race window even on a genuinely fresh volume.
- **Every 500 gets recorded to a DB table**, not just Railway's live console logs — `error_log`, written via both `errorhandler(500)` (full traceback, for genuinely unhandled exceptions) and an `after_request` catch-all (no traceback, but catches the ~15 routes that manually catch their own exception and return `(jsonify(...), 500)`, which never reach `errorhandler`). The three background job runners (photo-analyze, link-import, tryon) log explicitly too, since they never produce an HTTP response at all. View at `/admin/errors`.
- **Every real (non-cached) Gemini call's timing gets recorded** — `gemini_timing`, written from `imagegen.py` itself (`reconstruct_garment`, `tryon`), including whether `CUTOUT_RETRY`'s second full Gemini call fired (roughly doubles latency for whatever fraction of cutouts trigger it). View at `/admin/timing`. Real measurements so far: single cutout calls land around 9-10s — that's essentially all Gemini's own model inference/network round-trip, not our pre/post-processing (resize, chroma-key are local numpy/PIL work, milliseconds) — so it isn't something further code changes can meaningfully cut; the parallelization above is the real lever we have (wall-clock time for a multi-garment photo, not any single call's latency).
- **One-off admin routes** (all `ACCESS_CODE`-gated, same pattern as the pre-existing `/admin/import-data`): `GET /admin/list-users` (counts per user), `POST /admin/merge-user` (moves garments/outfits/calendar/wishlist/usage-history/style-profile/body-photo from one account to another — built for consolidating a legacy name-only account onto a newly-linked Google/email account), `GET /admin/errors`, `GET /admin/timing`. **`ACCESS_CODE` is currently unset on Railway** (removed at some point after the account-merge work was done) — these routes 403 until it's re-added as a Railway env var.

### Key files
| File | Role |
|------|------|
| `app.py` | Flask routes, runs on port 5002 |
| `config.py` | Keys + `DATA_DIR` + all image-pipeline feature flags and cost caps (env-overridable) |
| `database.py` | SQLite (`$DATA_DIR/closet.db`): users (+`body_photo_path`, auth columns), garments, garment_images, outfits, outfit_items, outfit_logs, style_profile, wishlist_items, image_events, tryon_history, `error_log`, `gemini_timing`. Auto-migrates on init |
| `link_import.py` | Scrapes a product URL for its photo/title/price for "add from a link" — `fetch_product_page()`, SSRF-guarded |
| `vector_store.py` | ChromaDB wrapper — `embed_garment`, `search_garments`, `delete_garment` |
| `agents.py` | `run_coordinator`, `suggest_outfit`, `refresh_style_profile`, `shopper_agent`, `identify_wardrobe_gaps` |
| `vision.py` | `autotag_garment`, `autotag_label`, `detect_garments_in_photo`, `check_body_photo` — Claude Haiku Vision, returns JSON |
| `imagegen.py` | Gemini image layer — `reconstruct_garment` (+cache/retry/force), `chroma_key_to_png`, `tryon`, `remove_bg` (rembg). Lazy clients so the app boots without keys |
| `shopping.py` | `search_shopping` — Serper Google Shopping API wrapper, price parsing/filtering |
| `templates/` | base.html (POP light theme, tangerine accent #e8590c) + PWA tags, index, add, add_photo, edit, outfit, calendar, ask, shop, login, signup, forgot_password, reset_password, secure_account, admin_errors, admin_timing (the last two standalone, not extending base.html — `ACCESS_CODE`-gated, not session-gated) |

### Models used
- `claude-haiku-4-5` — outfit suggestions, coordinator, auto-tagging, garment detection, shopper scoring, gap detection (cost-sensitive)
- `claude-sonnet-4-6` — style profile generation only (runs rarely, needs quality)
- `gemini-2.5-flash-image` — garment cutout reconstruction + virtual try-on (image generation; ~$0.04/image, needs billing-enabled key)

### Running locally
```bash
cp .env.example .env   # add ANTHROPIC_API_KEY, SERPER_API_KEY (serper.dev, free), GEMINI_API_KEY (AI Studio, billing on)
pip install -r requirements.txt
python app.py          # http://localhost:5002
```
GEMINI_API_KEY is optional — without it the app runs fine and the photo-import / try-on routes return a clean 503. The image model requires a **billing-enabled** Google Cloud project (not free tier). Set `DATA_DIR=/data` (a mounted volume) for production so redeploys don't wipe data; it defaults to the project root locally.

### What's next
- **Re-add `ACCESS_CODE` on Railway** if you want the admin routes (`/admin/list-users`, `/admin/merge-user`, `/admin/errors`, `/admin/timing`, `/admin/import-data`) usable again — it was removed at some point and they currently 403.
- **`RESEND_API_KEY` still unset** — password-reset links just log via `logger.info` instead of emailing. Matters once people besides you might lock themselves out.
- Front/back / multi-view "see the garment in the round" feature — deferred, waiting on a clearer description of the intended UX before building anything (two rough mockups exist: front/back flip vs. 4-view turntable, but neither matched the original vision).
- Retailer affiliate links on shopping results; deeper calendar analytics (wear streaks, cost-per-wear)
- The cutout-generation parallelization only covers the default eager path (`OPTIN_CUTOUTS=False`) — the opt-in confirm-path loop (`add_photo_confirm`) is still sequential. Not worth doing unless that mode gets used.

---

## Testing Status

**Current source of truth: `tests/test_security.py`, run with `python3 -m pytest -q`** — 55 automated tests as of 2026-08-14 (auth flows, cross-user isolation, upload safety, background jobs, admin routes, error/timing logging, link-import's SSRF guard). The phase-by-phase log below is the original manual verification pass from the initial build and is historical — real, but no longer the primary signal of what's covered.

### Phase 1 — Backend & Routes (COMPLETE, done without API key)

| Test | Status | Notes |
|------|--------|-------|
| DB init + all 5 tables | ✅ Pass | `closet.db` created correctly |
| Add / get / delete garment | ✅ Pass | Tags parsed as list, not string |
| Add outfit + worn_count increment | ✅ Pass | worn_count increments on save |
| Calendar log + date upsert | ✅ Pass | Same date overwrites, no duplicates |
| Style profile save/read | ✅ Pass | One-row upsert works |
| ChromaDB embed + semantic search | ✅ Pass | ONNX model, correct top results |
| GET all page routes (/, /add, /outfits, /calendar, /ask) | ✅ Pass | All 200 |
| POST /garments (add garment) | ✅ Pass | 302 redirect to catalog |
| POST /outfits | ✅ Pass | Returns `{"id":1,"ok":true}` |
| POST /calendar | ✅ Pass | Returns `{"ok":true}` |
| DELETE /garments/<id> | ✅ Pass | Returns `{"ok":true}` |
| Claude routes with no API key | ✅ Pass | Clean JSON error, no HTML crash |

### Phase 2 — Claude Features (COMPLETE)

Run these in order after `cp .env.example .env` and adding the key:

**2a. Auto-tag** — ⚠️ Manual test required (needs real photo upload via browser)
```
Skipped in automated run — requires actual image file. Test manually via /add.
```

**2b. Outfit suggestion** ✅ PASS
```
GET /suggest?occasion=smart-casual → Navy Slim Chinos + White Oxford Shirt + Olive Bomber Jacket + Black Derby Shoes (with garment-specific reasoning)
GET /suggest (no occasion) → Blue Jeans + Grey Crewneck Sweater + White Sneakers + Olive Bomber Jacket
```

**2c. Free-text ask (coordinator + tool use)** ✅ PASS
```
"what blue things do I own?" → Listed Navy Slim Chinos + Blue Jeans correctly
"what goes with my White Oxford Shirt?" → Specific pairings from wardrobe
"what have I worn the least?" → Correctly reported no wear history (worn_count=0)
"what is my style?" → Accurate summary referencing actual palette + pieces
```

**2d. Style profile** ✅ PASS
```
POST /style/refresh → Profile paragraph mentions navy/white/grey/olive palette, slim chinos,
Oxford shirt, derbies, jeans, sneakers, bomber — all specific to actual wardrobe.
Subsequent /suggest?occasion=formal references "restrained colour palette" from the profile.
```

**Bug fixed during Phase 2:**
- Python 3.9 incompatibility: `dict | None` and `list[int]` type hints — replaced with `Optional[dict]` and `List[int]` from `typing`

### Phase 3 — End-to-End UI (COMPLETE — browser walkthrough)

Done against the real wardrobe (60 garments) via live browser automation, not curl:
```
1. Catalog (/) → loads with real images, style profile banner, category filters — OK
2. Outfits (/outfits) → built and saved a test outfit (2 garments, 4 stars) → appeared
   correctly in "Saved Outfits", then cleaned up (deleted, worn_count reverted)
3. Calendar (/calendar) → logged a test entry with a garment + note → appeared at the
   top of Recent History with correct thumbnail, then cleaned up
4. Ask / Suggest (/ask) → style profile loads on page load; asked a free-text question
   ("What haven't I worn recently?") → coordinator answered using live DB state,
   correctly referencing the test outfit/log that existed at the time; "Suggest an
   Outfit" (smart casual dinner) → 4 real garments with garment-specific reasoning
5. Shop (/shop) → gap detection, search with budget filter, save/remove wishlist —
   all covered separately when those features were built (see below)
6. No console errors across the full walkthrough
```
**Not covered:** auto-tag photo upload via the browser (the browser automation tool's
file-upload path is broken in this environment — rejects host filesystem paths). Auto-tag
itself was verified earlier via a direct API call with a real image (returns pre-filled
JSON fields); only the literal "upload via /add form" click-path is unverified.
Garment deletion was verified via API in an earlier pass, not repeated here to avoid
touching real catalog data.

### Known edge cases to verify
- Garment with no image → shows 👕 placeholder (not broken)
- Auto-tag on a low-quality/dark photo → should fail gracefully, not crash
- Search with empty closet → `/suggest` returns friendly message, not 500
- Calendar log for same date twice → upserts, doesn't duplicate

---

### Do NOT
- Add sentence-transformers as a dependency — use `ONNXMiniLM_L6_V2` from chromadb instead
- Commit `closet.db`, `chroma_db/`, or `static/uploads/` — all in `.gitignore`
- Add a shopping feature without Tavily — web search is live/dynamic, not a static RAG problem

---

## Coding Guidelines

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
