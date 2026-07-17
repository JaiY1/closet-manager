# Closet Manager & AI Shopping Assistant

## Context

Jai wants a personal app to catalog his wardrobe, get AI-powered outfit suggestions, save favorite looks, and eventually get shopping recommendations matched to his existing style. The repo already has two Flask + Claude API projects (news-aggregator, nba-tool) so the tech stack and conventions are established. This becomes `closet-manager/` as a third project.

The user explicitly wants to understand and use:
- **RAG** (Retrieval-Augmented Generation) for semantic wardrobe search
- **Multi-agent design** for separating responsibilities
- **Claude API** (already in use) as the AI backbone

Shopping is deferred to v2+ — the web search problem is live/dynamic and warrants its own Tavily integration once the core closet UX is solid.

---

## Versioned Roadmap

### V1 — Core Closet Manager (build now)
- Garment catalog with image upload
- **Auto-tagging**: upload a photo → Claude Haiku Vision pre-fills the form (type, color, brand, fit) → user confirms or edits
- RAG-powered outfit suggestions (ChromaDB + Claude Haiku)
- Save & rate favorite outfit combos
- **Outfit calendar**: log what you wore each day — drives "worn recently" awareness in suggestions
- Free-text `/ask` via multi-agent coordinator (Closet Agent + Style Agent)
- Style profile auto-generated from your saved outfits and calendar history
- Simple web UI: catalog grid, outfit builder, calendar log, ask chat

### V2 — Shopping Assistant
- Shopper Agent using **Tavily API** for live web search
- Claude scores external results against your style profile
- Gap detection: "you own no formal trousers, here are 5 options"
- Shopping feed UI tab
- Cost: Tavily free tier = 1,000 searches/month (enough for personal use)

### V3 — Mobile-Friendly & Integrations
- Progressive Web App (PWA) — add to phone home screen, native camera feel
- Retailer affiliate links on shopping results
- Deeper calendar analytics: streak tracking, cost-per-wear calculations

---

## Cost Model

**Claude Code vs Claude API are billed separately.** Claude Code (this CLI) is your dev subscription; your app's `anthropic.messages.create()` calls are pay-per-token on a separate bill at console.anthropic.com — the same account already used by the news-aggregator.

### V1 Monthly Cost (personal use, ~you only)

| Component | Cost | Notes |
|-----------|------|-------|
| ChromaDB | Free | Runs in-process, open source |
| sentence-transformers | Free | Local CPU embeddings, no API |
| SQLite | Free | Built into Python |
| Hosting (local) | Free | Run on your own machine |
| Claude API | $0.30–$1.50/mo | See breakdown below |

**Claude API breakdown:**

| Action | Model | Cost/call | Calls/month | Monthly |
|--------|-------|-----------|-------------|---------|
| Outfit suggestion | Haiku 4.5 | ~$0.003 | 150 | $0.45 |
| Free-text `/ask` | Haiku 4.5 | ~$0.002 | 100 | $0.20 |
| Style profile refresh | Sonnet 4.6 | ~$0.017 | 4 | $0.07 |
| Auto-tag new garment | Haiku 4.5 Vision | ~$0.002 | ~10 | $0.02 |
| **Total** | | | | **~$0.74/mo** |

> Auto-tagging cost note: phone photos are resized to 768px before sending to Claude Vision — keeps cost at ~$0.002/image vs ~$0.01 for full-res. Onboarding a 100-garment wardrobe costs ~$0.16 one-time.

**Cost reduction levers:**
- **Prompt caching**: style profile + garment context sent every request → mark as cached blocks → ~90% discount on repeated input tokens → drops to ~$0.30/mo
- **Haiku for most calls**: 10× cheaper than Sonnet, sufficient for suggestion quality
- **Lazy style refresh**: only regenerate profile when new outfits are saved, not on every request

### V2 Addition (Shopping)

| Component | Cost | Notes |
|-----------|------|-------|
| Tavily API | Free tier | 1,000 searches/month, enough for personal use |
| Claude API (shopper) | +$0.20–$0.50/mo | Scoring results against style profile |

---

## How the Style Profile Works

The style profile is a natural language paragraph Claude writes about you — you never author it yourself.

