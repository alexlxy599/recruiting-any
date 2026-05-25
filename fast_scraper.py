"""Two-stage lab member scraper — fast and cheap.

Stage 1: Fetch homepage + candidate People/Team pages (no LLM, pure HTTP)
Stage 2: Send all text to LLM in one call for structured extraction

One LLM call per lab instead of 10-15 tool-use rounds.
"""

import json
import re
import requests
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from openai import OpenAI

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
})

# ─────────────────────────────────────────
# Stage 1: Fetch pages (no LLM)
# ─────────────────────────────────────────

def _fetch_text(url: str, timeout: int = 12) -> str:
    """Fetch URL and return clean text.

    Handles JS-rendered pages by extracting JSON data from raw HTML
    when the visible text content is too sparse.
    """
    try:
        resp = _session.get(url, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        raw_html = resp.text
        soup = BeautifulSoup(raw_html, "html.parser")

        # First try normal extraction
        for tag in soup(["style", "nav", "footer", "header"]):
            tag.decompose()

        # Extract text from scripts separately (for JS-rendered pages)
        script_data = _extract_json_names(raw_html)

        # Remove scripts for normal text extraction
        for tag in soup(["script"]):
            tag.decompose()

        # Keep emails and all outbound links visible (including icon-only links)
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("mailto:"):
                a.string = href.replace("mailto:", "")
            elif href.startswith("http"):
                text = a.get_text(strip=True)
                if text:
                    a.string = f"{text} [{href}]"
                else:
                    # Icon-only links (no visible text, just img/svg/icon)
                    a.string = f"[{href}]"
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)

        # If normal text is sparse but we found JSON data, append it
        if script_data and len(text) < 500:
            text = text + "\n\n=== Structured data from page ===\n" + script_data

        return text
    except Exception:
        return ""


def _extract_json_names(html: str) -> str:
    """Extract person names and URLs from JSON-like data in raw HTML.

    Many lab websites use JS frameworks (React, Vue) and embed member data
    as JSON in script tags. This extracts that structured info.
    """
    # Find all JSON objects with "name" fields
    entries = []
    for match in re.finditer(r'\{[^{}]*"name"\s*:\s*"([^"]+)"[^{}]*\}', html):
        block = match.group(0)
        name = match.group(1)
        # Skip non-person names (robots, single words that look like config)
        if len(name) < 3 or name.lower() in ('home', 'lab', 'research'):
            continue

        # Try to extract other fields
        url_match = re.search(r'"(?:url|website|homepage|link)"\s*:\s*"([^"]+)"', block)
        role_match = re.search(r'"(?:role|title|position)"\s*:\s*"([^"]+)"', block)
        url = url_match.group(1) if url_match else ""
        role = role_match.group(1) if role_match else ""

        entry = name
        if role:
            entry += f" ({role})"
        if url:
            entry += f" [{url}]"
        entries.append(entry)

    if not entries:
        return ""
    return "\n".join(entries)


def _smart_truncate(text: str, max_chars: int = 12000) -> str:
    """Truncate text but prioritize people/members sections.

    If the text has a recognizable members/people section (often at the end
    of very long pages), extract that section and combine with the page header.
    """
    if len(text) <= max_chars:
        return text

    # Look for people/members sections anywhere in the text
    people_keywords = [
        r"(?:current\s+)?members?:", r"(?:lab\s+)?people:", r"students?:",
        r"team:", r"group\s+members?:", r"ph\.?d\.?\s+students?:",
        r"postdoc", r"research\s+(?:group|team):",
    ]
    pattern = re.compile("|".join(people_keywords), re.IGNORECASE)
    match = pattern.search(text)

    if match and match.start() > max_chars // 2:
        # Members section is in the latter half — it would be truncated
        # Keep page header (first 2000 chars) + the members section
        header = text[:2000]
        members_section = text[match.start():match.start() + max_chars - 2500]
        return header + "\n\n[...]\n\n" + members_section
    else:
        # Members section is near the top or not found — normal truncation
        return text[:max_chars]


