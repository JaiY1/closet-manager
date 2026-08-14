import sqlite3
import json
from pathlib import Path
from typing import Optional, List

from config import DATA_DIR

DB_PATH = DATA_DIR / "closet.db"

DEFAULT_USER_NAME = "Jai Yadav"


class NameTaken(Exception):
    """Raised when a signup's display name collides with an existing user."""


class EmailTaken(Exception):
    """Raised when a signup/email-link collides with an existing user's email."""


class UserNotFound(Exception):
    """Raised by merge_user_data() when from_user_id or to_user_id doesn't exist."""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL UNIQUE COLLATE NOCASE,
            body_photo_path TEXT DEFAULT '',
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS garments (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER REFERENCES users(id),
            name             TEXT NOT NULL,
            type             TEXT DEFAULT '',
            color            TEXT DEFAULT '',
            brand            TEXT DEFAULT '',
            fit              TEXT DEFAULT '',
            occasion         TEXT DEFAULT '',
            tags             TEXT DEFAULT '[]',
            image_path       TEXT DEFAULT '',
            label_image_path TEXT DEFAULT '',
            material         TEXT DEFAULT '',
            notes            TEXT DEFAULT '',
            worn_count       INTEGER DEFAULT 0,
            added_at         TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS outfits (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER REFERENCES users(id),
            name        TEXT NOT NULL,
            occasion    TEXT DEFAULT '',
            rating      INTEGER DEFAULT 0,
            notes       TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS outfit_items (
            outfit_id   INTEGER REFERENCES outfits(id) ON DELETE CASCADE,
            garment_id  INTEGER REFERENCES garments(id) ON DELETE CASCADE,
            PRIMARY KEY (outfit_id, garment_id)
        );

        CREATE TABLE IF NOT EXISTS outfit_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER REFERENCES users(id),
            date        TEXT NOT NULL,
            outfit_id   INTEGER REFERENCES outfits(id) ON DELETE SET NULL,
            garment_ids TEXT DEFAULT '[]',
            notes       TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, date)
        );

        CREATE TABLE IF NOT EXISTS garment_images (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            garment_id  INTEGER REFERENCES garments(id) ON DELETE CASCADE,
            image_path  TEXT NOT NULL,
            sort_order  INTEGER DEFAULT 0,
            kind        TEXT DEFAULT 'photo'
        );

        CREATE TABLE IF NOT EXISTS style_profile (
            user_id      INTEGER PRIMARY KEY REFERENCES users(id),
            summary      TEXT,
            last_updated TEXT
        );

        CREATE TABLE IF NOT EXISTS wishlist_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER REFERENCES users(id),
            title       TEXT NOT NULL,
            price       TEXT DEFAULT '',
            source      TEXT DEFAULT '',
            link        TEXT DEFAULT '',
            image       TEXT DEFAULT '',
            category    TEXT DEFAULT '',
            reasoning   TEXT DEFAULT '',
            added_at    TEXT DEFAULT (datetime('now'))
        );

        -- One row per real (non-cached) Gemini image generation. Powers cost caps
        -- and the per-user usage meter.
        CREATE TABLE IF NOT EXISTS image_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER REFERENCES users(id),
            kind       TEXT DEFAULT 'cutout',
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- Saved try-on renders, for the history gallery.
        CREATE TABLE IF NOT EXISTS tryon_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER REFERENCES users(id),
            image_path  TEXT NOT NULL,
            garment_ids TEXT DEFAULT '[]',
            created_at  TEXT DEFAULT (datetime('now'))
        );

        -- Every 500 (request-cycle or background-job) gets a row here, so bugs
        -- hit by other users are visible without watching Railway's live
        -- console logs — see app.py's after_request/errorhandler(500) hooks
        -- and the background job runners' except blocks.
        CREATE TABLE IF NOT EXISTS error_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at   TEXT DEFAULT (datetime('now')),
            user_id       INTEGER,
            endpoint      TEXT DEFAULT '',
            error_type    TEXT DEFAULT '',
            error_message TEXT DEFAULT '',
            traceback     TEXT DEFAULT ''
        );

        -- One row per real (non-cached) Gemini image call — how long it
        -- actually took and whether the cutout retry fired. Written from
        -- imagegen.py itself (reconstruct_garment/tryon), not app.py, so every
        -- caller gets this for free with no per-call-site plumbing.
        CREATE TABLE IF NOT EXISTS gemini_timing (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT DEFAULT (datetime('now')),
            kind        TEXT DEFAULT 'cutout',
            duration_ms INTEGER DEFAULT 0,
            retried     INTEGER DEFAULT 0
        );
    """)
    conn.commit()

    # --- Migrations for DBs that predate a column/table ---
    existing = {row[1] for row in conn.execute("PRAGMA table_info(garments)")}
    for col, dflt in [("material", "''"), ("label_image_path", "''"), ("size", "''")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE garments ADD COLUMN {col} TEXT DEFAULT {dflt}")
    img_cols = {row[1] for row in conn.execute("PRAGMA table_info(garment_images)")}
    if "kind" not in img_cols:
        conn.execute("ALTER TABLE garment_images ADD COLUMN kind TEXT DEFAULT 'photo'")
    user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "body_photo_path" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN body_photo_path TEXT DEFAULT ''")
    conn.commit()

    _migrate_to_multiuser(conn)
    _migrate_auth_columns(conn)
    conn.close()


def _migrate_auth_columns(conn):
    """One-time migration: add real per-user auth (email/password/Google) on top
    of the old name-only accounts. Existing rows get auth_provider='legacy' —
    the app prompts them to secure their account with a password or Google."""
    user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "email" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "password_hash" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT DEFAULT ''")
    if "google_sub" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")
    if "auth_provider" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN auth_provider TEXT DEFAULT 'legacy'")
    # Partial unique indexes — NULL/'' emails or google_subs (legacy rows) don't
    # collide with each other, only real values do.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email COLLATE NOCASE) "
        "WHERE email IS NOT NULL AND email != ''"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub ON users(google_sub) "
        "WHERE google_sub IS NOT NULL AND google_sub != ''"
    )
    conn.commit()


def _migrate_to_multiuser(conn):
    """One-time migration: pre-multiuser DBs have no user_id columns and a
    single global style_profile row (id=1). Assign all existing data to a
    default account so nothing is orphaned."""
    garment_cols = {row[1] for row in conn.execute("PRAGMA table_info(garments)")}
    needs_migration = "user_id" not in garment_cols
    if not needs_migration:
        return

    default_user = conn.execute(
        "SELECT id FROM users WHERE name = ?", (DEFAULT_USER_NAME,)
    ).fetchone()
    if default_user:
        default_user_id = default_user['id']
    else:
        cur = conn.execute("INSERT INTO users (name) VALUES (?)", (DEFAULT_USER_NAME,))
        default_user_id = cur.lastrowid

    for table in ("garments", "outfits", "wishlist_items"):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "user_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER REFERENCES users(id)")
        conn.execute(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (default_user_id,))

    # outfit_logs needs its UNIQUE(date) constraint replaced with UNIQUE(user_id, date) —
    # SQLite can't alter constraints in place, so recreate the table.
    log_cols = {row[1] for row in conn.execute("PRAGMA table_info(outfit_logs)")}
    if "user_id" not in log_cols:
        conn.execute("ALTER TABLE outfit_logs RENAME TO outfit_logs_old")
        conn.execute("""
            CREATE TABLE outfit_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER REFERENCES users(id),
                date        TEXT NOT NULL,
                outfit_id   INTEGER REFERENCES outfits(id) ON DELETE SET NULL,
                garment_ids TEXT DEFAULT '[]',
                notes       TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now')),
                UNIQUE(user_id, date)
            )
        """)
        conn.execute(
            """INSERT INTO outfit_logs (id, user_id, date, outfit_id, garment_ids, notes, created_at)
               SELECT id, ?, date, outfit_id, garment_ids, notes, created_at FROM outfit_logs_old""",
            (default_user_id,)
        )
        conn.execute("DROP TABLE outfit_logs_old")

    # style_profile: single global row (id=1) -> keyed by user_id
    profile_cols = {row[1] for row in conn.execute("PRAGMA table_info(style_profile)")}
    if "user_id" not in profile_cols:
        old_row = conn.execute("SELECT summary, last_updated FROM style_profile WHERE id = 1").fetchone()
        conn.execute("ALTER TABLE style_profile RENAME TO style_profile_old")
        conn.execute("""
            CREATE TABLE style_profile (
                user_id      INTEGER PRIMARY KEY REFERENCES users(id),
                summary      TEXT,
                last_updated TEXT
            )
        """)
        if old_row and old_row['summary']:
            conn.execute(
                "INSERT INTO style_profile (user_id, summary, last_updated) VALUES (?, ?, ?)",
                (default_user_id, old_row['summary'], old_row['last_updated'])
            )
        conn.execute("DROP TABLE style_profile_old")

    conn.commit()


# --- Users ---

def get_user_by_name(name: str) -> Optional[dict]:
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE name = ?", (name.strip(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user(user_id: int) -> Optional[dict]:
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_users_with_counts() -> List[dict]:
    """Admin diagnostic for the one-off account-merge flow (see merge_user_data)
    — every user with a quick count of what they own, so the right from/to IDs
    can be identified before merging."""
    conn = get_db()
    rows = conn.execute("""
        SELECT u.id, u.name, u.email, u.auth_provider, u.created_at,
               (SELECT COUNT(*) FROM garments      WHERE user_id = u.id) AS garments,
               (SELECT COUNT(*) FROM outfits       WHERE user_id = u.id) AS outfits,
               (SELECT COUNT(*) FROM outfit_logs   WHERE user_id = u.id) AS outfit_logs,
               (SELECT COUNT(*) FROM wishlist_items WHERE user_id = u.id) AS wishlist_items,
               (SELECT COUNT(*) FROM tryon_history  WHERE user_id = u.id) AS tryon_history
        FROM users u ORDER BY u.id
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def merge_user_data(from_user_id: int, to_user_id: int) -> dict:
    """Move all wardrobe data (garments, outfits, calendar logs, wishlist,
    usage history, style profile, body photo) from one account onto another.
    For the case where a legacy name-only account's real data needs to land on
    a freshly-created email/Google account instead of staying stranded on the
    old one. The source user row is left in place, now empty — not deleted.
    Caller is responsible for re-embedding any moved garments in the vector
    store afterward (this function only touches SQLite)."""
    if from_user_id == to_user_id:
        raise ValueError("from_user_id and to_user_id must differ")
    conn = get_db()
    from_row = conn.execute("SELECT * FROM users WHERE id = ?", (from_user_id,)).fetchone()
    to_row = conn.execute("SELECT * FROM users WHERE id = ?", (to_user_id,)).fetchone()
    if not from_row or not to_row:
        conn.close()
        raise UserNotFound()

    moved = {}
    try:
        for table in ("garments", "outfits", "wishlist_items", "image_events", "tryon_history"):
            cur = conn.execute(f"UPDATE {table} SET user_id = ? WHERE user_id = ?", (to_user_id, from_user_id))
            moved[table] = cur.rowcount

        # outfit_logs has UNIQUE(user_id, date) — move what doesn't collide
        # with a log the destination already has on that date, skip (don't
        # lose) the rest.
        logs = conn.execute("SELECT id, date FROM outfit_logs WHERE user_id = ?", (from_user_id,)).fetchall()
        moved_logs, skipped_dates = 0, []
        for log in logs:
            collision = conn.execute(
                "SELECT 1 FROM outfit_logs WHERE user_id = ? AND date = ?", (to_user_id, log["date"])
            ).fetchone()
            if collision:
                skipped_dates.append(log["date"])
                continue
            conn.execute("UPDATE outfit_logs SET user_id = ? WHERE id = ?", (to_user_id, log["id"]))
            moved_logs += 1
        moved["outfit_logs"] = moved_logs
        if skipped_dates:
            moved["outfit_logs_skipped_dates"] = skipped_dates

        # style_profile: user_id is the PRIMARY KEY, so the destination can only
        # end up with one row. Move the source's only if the destination
        # doesn't already have one; otherwise keep the destination's.
        to_has_profile = conn.execute("SELECT 1 FROM style_profile WHERE user_id = ?", (to_user_id,)).fetchone()
        if to_has_profile:
            conn.execute("DELETE FROM style_profile WHERE user_id = ?", (from_user_id,))
            moved["style_profile"] = "destination already had one — kept it, discarded source's"
        else:
            cur = conn.execute("UPDATE style_profile SET user_id = ? WHERE user_id = ?", (to_user_id, from_user_id))
            moved["style_profile"] = bool(cur.rowcount)

        # body_photo_path lives on the users row itself — only copy it over if
        # the destination doesn't already have one.
        if not to_row["body_photo_path"] and from_row["body_photo_path"]:
            conn.execute("UPDATE users SET body_photo_path = ? WHERE id = ?", (from_row["body_photo_path"], to_user_id))
            moved["body_photo_path"] = True
        else:
            moved["body_photo_path"] = False

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return moved