**Flow:**
1. You save outfits and rate them (e.g. 5 stars on a navy blazer + chinos combo)
2. The Style Agent reads all your saved/highly-rated outfits + the outfit calendar (worn frequency, recency)
3. It calls Claude Sonnet, which writes a paragraph like:
   > *"You favor smart-casual with a preference for slim fits. Your palette is muted earth tones — navy, olive, tan. Your highest-rated looks pair structured tops with relaxed trousers. You avoid loud patterns. You've been wearing your grey chinos most frequently this season."*
4. That paragraph is stored in the `style_profile` table and prepended to **every outfit suggestion prompt** as context

**Why this works:** Claude gets your profile + semantically retrieved garments + your question all in one prompt. It reasons across all three. The profile refreshes automatically when you save new outfits or change ratings.

**Prompt caching:** Because the profile text is identical across most requests, it's marked as a cached block — Anthropic charges ~10% of normal token rate on cache hits. This is the biggest cost lever in V1.

---

## Architecture Overview

**V1 (no shopping):**

```
┌──────────────────────────────────────────────────┐
│                  Web UI (browser)                │
│  Catalog | Outfit Builder | Calendar | Ask Chat  │
└──────────────────┬───────────────────────────────┘
                   │ HTTP
┌──────────────────▼───────────────────────────────┐
│              Flask API (app.py)                  │
│  /garments  /outfits  /suggest  /ask  /calendar  │
└──────┬──────────────┬──────────────┬─────────────┘
       │              │              │
  SQLite DB      ChromaDB       Multi-Agent
  (source of     (semantic      Coordinator
   truth)         search)        (agents.py)
                   │              │        │
              Embeddings      Closet   Style
              (local CPU)      Agent   Agent
                                         │
                                   Style Profile
                                   (cached text)
```

**On image upload (auto-tagging flow):**
```
User uploads photo
→ app.py resizes to 768px
→ Claude Haiku Vision analyzes image
→ Returns: { type, color, brand, fit, occasion }
→ Form pre-filled, user confirms/edits
→ Saved to SQLite + embedded in ChromaDB
```

**V2 addition (shopping):**

```
Flask API adds:  /shop
agents.py adds:  Shopper Agent
                     │
                 Tavily API (live web search)
                     │
                 Claude scores results vs style profile
```

### Why RAG here?
When you ask "what should I wear today?" or "find me something new that fits my style," the system embeds your query and retrieves the most semantically relevant garments or style patterns rather than scanning all rows. This makes suggestions smarter than SQL keyword matching.

### Why Multi-Agent?
Each concern has different tools and knowledge:
- **Closet Agent** — has access to the SQLite DB + vector store; answers "what blue shirts do I have?"
- **Style Agent** — reads your saved outfits/ratings to build a preference profile; answers "what's my aesthetic?"
- **Shopper Agent** — takes the style profile + wardrobe gaps; searches the web; answers "what should I buy?"
- **Coordinator** — routes user queries, fans out to subagents, synthesizes the answer

---

## Data Model (SQLite — `closet.db`)

```sql
garments      id, name, type, color, brand, fit, occasion, tags (JSON),
              image_path, notes, worn_count, added_at

outfits       id, name, occasion, rating (1-5), notes, created_at

outfit_items  outfit_id, garment_id          -- join table

outfit_logs   id, date, outfit_id (nullable), garment_ids (JSON),
              notes, created_at              -- calendar: what you wore each day

style_profile id=1, summary (text), last_updated  -- one row, Claude-written
```

---

## RAG Layer (ChromaDB)

**Collection: `garments`**
- Document per garment: `"{type} {color} {brand} {tags} {notes}"`
- Metadata: garment_id, type, color, occasion
- On add/update garment → re-embed and upsert into ChromaDB

**Collection: `outfits`**  
- Document per outfit: flattened garment descriptions + occasion + notes
- Used by Style Agent to find "fits like what I like"

Embedding model: Use Claude's `messages` API with a prompt to generate a descriptive embedding string, then use `chromadb`'s default embedding (all-MiniLM-L6-v2 via sentence-transformers, runs locally, free).

---

## Multi-Agent Design (`agents.py`)

Uses Claude `claude-sonnet-4-6` with tool use. Each agent is a function that Claude can call as a tool.

```python
# Coordinator loop (simplified)
def run_coordinator(user_query: str) -> str:
    tools = [closet_tool, style_tool, shopper_tool]
    # Claude decides which agents to invoke, synthesizes result
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        tools=tools,
        messages=[{"role": "user", "content": user_query}]
    )
    # Handle tool_use blocks, call the right agent, loop until text response

def closet_agent(query: str) -> dict:
    # Semantic search via ChromaDB, then fetch full records from SQLite

def style_agent() -> dict:
    # Read top-rated outfits, call Claude to summarize style profile

def shopper_agent(style_profile: str, gaps: str) -> list:
    # Web search via SerpAPI or DuckDuckGo scraping
    # Claude re-ranks results against style profile
```

