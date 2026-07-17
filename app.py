import base64
import json
import logging
import os
import secrets
import shutil
import tarfile
import time
import uuid
from io import BytesIO
from pathlib import Path

from PIL import Image
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from config import (
    ANTHROPIC_API_KEY, GEMINI_API_KEY, SECRET_KEY, DATA_DIR,
    BODY_PHOTO_GATE, OPTIN_CUTOUTS, PERSIST_TRYON,
    DAILY_USER_CAP, DAILY_GLOBAL_CAP, MONTHLY_GLOBAL_CAP,
    ACCESS_CODE, FEEDBACK_EMAIL, SECURE_COOKIES,
)
from database import (
    init_db, add_garment, get_garment, get_all_garments, update_garment, delete_garment,
    add_garment_image, get_all_garment_images, get_garment_images, delete_garment_image,
    get_garment_image_paths, add_outfit, get_outfits, log_outfit, get_calendar, get_style_profile,
    add_wishlist_item, get_wishlist_items, delete_wishlist_item,
    get_or_create_user, get_user, get_body_photo, set_body_photo,
    record_image_event, count_images_today, count_images_month,
    add_tryon_history, get_tryon_history,
)
from vector_store import embed_garment, delete_garment as vs_delete
from agents import (
    run_coordinator, suggest_outfit, refresh_style_profile, detect_outfit_rag,
    shopper_agent, identify_wardrobe_gaps, invalidate_gaps_cache
)
from vision import autotag_garment, autotag_label, detect_garments_in_photo, check_body_photo
from imagegen import reconstruct_garment, tryon, remove_bg, cutout_cached

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("closet")

app = Flask(__name__)
if not SECRET_KEY:
    logger.warning("SECRET_KEY not set — using a random key; sessions won't survive a restart. Set it in production.")
app.secret_key = SECRET_KEY or secrets.token_hex(32)

# Behind the platform's TLS load balancer, trust one hop of X-Forwarded-* so
# request.scheme / remote_addr are correct.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.config.update(
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,   # reject >12 MB uploads before disk/memory
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=SECURE_COOKIES,  # True in prod (HTTPS); off locally
)


@app.errorhandler(413)
def _too_large(e):
    return jsonify({"error": "That image is too large (max 12 MB). Please use a smaller file."}), 413


@app.errorhandler(404)
def _not_found(e):
    if request.path.startswith(('/add/photo/', '/suggest/', '/settings/', '/garments/', '/usage', '/tryon/')):
        return jsonify({"error": "Not found"}), 404
    return render_template("error.html", code=404,
                           message="That page doesn't exist."), 404


@app.errorhandler(500)
def _server_error(e):
    logger.exception("Unhandled 500 on %s", request.path)
    return render_template("error.html", code=500,
                           message="Something went wrong on our end. Please try again."), 500


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})

STATIC_DIR = Path(__file__).parent / "static"   # app assets (css/icons/manifest/sw) — ship with code
UPLOAD_DIR = DATA_DIR / "uploads"                # user/generated images — live under DATA_DIR
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _migrate_uploads():
    """One-time: relocate legacy static/uploads into DATA_DIR/uploads. Idempotent —
    only moves files that aren't already at the destination."""
    legacy = STATIC_DIR / "uploads"
    if legacy.resolve() == UPLOAD_DIR.resolve() or not legacy.is_dir():
        return
    moved = 0
    for f in legacy.iterdir():
        dest = UPLOAD_DIR / f.name
        if not dest.exists():
            shutil.move(str(f), str(dest))
            moved += 1
    try:
        legacy.rmdir()
    except OSError:
        pass
    if moved:
        print(f"Migrated {moved} upload(s) from static/uploads to {UPLOAD_DIR}")


_migrate_uploads()

init_db()


def _unique_name(prefix: str, ext: str) -> str:
    """Timestamp alone collides when two uploads land in the same second."""
    return f"{prefix}{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"


_ALLOWED_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.heic', '.heif', '.bmp', '.tiff'}


def _safe_ext(filename: str) -> str:
    """Only ever persist a known image extension — never .html/.svg/.exe etc."""
    ext = Path(filename or '').suffix.lower()
    return ext if ext in _ALLOWED_EXTS else '.jpg'


