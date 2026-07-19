"""Auth gates, upload safety, and cross-user isolation regressions."""


# --- Login gate ---

def test_pages_require_login(client):
    for path in ("/", "/outfits", "/calendar", "/ask", "/shop", "/add"):
        r = client.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], path


def test_uploads_require_login(client):
    # Regression: serve_upload used to be login-exempt, exposing body photos
    # and try-on renders to anyone with a URL.
    r = client.get("/static/uploads/whatever.png")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_healthz_and_login_are_public(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/login").status_code == 200


def test_login_creates_session_and_grants_access(client):
    r = client.post("/login", data={"name": "Login Flow User"}, follow_redirects=False)
    assert r.status_code == 302
    assert client.get("/").status_code == 200


# --- Upload helpers ---

def test_safe_ext_whitelist(app_module):
    assert app_module._safe_ext("photo.jpg") == ".jpg"
    assert app_module._safe_ext("photo.PNG") == ".png"
    # Never persist active-content extensions
    assert app_module._safe_ext("evil.html") == ".jpg"
    assert app_module._safe_ext("evil.svg") == ".jpg"
    assert app_module._safe_ext("") == ".jpg"
    assert app_module._safe_ext(None) == ".jpg"


def test_delete_upload_refuses_paths_outside_uploads(app_module):
    from config import DATA_DIR
    # A real file under DATA_DIR but NOT under uploads/ must survive
    target = DATA_DIR / "precious.txt"
    target.write_text("keep me")
    app_module._delete_upload("precious.txt")
    app_module._delete_upload("../precious.txt")
    app_module._delete_upload("uploads/../precious.txt")
    assert target.is_file()
    # ...while a genuine upload is deleted
    inside = app_module.UPLOAD_DIR / "temp.png"
    inside.write_bytes(b"x")
    app_module._delete_upload("uploads/temp.png")
    assert not inside.exists()


# --- Cost caps ---

def test_daily_user_cap_blocks(app_module):
    from database import get_or_create_user, record_image_event
    from config import DAILY_USER_CAP
    user = get_or_create_user("Cap Test User")
    assert app_module._cap_blocked(user["id"]) is None or "limit" in app_module._cap_blocked(user["id"])
    for _ in range(DAILY_USER_CAP):
        record_image_event(user["id"], "cutout")
    msg = app_module._cap_blocked(user["id"])
    assert msg and "limit" in msg


# --- Friendly Gemini errors ---

def test_friendly_gemini_error_mapping(app_module):
    f = app_module._friendly_gemini_error
    assert "busy" in f(Exception("429 RESOURCE_EXHAUSTED quota"))
    assert "unavailable" in f(Exception("403 permission denied on billing"))
    assert "couldn't be processed" in f(Exception("blocked by safety filters"))
    assert "failed" in f(Exception("some novel explosion"))


# --- Cross-user isolation (regression for the outfit/calendar write hole) ---

def _add_garment_for(uid, name):
    from database import add_garment
    return add_garment(user_id=uid, name=name, type_="shirt", color="blue",
                       brand="", fit="", occasion="", tags=[], image_path="",
                       notes="")


def test_outfit_cannot_attach_foreign_garments():
    from database import get_or_create_user, add_outfit, get_outfits, get_garment
    victim = get_or_create_user("Victim User")["id"]
    attacker = get_or_create_user("Attacker User")["id"]
    vg = _add_garment_for(victim, "Victim Shirt")
    ag = _add_garment_for(attacker, "Attacker Shirt")

    oid = add_outfit(attacker, "steal", "", 0, "", [vg, ag, 999999, "junk"])
    outs = [o for o in get_outfits(attacker) if o["id"] == oid]
    names = [g["name"] for g in outs[0]["garments"]]
    assert names == ["Attacker Shirt"], names
    # Victim's worn_count untouched by the attempt
    assert get_garment(vg, victim)["worn_count"] == 0


def test_calendar_cannot_log_foreign_garments_or_outfits():
    from database import (get_or_create_user, add_outfit, log_outfit,
                         get_calendar, get_garment)
    victim = get_or_create_user("Victim User")["id"]
    attacker = get_or_create_user("Attacker User")["id"]
    vg = _add_garment_for(victim, "Victim Jacket")
    ag = _add_garment_for(attacker, "Attacker Jacket")
    victim_outfit = add_outfit(victim, "vfit", "", 0, "", [vg])

    log_outfit(attacker, "2026-07-18", victim_outfit, [vg, ag], "")
    logs = [l for l in get_calendar(attacker) if l["date"] == "2026-07-18"]
    assert logs and logs[0]["garment_ids"] == [ag]
    assert logs[0]["outfit_id"] is None  # foreign outfit id rejected
    assert get_garment(vg, victim)["worn_count"] == 1  # only the victim's own outfit bump


def test_calendar_relog_corrects_worn_count():
    from database import get_or_create_user, log_outfit, get_garment
    uid = get_or_create_user("Relog User")["id"]
    g1 = _add_garment_for(uid, "First")
    g2 = _add_garment_for(uid, "Second")
    log_outfit(uid, "2026-07-01", None, [g1], "")
    log_outfit(uid, "2026-07-01", None, [g2], "")  # correction replaces the day
    assert get_garment(g1, uid)["worn_count"] == 0
    assert get_garment(g2, uid)["worn_count"] == 1


def test_calendar_requires_date(logged_in):
    c, uid = logged_in
    r = c.post("/calendar", json={"garment_ids": []})
    assert r.status_code == 400
