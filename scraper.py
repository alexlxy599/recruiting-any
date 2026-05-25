"""Scrape professor homepages and extract lab members using LLM."""

import json
import re
import requests
from bs4 import BeautifulSoup
from openai import OpenAI

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
})

# Lazy-loaded Playwright browser
_browser = None


def _get_browser():
    """Get or create a shared Playwright browser instance."""
    global _browser
    if _browser is None:
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            _browser = pw.chromium.launch(headless=True)
        except Exception:
            return None
    return _browser


def fetch_page_text_js(url: str, timeout: int = 30000) -> str:
    """Fetch a URL using headless browser (handles JS-rendered pages)."""
    browser = _get_browser()
    if not browser:
        return ""
    try:
        page = browser.new_page()
        page.goto(url, timeout=timeout)
        page.wait_for_load_state("networkidle")
        # Also try clicking "People" link if it exists (for SPA sites)
        try:
            people_link = page.locator('a:text-matches("people|members|team|group", "i")').first
            if people_link.is_visible(timeout=1000):
                people_link.click()
                page.wait_for_timeout(2000)
        except Exception:
            pass
        text = page.inner_text("body")
        page.close()
        return re.sub(r"\n{3,}", "\n\n", text.strip())
    except Exception:
        return ""


def fetch_page_text(url: str, timeout: int = 15) -> str:
    """Fetch a URL and return cleaned text content.
    Falls back to headless browser if static fetch returns too little content."""
    try:
        resp = _session.get(url, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove script/style
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)

        # If too little content, page might be JS-rendered
        if len(text) < 200:
            js_text = fetch_page_text_js(url)
            if len(js_text) > len(text):
                return js_text[:12000]

        return text[:12000]
    except Exception:
        # Try JS fallback
        return fetch_page_text_js(url)[:12000]


def find_lab_pages(homepage: str) -> list[str]:
    """Given a professor's homepage, find links to 'people', 'group', 'team', 'lab' pages."""
    from urllib.parse import urlparse
    home_domain = urlparse(homepage).netloc.lower()
    urls = [homepage]
    try:
        resp = _session.get(homepage, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Prioritize links with people-related keywords
        people_kw = ["people", "members", "team", "group", "students"]
        other_kw = ["lab", "research"]

        scored_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].lower()
            link_text = a.get_text().lower()
            full_url = requests.compat.urljoin(homepage, a["href"])
            # Only keep links on same domain or closely related (e.g. lab subdomain)
            link_domain = urlparse(full_url).netloc.lower()
            if link_domain != home_domain and not link_domain.endswith("." + home_domain.split(".")[-2] + "." + home_domain.split(".")[-1]):
                # Allow github.io sibling sites (e.g. xxlya.github.io -> ubc-tea.github.io)
                if not (home_domain.endswith(".github.io") and link_domain.endswith(".github.io")):
                    continue
            if full_url in urls:
                continue
            # Score: people-related keywords worth more
            score = 0
            for kw in people_kw:
                if kw in href or kw in link_text:
                    score += 2
            for kw in other_kw:
                if kw in href or kw in link_text:
                    score += 1
            if score > 0:
                scored_links.append((score, full_url))

        scored_links.sort(key=lambda x: -x[0])
        for _, link in scored_links[:3]:
            if link not in urls:
                urls.append(link)

        # Also try common sub-paths from the base domain
        parsed = urlparse(homepage)
        base = f"{parsed.scheme}://{parsed.netloc}"
        for suffix in ["/people", "/team", "/members", "/group"]:
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
    return urls[:4]


def extract_members_llm(page_text: str, professor_name: str,
                        api_key: str = "", base_url: str = "",
                        model: str = "") -> list[dict]:
    """Use LLM to extract lab members from page text.

    Returns list of {name, role, research_area, email, personal_page}.
    """
    if not page_text.strip():
        return []

    api_key = api_key or "ollama"
    base_url = base_url or "http://localhost:11434/v1"
    model = model or "qwen3:8b"

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=300)

    prompt = f"""Analyze this webpage text from Professor {professor_name}'s lab/group page.
Extract ALL lab members you can find. For each person, extract:
- name: full name
- role: one of "Professor", "Postdoc", "PhD Student", "Master Student", "Research Scientist", "Visiting Scholar", "Alumni", or "Unknown"
- research_area: their research focus if mentioned, otherwise empty string
- email: email address if found, otherwise empty string
- personal_page: personal website URL if found, otherwise empty string

Return ONLY a JSON array, no explanation. If no members found, return [].
Example: [{{"name": "John Doe", "role": "PhD Student", "research_area": "computer vision", "email": "john@uni.edu", "personal_page": "https://johndoe.com"}}]

Webpage text:
{page_text}"""

    try:
        resp = client.chat.completions.create(
            model=model,
            max_tokens=4096,
            messages=[
                {"role": "system", "content": "You extract structured data from webpages. Output only valid JSON.\n/no_think"},
                {"role": "user", "content": prompt},
            ],
        )
        content = (resp.choices[0].message.content or "").strip()

        # Try to extract JSON array from response
        match = re.search(r"\[.*\]", content, re.DOTALL)
        if match:
            members = json.loads(match.group())
            # Validate structure
            valid = []
            for m in members:
                if isinstance(m, dict) and m.get("name"):
                    valid.append({
                        "name": m.get("name", "").strip(),
                        "role": m.get("role", "Unknown").strip(),
                        "research_area": m.get("research_area", "").strip(),
                        "email": m.get("email", "").strip(),
                        "personal_page": m.get("personal_page", "").strip(),
                    })
            return valid
    except Exception:
        pass
    return []


