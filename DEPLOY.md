# Deploying Closet Manager (UAT)

Container-based, targets Fly.io / Railway / Render. All app state lives under
`DATA_DIR` (default `/data` in the image) — mount a **persistent volume** there so
redeploys don't wipe data.

## Required environment variables

| Var | Notes |
|-----|-------|
| `ANTHROPIC_API_KEY` | Claude — detection, tagging, suggestions, ask |
| `GEMINI_API_KEY` | Gemini image model. **Must be on a billing-enabled Google Cloud project** (image gen isn't on the free tier) |
| `SERPER_API_KEY` | Shopping search (serper.dev) |
| `SECRET_KEY` | **Set a fixed value** (`python -c "import secrets;print(secrets.token_hex(32))"`) or sessions reset on every restart |
| `DATA_DIR` | `/data` (the mounted volume). Set in the image already |
| `SECURE_COOKIES` | `1` in production (HTTPS). Set in the image already |
| `ACCESS_CODE` | Shared gate for UAT — testers must enter this to log in. Keeps the URL closed to strangers |
| `FEEDBACK_EMAIL` | Optional — shows a "Feedback" link that mailto:s testers' reports to you |

Optional tuning: `GUNICORN_THREADS` (default 4), cost caps (`DAILY_USER_CAP`,
`DAILY_GLOBAL_CAP`, `MONTHLY_GLOBAL_CAP`), and any image-pipeline feature flag
(see `config.py`).

> **Concurrency note:** runs as a **single gunicorn worker with threads** on
> purpose — local ChromaDB + SQLite + in-process caches don't tolerate multiple
> worker processes. Scale with `GUNICORN_THREADS`, not workers. Fine for UAT load.

## Fly.io

```bash
fly launch --no-deploy                 # generates fly.toml (it detects the Dockerfile)
fly volumes create closet_data --size 3 --region <your-region>
# In fly.toml: mount the volume at /data, and set internal_port = 8080
fly secrets set ANTHROPIC_API_KEY=... GEMINI_API_KEY=... SERPER_API_KEY=... \
                SECRET_KEY=... ACCESS_CODE=... FEEDBACK_EMAIL=...
fly deploy
```
`fly.toml` mount section:
```toml
[[mounts]]
  source = "closet_data"
  destination = "/data"
```
Health check: point it at `/healthz`.

## Railway

1. New project → Deploy from repo (it builds the Dockerfile).
2. Add a **Volume** mounted at `/data` (Service → Settings → Volumes — this is required for persistence; the Dockerfile deliberately has no `VOLUME` instruction because Railway rejects images that declare one).
3. Variables tab → add the env vars above.
4. Railway injects `PORT`; the app already binds to it. Health check path `/healthz`.

## Render

1. New → **Web Service** → from repo, runtime **Docker**.
2. Add a **Disk** mounted at `/data` (size ~3 GB).
3. Environment → add the env vars above. Health check path `/healthz`.

## Migrating your local closet to UAT

A fresh deploy starts with an **empty** `/data` volume — your local garments,
images, and search index won't appear until you move them over. Do this once,
right after the first deploy (before you rely on the UAT copy):

```bash
# 1. Locally — package your closet.db, chroma_db/, uploads/, and caches
./scripts/export_data.sh
# -> writes closet-data-YYYYMMDD-HHMMSS.tar.gz (can be a few hundred MB)
```

Then get that file onto the server and extract it into `DATA_DIR`:

**Fly.io**
```bash
fly ssh sftp shell
> put closet-data-*.tar.gz /tmp/
> exit
fly ssh console -C "bash /app/scripts/import_data.sh /tmp/closet-data-*.tar.gz"
fly apps restart <app-name>
```

**Railway / Render**
Use the platform's shell (Railway `railway run bash`, Render's Shell tab) to
upload the tarball (or `curl` it from a temporary link) into the container, then:
```bash
bash scripts/import_data.sh /path/to/closet-data-*.tar.gz
```
Restart the service afterward.

Logging in with the **same name** ("Jai Yadav") afterward loads the restored
closet — nothing needs to be re-added through the UI. Do this once; after that,
UAT and local diverge as separate copies (there's no ongoing sync).

## Backups

All state is one directory (`/data`): SQLite DB, ChromaDB, images, caches. Snapshot it.

- **Fly:** volumes are snapshotted automatically; `fly volumes snapshots list <vol>`. For an ad-hoc copy: `fly ssh console -C "tar czf - -C /data ." > backup-$(date +%F).tgz`.
- **Railway/Render:** use the platform's disk snapshot feature, or periodically
  `tar czf backup.tgz -C /data .` from a shell and download it.
- The SQLite DB is the critical piece; ChromaDB can be rebuilt by re-embedding, but
  backing up all of `/data` is simplest.

## Local smoke test of the container

```bash
docker build -t closet .
docker run --rm -p 8080:8080 --env-file .env -v $(pwd)/data:/data closet
# open http://localhost:8080/healthz  -> {"status":"ok"}
```

## Before a *public* (non-UAT) launch
`ACCESS_CODE` gates UAT, but the underlying auth is still name-only (anyone who
knows the code can be any name). Add real per-user auth (passwords / magic-link)
before removing the shared gate.
