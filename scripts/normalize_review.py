"""Local, one-off catalog-normalization review tool (NOT shipped to prod).

Regenerates every garment image through imagegen.normalize_garment_image (single-
item, per-category framing, transparent cutout) and lets you pick the best Gemini
output per garment in a browser — with a "generate another" re-roll for the ones
Gemini botches (e.g. renders two items instead of one). On Apply it backs up the
originals and repoints each garment to the chosen cutout.

Run locally:  python scripts/normalize_review.py   →  http://localhost:5055
Then, once applied:  bash scripts/export_data.sh   and import to Railway.
"""
import io
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, request, send_file, abort  # noqa: E402

from config import DATA_DIR  # noqa: E402
from database import get_all_garments, get_garment, update_garment  # noqa: E402
from imagegen import normalize_garment_image  # noqa: E402

USER_ID = 1  # Jai
STAGING = DATA_DIR / "normalize_staging"
BACKUP = DATA_DIR / "normalize_backup"
CHOICES_FILE = STAGING / "choices.json"
UPLOADS = DATA_DIR / "uploads"
STAGING.mkdir(exist_ok=True)

app = Flask(__name__)
_gen_count = {"n": 0}  # running tally of real Gemini generations this session


def _load_choices() -> dict:
    if CHOICES_FILE.is_file():
        return json.loads(CHOICES_FILE.read_text())
    return {}


def _save_choices(ch: dict):
    CHOICES_FILE.write_text(json.dumps(ch, indent=2))


def _description(g: dict) -> str:
    notes = (g.get("notes") or "")[:180]
    bits = [g.get("color"), g.get("name"), notes]
    return ", ".join(b for b in bits if b)


def _candidates(gid: int) -> list:
    d = STAGING / str(gid)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.glob("cand_*.png"))


def _garments() -> list:
    return get_all_garments(USER_ID)


@app.route("/")
def index():
    return _PAGE


@app.route("/api/garments")
def api_garments():
    choices = _load_choices()
    out = []
    for g in _garments():
        gid = g["id"]
        out.append({
            "id": gid,
            "name": g.get("name") or "(unnamed)",
            "type": g.get("type") or "",
            "color": g.get("color") or "",
            "candidates": _candidates(gid),
            "choice": choices.get(str(gid)),
        })
    return jsonify({"garments": out, "gen_count": _gen_count["n"]})