def get_or_create_user(name: str) -> dict:
    name = name.strip()
    existing = get_user_by_name(name)
    if existing:
        return existing
    conn = get_db()
    cur = conn.execute("INSERT INTO users (name) VALUES (?)", (name,))
    user_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": user_id, "name": name}


def get_user_by_email(email: str) -> Optional[dict]:
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email.strip(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_google_sub(sub: str) -> Optional[dict]:
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE google_sub = ?", (sub,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_user_password(name: str, email: str, password_hash: str) -> dict:
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO users (name, email, password_hash, auth_provider) VALUES (?, ?, ?, 'password')",
            (name.strip(), email.strip(), password_hash)
        )
        user_id = cur.lastrowid
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.close()
        raise EmailTaken() if "email" in str(e) else NameTaken()
    conn.close()
    return {"id": user_id, "name": name.strip(), "email": email.strip()}


def create_user_google(name: str, email: str, google_sub: str) -> dict:
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO users (name, email, google_sub, auth_provider) VALUES (?, ?, ?, 'google')",
            (name.strip(), email.strip(), google_sub)
        )
        user_id = cur.lastrowid
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.close()
        raise EmailTaken() if "email" in str(e) else NameTaken()
    conn.close()
    return {"id": user_id, "name": name.strip(), "email": email.strip()}