def _delete_upload(rel_path):
    """Delete a file under the uploads dir; ignores empty or out-of-tree paths.
    Stored paths look like 'uploads/x.png' and resolve under DATA_DIR."""
    if not rel_path:
        return
    p = (DATA_DIR / rel_path).resolve()
    if UPLOAD_DIR.resolve() in p.parents and p.is_file():
        p.unlink()


def _crop_bbox(image_bytes, bbox, pad=0.12):
    """Crop a garment out of a full photo using a normalized [l,t,r,b] bbox,
    padded ~12% so the reconstruction has full context. Bad boxes fall back to
    the whole image rather than crashing."""
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    try:
        l, t, r, b = [float(v) for v in bbox]
    except (TypeError, ValueError):
        l, t, r, b = 0.0, 0.0, 1.0, 1.0
    l, r = sorted((max(0.0, min(1.0, l)), max(0.0, min(1.0, r))))
    t, b = sorted((max(0.0, min(1.0, t)), max(0.0, min(1.0, b))))
    bw, bh = r - l, b - t
    l, r = max(0.0, l - bw * pad), min(1.0, r + bw * pad)
    t, b = max(0.0, t - bh * pad), min(1.0, b + bh * pad)
    box = (int(l * w), int(t * h), int(r * w), int(b * h))
    crop = img if (box[2] - box[0] < 4 or box[3] - box[1] < 4) else img.crop(box)
    out = BytesIO()
    crop.save(out, format="JPEG", quality=92)
    return out.getvalue()


def _cap_blocked(uid):
    """Return a user-facing message if a new image generation would exceed a cost
    cap, else None. Checked before every real (non-cached) Gemini image call."""
    if DAILY_USER_CAP and count_images_today(uid) >= DAILY_USER_CAP:
        return f"You've hit today's limit of {DAILY_USER_CAP} image generations. Try again tomorrow."
    if DAILY_GLOBAL_CAP and count_images_today() >= DAILY_GLOBAL_CAP:
        return "The app has reached its daily image-generation limit. Please try again tomorrow."
    if MONTHLY_GLOBAL_CAP and count_images_month() >= MONTHLY_GLOBAL_CAP:
        return "The app has reached its monthly image-generation limit. Please try again next month."
    return None


def _friendly_gemini_error(e) -> str:
    """Turn a raw Gemini SDK exception into a clean user-facing message."""
    low = str(e).lower()
    if any(k in low for k in ("resource_exhausted", "429", "quota", "rate limit")):
        return "Image generation is temporarily busy (rate limit reached). Please try again in a minute."
    if any(k in low for k in ("billing", "permission", "403", "api key")):
        return "Image generation is currently unavailable. Please try again later."
    if any(k in low for k in ("safety", "blocked", "refused", "no image")):
        return "That photo couldn't be processed. Try a clearer or different image."
    return "Image generation failed. Please try again."


# --- Auth ---

@app.before_request
def require_login():
    if request.endpoint in ('login_page', 'do_login', 'static', 'service_worker', 'serve_upload', 'healthz', 'admin_import_data') or request.endpoint is None:
        return
    if 'user_id' not in session:
        return redirect(url_for('login_page'))


@app.context_processor
def inject_current_user():
    ctx = {"current_user": None, "feedback_email": FEEDBACK_EMAIL}
    if 'user_id' in session:
        ctx["current_user"] = {"id": session['user_id'], "name": session.get('user_name', '')}
    return ctx


@app.route("/login")
def login_page():
    if 'user_id' in session:
        return redirect(url_for('index'))
    return render_template("login.html", require_code=bool(ACCESS_CODE))


@app.route("/login", methods=["POST"])
def do_login():
    name = (request.form.get('name') or '').strip()
    if not name:
        return render_template("login.html", require_code=bool(ACCESS_CODE), error="Please enter a name.")
    # Shared access gate for UAT — only enforced when ACCESS_CODE is configured.
    if ACCESS_CODE and (request.form.get('code') or '').strip() != ACCESS_CODE:
        return render_template("login.html", require_code=True, error="Incorrect access code.")
    user = get_or_create_user(name)
    session['user_id'] = user['id']
    session['user_name'] = user['name']
    return redirect(url_for('index'))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('login_page'))


