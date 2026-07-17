"""Gunicorn config for UAT/production.

IMPORTANT: single worker. The app uses a local ChromaDB PersistentClient, a local
SQLite DB, and several in-process caches — multiple worker *processes* would fight
over the ChromaDB directory and not share caches. We scale with threads instead,
which share memory and comfortably handle a few concurrent testers even while a
Gemini image call is in flight.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers = 1
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
worker_class = "gthread"

# Gemini image generation is slow (~8-15s each) and a multi-item photo-import
# confirm runs them sequentially (~1 min). The default 30s timeout would kill
# those requests mid-flight, so raise it well above the worst case.
timeout = 180
graceful_timeout = 30
keepalive = 5

accesslog = "-"   # -> stdout, captured by the platform
errorlog = "-"
loglevel = "info"