def set_user_password(user_id: int, password_hash: str):
    """Set/replace a user's password. Promotes 'google' -> 'both'; leaves 'legacy' as 'password'."""
    conn = get_db()
    conn.execute(
        "UPDATE users SET password_hash = ?, "
        "auth_provider = CASE WHEN auth_provider = 'google' THEN 'both' ELSE 'password' END "
        "WHERE id = ?",
        (password_hash, user_id)
    )
    conn.commit()
    conn.close()


def set_user_email_and_password(user_id: int, email: str, password_hash: str):
    """Used by the legacy-account 'secure your account' flow, which sets an email
    for the first time alongside the password."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE users SET email = ?, password_hash = ?, "
            "auth_provider = CASE WHEN auth_provider = 'google' THEN 'both' ELSE 'password' END "
            "WHERE id = ?",
            (email.strip(), password_hash, user_id)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise EmailTaken()
    conn.close()


def link_google_to_user(user_id: int, google_sub: str, email: str = ""):
    """Attach a Google identity to an already-logged-in account. Fills in email
    only if the account doesn't already have one — never overwrites it."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE users SET google_sub = ?, email = COALESCE(NULLIF(email, ''), NULLIF(?, '')), "
            "auth_provider = CASE WHEN auth_provider = 'password' THEN 'both' ELSE 'google' END "
            "WHERE id = ?",
            (google_sub, email, user_id)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise EmailTaken()
    conn.close()


