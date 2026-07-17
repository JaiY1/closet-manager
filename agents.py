import json
import time
import anthropic

from config import ANTHROPIC_API_KEY
from database import (
    get_garment, get_all_garments, get_outfits, get_calendar,
    get_style_profile, save_style_profile
)
from vector_store import search_garments
from vision import describe_outfit
from shopping import search_shopping

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_shop_cache = {}  # (user_id, normalized query, max_price) -> (cached_at, results)
_SHOP_CACHE_TTL = 3600  # seconds — shopping results/prices go stale, unlike a style profile

_SHOP_SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "scored": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "category": {"type": "string", "enum": ["similar", "different"]},
                    "reasoning": {"type": "string"}
                },
                "required": ["index", "category", "reasoning"],
                "additionalProperties": False
            }
        }
    },
    "required": ["scored"],
    "additionalProperties": False
}

_gaps_cache = {}  # user_id -> (cached_at, gaps)
_GAPS_CACHE_TTL = 3600  # seconds


def invalidate_gaps_cache(user_id: int):
    """Call whenever a garment is added, edited, or deleted so gap detection
    reflects the current wardrobe instead of serving a stale cached read."""
    _gaps_cache.pop(user_id, None)

_GAP_SCHEMA = {
    "type": "object",
    "properties": {
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Short label, e.g. 'Formal trousers'"},
                    "reason": {"type": "string", "description": "Why this is a gap, referencing their actual wardrobe/style"},
                    "search_query": {"type": "string", "description": "A good shopping search query to fill this gap, e.g. 'navy wool dress trousers slim fit'"}
                },
                "required": ["category", "reason", "search_query"],
                "additionalProperties": False
            }
        }
    },
    "required": ["gaps"],
    "additionalProperties": False
}

_COORDINATOR_TOOLS = [
    {
        "name": "search_closet",
        "description": "Search the wardrobe for garments matching a description. Use for questions about specific items, colors, types, brands, or occasions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language description, e.g. 'blue casual shirts', 'smart trousers', 'something warm for winter'"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_style_profile",
        "description": "Get the user's style profile — their aesthetic preferences, favourite colours, fit preferences, and typical occasions.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_recent_outfits",
        "description": "Get saved outfits (with ratings) and recent calendar entries showing what the user has been wearing.",
        "input_schema": {"type": "object", "properties": {}}
    }
]


def _tool_search_closet(query: str, user_id: int) -> str:
    garment_ids = search_garments(query, user_id, n_results=8)
    if not garment_ids:
        return "No garments found matching that query."
    lines = []
    for gid in garment_ids:
        g = get_garment(gid, user_id)
        if not g:
            continue
        desc = f"ID {g['id']}: {g['name']}"
        details = [g.get('type'), g.get('color'), g.get('brand')]
        detail_str = ", ".join(d for d in details if d)
        if detail_str:
            desc += f" ({detail_str})"
        if g.get('fit'):
            desc += f" — {g['fit']} fit"
        if g.get('occasion'):
            desc += f" [{g['occasion']}]"
        desc += f" worn {g['worn_count']}x"
        lines.append(f"- {desc}")
    return "\n".join(lines) if lines else "No matching garments found."


def _tool_get_style_profile(user_id: int) -> str:
    profile = get_style_profile(user_id)
    if profile and profile.get('summary'):
        return profile['summary']
    return "No style profile yet. Save some outfits and rate them to build one."


def _tool_get_recent_outfits(user_id: int) -> str:
    outfits = get_outfits(user_id, limit=8)
    calendar = get_calendar(user_id, limit=14)

    lines = ["Saved outfits (highest rated first):"]
    if outfits:
        for o in outfits[:5]:
            garment_names = ", ".join(g['name'] for g in o.get('garments', []))
            lines.append(f"  - {o['name']} ({o['rating']}/5, {o.get('occasion') or 'no occasion'}): {garment_names}")
    else:
        lines.append("  None saved yet.")

    lines.append("\nRecent calendar entries:")
    worn = [l for l in calendar if l.get('garments')]
    if worn:
        for log in worn[:7]:
            garment_names = ", ".join(g['name'] for g in log['garments'])
            lines.append(f"  - {log['date']}: {garment_names}")
    else:
        lines.append("  No calendar entries yet.")

    return "\n".join(lines)