def _find_people_pages(homepage: str) -> list[str]:
    """From a homepage, find People/Team/Members/Group sub-pages.

    Also follows cross-domain "lab" links (e.g. professor homepage links
    to a separate lab website) and discovers people pages there.
    """
    urls = [homepage]
    lab_sites = []  # cross-domain lab links to explore
    try:
        resp = _session.get(homepage, timeout=12, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        home_domain = urlparse(homepage).netloc.lower()

        people_kw = ["people", "members", "team", "group", "students", "lab", "faculty"]
        scored = []

        for a in soup.find_all("a", href=True):
            href_lower = a["href"].lower()
            link_text = a.get_text().lower().strip()
            full_url = urljoin(homepage, a["href"])
            link_domain = urlparse(full_url).netloc.lower()

            if full_url in urls:
                continue

            # Cross-domain: if the link text is "lab" or contains lab-related
            # keywords, save it for exploration (e.g. "LAB -> https://real.stanford.edu/")
            if link_domain != home_domain:
                if home_domain.endswith(".github.io") and link_domain.endswith(".github.io"):
                    pass  # sibling github.io — treat as same domain
                elif link_text in ("lab", "research group", "group") or \
                     any(kw in link_text for kw in ["lab", "research group"]):
                    lab_sites.append(full_url)
                    continue
                else:
                    continue

            score = sum(2 for kw in people_kw if kw in href_lower or kw in link_text)
            if score > 0:
                scored.append((score, full_url))

        scored.sort(key=lambda x: -x[0])
        for _, link in scored[:3]:
            if link not in urls:
                urls.append(link)

        # Try common sub-paths on homepage domain
        parsed = urlparse(homepage)
        base = f"{parsed.scheme}://{parsed.netloc}"
        for suffix in ["/people", "/team", "/members", "/group", "/lab",
                       "/people.html", "/team.html", "/members.html", "/lab.html"]:
            candidate = base + suffix
            if candidate not in urls:
                try:
                    r = _session.head(candidate, timeout=5, allow_redirects=True)
                    if r.status_code == 200:
                        urls.append(candidate)
                except Exception:
                    pass
    except Exception:
        pass

    # Explore cross-domain lab sites for people pages
    for lab_url in lab_sites[:2]:
        if lab_url not in urls:
            urls.append(lab_url)
        try:
            resp2 = _session.get(lab_url, timeout=12, allow_redirects=True)
            resp2.raise_for_status()
            soup2 = BeautifulSoup(resp2.text, "html.parser")
            lab_domain = urlparse(lab_url).netloc.lower()
            lab_base = f"{urlparse(lab_url).scheme}://{lab_domain}"

            # Check links on lab homepage
            for a in soup2.find_all("a", href=True):
                href_lower = a["href"].lower()
                link_text = a.get_text().lower().strip()
                full_url = urljoin(lab_url, a["href"])
                if urlparse(full_url).netloc.lower() != lab_domain:
                    continue
                if full_url in urls:
                    continue
                if any(kw in href_lower or kw in link_text for kw in people_kw):
                    urls.append(full_url)

            # Try common sub-paths on lab domain
            for suffix in ["/people", "/team", "/members", "/lab",
                           "/people.html", "/team.html", "/members.html", "/lab.html"]:
                candidate = lab_base + suffix
                if candidate not in urls:
                    try:
                        r = _session.head(candidate, timeout=5, allow_redirects=True)
                        if r.status_code == 200:
                            urls.append(candidate)
                    except Exception:
                        pass
        except Exception:
            pass

    return urls[:6]


def fetch_lab_content(homepage: str, professor_name: str = "",
                      status_cb=None) -> dict:
    """Stage 1: Fetch all relevant pages and return combined text.

    Returns {pages: [{url, text}], combined_text: str, professor_name: str}
    """
    def report(msg):
        if status_cb:
            status_cb(msg)

    # Auto-prepend https:// if no scheme
    if homepage and not homepage.startswith(("http://", "https://")):
        homepage = "https://" + homepage

    report(f"Finding lab pages from: {homepage}")
    page_urls = _find_people_pages(homepage)
    report(f"Found {len(page_urls)} candidate pages")

    pages = []
    combined_parts = []

    if professor_name:
        combined_parts.append(f"=== Professor: {professor_name} ===")
        combined_parts.append(f"Homepage: {homepage}\n")

    for i, url in enumerate(page_urls):
        report(f"Fetching ({i+1}/{len(page_urls)}): {url}")
        text = _fetch_text(url)
        if text:
            truncated = _smart_truncate(text)
            pages.append({"url": url, "text": truncated})
            combined_parts.append(f"=== Page: {url} ===")
            combined_parts.append(truncated)
            combined_parts.append("")

    combined_text = "\n".join(combined_parts)

    # Cap total at ~30k chars to fit in context
    if len(combined_text) > 30000:
        combined_text = combined_text[:30000] + "\n\n[... truncated ...]"

    report(f"Fetched {len(pages)} pages, {len(combined_text)} chars total")
    return {
        "pages": pages,
        "combined_text": combined_text,
        "professor_name": professor_name,
    }


# ─────────────────────────────────────────
# Stage 2: LLM extraction (one call)
# ─────────────────────────────────────────

EXTRACTION_PROMPT = """\
You are extracting lab member information from web page text.

From the following pages of a research lab, extract ALL current lab members.
Output a JSON array of members. For each member include:

- "name": full name (required)
- "role": one of: Professor, Postdoc, PhD Student, Master Student, Research Scientist, Visiting Scholar, Undergraduate, Research Engineer, or Other
- "email": email address if visible
- "research_area": research interests (comma-separated)
- "personal_page": personal homepage URL if visible
- "start_year": year they joined (if visible, e.g. from "PhD 2022-")
- "expected_graduation": estimated graduation year (integer). Rules for this field:
  - If explicitly stated on the page, use that
  - If PhD student with start_year, estimate: start_year + 5
  - If Master student with start_year, estimate: start_year + 2
  - For Postdoc/Professor/Research Scientist, omit this field
- "advisor": advisor name if listed

Rules:
- Include the PI/professor themselves
- Only include CURRENT members (skip Alumni/Graduated sections)
- If you see a year range like "2022-present" or "2022-", extract start_year as "2022"
- Skip administrative staff
- If unsure about a field, omit it rather than guess

Output ONLY the JSON array, no other text."""


def extract_members_from_text(combined_text: str, api_key: str,
                              base_url: str = "", model: str = "",
                              status_cb=None) -> list[dict]:
    """Stage 2: Send text to LLM, get structured member list back."""
    def report(msg):
        if status_cb:
            status_cb(msg)

    report("Sending to LLM for extraction...")

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)

    if not model:
        model = "deepseek/deepseek-chat-v4-0514"

    response = client.chat.completions.create(
        model=model,
        max_tokens=8000,
        messages=[
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": combined_text},
        ],
        temperature=0.1,
    )

    text = response.choices[0].message.content or ""

    # Parse JSON from response (handle markdown code blocks)
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()

    try:
        members = json.loads(text)
        if isinstance(members, list):
            report(f"Extracted {len(members)} members")
            return members
    except json.JSONDecodeError:
        # Try to find JSON array in the text
        match = re.search(r"\[[\s\S]*\]", text)
        if match:
            try:
                members = json.loads(match.group())
                report(f"Extracted {len(members)} members")
                return members
            except json.JSONDecodeError:
                pass

    report("Failed to parse LLM output")
    return []


# ─────────────────────────────────────────
# Combined pipeline
# ─────────────────────────────────────────

def fast_scrape_lab(homepage: str, professor_name: str = "",
                    api_key: str = "", base_url: str = "",
                    model: str = "", status_cb=None) -> list[dict]:
    """Full two-stage pipeline: fetch pages → LLM extract.

    Returns list of member dicts.
    """
    def report(msg):
        if status_cb:
            status_cb(msg)

    # Stage 1
    content = fetch_lab_content(homepage, professor_name, status_cb=report)

    if not content["combined_text"].strip():
        report("No content fetched from lab pages")
        return []

    # Stage 2
    members = extract_members_from_text(
        content["combined_text"],
        api_key=api_key,
        base_url=base_url,
        model=model,
        status_cb=report,
    )

    # Add source/advisor info
    for m in members:
        if not m.get("advisor") and professor_name:
            if m.get("name", "").lower() != professor_name.lower():
                m["advisor"] = professor_name
        m["source"] = professor_name or homepage

    report(f"Done: {len(members)} members found")
    return members