def get_body_photo(user_id: int) -> str:
    conn = get_db()
    row = conn.execute("SELECT body_photo_path FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return (row["body_photo_path"] if row else "") or ""


def set_body_photo(user_id: int, path: str):
    conn = get_db()
    conn.execute("UPDATE users SET body_photo_path = ? WHERE id = ?", (path, user_id))
    conn.commit()
    conn.close()


def _parse_garment(row) -> dict:
    g = dict(row)
    g['tags'] = json.loads(g.get('tags') or '[]')
    return g


# --- Garments ---

def add_garment(user_id, name, type_, color, brand, fit, occasion, tags, image_path, notes,
                material='', label_image_path='', size='') -> int:
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO garments
               (user_id, name, type, color, brand, fit, occasion, tags, image_path, notes, material, label_image_path, size)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, name, type_, color, brand, fit, occasion, json.dumps(tags or []),
         image_path or '', notes or '', material or '', label_image_path or '', size or '')
    )
    garment_id = cur.lastrowid
    conn.commit()
    conn.close()
    return garment_id


def get_garment(garment_id: int, user_id: int) -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM garments WHERE id = ? AND user_id = ?", (garment_id, user_id)
    ).fetchone()
    conn.close()
    return _parse_garment(row) if row else None


def get_all_garments(user_id: int) -> List[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM garments WHERE user_id = ? ORDER BY added_at DESC", (user_id,)
    ).fetchall()
    conn.close()
    return [_parse_garment(r) for r in rows]