def run_coordinator(user_query: str, user_id: int, history: list = None) -> str:
    system = (
        "You are a personal style assistant with access to the user's wardrobe. "
        "Use the tools to look up their actual clothes, style profile, and outfit history before answering. "
        "Be concise and practical. Reference specific items from their wardrobe by name."
    )
    # Include recent chat turns so follow-up questions have context
    messages = [
        m for m in (history or [])[-12:]
        if isinstance(m, dict) and m.get('role') in ('user', 'assistant')
        and isinstance(m.get('content'), str) and m['content'].strip()
    ]
    while messages and messages[0]['role'] != 'user':
        messages.pop(0)
    messages.append({"role": "user", "content": user_query})

    for _ in range(5):
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=600,
            system=system,
            tools=_COORDINATOR_TOOLS,
            messages=messages
        )

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    if block.name == "search_closet":
                        result = _tool_search_closet(block.input.get('query', ''), user_id)
                    elif block.name == "get_style_profile":
                        result = _tool_get_style_profile(user_id)
                    elif block.name == "get_recent_outfits":
                        result = _tool_get_recent_outfits(user_id)
                    else:
                        result = "Unknown tool."
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        # end_turn, max_tokens, etc. — return whatever text we have instead of
        # re-sending the identical request
        for block in response.content:
            if hasattr(block, 'text'):
                return block.text
        return "Done."

    return "Sorry, I couldn't process that request."


_TOPS       = ["shirt", "t-shirt", "polo", "hoodie", "sweater", "knitwear", "blazer"]
_BOTTOMS    = ["jeans", "trousers", "chinos", "shorts", "skirt"]
_SHOES      = ["shoes", "sneakers", "boots", "loafers"]
_OUTERWEAR  = ["jacket", "coat"]
_SUMMER_WORDS = {"summer", "hot", "warm", "beach", "humid", "sunny"}

# Structured-output schemas — guarantee valid JSON, no code-fence stripping needed
_SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "reason": {"type": "string"}
                },
                "required": ["id", "reason"],
                "additionalProperties": False
            }
        }
    },
    "required": ["items"],
    "additionalProperties": False
}

_DETECT_SCHEMA = {
    "type": "object",
    "properties": {
        "detected_ids": {"type": "array", "items": {"type": "integer"}}
    },
    "required": ["detected_ids"],
    "additionalProperties": False
}


def _is_summer(occasion: str, weather: str) -> bool:
    text = f"{occasion or ''} {weather or ''}".lower()
    return any(w in text for w in _SUMMER_WORDS)


def _search_category(query: str, types: list, user_id: int, n: int = 4) -> list:
    ids = search_garments(query, user_id, n_results=n, types=types)
    return [g for gid in ids for g in [get_garment(gid, user_id)] if g]


def _garment_line(g: dict) -> str:
    parts = [g.get('color', ''), g.get('brand', ''), g.get('fit', '') and g['fit'] + ' fit',
             g.get('material', ''), g.get('notes', '')]
    detail = ", ".join(p for p in parts if p)
    worn = f"worn {g['worn_count']}x" if g.get('worn_count') else "never worn"
    return f"- ID {g['id']} [{g.get('type','')}]: {g['name']} — {detail} ({worn})"


def suggest_outfit(user_id: int, occasion: str = None, weather: str = None) -> list:
    query = f"{occasion or 'everyday casual'} {weather or ''}".strip()
    summer = _is_summer(occasion, weather)

    # Retrieve candidates per category so Claude always has a full outfit to build
    tops      = _search_category(query, _TOPS, user_id, n=4)
    bottoms   = _search_category(query, _BOTTOMS, user_id, n=3)
    shoes     = _search_category(query, _SHOES, user_id, n=3)
    outerwear = [] if summer else _search_category(query, _OUTERWEAR, user_id, n=3)

    all_candidates = tops + bottoms + shoes + outerwear
    if not all_candidates:
        return []

    # Build rich per-garment context
    sections = []
    if tops:
        sections.append("TOPS:\n" + "\n".join(_garment_line(g) for g in tops))
    if bottoms:
        sections.append("BOTTOMS:\n" + "\n".join(_garment_line(g) for g in bottoms))
    if shoes:
        sections.append("SHOES:\n" + "\n".join(_garment_line(g) for g in shoes))
    if outerwear:
        sections.append("OUTERWEAR:\n" + "\n".join(_garment_line(g) for g in outerwear))

    # Include top-rated outfit history as style examples
    top_outfits = get_outfits(user_id, limit=3)
    outfit_examples = ""
    if top_outfits:
        lines = []
        for o in top_outfits:
            names = ", ".join(g['name'] for g in o.get('garments', []))
            if names:
                lines.append(f"  - {o['name']} ({o['rating']}/5): {names}")
        if lines:
            outfit_examples = "Past highly-rated outfits (for style reference):\n" + "\n".join(lines)

    prompt = f"""You are a personal stylist. Build one complete outfit for the occasion below.

Style profile: {_tool_get_style_profile(user_id)}

{outfit_examples}

Available garments by category:
{"".join(s + chr(10) for s in sections)}
Occasion: {occasion or 'everyday casual'}
{f'Weather / conditions: {weather}' if weather else ''}
{'Note: warm weather — no outerwear needed.' if summer else ''}

Pick exactly one item from each available category to form a complete, cohesive outfit.
For each pick give one sentence on why the piece works in this outfit."""

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=500,
        output_config={"format": {"type": "json_schema", "schema": _SUGGEST_SCHEMA}},
        messages=[{"role": "user", "content": prompt}]
    )
    data = json.loads(response.content[0].text)

    results = []
    for item in data.get("items", []):
        g = get_garment(int(item["id"]), user_id)
        if g:
            results.append({"garment": g, "reason": item["reason"]})
    return results