# --- One-time local-to-server data migration ---
# Platforms like Railway don't offer a shell/SFTP into the running container, so
# this is the portable way to get a scripts/export_data.sh tarball onto the
# server's DATA_DIR volume: POST it here instead. Gated by the same ACCESS_CODE
# used for login (a deliberate choice — anyone who can log in via the UAT code
# could also run this). Refuses entirely if ACCESS_CODE isn't set, since without
# it there'd be no gate at all.

_ADMIN_IMPORT_MAX_BYTES = 512 * 1024 * 1024  # generous for a full data export


@app.before_request
def _admin_import_upload_limit():
    if request.endpoint == 'admin_import_data':
        request.max_content_length = _ADMIN_IMPORT_MAX_BYTES


@app.route("/admin/import-data", methods=["POST"])
def admin_import_data():
    """Extract a scripts/export_data.sh tarball into DATA_DIR, merging/overwriting
    matching files. Usage: curl -F "code=<ACCESS_CODE>" -F "data=@closet-data-*.tar.gz" <url>/admin/import-data
    Restart the app afterward so DB/vector-store connections see the new files."""
    if not ACCESS_CODE:
        return jsonify({"error": "ACCESS_CODE is not configured — refusing to run unguarded."}), 403
    if (request.form.get('code') or '') != ACCESS_CODE:
        return jsonify({"error": "Invalid or missing access code."}), 403
    if 'data' not in request.files or not request.files['data'].filename:
        return jsonify({"error": "No tarball provided (multipart field name: data)."}), 400

    data_dir_resolved = DATA_DIR.resolve()
    tmp_path = data_dir_resolved / f"_import_upload_{uuid.uuid4().hex}.tar.gz"
    request.files['data'].save(tmp_path)

    try:
        with tarfile.open(tmp_path, "r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                # Reject path traversal, absolute paths, and symlinks before
                # extracting anything — this archive came in over the network.
                member_path = (data_dir_resolved / member.name).resolve()
                if member_path != data_dir_resolved and data_dir_resolved not in member_path.parents:
                    return jsonify({"error": f"Refusing unsafe path in archive: {member.name}"}), 400
                if member.issym() or member.islnk():
                    return jsonify({"error": f"Refusing symlink in archive: {member.name}"}), 400
            tar.extractall(path=data_dir_resolved)
            extracted_count = len(members)
    except tarfile.TarError as e:
        return jsonify({"error": f"Invalid tarball: {e}"}), 400
    finally:
        tmp_path.unlink(missing_ok=True)

    logger.info("Admin data import: extracted %d entries into %s", extracted_count, DATA_DIR)
    return jsonify({
        "ok": True,
        "extracted_count": extracted_count,
        "note": "Restart the app service now so the DB/vector-index pick up the new data.",
    })


@app.route("/sw.js")
def service_worker():
    """Served from root so the service worker's scope covers the whole app."""
    resp = send_from_directory(STATIC_DIR, "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/static/uploads/<path:filename>")
def serve_upload(filename):
    """Uploaded/generated images live under DATA_DIR/uploads (outside the code's
    static/ folder) but keep their /static/uploads/... URLs. This more-specific
    route overrides Flask's built-in static handler for that subpath."""
    return send_from_directory(UPLOAD_DIR, filename)


# --- Catalog ---

@app.route("/")
def index():
    uid = session['user_id']
    garments = get_all_garments(uid)
    extra_images = get_all_garment_images(uid)
    for g in garments:
        extras = extra_images.get(g['id'], [])
        g['all_images'] = ([g['image_path']] if g['image_path'] else []) + extras
    profile = get_style_profile(uid)
    return render_template("index.html", garments=garments, profile=profile)


@app.route("/add")
def add_page():
    return render_template("add.html")


@app.route("/garments", methods=["POST"])
def create_garment():
    uid = session['user_id']
    data = request.form
    image_path = ""

    if 'image' in request.files and request.files['image'].filename:
        f = request.files['image']
        ext = _safe_ext(f.filename)
        filename = _unique_name("", ext)
        f.save(UPLOAD_DIR / filename)
        image_path = f"uploads/{filename}"

    label_image_path = ""
    if 'label_image' in request.files and request.files['label_image'].filename:
        f = request.files['label_image']
        ext = _safe_ext(f.filename)
        filename = _unique_name("label_", ext)
        f.save(UPLOAD_DIR / filename)
        label_image_path = f"uploads/{filename}"

    tags = [t.strip() for t in data.get('tags', '').split(',') if t.strip()]

    garment_id = add_garment(
        user_id=uid,
        name=data.get('name', '').strip(),
        type_=data.get('type', ''),
        color=data.get('color', ''),
        brand=data.get('brand', ''),
        fit=data.get('fit', ''),
        occasion=data.get('occasion', ''),
        tags=tags,
        image_path=image_path,
        notes=data.get('notes', ''),
        material=data.get('material', ''),
        label_image_path=label_image_path,
        size=data.get('size', ''),
    )

    # Save newly added extra photos
    for i, f in enumerate(request.files.getlist('extra_images')):
        if f and f.filename:
            ext = _safe_ext(f.filename)
            fname = _unique_name(f"extra_{garment_id}_{i}_", ext)
            f.save(UPLOAD_DIR / fname)
            add_garment_image(garment_id, f"uploads/{fname}", sort_order=i)

    # Save extra label photos — recorded as kind='label' so they're tracked
    # (excluded from the gallery) and cleaned up when the garment is deleted
    for i, f in enumerate(request.files.getlist('extra_label_images')):
        if f and f.filename:
            ext = _safe_ext(f.filename)
            fname = _unique_name(f"label_{garment_id}_{i}_", ext)
            f.save(UPLOAD_DIR / fname)
            add_garment_image(garment_id, f"uploads/{fname}", sort_order=i, kind='label')

    garment = get_garment(garment_id, uid)
    embed_garment(garment)
    invalidate_gaps_cache(uid)

    return redirect(url_for('index'))


@app.route("/garments/<int:garment_id>/edit", methods=["GET"])
def edit_page(garment_id):
    uid = session['user_id']
    g = get_garment(garment_id, uid)
    if not g:
        return redirect(url_for('index'))
    extra_images = get_garment_images(garment_id)
    return render_template("edit.html", g=g, extra_images=extra_images)


@app.route("/garments/<int:garment_id>/edit", methods=["POST"])
def update_garment_route(garment_id):
    uid = session['user_id']
    existing = get_garment(garment_id, uid)
    if not existing:
        return redirect(url_for('index'))

    data = request.form
    image_path = None

    if 'image' in request.files and request.files['image'].filename:
        f = request.files['image']
        ext = _safe_ext(f.filename)
        filename = _unique_name("", ext)
        f.save(UPLOAD_DIR / filename)
        image_path = f"uploads/{filename}"

    fields = dict(
        name=data.get('name', '').strip(),
        type=data.get('type', ''),
        color=data.get('color', ''),
        brand=data.get('brand', ''),
        fit=data.get('fit', ''),
        occasion=data.get('occasion', ''),
        material=data.get('material', ''),
        size=data.get('size', ''),
        notes=data.get('notes', ''),
        tags=[t.strip() for t in data.get('tags', '').split(',') if t.strip()],
    )
    if image_path:
        fields['image_path'] = image_path

    update_garment(garment_id, uid, **fields)
    if image_path and existing.get('image_path'):
        _delete_upload(existing['image_path'])

    for i, f in enumerate(request.files.getlist('extra_images')):
        if f and f.filename:
            ext = _safe_ext(f.filename)
            fname = _unique_name(f"extra_{garment_id}_{i}_", ext)
            f.save(UPLOAD_DIR / fname)
            add_garment_image(garment_id, f"uploads/{fname}", sort_order=i)

    garment = get_garment(garment_id, uid)
    embed_garment(garment)
    invalidate_gaps_cache(uid)

    return redirect(url_for('index'))


@app.route("/garments/<int:garment_id>/images/<int:image_id>", methods=["DELETE"])
def remove_garment_image(garment_id, image_id):
    uid = session['user_id']
    if not get_garment(garment_id, uid):
        return jsonify({"error": "Garment not found"}), 404
    path = delete_garment_image(image_id, garment_id)
    if path is None:
        return jsonify({"error": "Image not found for this garment"}), 404
    _delete_upload(path)
    return jsonify({"ok": True})


@app.route("/garments/<int:garment_id>", methods=["DELETE"])
def remove_garment(garment_id):
    uid = session['user_id']
    g = get_garment(garment_id, uid)
    if not g:
        return jsonify({"ok": True})
    extra_paths = get_garment_image_paths(garment_id)
    vs_delete(garment_id)
    delete_garment(garment_id, uid)
    for p in [g.get('image_path'), g.get('label_image_path'), *extra_paths]:
        _delete_upload(p)
    invalidate_gaps_cache(uid)
    return jsonify({"ok": True})


@app.route("/garments/autotag", methods=["POST"])
def autotag():
    if 'image' not in request.files or not request.files['image'].filename:
        return jsonify({"error": "No image provided"}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not set in .env"}), 503
    try:
        result = autotag_garment(request.files['image'].read())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/garments/autotag/label", methods=["POST"])
def autotag_label_route():
    if 'image' not in request.files or not request.files['image'].filename:
        return jsonify({"error": "No image provided"}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not set in .env"}), 503
    try:
        result = autotag_label(request.files['image'].read())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Photo import (outfit photo -> garment cutouts) ---

@app.route("/add/photo")
def add_photo_page():
    return render_template("add_photo.html")


@app.route("/add/photo/analyze", methods=["POST"])
def add_photo_analyze():
    """Detect every garment in an uploaded outfit photo, reconstruct each as a
    clean transparent cutout, and return previews (base64) for review. Nothing is
    saved to the closet here — the user confirms via /add/photo/confirm."""
    if 'image' not in request.files or not request.files['image'].filename:
        return jsonify({"error": "No image provided"}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not set in .env"}), 503
    # In opt-in mode we don't call Gemini here (reconstruction happens on confirm),
    # so only require the Gemini key up front in the eager mode.
    if not OPTIN_CUTOUTS and not GEMINI_API_KEY:
        return jsonify({"error": "GEMINI_API_KEY not set in .env"}), 503

    uid = session['user_id']
    photo_bytes = request.files['image'].read()
    try:
        detected = detect_garments_in_photo(photo_bytes)
    except Exception as e:
        return jsonify({"error": f"Detection failed: {e}"}), 500

    items = []
    for g in detected:
        try:
            crop = _crop_bbox(photo_bytes, g.get('bbox'))
            g.pop('bbox', None)
            # Always carry the crop so the review grid can preview/regenerate a cutout.
            g['crop_b64'] = base64.b64encode(crop).decode('ascii')
            if not OPTIN_CUTOUTS:
                # Eager path: reconstruct every item up front.
                desc = ", ".join(x for x in [g.get('color'), g.get('name'), g.get('notes')] if x)
                will_bill = not cutout_cached(crop, desc)
                if will_bill:
                    blocked = _cap_blocked(uid)
                    if blocked:
                        g['error'] = blocked
                        items.append(g)
                        continue
                png = reconstruct_garment(crop, desc)
                if will_bill:
                    record_image_event(uid, 'cutout')
                g['cutout_b64'] = base64.b64encode(png).decode('ascii')
            items.append(g)
        except Exception as e:
            # One garment failing shouldn't sink the whole batch.
            g['error'] = _friendly_gemini_error(e)
            g.pop('bbox', None)
            items.append(g)

    return jsonify({"garments": items, "optin": OPTIN_CUTOUTS})


@app.route("/add/photo/cutout", methods=["POST"])
def add_photo_cutout():
    """Generate — or regenerate — a single cutout from a crop, for preview in the
    review grid. Bills one generation; regenerate (force) re-rolls a fresh image."""
    uid = session['user_id']
    if not GEMINI_API_KEY:
        return jsonify({"error": "GEMINI_API_KEY not set in .env"}), 503
    data = request.json or {}
    crop_b64 = data.get('crop_b64')
    if not crop_b64:
        return jsonify({"error": "No crop provided"}), 400
    try:
        crop = base64.b64decode(crop_b64)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid crop"}), 400

    force = bool(data.get('regenerate'))
    desc = ", ".join(x for x in [data.get('color'), data.get('name'), data.get('notes')] if x)
    will_bill = force or not cutout_cached(crop, desc)
    if will_bill:
        blocked = _cap_blocked(uid)
        if blocked:
            return jsonify({"error": blocked, "cap": True}), 429
    try:
        png = reconstruct_garment(crop, desc, force=force)
    except Exception as e:
        return jsonify({"error": _friendly_gemini_error(e)}), 502
    if will_bill:
        record_image_event(uid, 'cutout')
    return jsonify({"cutout_b64": base64.b64encode(png).decode('ascii')})


@app.route("/usage")
def usage():
    """Per-user generation counts + caps, for the usage meter."""
    uid = session['user_id']
    return jsonify({
        "today": count_images_today(uid),
        "month": count_images_month(uid),
        "daily_cap": DAILY_USER_CAP,
        "monthly_cap": MONTHLY_GLOBAL_CAP,
    })


@app.route("/add/photo/confirm", methods=["POST"])
def add_photo_confirm():
    """Persist the reviewed cutouts the user chose to keep. Each item carries its
    edited fields plus the base64 cutout PNG produced during analyze."""
    uid = session['user_id']
    data = request.json or {}
    items = data.get('items') or []

    # Opt-in mode sends raw crops that still need reconstructing here.
    needs_gen = any(it.get('crop_b64') and not it.get('cutout_b64') for it in items)
    if needs_gen and not GEMINI_API_KEY:
        return jsonify({"error": "GEMINI_API_KEY not set in .env"}), 503

    saved = 0
    cap_msg = None
    for it in items:
        png = None
        if it.get('cutout_b64'):
            try:
                png = base64.b64decode(it['cutout_b64'])
            except (ValueError, TypeError):
                png = None
        elif it.get('crop_b64'):
            try:
                crop = base64.b64decode(it['crop_b64'])
                desc = ", ".join(x for x in [it.get('color'), it.get('name'), it.get('notes')] if x)
                will_bill = not cutout_cached(crop, desc)
                if will_bill:
                    cap_msg = _cap_blocked(uid)
                    if cap_msg:
                        break  # stop generating; return what we already saved
                png = reconstruct_garment(crop, desc)
                if will_bill:
                    record_image_event(uid, 'cutout')
            except Exception:
                png = None
        if png is None:
            continue
        fname = _unique_name("cutout_", ".png")
        (UPLOAD_DIR / fname).write_bytes(png)
        image_path = f"uploads/{fname}"

        tags = it.get('tags') or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(',') if t.strip()]

        garment_id = add_garment(
            user_id=uid,
            name=(it.get('name') or '').strip(),
            type_=it.get('type', ''),
            color=it.get('color', ''),
            brand=it.get('brand', ''),
            fit=it.get('fit', ''),
            occasion=it.get('occasion', ''),
            tags=tags,
            image_path=image_path,
            notes=it.get('notes', ''),
            material=it.get('material', ''),
            label_image_path="",
            size=it.get('size', ''),
        )
        embed_garment(get_garment(garment_id, uid))
        saved += 1

    if saved:
        invalidate_gaps_cache(uid)
    return jsonify({"ok": True, "saved": saved, "cap": cap_msg})


# --- Outfits ---

@app.route("/outfits")
def outfits_page():
    uid = session['user_id']
    outfits = get_outfits(uid)
    garments = get_all_garments(uid)
    return render_template("outfit.html", outfits=outfits, garments=garments)


@app.route("/outfits", methods=["POST"])
def create_outfit():
    uid = session['user_id']
    data = request.json or {}
    outfit_id = add_outfit(
        user_id=uid,
        name=data.get('name', 'Untitled Outfit'),
        occasion=data.get('occasion', ''),
        rating=data.get('rating', 0),
        notes=data.get('notes', ''),
        garment_ids=data.get('garment_ids', [])
    )
    return jsonify({"id": outfit_id, "ok": True})


# --- Calendar ---

@app.route("/calendar")
def calendar_page():
    uid = session['user_id']
    logs = get_calendar(uid, limit=30)
    garments = get_all_garments(uid)
    outfits = get_outfits(uid)
    return render_template("calendar.html", logs=logs, garments=garments, outfits=outfits)


@app.route("/calendar/detect", methods=["POST"])
def detect_calendar_outfit():
    if 'image' not in request.files or not request.files['image'].filename:
        return jsonify({"error": "No image provided"}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not set in .env"}), 503
    try:
        detected_ids = detect_outfit_rag(request.files['image'].read(), session['user_id'])
        return jsonify({"detected_ids": detected_ids})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/calendar", methods=["POST"])
def log_today():
    uid = session['user_id']
    data = request.json or {}
    log_outfit(
        user_id=uid,
        date=data.get('date'),
        outfit_id=data.get('outfit_id'),
        garment_ids=data.get('garment_ids', []),
        notes=data.get('notes', '')
    )
    return jsonify({"ok": True})


# --- Suggest ---

@app.route("/suggest")
def suggest():
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not set in .env"}), 503
    try:
        occasion = request.args.get('occasion', '').strip() or None
        weather = request.args.get('weather', '').strip() or None
        items = suggest_outfit(session['user_id'], occasion=occasion, weather=weather)
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Virtual try-on ---

# Cache try-on renders so re-viewing a suggestion doesn't re-bill Gemini. Keyed by
# (user, body photo, garment set) — a new body photo gets a fresh filename, so it
# naturally busts the cache. E: persisted to disk so a restart reuses old renders.
_TRYON_INDEX = DATA_DIR / ".tryon_cache.json"
_tryon_cache = {}


def _tryon_key(uid, body_rel, ids):
    return f"{uid}|{body_rel}|{','.join(str(i) for i in ids)}"


def _load_tryon_cache():
    global _tryon_cache
    if PERSIST_TRYON and _TRYON_INDEX.is_file():
        try:
            _tryon_cache = json.loads(_TRYON_INDEX.read_text())
        except Exception:
            _tryon_cache = {}


def _save_tryon_cache():
    if PERSIST_TRYON:
        try:
            _TRYON_INDEX.write_text(json.dumps(_tryon_cache))
        except Exception:
            pass


_load_tryon_cache()


def _garment_png_bytes(rel_path):
    """Load a garment image off disk as PNG bytes, preserving transparency for
    cutouts. Returns None if the file is missing."""
    if not rel_path:
        return None
    p = (DATA_DIR / rel_path).resolve()
    if not (UPLOAD_DIR.resolve() in p.parents and p.is_file()):
        return None
    img = Image.open(p)
    img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


@app.route("/settings/body-photo", methods=["GET"])
def get_body_photo_route():
    rel = get_body_photo(session['user_id'])
    return jsonify({"path": rel, "url": (f"/static/{rel}" if rel else None)})


@app.route("/settings/body-photo", methods=["POST"])
def set_body_photo_route():
    uid = session['user_id']
    if 'image' not in request.files or not request.files['image'].filename:
        return jsonify({"error": "No image provided"}), 400
    f = request.files['image']
    img_bytes = f.read()

    # A: advisory quality check — we still save the photo, but tell the user if it's
    # a poor try-on reference (face covered, not full-body, etc.).
    warning = ""
    if BODY_PHOTO_GATE and ANTHROPIC_API_KEY:
        try:
            warning = check_body_photo(img_bytes).get("warning", "")
        except Exception:
            warning = ""

    ext = _safe_ext(f.filename)
    filename = _unique_name(f"body_{uid}_", ext)
    (UPLOAD_DIR / filename).write_bytes(img_bytes)
    rel = f"uploads/{filename}"
    old = get_body_photo(uid)
    set_body_photo(uid, rel)
    if old and old != rel:
        _delete_upload(old)
    return jsonify({"path": rel, "url": f"/static/{rel}", "warning": warning})


@app.route("/garments/remove-bg", methods=["POST"])
def remove_bg_route():
    """G: free local background removal (rembg). For flat/product garment photos in
    the normal add flow — no Gemini spend. Returns the cutout as base64 PNG."""
    if 'image' not in request.files or not request.files['image'].filename:
        return jsonify({"error": "No image provided"}), 400
    try:
        png = remove_bg(request.files['image'].read())
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"image_b64": base64.b64encode(png).decode('ascii')})


@app.route("/suggest/tryon", methods=["POST"])
def suggest_tryon():
    uid = session['user_id']
    if not GEMINI_API_KEY:
        return jsonify({"error": "GEMINI_API_KEY not set in .env"}), 503

    data = request.json or {}
    try:
        garment_ids = sorted({int(x) for x in (data.get('garment_ids') or [])})
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid garment_ids"}), 400
    if not garment_ids:
        return jsonify({"error": "No garments selected"}), 400

    body_rel = get_body_photo(uid)
    body_bytes = _garment_png_bytes(body_rel)
    if not body_bytes:
        return jsonify({"error": "no_body_photo"}), 400

    key = _tryon_key(uid, body_rel, garment_ids)
    cached = _tryon_cache.get(key)
    if cached and (DATA_DIR / cached).is_file():
        return jsonify({"url": f"/static/{cached}", "cached": True})

    pngs, names = [], []
    for gid in garment_ids:
        g = get_garment(gid, uid)
        if not g:
            continue
        png = _garment_png_bytes(g.get('image_path'))
        if png:
            pngs.append(png)
            names.append(g.get('name') or g.get('type') or 'item')
    if not pngs:
        return jsonify({"error": "None of the selected garments have an image"}), 400

    blocked = _cap_blocked(uid)
    if blocked:
        return jsonify({"error": blocked, "cap": True}), 429

    try:
        result = tryon(body_bytes, pngs, ", ".join(names))
    except Exception as e:
        return jsonify({"error": _friendly_gemini_error(e)}), 502

    fname = _unique_name("tryon_", ".png")
    (UPLOAD_DIR / fname).write_bytes(result)
    rel = f"uploads/{fname}"
    _tryon_cache[key] = rel
    _save_tryon_cache()
    record_image_event(uid, 'tryon')
    add_tryon_history(uid, rel, garment_ids)
    return jsonify({"url": f"/static/{rel}", "cached": False})


@app.route("/tryon/history")
def tryon_history_route():
    items = get_tryon_history(session['user_id'], limit=12)
    return jsonify({"items": [
        {"url": f"/static/{i['image_path']}", "created_at": i['created_at'], "garment_ids": i['garment_ids']}
        for i in items if (DATA_DIR / i['image_path']).is_file()
    ]})


@app.route("/shop")
def shop_page():
    return render_template("shop.html")


@app.route("/shop", methods=["POST"])
def shop():
    data = request.json or {}
    query = data.get('query', '').strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not set in .env"}), 503
    try:
        max_price = data.get('max_price')
        max_price = float(max_price) if max_price not in (None, '') else None
    except (TypeError, ValueError):
        return jsonify({"error": "max_price must be a number"}), 400
    try:
        results = shopper_agent(query, session['user_id'], max_price=max_price)
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/shop/gaps")
def shop_gaps():
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not set in .env"}), 503
    try:
        gaps = identify_wardrobe_gaps(session['user_id'])
        return jsonify({"gaps": gaps})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/wishlist")
def wishlist():
    return jsonify({"items": get_wishlist_items(session['user_id'])})


@app.route("/wishlist", methods=["POST"])
def wishlist_add():
    uid = session['user_id']
    data = request.json or {}
    title = data.get('title', '').strip()
    if not title:
        return jsonify({"error": "No title provided"}), 400
    item_id = add_wishlist_item(
        user_id=uid,
        title=title,
        price=data.get('price', ''),
        source=data.get('source', ''),
        link=data.get('link', ''),
        image=data.get('image', ''),
        category=data.get('category', ''),
        reasoning=data.get('reasoning', ''),
    )
    return jsonify({"id": item_id, "ok": True})


@app.route("/wishlist/<int:item_id>", methods=["DELETE"])
def wishlist_remove(item_id):
    delete_wishlist_item(item_id, session['user_id'])
    return jsonify({"ok": True})


# --- Ask ---

@app.route("/ask")
def ask_page():
    return render_template("ask.html")


@app.route("/ask", methods=["POST"])
def ask():
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not set in .env"}), 503
    data = request.json or {}
    query = data.get('query', '').strip()
    if not query:
        return jsonify({"error": "No query provided"}), 400
    try:
        answer = run_coordinator(query, session['user_id'], history=data.get('history') or [])
        return jsonify({"answer": answer})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Style Profile ---

@app.route("/style")
def style():
    profile = get_style_profile(session['user_id'])
    return jsonify(profile or {"summary": None, "last_updated": None})


@app.route("/style/refresh", methods=["POST"])
def style_refresh():
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not set in .env"}), 503
    try:
        summary = refresh_style_profile(session['user_id'])
        return jsonify({"summary": summary})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        print("WARNING: ANTHROPIC_API_KEY not set in .env")
    # Debug mode exposes the Werkzeug debugger (arbitrary code execution) — never
    # combine it with 0.0.0.0. Set FLASK_DEBUG=1 for local development reload.
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", host="0.0.0.0", port=int(os.environ.get("PORT", 5002)))