def update_garment(garment_id: int, user_id: int, **fields):
    if not fields:
        return
    if 'tags' in fields:
        fields['tags'] = json.dumps(fields['tags'])
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn = get_db()
    conn.execute(
        f"UPDATE garments SET {set_clause} WHERE id = ? AND user_id = ?",
        [*fields.values(), garment_id, user_id]
    )
    conn.commit()
    conn.close()


def delete_garment(garment_id: int, user_id: int):
    conn = get_db()
    conn.execute("DELETE FROM garments WHERE id = ? AND user_id = ?", (garment_id, user_id))
    conn.commit()
    conn.close()


def add_garment_image(garment_id: int, image_path: str, sort_order: int = 0, kind: str = 'photo'):
    conn = get_db()
    conn.execute(
        "INSERT INTO garment_images (garment_id, image_path, sort_order, kind) VALUES (?, ?, ?, ?)",
        (garment_id, image_path, sort_order, kind)
    )
    conn.commit()
    conn.close()


def get_all_garment_images(user_id: int) -> dict:
    """Returns {garment_id: [path1, path2, ...]} of gallery photos for this user's garments."""
    conn = get_db()
    rows = conn.execute(
        """SELECT gi.garment_id, gi.image_path FROM garment_images gi
           JOIN garments g ON g.id = gi.garment_id
           WHERE gi.kind = 'photo' AND g.user_id = ?
           ORDER BY gi.garment_id, gi.sort_order""",
        (user_id,)
    ).fetchall()
    conn.close()
    result: dict = {}
    for row in rows:
        result.setdefault(row['garment_id'], []).append(row['image_path'])
    return result