---

## File Structure

```
closet-manager/
├── app.py              # Flask routes
├── database.py         # SQLite init + CRUD
├── vector_store.py     # ChromaDB wrapper (add, search, delete)
├── agents.py           # Coordinator + subagent functions
├── vision.py           # Auto-tag: resize image + call Claude Haiku Vision
├── requirements.txt
├── static/
│   └── uploads/        # clothing images (stored after resize)
└── templates/
    ├── index.html       # catalog grid view
    ├── add.html         # add garment (form pre-filled by auto-tag)
    ├── outfit.html      # outfit builder + save/rate
    ├── calendar.html    # outfit calendar log
    └── ask.html         # free-text ask chat
```

> `shopping.py` is not included in V1 — added in V2.

---

## Key API Routes

### V1
| Method | Route | Purpose |
|--------|-------|---------|
| GET/POST | `/garments` | List all / add new garment |
| PUT/DELETE | `/garments/<id>` | Update or remove a garment |
| POST | `/garments/autotag` | Upload photo → Claude Vision → return pre-filled fields |
| GET/POST | `/outfits` | List all / save new outfit |
| GET | `/suggest` | AI outfit suggestion (RAG + Closet Agent) |
| POST | `/ask` | Free-text query → Coordinator Agent |
| GET | `/style` | View/refresh style profile |
| GET/POST | `/calendar` | View log / add today's outfit entry |

### V2 additions
| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/shop` | Shopping recs via Tavily + Shopper Agent |
| GET | `/shop/gaps` | Wardrobe gaps Claude identified |

---

## Shopping Integration (V2 — deferred)

Shopper Agent flow using Tavily:
```
Style Agent output → extract wardrobe gaps
→ Build search queries (e.g., "slim fit navy chinos casual")
→ Tavily API fetches top 10 live results
→ Claude scores each result (0-10) against style profile
→ Return top 5 with reasoning
```

Tavily free tier: 1,000 searches/month — sufficient for personal use.

---

## Frontend (Simple, No Framework)

Plain HTML + vanilla JS (consistent with existing templates in the repo). Three views:
- **Catalog**: grid of clothing cards with image, tags, worn count
- **Outfit Builder**: drag-and-drop or checkbox multi-select → save combo → rate it
- **Ask / Shop**: chat-style input → streamed response from `/ask` or `/shop`

---

## Build Order

### V1
1. `database.py` + `app.py` scaffold — garments CRUD, SQLite (includes `outfit_logs` table)
2. `vision.py` — image resize + Claude Haiku Vision auto-tag on upload
3. `vector_store.py` — ChromaDB setup, embed on garment add/update
4. `agents.py` — Closet Agent + outfit suggestion via `/suggest`
5. Style Agent — reads outfits + calendar data, writes style profile (cached)
6. `/ask` free-text Coordinator endpoint
7. UI — catalog grid, add form (auto-tag flow), outfit builder, calendar log, ask chat

### V2
8. `shopping.py` + Tavily integration
9. Shopper Agent in `agents.py`
10. `/shop` and `/shop/gaps` routes
11. Shopping feed UI tab

---

## Dependencies

### V1
```
flask
anthropic
chromadb                # vector store + local embeddings
sentence-transformers   # local CPU embedding model (all-MiniLM-L6-v2, free)
Pillow                  # image resize before Vision API call
```

### V2 additions
```
tavily-python           # live web search for shopping agent
```

---

## Verification

### V1
- Upload a garment photo → `POST /garments/autotag` → form arrives pre-filled with type/color/brand/fit
- Add 5+ garments → confirm they appear in catalog grid
- `GET /suggest` → Claude returns a coherent outfit from your garments using RAG
- `POST /outfits` to save a combo → rate it 5 stars
- `GET /style` → style profile reflects your saved outfits and calendar data
- `POST /ask` with "what blue things do I own?" → Closet Agent returns correct items via semantic search
- `POST /calendar` → log today's outfit → `GET /calendar` → see the history feed

### V2
- `GET /shop` → Tavily fetches live results, Claude scores and returns top 5 with reasoning
- `GET /shop/gaps` → Claude identifies wardrobe gaps from style profile