def detect_outfit_rag(image_bytes: bytes, user_id: int) -> list:
    # Step 1 — Vision: describe what's visible in the photo
    description = describe_outfit(image_bytes)

    # Step 2 — RAG: semantic search against ChromaDB using the description
    # Run a search per described garment line for better per-item recall
    candidate_ids = []
    for line in description.splitlines():
        line = line.strip()
        if line:
            candidate_ids.extend(search_garments(line, user_id, n_results=5))
    # Deduplicate while preserving order
    seen = set()
    unique_candidates = []
    for gid in candidate_ids:
        if gid not in seen:
            seen.add(gid)
            unique_candidates.append(gid)
    candidates = []
    for gid in unique_candidates:
        g = get_garment(gid, user_id)
        if g:
            candidates.append(g)

    if not candidates:
        return []

    # Step 3 — Text match: confirm which candidates are actually being worn
    candidate_lines = "\n".join(
        f"ID {g['id']}: {g['name']} — {g['color']} {g['type']}, {g.get('fit', '')} fit"
        for g in candidates
    )
    prompt = f"""A person is wearing this outfit:
{description}

These are candidate garments from their wardrobe:
{candidate_lines}

For each garment visible in the outfit, return its ID only if you are highly confident it matches — same type AND similar color/pattern. If nothing in the list is a confident match for a visible garment, skip it. It is fine to return an empty list. Do not guess."""

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=300,
        output_config={"format": {"type": "json_schema", "schema": _DETECT_SCHEMA}},
        messages=[{"role": "user", "content": prompt}]
    )
    data = json.loads(response.content[0].text)
    return [int(i) for i in data.get("detected_ids", [])]


def refresh_style_profile(user_id: int) -> str:
    outfits = get_outfits(user_id, limit=20)
    calendar = get_calendar(user_id, limit=30)
    all_garments = get_all_garments(user_id)

    if not all_garments:
        return "Not enough data yet. Add some garments to build your style profile."

    outfit_text = ""
    for o in outfits:
        names = ", ".join(g['name'] for g in o.get('garments', []))
        outfit_text += f"- {o['name']} (rated {o['rating']}/5, {o.get('occasion') or 'no occasion'}): {names}\n"

    calendar_text = ""
    for log in calendar:
        names = ", ".join(g['name'] for g in log.get('garments', []))
        if names:
            calendar_text += f"- {log['date']}: {names}\n"

    top_worn = sorted(all_garments, key=lambda g: g['worn_count'], reverse=True)[:5]
    worn_text = ", ".join(
        f"{g['name']} ({g['worn_count']}x)" for g in top_worn if g['worn_count'] > 0
    )

    garment_lines = "\n".join(
        f"- {g['name']} ({g.get('type','')}, {g.get('color','')}, {g.get('brand','') or 'no brand'}, {g.get('fit','') or 'unknown fit'})"
        for g in all_garments
    )

    prompt = f"""Based on this person's wardrobe, write a 3–4 sentence style profile in second person ("You...").
Cover: overall aesthetic, colour palette, fit preferences, and typical occasions.
Be specific — reference actual colours, brands, and garment types you can see in the data. Do not give generic fashion advice.

Full wardrobe ({len(all_garments)} items):
{garment_lines}

Saved outfits:
{outfit_text or 'None yet'}

Recent calendar (what they actually wore):
{calendar_text or 'None yet'}

Most worn items: {worn_text or 'None yet'}

Write the style profile now:"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    import re
    summary = response.content[0].text.strip()
    # Strip markdown so it renders cleanly in the plain-text profile strip
    summary = re.sub(r'\*\*(.+?)\*\*', r'\1', summary)
    summary = re.sub(r'^#+\s*', '', summary, flags=re.MULTILINE)
    summary = summary.strip()
    save_style_profile(user_id, summary)
    return summary


def shopper_agent(query: str, user_id: int, max_price: float = None) -> list[dict]:
    """Search live shopping results for `query` and label each one against the
    user's style profile and existing wardrobe (via RAG) as "similar" — matches
    what they already own/wear — or "different" — a stretch from their usual
    style but still plausible for them. Optionally capped to `max_price`."""
    key = (user_id, query.strip().lower(), max_price or None)
    cached = _shop_cache.get(key)
    if cached and time.time() - cached[0] < _SHOP_CACHE_TTL:
        return cached[1]

    results = search_shopping(query, num=10, max_price=max_price)
    if not results:
        return []

    profile = get_style_profile(user_id)
    style_text = profile['summary'] if profile and profile.get('summary') else "No style profile yet."

    related_ids = search_garments(query, user_id, n_results=6)
    related = [g for g in (get_garment(gid, user_id) for gid in related_ids) if g]
    wardrobe_text = "\n".join(
        f"- {g['name']} ({g.get('type', '')}, {g.get('color', '')}, {g.get('fit', '') or 'unknown fit'})"
        for g in related
    ) or "No closely related items owned yet."

    listings_text = "\n".join(
        f"{i}. {r['title']} — {r.get('price') or 'no price'} ({r.get('source') or 'unknown store'})"
        for i, r in enumerate(results)
    )

    budget_line = f"\nBudget: nothing over ${max_price:g}." if max_price else ""

    prompt = f"""A person is shopping for: "{query}"{budget_line}

