# Closet Manager

AI-powered wardrobe catalog built with Flask and the Claude API. Catalog your garments, get outfit suggestions, and build a style profile — all backed by semantic search over your own closet.

## Features

- **Garment catalog** — add clothes with photos; Claude Haiku Vision auto-tags type, color, brand, fit, and occasion
- **Semantic search** — ChromaDB with ONNX embeddings finds garments by meaning, not just keywords
- **Ask** — free-text chat about your wardrobe ("what blue things do I own?", "what goes with my white oxford shirt?"), powered by a multi-agent coordinator using Claude tool use
- **Outfit builder** — assemble and save outfits, rate them 1–5 stars
- **Calendar** — log what you wore each day; auto-detect the outfit from a photo
- **Style profile** — Claude Sonnet writes a natural-language summary of your style from your outfits and calendar history, used to personalize suggestions
- **Outfit suggester** — RAG pipeline: semantic search retrieves relevant garments, Claude Haiku picks the outfit

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # add your ANTHROPIC_API_KEY
python app.py           # http://localhost:5002
```

## Deployment

Includes a `Procfile` (`gunicorn app:app`) and reads `PORT` from the environment, so it's ready to deploy on any Procfile-based host (Railway, Render, Heroku, etc.). Set `ANTHROPIC_API_KEY` in the host's environment variables.

## Stack

Flask · SQLite · ChromaDB (ONNX MiniLM embeddings) · Claude Haiku & Sonnet

See `CLAUDE.md` for architecture notes and testing status.
