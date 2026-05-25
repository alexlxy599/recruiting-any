# Recruiting Any

[中文版](README_CN.md)

Local-first AI recruiting platform. Discover candidates from academic labs and industry, manage a unified talent pool, and generate personalized outreach — all from your browser.

![Pool Dashboard](docs/pool-dashboard.png)

![Candidate Detail & Outreach](docs/person-detail.png)

## What It Does

```
Discover  →  Pool  →  Outreach
```

**Discover** — Find candidates at scale
- **CSRankings Integration**: Search top CS faculty by institution, auto-extract their lab members (PhD students, postdocs, research scientists)
- **Smart Search**: Point at any professor homepage or lab page, extract members via LLM
- **Department Page Scraper**: Batch-extract from university department pages
- **Conference Papers** *(coming soon)*: Identify first-author candidates from NeurIPS, ICML, ICLR, ACL, CVPR proceedings
- Two-stage scraping: fast HTTP fetch + single LLM call per lab (no expensive multi-round agent loops)
- Handles JS-rendered pages, cross-domain lab sites, icon-only links, 100K+ char pages

**Pool** — Manage candidates in one place
- Full-text search (FTS5), boolean queries, semantic matching
- **All / Academic / Industry** tabs — one pool, filtered views
- Academic view: filter by advisor, institution, graduation year, research area, pipeline status
- Insights dashboard: company distribution, degree breakdown, top schools, outreach funnel
- Chrome extension for LinkedIn profile capture

**Outreach** — Generate personalized messages
- Three modes: direct pitch, open-source community, academic collaboration
- Bilingual (Chinese / English)
- Context-aware: pulls candidate's full profile, work history, education, publications
- Configurable sender identity
- History tracking with search and export

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

Run:

```bash
python app.py
```

Open http://localhost:5055

## LLM Configuration

The platform supports multiple LLM providers. Configure in the sidebar or per-module:

| Provider | Use Case | Setup |
|----------|----------|-------|
| **LM Studio** (local) | Free, private, fast scraping | Run locally on port 1234 |
| **OpenRouter** | Access to DeepSeek, Qwen, Llama | API key from openrouter.ai |
| **Anthropic** | Best quality outreach + web search | API key from console.anthropic.com |

For Discover/scraping, local models work well. For Outreach message generation, Claude produces the best results.

## Architecture

```
SQLite (data.db)  ←→  Flask API  ←→  Web UI
         ↕                ↕
    FTS5 + LanceDB    LLM Provider (Anthropic / OpenAI-compat / Local)
```

- **All data stays local** — only LLM API calls go to the cloud (switchable to fully offline with Ollama/LM Studio)
- Single SQLite database with WAL mode, FTS5 full-text search, and LanceDB for vector embeddings
- No React, no build step — vanilla HTML/CSS/JS with server-side rendering

## Project Structure

```
app.py                  # Flask routes and API endpoints
db.py                   # Schema, migrations, all database operations
fast_scraper.py         # Two-stage lab scraper (HTTP fetch + LLM extract)
agent_scraper.py        # Agentic web search with tool use
enrich_academic.py      # Batch enrich from personal pages
csrankings.py           # CSRankings data integration
scraper.py              # Legacy scraper
ai/
  embedder.py           # Vector embeddings + semantic search
templates/
  lab_sourcer.html      # Discover page
  people.html           # Pool page (All + Academic + Industry tabs)
  person.html           # Candidate detail page
  index.html            # Outreach page
  academic.html         # Standalone academic view (redirects to Pool)
chrome-extension/       # LinkedIn profile capture extension
ingest/
  import_csv.py         # CSV batch import
```

## Tech Stack

- **Backend**: Python, Flask, SQLite + FTS5
- **Frontend**: Vanilla HTML/CSS/JS, Chart.js
- **Scraping**: BeautifulSoup, Requests
- **LLM**: Anthropic SDK, OpenAI SDK (compatible with OpenRouter, LM Studio, Ollama)
- **Search**: LanceDB (vectors), FTS5 (full-text), boolean query parser
- **Data Sources**: CSRankings, GitHub API, Semantic Scholar, Google Scholar, DuckDuckGo

## License

Private project. All rights reserved.