Their style profile:
{style_text}

Related items they already own:
{wardrobe_text}

Shopping results found:
{listings_text}

For each result, decide:
- "similar" — closely matches what they already own or their established style, a safe pick
- "different" — a stretch from their usual style but still fits their overall aesthetic, worth trying

Give a one-sentence reason for each, referencing their actual style profile or wardrobe by name."""

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1500,
        output_config={"format": {"type": "json_schema", "schema": _SHOP_SCORE_SCHEMA}},
        messages=[{"role": "user", "content": prompt}]
    )

    scored = json.loads(response.content[0].text)["scored"]
    out = []
    for s in scored:
        idx = s.get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(results)):
            continue
        item = dict(results[idx])
        item["category"] = s.get("category")
        item["reasoning"] = s.get("reasoning")
        out.append(item)

    if len(_shop_cache) > 200:
        _shop_cache.clear()
    _shop_cache[key] = (time.time(), out)
    return out


def identify_wardrobe_gaps(user_id: int) -> list[dict]:
    """Have Claude look at the full wardrobe + style profile and identify a
    handful of gaps — categories the user owns little/nothing of but that fit
    their established style. Each gap includes a ready-to-use shopping query."""
    cached = _gaps_cache.get(user_id)
    if cached and time.time() - cached[0] < _GAPS_CACHE_TTL:
        return cached[1]

    all_garments = get_all_garments(user_id)
    if not all_garments:
        return []

    profile = get_style_profile(user_id)
    style_text = profile['summary'] if profile and profile.get('summary') else "No style profile yet."

    type_counts = {}
    for g in all_garments:
        t = g.get('type') or 'other'
        type_counts[t] = type_counts.get(t, 0) + 1
    counts_text = ", ".join(f"{t}: {n}" for t, n in sorted(type_counts.items(), key=lambda x: -x[1]))

    garment_lines = "\n".join(
        f"- {g['name']} ({g.get('type', '')}, {g.get('color', '')}, {g.get('occasion', '') or 'no occasion'})"
        for g in all_garments
    )

    prompt = f"""Here is a person's full wardrobe and style profile.

Style profile:
{style_text}

Garment counts by type: {counts_text}

Full wardrobe ({len(all_garments)} items):
{garment_lines}

Identify 3-5 real gaps in this wardrobe — categories they own little or nothing of but that
would fit their established style and the occasions they already dress for. Do not suggest
things that contradict their style profile. Only suggest gaps that are genuinely missing or
thin, not items they already have plenty of.

For each gap, give a short category label, a one-sentence reason grounded in their actual
wardrobe/style, and a specific shopping search query to fill it."""

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=800,
        output_config={"format": {"type": "json_schema", "schema": _GAP_SCHEMA}},
        messages=[{"role": "user", "content": prompt}]
    )

    gaps = json.loads(response.content[0].text)["gaps"]
    _gaps_cache[user_id] = (time.time(), gaps)
    return gaps