def get_garment_images(garment_id: int) -> List[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT id, image_path FROM garment_images WHERE garment_id = ? AND kind = 'photo' ORDER BY sort_order",
        (garment_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_garment_image_paths(garment_id: int) -> List[str]:
    """All extra image paths (photos and labels) for a garment — used for file cleanup."""
    conn = get_db()
    rows = conn.execute(
        "SELECT image_path FROM garment_images WHERE garment_id = ?", (garment_id,)
    ).fetchall()
    conn.close()
    return [r['image_path'] for r in rows]


def delete_garment_image(image_id: int, garment_id: int) -> Optional[str]:
    """Delete an extra image only if it belongs to the garment; returns its path."""
    conn = get_db()
    row = conn.execute(
        "SELECT image_path FROM garment_images WHERE id = ? AND garment_id = ?",
        (image_id, garment_id)
    ).fetchone()
    if row:
        conn.execute("DELETE FROM garment_images WHERE id = ?", (image_id,))
        conn.commit()
    conn.close()
    return row['image_path'] if row else None


def _increment_worn(garment_id: int):
    conn = get_db()
    conn.execute("UPDATE garments SET worn_count = worn_count + 1 WHERE id = ?", (garment_id,))
    conn.commit()
    conn.close()


def _decrement_worn(garment_id: int):
    conn = get_db()
    conn.execute("UPDATE garments SET worn_count = MAX(worn_count - 1, 0) WHERE id = ?", (garment_id,))
    conn.commit()
    conn.close()


# --- Outfits ---

def _owned_garment_ids(conn, user_id: int, garment_ids) -> list:
    """Filter caller-supplied garment ids down to ones this user actually owns.
    Garment ids are small sequential integers, so trusting them as-is would let
    one account attach (and read back / bump worn_count on) another account's
    garments."""
    ids = []
    for g in garment_ids or []:
        try:
            ids.append(int(g))
        except (TypeError, ValueError):
            continue
    if not ids:
        return []
    placeholders = ','.join('?' * len(ids))
    rows = conn.execute(
        f"SELECT id FROM garments WHERE id IN ({placeholders}) AND user_id = ?",
        [*ids, user_id]
    ).fetchall()
    owned = {r['id'] for r in rows}
    # Dedupe (preserving order) — a repeated id would double-bump worn_count
    seen = set()
    out = []
    for g in ids:
        if g in owned and g not in seen:
            seen.add(g)
            out.append(g)
    return out


def add_outfit(user_id, name, occasion, rating, notes, garment_ids) -> int:
    conn = get_db()
    garment_ids = _owned_garment_ids(conn, user_id, garment_ids)
    cur = conn.execute(
        "INSERT INTO outfits (user_id, name, occasion, rating, notes) VALUES (?, ?, ?, ?, ?)",
        (user_id, name, occasion or '', rating or 0, notes or '')
    )
    outfit_id = cur.lastrowid
    for gid in garment_ids:
        conn.execute("INSERT OR IGNORE INTO outfit_items VALUES (?, ?)", (outfit_id, gid))
    conn.commit()
    conn.close()
    for gid in garment_ids:
        _increment_worn(gid)
    return outfit_id


def get_outfits(user_id: int, limit: int = 50) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM outfits WHERE user_id = ? ORDER BY rating DESC, created_at DESC LIMIT ?",
        (user_id, limit)
    ).fetchall()
    outfits = []
    for row in rows:
        o = dict(row)
        items = conn.execute(
            "SELECT g.* FROM garments g JOIN outfit_items oi ON g.id = oi.garment_id WHERE oi.outfit_id = ?",
            (o['id'],)
        ).fetchall()
        o['garments'] = [_parse_garment(i) for i in items]
        outfits.append(o)
    conn.close()
    return outfits


def get_outfit(outfit_id: int, user_id: int) -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM outfits WHERE id = ? AND user_id = ?", (outfit_id, user_id)
    ).fetchone()
    if not row:
        conn.close()
        return None
    o = dict(row)
    items = conn.execute(
        "SELECT g.* FROM garments g JOIN outfit_items oi ON g.id = oi.garment_id WHERE oi.outfit_id = ?",
        (outfit_id,)
    ).fetchall()
    o['garments'] = [_parse_garment(i) for i in items]
    conn.close()
    return o


# --- Calendar ---

