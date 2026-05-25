"""Enrich academic candidates by scraping their personal pages.

Given a list of members with personal_page URLs, fetches each page
and extracts additional info: Google Scholar link, publication count,
refined research interests, graduation year if explicitly stated.

Uses one LLM call per batch (not per person) for efficiency.
"""

import json
import re
from openai import OpenAI
from fast_scraper import _fetch_text, _smart_truncate


ENRICH_PROMPT = """\
You are enriching academic candidate profiles by analyzing their personal web pages.

For each person below, I've fetched their personal page content. Extract any of the following information that is available:

- "google_scholar": Google Scholar profile URL (look for links containing scholar.google.com)
- "publications_count": approximate number of publications (count paper entries if visible)
- "research_area": refined/complete research interests (comma-separated)
- "expected_graduation": graduation year if explicitly stated (e.g. "I'm a 4th year PhD student" + current year 2026 → 2027)
- "github": GitHub profile URL if present

Output a JSON array with one object per person. Each object MUST have:
- "name": the person's name (to match back)
- Only include fields where you found actual data (skip unknowns)

Output ONLY the JSON array, no other text."""


def enrich_from_personal_pages(members: list[dict], api_key: str,
                                base_url: str = "", model: str = "",
                                status_cb=None) -> list[dict]:
    """Fetch personal pages and enrich member data via LLM.

    Args:
        members: list of member dicts (must have 'name' and 'personal_page')
        api_key: LLM API key
        base_url: optional OpenAI-compatible base URL
        model: model to use
        status_cb: callback for progress updates

    Returns:
        Updated list of member dicts with enriched fields
    """
    def report(msg):
        if status_cb:
            status_cb(msg)

    # Filter to only members with personal pages
    enrichable = [(i, m) for i, m in enumerate(members) if m.get("personal_page")]
    if not enrichable:
        report("No members have personal pages to enrich")
        return members

    report(f"Enriching {len(enrichable)} members with personal pages...")

    # Stage 1: Fetch all personal pages
    page_texts = []
    for idx, (orig_idx, m) in enumerate(enrichable):
        url = m["personal_page"]
        report(f"Fetching ({idx+1}/{len(enrichable)}): {m.get('name', 'unknown')}")
        text = _fetch_text(url)
        if text:
            text = _smart_truncate(text, max_chars=6000)
        page_texts.append((m["name"], text or "(page could not be fetched)"))

    # Stage 2: One LLM call for all
    combined = "\n\n".join(
        f"=== {name} ===\n{text}" for name, text in page_texts
    )

    # Cap at 30k
    if len(combined) > 30000:
        combined = combined[:30000] + "\n\n[... truncated ...]"

    report("Sending to LLM for enrichment...")

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)

    if not model:
        model = "deepseek/deepseek-chat-v4-0514"

    response = client.chat.completions.create(
        model=model,
        max_tokens=4000,
        messages=[
            {"role": "system", "content": ENRICH_PROMPT},
            {"role": "user", "content": combined},
        ],
        temperature=0.1,
    )

    text = response.choices[0].message.content or ""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()

    try:
        enrichments = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", text)
        if match:
            try:
                enrichments = json.loads(match.group())
            except json.JSONDecodeError:
                enrichments = []
        else:
            enrichments = []

    if not isinstance(enrichments, list):
        enrichments = []

    # Match enrichments back to members by name
    enrich_map = {}
    for e in enrichments:
        name = (e.get("name") or "").strip().lower()
        if name:
            enrich_map[name] = e

    enriched_count = 0
    for orig_idx, m in enrichable:
        name_key = (m.get("name") or "").strip().lower()
        if name_key in enrich_map:
            e = enrich_map[name_key]
            # Only update fields that have new data
            for field in ["google_scholar", "publications_count", "research_area",
                         "expected_graduation", "github"]:
                if field in e and e[field]:
                    members[orig_idx][field] = e[field]
            enriched_count += 1

    report(f"Enriched {enriched_count}/{len(enrichable)} members")
    return members
