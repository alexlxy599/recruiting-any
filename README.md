# Recruiting Any

[中文版](README_CN.md)

Local-first AI recruiting platform. Discover candidates from academic labs, top AI conferences, and industry; manage a unified talent pool; generate personalized outreach — all from your browser, all on your machine.

![Pool Dashboard](docs/pool-dashboard.png)

## What It Does

```
Discover  →  Pool  →  Outreach
```

### Discover — Find candidates at scale

Five sourcing modes, one import pipeline:

- **CSRankings Search**: Search top CS faculty by institution, auto-extract their lab members (PhD students, postdocs, research scientists)
- **Smart Search**: Point at any professor homepage or lab page, extract members via LLM
- **Department Page**: Batch-extract from university department pages
- **Conference Sourcing**: Pull accepted papers from NeurIPS / ICML / ICLR (OpenReview + conference virtual pages), filter authors, then enrich affiliations & emails from arXiv HTML — with dual-evidence paper matching (title similarity × author overlap) to survive preprint renames
- **Citation Expansion**: Start from one strong candidate, walk the Semantic Scholar citation graph to find similar people outside the pool

Two-stage scraping keeps costs low: fast HTTP fetch + a single LLM call per page. Handles JS-rendered pages, cross-domain lab sites, icon-only links, 100K+ char pages.

![Lab Sourcer](docs/lab-sourcer.png)

### Pool — Manage candidates in one place

- Full-text search (FTS5), boolean queries, and **semantic search** (LanceDB vectors)
- **All / Academic / Industry / Conference / Open-source** lenses — one pool, filtered views
- Academic view: filter by advisor, institution, graduation year, research area, **venue badges** (ICML / NeurIPS / CVPR…), and pipeline status (New → Contacted → Replied → Interview)
- Insights dashboard: company distribution, degree breakdown, top schools, outreach funnel, and a clickable **research-area word cloud**
- **GitHub identity verification review**: every GitHub link is tiered — hard-verified (email / LinkedIn backlink) → LLM-adjudicated → needs review → excluded — so you never message the wrong account
- Co-author relationship graph per candidate
- Chrome extension for LinkedIn profile capture

![Academic Pipeline](docs/academic-pipeline.png)

![GitHub Verification Review](docs/github-review.png)

### Outreach — Generate personalized messages

- Three modes: direct pitch, open-source community, academic collaboration
- Bilingual (Chinese / English)
- Context-aware: pulls the candidate's full profile — work history, education, publications, AI-extracted homepage portrait
- One click from any candidate page; history tracked per person with reply status

![Candidate Detail & Outreach](docs/person-detail.png)

> Screenshots are blurred/redacted: names, emails, avatars, and outreach content are masked to protect candidate privacy.

## Quick Start

```bash
git clone https://github.com/alexlxy599/recruiting-any.git
cd recruiting-any
pip install flask anthropic openai requests beautifulsoup4 lxml duckduckgo-search python-dotenv lancedb
```

Create `.env`:

```
GITHUB_TOKEN=ghp_...          # optional, for GitHub enrichment (5000 req/hr)
ANTHROPIC_API_KEY=sk-ant-...  # optional, for Claude-powered outreach
```

Restore the talent pool (private repo — the dump ships with the code):

```bash
gzip -dc data/exports/talent.sql.gz | sqlite3 data.db
```

Run:

```bash
python app.py
```

Open http://localhost:5055

### Refreshing the dump

`data.db` itself is gitignored — commit the text dump instead, so git stores deltas rather than a new 34 MB binary blob on every change:

```bash
sqlite3 data.db "PRAGMA wal_checkpoint(TRUNCATE);"
sqlite3 data.db .dump | gzip -n9 > data/exports/talent.sql.gz
```

> The dump contains real candidate PII. This repository must stay **private**.

## LLM Configuration

The platform supports multiple LLM providers. Configure in the sidebar or per-module:

| Provider | Use Case | Setup |
|----------|----------|-------|
| **LM Studio** (local) | Free, private, fast scraping | Run locally on port 1234 |
| **OpenRouter** | Access to DeepSeek, Qwen, Llama | API key from openrouter.ai |
| **Anthropic** | Best quality outreach + web search | API key from console.anthropic.com |

For Discover/scraping and batch enrichment, local models work well. For Outreach message generation, Claude produces the best results.

## Architecture

```
SQLite (data.db)  ←→  Flask API  ←→  Web UI
         ↕                ↕
    FTS5 + LanceDB    LLM Provider (Anthropic / OpenAI-compat / Local)
```

- **All data stays local** — only LLM API calls go to the cloud (switchable to fully offline with Ollama/LM Studio)
- Single SQLite database with WAL mode, FTS5 full-text search, and LanceDB for vector embeddings
- No React, no build step — vanilla HTML/CSS/JS with server-side rendering
- `mcp_server.py` exposes the pool to MCP clients (query candidates from Claude Code / other agents)

## Project Structure

```
app.py                  # Flask routes and API endpoints
db.py                   # Schema, migrations, all database operations
conference_scraper.py   # Conference paper sourcing (OpenReview / uploads / author filter)
discover_s2.py          # Semantic Scholar citation-graph expansion
fast_scraper.py         # Two-stage lab scraper (HTTP fetch + LLM extract)
agent_scraper.py        # Agentic web search with tool use
enrich_homepage.py      # Homepage snapshot → LLM extraction → normalized profile
enrich_github_repos.py  # GitHub repo signals
verify_github.py        # Tiered GitHub identity verification
csrankings.py           # CSRankings data integration
mcp_server.py           # MCP server over the talent pool
ai/
  embedder.py           # Vector embeddings + semantic search
templates/              # Discover / Pool / Person / Academic / Outreach pages
chrome-extension/       # LinkedIn profile capture extension
ingest/
  import_csv.py         # CSV batch import
  import_icml.py        # Conference author import
  import_competition.py # Math/CS competition award lists (Yau contest, etc.)
  build_coauthor_graph.py
migrations/             # Destructive schema changes
```

## Tech Stack

- **Backend**: Python, Flask, SQLite + FTS5
- **Frontend**: Vanilla HTML/CSS/JS, Chart.js
- **Scraping**: BeautifulSoup, Requests
- **LLM**: Anthropic SDK, OpenAI SDK (compatible with OpenRouter, LM Studio, Ollama)
- **Search**: LanceDB (vectors), FTS5 (full-text), boolean query parser
- **Data Sources**: CSRankings, OpenReview, arXiv, GitHub API, Semantic Scholar, Google Scholar, DuckDuckGo

## License

Private project. All rights reserved.