def log_outfit(user_id: int, date: str, outfit_id, garment_ids: list, notes: str):
    conn = get_db()
    garment_ids = _owned_garment_ids(conn, user_id, garment_ids)
    if outfit_id is not None:
        owned = conn.execute(
            "SELECT 1 FROM outfits WHERE id = ? AND user_id = ?", (outfit_id, user_id)
        ).fetchone()
        if not owned:
            outfit_id = None
    prev = conn.execute(
        "SELECT garment_ids FROM outfit_logs WHERE user_id = ? AND date = ?", (user_id, date)
    ).fetchone()
    prev_ids = json.loads(prev['garment_ids'] or '[]') if prev else []
    conn.execute(
        """INSERT INTO outfit_logs (user_id, date, outfit_id, garment_ids, notes) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id, date) DO UPDATE SET
               outfit_id = excluded.outfit_id,
               garment_ids = excluded.garment_ids,
               notes = excluded.notes""",
        (user_id, date, outfit_id, json.dumps(garment_ids or []), notes or '')
    )
    conn.commit()
    conn.close()
    # Re-logging a date replaces that day's outfit — undo the old counts first
    # so worn_count doesn't inflate on every correction
    for gid in prev_ids:
        _decrement_worn(gid)
    for gid in garment_ids or []:
        _increment_worn(gid)


def get_calendar(user_id: int, limit: int = 30) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM outfit_logs WHERE user_id = ? ORDER BY date DESC LIMIT ?", (user_id, limit)
    ).fetchall()
    logs = []
    for row in rows:
        log = dict(row)
        gids = json.loads(log.get('garment_ids') or '[]')
        log['garment_ids'] = gids
        if gids:
            placeholders = ','.join('?' * len(gids))
            garments = conn.execute(
                f"SELECT id, name, type, color, image_path FROM garments WHERE id IN ({placeholders}) AND user_id = ?",
                [*gids, user_id]
            ).fetchall()
            log['garments'] = [dict(g) for g in garments]
        else:
            log['garments'] = []
        logs.append(log)
    conn.close()
    return logs


# --- Style Profile ---