@app.route("/api/generate/<int:gid>", methods=["POST"])
def api_generate(gid):
    g = get_garment(gid, USER_ID)
    if not g:
        abort(404)
    src = DATA_DIR / g["image_path"]
    if not src.is_file():
        return jsonify({"error": f"source image missing: {g['image_path']}"}), 400
    try:
        png = normalize_garment_image(
            src.read_bytes(), _description(g), g.get("type") or "", force=True
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    _gen_count["n"] += 1
    d = STAGING / str(gid)
    d.mkdir(exist_ok=True)
    name = f"cand_{int(time.time() * 1000)}.png"
    (d / name).write_bytes(png)
    return jsonify({"candidate": name, "gen_count": _gen_count["n"]})


@app.route("/api/choose/<int:gid>", methods=["POST"])
def api_choose(gid):
    choice = (request.json or {}).get("choice")  # a cand filename, "original", "skip", or None
    ch = _load_choices()
    if choice is None:
        ch.pop(str(gid), None)
    else:
        ch[str(gid)] = choice
    _save_choices(ch)
    return jsonify({"ok": True})


@app.route("/api/apply", methods=["POST"])
def api_apply():
    choices = _load_choices()
    BACKUP.mkdir(exist_ok=True)
    applied, kept, skipped = 0, 0, 0
    for g in _garments():
        gid = g["id"]
        choice = choices.get(str(gid))
        if not choice or choice == "original":
            kept += 1
            continue
        if choice == "skip":
            skipped += 1
            continue
        cand = STAGING / str(gid) / choice
        if not cand.is_file():
            continue
        # back up the current image, then repoint the garment to a fresh cutout file
        old_rel = g["image_path"]
        old_abs = DATA_DIR / old_rel
        if old_abs.is_file():
            shutil.copy2(old_abs, BACKUP / f"{gid}_{old_abs.name}")
        new_name = f"norm_{gid}_{int(time.time())}.png"
        shutil.copy2(cand, UPLOADS / new_name)
        update_garment(gid, USER_ID, image_path=f"uploads/{new_name}")
        applied += 1
    return jsonify({"ok": True, "applied": applied, "kept": kept, "skipped": skipped})


@app.route("/orig/<int:gid>")
def orig(gid):
    g = get_garment(gid, USER_ID)
    if not g:
        abort(404)
    p = DATA_DIR / g["image_path"]
    if not p.is_file():
        abort(404)
    return send_file(p)


@app.route("/cand/<int:gid>/<name>")
def cand(gid, name):
    if not name.startswith("cand_") or "/" in name or "\\" in name:
        abort(400)
    p = STAGING / str(gid) / name
    if not p.is_file():
        abort(404)
    return send_file(p, mimetype="image/png")


_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Catalog normalize — review</title>
<style>
  :root { --line:#1a1a1a; --accent:#e8590c; --bg:#faf7f0; --surf:#fff; --muted:#7a7365; --ok:#1e8a5b; }
  * { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--line);
    font-family:system-ui,-apple-system,"Segoe UI",sans-serif; }
  header { position:sticky; top:0; z-index:5; background:var(--surf); border-bottom:2px solid var(--line);
    padding:12px 18px; display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
  h1 { font-size:1.05rem; margin:0; font-weight:800; }
  .spacer { flex:1; }
  .btn { font-weight:700; border:2px solid var(--line); border-radius:10px; padding:8px 14px; cursor:pointer;
    background:var(--surf); color:var(--line); font-size:.85rem; }
  .btn.primary { background:var(--accent); color:#fff; }
  .btn:disabled { opacity:.5; cursor:default; }
  .meta { font-size:.8rem; color:var(--muted); font-variant-numeric:tabular-nums; }
  main { padding:18px; display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:16px; }
  .card { background:var(--surf); border:2px solid var(--line); border-radius:14px; padding:14px; }
  .card h2 { font-size:.95rem; margin:0 0 2px; }
  .card .sub { font-size:.75rem; color:var(--muted); margin-bottom:10px; }
  .card.decided { outline:3px solid var(--ok); outline-offset:-1px; }
  .tiles { display:flex; gap:8px; flex-wrap:wrap; align-items:flex-start; }
  .tile { width:96px; }
  .tile .imgwrap { width:96px; height:96px; border:2px solid var(--line); border-radius:8px; overflow:hidden;
    background:repeating-conic-gradient(#eee 0% 25%, #fff 0% 50%) 0 0/16px 16px; cursor:pointer;
    display:grid; place-items:center; }
  .tile.sel .imgwrap { outline:3px solid var(--accent); outline-offset:1px; }
  .tile img { max-width:100%; max-height:100%; display:block; }
  .tile .lab { font-size:.66rem; text-align:center; color:var(--muted); margin-top:3px; }
  .tile.orig .lab { color:var(--line); font-weight:700; }
  .row2 { display:flex; gap:8px; margin-top:10px; flex-wrap:wrap; }
  .mini { font-size:.72rem; border:1.5px solid var(--line); border-radius:8px; padding:5px 9px; cursor:pointer; background:var(--surf); }
  .mini.on { background:var(--line); color:#fff; }
  .status { font-size:.72rem; color:var(--muted); margin-top:6px; min-height:1em; }
  .spin { display:inline-block; width:11px; height:11px; border:2px solid var(--muted); border-top-color:transparent;
    border-radius:50%; animation:s .7s linear infinite; vertical-align:-1px; }
  @keyframes s { to { transform:rotate(360deg); } }
</style></head><body>
<header>
  <h1>Catalog normalize</h1>
  <span class="meta" id="counts"></span>
  <div class="spacer"></div>
  <button class="btn" id="firstPass">Generate first pass</button>
  <button class="btn primary" id="apply">Apply chosen</button>
</header>
<main id="grid"></main>
<script>
let G = [];
const est = n => "$" + (n*0.04).toFixed(2);

async function load() {
  const r = await fetch("/api/garments"); const d = await r.json();
  G = d.garments; window._gen = d.gen_count; render();
}
function counts() {
  const decided = G.filter(g => g.choice).length;
  const pending = G.filter(g => !g.candidates.length).length;
  document.getElementById("counts").textContent =
    `${G.length} items · ${decided} chosen · ${pending} not yet generated · ${window._gen} gens (~${est(window._gen)})`;
}
function tile(g, name) {
  const sel = g.choice === name;
  return `<div class="tile ${sel?'sel':''}" data-gid="${g.id}" data-choice="${name}">
    <div class="imgwrap"><img src="/cand/${g.id}/${name}?t=${Date.now()}"></div>
    <div class="lab">option</div></div>`;
}
function render() {
  const grid = document.getElementById("grid"); grid.innerHTML = "";
  for (const g of G) {
    const el = document.createElement("div");
    el.className = "card" + (g.choice ? " decided" : "");
    el.id = "card-" + g.id;
    const origSel = g.choice === "original";
    el.innerHTML = `
      <h2>${g.name}</h2>
      <div class="sub">${[g.type, g.color].filter(Boolean).join(" · ")}</div>
      <div class="tiles">
        <div class="tile orig ${origSel?'sel':''}" data-gid="${g.id}" data-choice="original">
          <div class="imgwrap"><img src="/orig/${g.id}"></div>
          <div class="lab">original</div>
        </div>
        ${g.candidates.map(c => tile(g, c)).join("")}
      </div>
      <div class="row2">
        <button class="mini gen" data-gid="${g.id}">Generate another</button>
        <button class="mini skip ${g.choice==='skip'?'on':''}" data-gid="${g.id}">Skip</button>
        <span class="status" id="st-${g.id}"></span>
      </div>`;
    grid.appendChild(el);
  }
  counts();
  grid.querySelectorAll(".tile").forEach(t => t.onclick = () => choose(t.dataset.gid, t.dataset.choice));
  grid.querySelectorAll(".gen").forEach(b => b.onclick = () => genOne(b.dataset.gid, b));
  grid.querySelectorAll(".skip").forEach(b => b.onclick = () => choose(b.dataset.gid, "skip"));
}
async function choose(gid, choice) {
  const g = G.find(x => x.id == gid);
  if (g.choice === choice) choice = null;         // click again to unchoose
  await fetch("/api/choose/" + gid, {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({choice})});
  g.choice = choice; render();
}
async function genOne(gid, btn) {
  const st = document.getElementById("st-" + gid);
  if (btn) btn.disabled = true; st.innerHTML = '<span class="spin"></span> generating…';
  try {
    const r = await fetch("/api/generate/" + gid, {method:"POST"});
    const d = await r.json();
    if (d.error) { st.textContent = "⚠️ " + d.error; }
    else {
      const g = G.find(x => x.id == gid); g.candidates.push(d.candidate);
      window._gen = d.gen_count; st.textContent = ""; render();
    }
  } catch (e) { st.textContent = "⚠️ failed"; }
  if (btn) btn.disabled = false;
}
document.getElementById("firstPass").onclick = async (e) => {
  e.target.disabled = true; e.target.textContent = "Generating…";
  for (const g of G) { if (!g.candidates.length) await genOne(g.id, null); }
  e.target.disabled = false; e.target.textContent = "Generate first pass";
};
document.getElementById("apply").onclick = async (e) => {
  if (!confirm("Apply chosen cutouts? Originals are backed up first.")) return;
  e.target.disabled = true;
  const r = await fetch("/api/apply", {method:"POST"}); const d = await r.json();
  alert(`Applied ${d.applied} · kept original ${d.kept} · skipped ${d.skipped}`);
  e.target.disabled = false; load();
};
load();
</script></body></html>"""


if __name__ == "__main__":
    print("Catalog normalize review → http://localhost:5055")
    app.run(host="127.0.0.1", port=5055, debug=False)