def fetch_page_html(url: str, timeout: int = 15):
    """Fetch a URL and return BeautifulSoup object."""
    try:
        resp = _session.get(url, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return None


ROLE_PATTERNS = [
    (r'Professor of Teaching', 'Professor of Teaching'),
    (r'Assistant Professor', 'Assistant Professor'),
    (r'Associate Professor', 'Associate Professor'),
    (r'\bProfessor\b', 'Professor'),
    (r'Postdoc', 'Postdoc'),
    (r'Research Associate', 'Research Associate'),
    (r'Research Scientist', 'Research Scientist'),
    (r'Lecturer', 'Lecturer'),
    (r'PhD Student|Doctoral', 'PhD Student'),
    (r'Master Student|MSc', 'Master Student'),
]

AREA_KEYWORDS = [
    "Artificial Intelligence", "Machine Learning", "Computer Vision",
    "Natural Language Processing", "Robotics", "HCI", "Human-Computer",
    "Data Mining", "Security", "Systems", "Algorithms", "Programming Languages",
    "Software Engineering", "Bioinformatics", "Scientific Computing",
    "Visualization", "NLP", "Deep Learning", "Reinforcement Learning",
]

SKIP_LINK_WORDS = {"personal page", "personal website", "homepage",
                    "website", "google scholar", "scholar"}


def _detect_role(text: str) -> str:
    for pat, role in ROLE_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return role
    return "Unknown"


def _detect_areas(text: str) -> str:
    found = [kw for kw in AREA_KEYWORDS if kw.lower() in text.lower()]
    return ", ".join(found[:3])


def extract_people_from_html(soup, base_url: str = "") -> list[dict]:
    """Extract people from HTML, preserving links and emails."""
    people = []
    seen = set()

    def resolve(href):
        return requests.compat.urljoin(base_url, href) if base_url else href

    # Strategy 1: Table rows with email addresses
    for tr in soup.find_all("tr"):
        text = tr.get_text(separator=" ", strip=True)
        email_match = re.search(r'[\w.+-]+@[\w.-]+\.\w+', text)
        if not email_match:
            continue

        email = email_match.group()

        # Scan ALL links in the row for name, personal page, etc.
        personal_page = ""
        name = ""
        links = tr.find_all("a", href=True)
        for a in links:
            href = a.get("href", "")
            link_text = a.get_text(strip=True)
            lt_lower = link_text.lower()

            # Personal page link
            if lt_lower in SKIP_LINK_WORDS:
                if lt_lower != "google scholar" and href:
                    personal_page = resolve(href)
                continue

            if "scholar.google" in href or "mailto:" in href:
                continue

            # First substantive link text → name candidate
            if not name and link_text and len(link_text) > 2:
                name = link_text

        # Fallback: look for name in <td> cells via bold/strong or first text
        if not name:
            tds = tr.find_all("td")
            for td in tds:
                # Try bold text first
                bold = td.find(["strong", "b"])
                if bold:
                    candidate = bold.get_text(strip=True)
                    candidate = re.sub(r'(Personal Page|Google Scholar)', '', candidate).strip()
                    if candidate and len(candidate) > 2:
                        name = candidate
                        break
                # Try first text node
                td_text = td.get_text(separator=" ", strip=True)
                td_text = re.sub(r'(Personal Page|Google Scholar|Homepage)', '', td_text).strip()
                if td_text and len(td_text) > 2 and "@" not in td_text:
                    name = td_text.split("\n")[0].strip()
                    break

        # If still no personal_page, check if name link itself is one
        if not personal_page and name:
            for a in links:
                if a.get_text(strip=True) == name:
                    href = a.get("href", "")
                    if href and "mailto:" not in href and "scholar" not in href:
                        personal_page = resolve(href)
                    break

        role = _detect_role(text)
        research_area = _detect_areas(text)

        if name and len(name) > 2 and name.lower() not in seen:
            seen.add(name.lower())
            people.append({
                "name": name,
                "role": role,
                "research_area": research_area,
                "email": email,
                "personal_page": personal_page,
            })

    # Strategy 2: div/card structures (if no table matches)
    if not people:
        for el in soup.find_all(["div", "li", "article"]):
            text = el.get_text(separator=" ", strip=True)
            email_match = re.search(r'[\w.+-]+@[\w.-]+\.\w+', text)
            if not email_match:
                continue
            if len(text) > 1000:
                continue

            email = email_match.group()
            personal_page = ""
            name = ""
            links = el.find_all("a", href=True)

            for a in links:
                href = a.get("href", "")
                lt = a.get_text(strip=True)
                lt_lower = lt.lower()
                if lt_lower in SKIP_LINK_WORDS:
                    if lt_lower != "google scholar" and href:
                        personal_page = resolve(href)
                    continue
                if "scholar.google" in href or "mailto:" in href:
                    continue
                if not name and lt and len(lt) > 2:
                    name = lt
                    if not personal_page and href:
                        personal_page = resolve(href)

            if not name:
                bold = el.find(["strong", "b", "h3", "h4"])
                if bold:
                    name = bold.get_text(strip=True)

            if name and len(name) > 2 and name.lower() not in seen:
                seen.add(name.lower())
                people.append({
                    "name": name,
                    "role": _detect_role(text),
                    "research_area": _detect_areas(text),
                    "email": email,
                    "personal_page": personal_page,
                })

    return people


def scrape_department_page(url: str,
                           api_key: str = "", base_url: str = "",
                           model: str = "") -> list[dict]:
    """Scrape a department people/faculty page and extract all members.

    First tries direct HTML parsing (fast, preserves links).
    Falls back to LLM extraction if HTML parsing finds nothing.
    """
    soup = fetch_page_html(url)
    if not soup:
        return []

    # Try direct HTML extraction first
    people = extract_people_from_html(soup, base_url=url)

    if people:
        return people

    # Fallback: use LLM on text (loses URLs but catches unusual formats)
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    full_text = soup.get_text(separator="\n", strip=True)
    full_text = re.sub(r"\n{3,}", "\n\n", full_text)

    if not full_text.strip():
        return []

    api_key = api_key or "ollama"
    base_url_llm = base_url or "http://localhost:11434/v1"
    model = model or "qwen3:8b"
    client = OpenAI(api_key=api_key, base_url=base_url_llm)

    all_members = {}
    chunk_size = 12000
    chunks = [full_text[i:i+chunk_size] for i in range(0, len(full_text), chunk_size)]

    for chunk in chunks:
        prompt = f"""Extract ALL people from this department listing page.
For each person, return: name, role, research_area, email, personal_page.
Return ONLY a JSON array. If none found, return [].

Text:
{chunk}"""
        try:
            resp = client.chat.completions.create(
                model=model, max_tokens=8192,
                messages=[
                    {"role": "system", "content": "You extract structured data from webpages. Output only valid JSON.\n/no_think"},
                    {"role": "user", "content": prompt},
                ],
            )
            content = (resp.choices[0].message.content or "").strip()
            match = re.search(r"\[.*\]", content, re.DOTALL)
            if match:
                members = json.loads(match.group())
                for m in members:
                    if isinstance(m, dict) and m.get("name"):
                        key = m["name"].lower().strip()
                        if key and key not in all_members:
                            all_members[key] = {
                                "name": m.get("name", "").strip(),
                                "role": m.get("role", "Unknown").strip(),
                                "research_area": m.get("research_area", "").strip(),
                                "email": m.get("email", "").strip(),
                                "personal_page": m.get("personal_page", "").strip(),
                            }
        except Exception:
            pass

    return list(all_members.values())


def fetch_page_text_full(url: str, timeout: int = 15) -> str:
    """Fetch a URL and return cleaned text content (no truncation).
    Falls back to headless browser for JS-rendered pages."""
    try:
        resp = _session.get(url, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        if len(text) < 200:
            js_text = fetch_page_text_js(url)
            if len(js_text) > len(text):
                return js_text
        return text
    except Exception:
        return fetch_page_text_js(url)


def scrape_lab(homepage: str, professor_name: str,
               api_key: str = "", base_url: str = "",
               model: str = "", status_cb=None) -> list[dict]:
    """Full pipeline: find lab pages → scrape → LLM extract members.

    status_cb: optional callback(message) for progress reporting.
    """
    all_members = {}

    if status_cb:
        status_cb(f"Finding lab pages from {homepage}...")
    pages = find_lab_pages(homepage)
    if status_cb:
        status_cb(f"Found {len(pages)} pages to analyze")
    for url in pages:
        if status_cb:
            status_cb(f"Fetching {url}...")
        full_text = fetch_page_text_full(url)
        if not full_text or len(full_text) < 100:
            if status_cb:
                status_cb(f"  Skipped (too short or empty)")
            continue
        # Skip obvious template/placeholder pages
        if "write your biography here" in full_text.lower():
            if status_cb:
                status_cb(f"  Skipped (template page)")
            continue

        if status_cb:
            status_cb(f"  Analyzing with LLM ({len(full_text)} chars)...")

        # Split long pages into chunks for LLM
        chunk_size = 12000
        chunks = [full_text[i:i+chunk_size] for i in range(0, len(full_text), chunk_size)]

        for chunk in chunks:
            members = extract_members_llm(
                chunk, professor_name,
                api_key=api_key, base_url=base_url, model=model
            )
            for m in members:
                key = m["name"].lower().strip()
                if key and key not in all_members:
                    all_members[key] = m

        if status_cb:
            status_cb(f"  Found {len(all_members)} members so far")

        # If we already found 3+ members, skip remaining pages to save time
        if len(all_members) >= 3:
            break

    return list(all_members.values())