def get_style_profile(user_id: int) -> Optional[dict]:
    conn = get_db()
    row = conn.execute("SELECT * FROM style_profile WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def save_style_profile(user_id: int, summary: str):
    conn = get_db()
    conn.execute(
        """INSERT INTO style_profile (user_id, summary, last_updated) VALUES (?, ?, datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET summary = excluded.summary, last_updated = excluded.last_updated""",
        (user_id, summary)
    )
    conn.commit()
    conn.close()


# --- Wishlist ---

def add_wishlist_item(user_id, title, price, source, link, image, category, reasoning) -> int:
    conn = get_db()
    if link:
        existing = conn.execute(
            "SELECT id FROM wishlist_items WHERE user_id = ? AND link = ?", (user_id, link)
        ).fetchone()
        if existing:
            conn.close()
            return existing['id']
    cur = conn.execute(
        """INSERT INTO wishlist_items (user_id, title, price, source, link, image, category, reasoning)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, title, price or '', source or '', link or '', image or '', category or '', reasoning or '')
    )
    conn.commit()
    item_id = cur.lastrowid
    conn.close()
    return item_id


def get_wishlist_items(user_id: int) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM wishlist_items WHERE user_id = ? ORDER BY added_at DESC", (user_id,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def delete_wishlist_item(item_id: int, user_id: int):
    conn = get_db()
    conn.execute("DELETE FROM wishlist_items WHERE id = ? AND user_id = ?", (item_id, user_id))
    conn.commit()
    conn.close()


# --- Image generation events (cost caps + usage meter) ---

def record_image_event(user_id: int, kind: str = "cutout"):
    conn = get_db()
    conn.execute("INSERT INTO image_events (user_id, kind) VALUES (?, ?)", (user_id, kind))
    conn.commit()
    conn.close()


def count_images_today(user_id: Optional[int] = None) -> int:
    """Count real generations since UTC midnight. Global when user_id is None."""
    conn = get_db()
    if user_id is None:
        row = conn.execute(
            "SELECT COUNT(*) FROM image_events WHERE created_at >= date('now')"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM image_events WHERE user_id = ? AND created_at >= date('now')",
            (user_id,)
        ).fetchone()
    conn.close()
    return row[0]


def count_images_month(user_id: Optional[int] = None) -> int:
    conn = get_db()
    if user_id is None:
        row = conn.execute(
            "SELECT COUNT(*) FROM image_events WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m','now')"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM image_events WHERE user_id = ? "
            "AND strftime('%Y-%m', created_at) = strftime('%Y-%m','now')",
            (user_id,)
        ).fetchone()
    conn.close()
    return row[0]


# --- Try-on history ---

def add_tryon_history(user_id: int, image_path: str, garment_ids: list) -> int:
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO tryon_history (user_id, image_path, garment_ids) VALUES (?, ?, ?)",
        (user_id, image_path, json.dumps(garment_ids))
    )
    conn.commit()
    item_id = cur.lastrowid
    conn.close()
    return item_id


def get_tryon_history(user_id: int, limit: int = 12) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM tryon_history WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["garment_ids"] = json.loads(d.get("garment_ids") or "[]")
        except (ValueError, TypeError):
            d["garment_ids"] = []
        out.append(d)
    return out


_ERROR_LOG_CAP = 1000  # keep growth bounded — a bug that errors on every request shouldn't fill the disk


def log_error(endpoint: str, error_type: str, error_message: str, traceback: str = "", user_id=None):
    """Record a 500 (or background-job failure) so it's visible without
    watching Railway's live console — see app.py's after_request/
    errorhandler(500) hooks and the background job runners. Caller's
    responsibility to catch their own exceptions around this call; a failure
    here must never be allowed to break whatever was already failing."""
    conn = get_db()
    conn.execute(
        "INSERT INTO error_log (user_id, endpoint, error_type, error_message, traceback) VALUES (?, ?, ?, ?, ?)",
        (user_id, endpoint, error_type, (error_message or "")[:2000], traceback)
    )
    conn.execute(
        "DELETE FROM error_log WHERE id NOT IN (SELECT id FROM error_log ORDER BY id DESC LIMIT ?)",
        (_ERROR_LOG_CAP,)
    )
    conn.commit()
    conn.close()


def get_recent_errors(limit: int = 100) -> list:
    conn = get_db()
    rows = conn.execute("SELECT * FROM error_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


_GEMINI_TIMING_CAP = 2000  # keep growth bounded, same reasoning as error_log's cap


def record_gemini_timing(kind: str, duration_ms: int, retried: bool = False):
    """Called from imagegen.py itself after every real (non-cached) Gemini
    call, so timing data doesn't need separate plumbing at each of
    reconstruct_garment's several app.py call sites. Best-effort — a metrics
    write must never be allowed to break the image generation it's timing."""
    conn = get_db()
    conn.execute(
        "INSERT INTO gemini_timing (kind, duration_ms, retried) VALUES (?, ?, ?)",
        (kind, int(duration_ms), int(bool(retried)))
    )
    conn.execute(
        "DELETE FROM gemini_timing WHERE id NOT IN (SELECT id FROM gemini_timing ORDER BY id DESC LIMIT ?)",
        (_GEMINI_TIMING_CAP,)
    )
    conn.commit()
    conn.close()


def get_gemini_timing_summary() -> dict:
    """Per-kind count/avg/min/max/retry-rate, plus the 20 most recent raw
    samples for spot-checking outliers."""
    conn = get_db()
    by_kind = [dict(r) for r in conn.execute("""
        SELECT kind, COUNT(*) AS count,
               ROUND(AVG(duration_ms)) AS avg_ms,
               MIN(duration_ms) AS min_ms,
               MAX(duration_ms) AS max_ms,
               ROUND(100.0 * SUM(retried) / COUNT(*), 1) AS retry_pct
        FROM gemini_timing GROUP BY kind ORDER BY kind
    """).fetchall()]
    recent = [dict(r) for r in conn.execute(
        "SELECT * FROM gemini_timing ORDER BY id DESC LIMIT 20"
    ).fetchall()]
    conn.close()
    return {"by_kind": by_kind, "recent": recent}
