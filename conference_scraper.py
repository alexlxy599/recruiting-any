"""
Conference Sourcing — Multi-phase pipeline for discovering candidates from ML conferences.

Pipeline:
  1. Load papers (Paper Digest JSON/CSV upload, or OpenReview API)
  2. Chinese surname filter
  3. arXiv HTML parse for per-author institution + email
  4. Classify: industry / academic / unknown
  5. Results → import to talent pool

Network resilience: signal.alarm hard timeout per request (survives WiFi/VPN switches).
"""

import json
import os
import re
import signal
import socket
import time
import urllib.parse

import requests

# ── Hard timeout — kills request even if TCP is stuck ─────────

class RequestTimeout(Exception):
    pass

def _alarm_handler(signum, frame):
    raise RequestTimeout()

signal.signal(signal.SIGALRM, _alarm_handler)

def _safe_get(session, url, **kwargs):
    signal.alarm(30)
    try:
        r = session.get(url, **kwargs)
        signal.alarm(0)
        return r
    except RequestTimeout:
        return None
    except Exception:
        signal.alarm(0)
        return None

def _new_session():
    s = requests.Session()
    socket.setdefaulttimeout(30)
    return s

# ── Chinese surname list ──────────────────────────────────────

CHINESE_SURNAMES = {
    "wang", "li", "zhang", "liu", "chen", "yang", "zhao", "huang", "zhou", "wu",
    "xu", "sun", "ma", "zhu", "hu", "guo", "he", "gao", "lin", "luo",
    "zheng", "liang", "xie", "song", "tang", "han", "feng", "yu", "dong", "xiao",
    "cheng", "cao", "yuan", "deng", "fu", "shen", "zeng", "peng", "lu", "su",
    "cai", "jia", "ding", "wei", "xue", "ye", "yan", "pan", "du", "dai",
    "jiang", "fan", "zhong", "liao", "tan", "jin", "shi", "cui", "kong", "kang",
    "mao", "qiu", "qin", "hou", "long", "wan", "gu", "gong", "lai", "meng",
    "xiong", "bai", "yin", "hao", "qian", "shao", "fang", "qiao", "ren",
    "tian", "yao", "zou", "zhan", "tao", "xia", "ling", "ni", "ge",
    # 补充：ICML 2026 量化批次实测漏检的常见姓
    "lv", "lyu", "duan", "duanmu", "gan", "bao", "dang", "sha", "chu",
    "zhuang", "teng", "mu", "miao", "ning", "pang", "qi", "qu", "rong",
    "ruan", "shan", "sheng", "tong", "wen", "weng", "xin", "xing", "yi",
    "zang", "zhai", "zuo", "chai", "chi", "cong", "fei", "geng", "guan",
    "hua", "huo", "ji", "jiao", "kuang", "lei", "lian", "luan", "mei",
    "nie", "pei", "pu", "sui", "tu", "xiang", "yue", "zhuo", "zong",
    "ouyang", "sima", "zhuge", "shangguan",
}
# 港台/海外拼法：单独一档。这些姓与韩/越/其他族裔重叠严重（Lee/Chang/Ng），
# 直接并进主表会大量误判，只在明确要覆盖港台候选人时才启用。
HK_TW_SURNAMES = {
    "chan", "cheung", "cheng", "chiang", "choi", "chow", "chu", "fong",
    "ho", "hsieh", "hsu", "hui", "kwok", "kuo", "lam", "lau", "leung",
    "mak", "ng", "pang", "shek", "sit", "siu", "tsai", "tse", "tsang",
    "wong", "woo", "yeung", "yip", "yuen",
}

def has_chinese_surname(name):
    if not name:
        return False
    parts = name.strip().split()
    if not parts:
        return False
    return parts[-1].lower().rstrip(",;.") in CHINESE_SURNAMES

# ── Classification ────────────────────────────────────────────

INDUSTRY_KEYWORDS = [
    "google", "meta", "facebook", "microsoft", "apple", "nvidia",
    "amazon", "openai", "adobe", "intel", "ibm", "oracle",
    "deepmind", "anthropic", "salesforce", "samsung",
    "bosch", "huawei", "baidu", "bytedance", "tencent",
    "alibaba", "scale ai", "together ai", "cohere",
    "mistral", "stability ai", "databricks", "sensetime",
    "megvii", "deepseek", "kuaishou", "didi",
    "meituan", "jd.com", "jd ", "xiaomi", "oppo", "vivo",
    "netease", "bilibili", "sea ai", "grab", "shopee",
    "snap", "uber", "lyft", "airbnb", "stripe", "palantir",
    "tesla", "waymo", "cruise", "zoox", "aurora",
    "qualcomm", "arm", "amd", "broadcom",
    "sony", "panasonic", "toyota", "honda",
    "siemens", "philips", "sap",
    "tiktok", "zhipu", "moonshot", "minimax", "01.ai",
    "stepfun", "light year", "baichuan",
]

ACADEMIC_KEYWORDS = [
    "university", "université", "universität", "universidad", "universidade",
    "institute of technology", "college", "école", "school of",
    "technische", "politecnico", "politécnica",
    "department of", "faculty of",
    "laboratory", "research institute",
    "a*star", "inria", "cnrs", "max planck",
    "eth zurich", "epfl", "kaist", "postech",
    "caltech", "carnegie mellon", "princeton",
    "nanyang technological", "management university",
    "imperial college", "kings college",
    "tsinghua", "peking university", "beida", "fudan",
    "shanghai jiao tong", "zhejiang university",
    "ustc", "university of science and technology of china",
    "nanjing university", "harbin institute", "beihang",
    "renmin university", "wuhan university", "sun yat-sen",
    "chinese academy", "institute of automation", "institute of computing",
    "shanghai artificial intelligence laboratory", "shanghai ai lab", "pjlab",
    "beijing academy of artificial intelligence",
    "microsoft research asia",
    "westlake", "shanghaitech", "sustech",
    "hong kong", "hku", "cuhk", "hkust", "polyu", "cityu", "lingnan",
    "national taiwan", "academia sinica", "nthu", "nctu", "ntu",
    "macau", "macao",
]

ACADEMIC_EMAIL_DOMAINS = [
    "ethz.ch", "epfl.ch", "uzh.ch", "tuebingen.mpg.de",
    "mpi-inf.mpg.de", "inria.fr", "ed.ac.uk", "kcl.ac.uk",
    "ic.ac.uk", "ucl.ac.uk", "ox.ac.uk", "cam.ac.uk",
    "connect.hku.hk", "link.cuhk.edu.hk", "connect.ust.hk",
    "e.ntu.edu.sg", "u.nus.edu",
]


def classify_author(institution, email=""):
    inst = (institution or "").lower()
    if inst:
        # Check industry first (more specific)
        if any(kw in inst for kw in INDUSTRY_KEYWORDS):
            return "industry"
        if any(kw in inst for kw in ACADEMIC_KEYWORDS):
            return "academic"
    # Check email domain
    if email and "@" in email:
        domain = email.split("@")[-1].lower()
        if ".edu" in domain or ".ac." in domain:
            return "academic"
        for d in ACADEMIC_EMAIL_DOMAINS:
            if domain == d or domain.endswith("." + d):
                return "academic"
    return "unknown"


# ── OpenReview (kept as data source option) ───────────────────

CONFERENCE_VENUES = {
    "ICLR": "ICLR.cc/{year}/Conference",
    "NeurIPS": "NeurIPS.cc/{year}/Conference",
    "ICML": "ICML.cc/{year}/Conference",
}

TIER_RANK = {"Oral": 0, "Spotlight": 1, "Poster": 2}

_last_request_time = 0.0

def _rate_limit(min_interval=1.0):
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_request_time = time.time()

_or_session = requests.Session()
_or_session.headers["User-Agent"] = "RecruitingAny/1.0 (academic-talent-discovery)"
API_BASE = "https://api2.openreview.net"


def fetch_papers_openreview(venue, year, tiers, status_cb=None):
    """Fetch papers from OpenReview. Returns list of standardized paper dicts."""
    def report(msg):
        if status_cb:
            status_cb(msg)

    venue_template = CONFERENCE_VENUES.get(venue)
    if not venue_template:
        report(f"Unknown venue: {venue}")
        return []

    TIER_VENUE_NAMES = {
        "Oral": ["Oral", "oral"],
        "Spotlight": ["Spotlight", "spotlight", "Spotlight Poster"],
        "Poster": ["Poster", "poster"],
    }

    papers = []
    for tier in tiers:
        tier_names = TIER_VENUE_NAMES.get(tier, [tier])
        for tier_name in tier_names:
            venue_str = f"{venue} {year} {tier_name}"
            report(f"Fetching {venue_str}...")

            offset = 0
            limit = 250
            tier_count = 0

            while True:
                _rate_limit(0.5)
                try:
                    resp = _or_session.get(
                        f"{API_BASE}/notes",
                        params={"content.venue": venue_str, "limit": limit, "offset": offset},
                        timeout=30,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as e:
                    report(f"API error fetching {venue_str}: {e}")
                    break

                notes = data.get("notes", [])
                if not notes:
                    break

                for note in notes:
                    content = note.get("content", {})
                    title = content.get("title", {}).get("value", "")
                    authors = content.get("authors", {}).get("value", [])
                    authorids = content.get("authorids", {}).get("value", [])

                    if not authors:
                        continue

                    papers.append({
                        "title": title,
                        "authors": authors,
                        "authorids": authorids,
                        "venue_tier": tier,
                        "org": "",
                        "rank": str(len(papers) + 1),
                    })

                tier_count += len(notes)
                offset += limit
                if len(notes) < limit:
                    break

            if tier_count > 0:
                report(f"  {tier}: {tier_count} papers")

    report(f"Total papers from OpenReview: {len(papers)}")

    # Deduplicate by first author, keep highest tier
    seen = {}
    for p in papers:
        key = p["authorids"][0] if p.get("authorids") else p["authors"][0]
        existing = seen.get(key)
        if not existing or TIER_RANK.get(p["venue_tier"], 9) < TIER_RANK.get(existing["venue_tier"], 9):
            seen[key] = p
    papers = list(seen.values())
    papers.sort(key=lambda p: TIER_RANK.get(p["venue_tier"], 9))
    report(f"After dedup: {len(papers)} unique first authors")
    return papers


def fetch_author_profile(author_id):
    """Fetch a single author's OpenReview profile."""
    if not author_id or not author_id.startswith("~"):
        return None

    max_retries = 3
    for attempt in range(max_retries):
        _rate_limit(1.0)
        try:
            resp = _or_session.get(
                f"{API_BASE}/profiles",
                params={"id": author_id},
                timeout=10,
            )
            if resp.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            if resp.status_code != 200:
                return None

            data = resp.json()
            profiles = data.get("profiles", [])
            if not profiles:
                return None

            content = profiles[0].get("content", {})
            history = content.get("history", [])
            position = ""
            institution = ""
            start_year = None
            end_year = None

            if history:
                h = history[0]
                position = h.get("position", "")
                inst = h.get("institution", {})
                institution = inst.get("name", "") if isinstance(inst, dict) else str(inst)
                start_year = h.get("start")
                end_year = h.get("end")

            return {
                "position": position,
                "institution": institution,
                "start_year": start_year,
                "end_year": end_year,
                "emails": content.get("emails", []),
                "homepage": content.get("homepage", ""),
                "gscholar": content.get("gscholar", ""),
                "history": [
                    {
                        "position": h.get("position", ""),
                        "institution": (h.get("institution", {}).get("name", "")
                                        if isinstance(h.get("institution"), dict)
                                        else str(h.get("institution", ""))),
                        "start": h.get("start"),
                        "end": h.get("end"),
                    }
                    for h in history
                ],
            }
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return None
    return None


# ── Paper Digest parser ───────────────────────────────────────

def parse_paperdigest_json(data):
    """Parse Paper Digest JSON (list of paper dicts).

    Accepts raw Paper Digest format with fields:
      rank, title, authors (semicolon-separated string), org, highlight, venue, year

    Returns standardized paper list.
    """
    papers = []
    for p in data:
        title = p.get("title", "").strip()
        if not title:
            continue
        # authors can be semicolon-separated string or list
        raw_authors = p.get("authors", "")
        if isinstance(raw_authors, str):
            authors = [a.strip().rstrip(";") for a in raw_authors.split(";") if a.strip()]
        else:
            authors = raw_authors

        papers.append({
            "title": title,
            "authors": authors,
            "authorids": [],
            "venue_tier": "",
            "org": p.get("org", ""),
            "rank": str(p.get("rank", "")),
            "url": p.get("title_link", "") or p.get("url", ""),
        })
    return papers


def parse_paperdigest_csv(text):
    """Parse CSV with columns: rank, title, authors, org (at minimum)."""
    import csv
    import io
    reader = csv.DictReader(io.StringIO(text))
    papers = []
    for row in reader:
        title = (row.get("title") or row.get("Title") or "").strip()
        if not title:
            continue
        raw_authors = row.get("authors") or row.get("Authors") or ""
        if isinstance(raw_authors, str):
            authors = [a.strip().rstrip(";") for a in raw_authors.split(";") if a.strip()]
        else:
            authors = [raw_authors]
        papers.append({
            "title": title,
            "authors": authors,
            "authorids": [],
            "venue_tier": "",
            "org": row.get("org") or row.get("Org") or "",
            "rank": row.get("rank") or row.get("Rank") or str(len(papers) + 1),
        })
    return papers


# ── Chinese author filter ─────────────────────────────────────

def filter_chinese_authors(papers):
    """Filter papers to those with at least one Chinese-surnamed author.

    Returns (chinese_papers, stats) where each paper gets a 'chinese_authors' field.
    """
    result = []
    for p in papers:
        authors = p.get("authors", [])
        chinese = [a for a in authors if has_chinese_surname(a)]
        if chinese:
            paper = dict(p)
            paper["chinese_authors"] = chinese
            result.append(paper)
    return result, {"total": len(papers), "chinese": len(result)}


# ── arXiv HTML parser ─────────────────────────────────────────

ARXIV_COOKIES = {
    'arxiv_labs': '{%22sameSite%22:%22strict%22%2C%22expires%22:365}',
    'browser': '24.114.39.223.1780529824799666',
}

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


def search_arxiv_id(title, session):
    """Search arXiv for a paper by title. Returns arxiv_id, 'RATE_LIMITED', or None."""
    clean = re.sub(r'[^\w\s]', ' ', title).strip()
    words = clean.split()[:8]
    query = '+'.join(urllib.parse.quote(w) for w in words if len(w) > 2)

    r = _safe_get(session,
                  f'https://arxiv.org/search/?query={query}&searchtype=all',
                  cookies=ARXIV_COOKIES, headers=BROWSER_HEADERS, timeout=(5, 15))
    if r is None:
        return None
    if r.status_code == 200:
        if len(r.text) < 1000:
            return None  # empty page = possible IP block
        ids = re.findall(r'arxiv\.org/abs/(\d+\.\d+)', r.text)
        if ids:
            for aid in ids[:3]:
                idx = r.text.find(aid)
                if idx >= 0:
                    snippet = r.text[idx:idx + 500].lower()
                    title_words = [w.lower() for w in title.split()[:5] if len(w) > 3]
                    matches = sum(1 for w in title_words if w in snippet)
                    if matches >= len(title_words) * 0.5:
                        return aid
            return ids[0]
    elif r.status_code == 429:
        return 'RATE_LIMITED'
    return None


def parse_arxiv_html(html):
    """Parse arXiv HTML paper page to extract author names, affiliations, emails."""
    results = []
    idx_start = html.find('class="ltx_authors"')
    if idx_start < 0:
        return results

    div_start = html.rfind('<div', 0, idx_start)
    idx_end = html.find('class="ltx_abstract"', idx_start)
    if idx_end < 0:
        idx_end = html.find('<section', idx_start)
    if idx_end < 0:
        idx_end = idx_start + 20000
    authors_html = html[div_start:idx_end]

    first_chunk = html[:50000]
    emails_found = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', first_chunk)
    emails_found = [e for e in emails_found if not e.endswith('.png') and not e.endswith('.jpg')]

    # Strategy 1: Per-author blocks
    blocks = list(re.finditer(
        r'<span[^>]*class="ltx_creator ltx_role_author"[^>]*>(.*?)</span>\s*(?=<span[^>]*class="ltx_creator|</div>)',
        authors_html, re.DOTALL))
    if blocks:
        for block in blocks:
            content = block.group(1)
            name_m = re.search(r'class="ltx_personname"[^>]*>(.*?)</span>', content, re.DOTALL)
            if not name_m:
                continue
            name = re.sub(r'<[^>]+>', '', name_m.group(1)).strip().rstrip('*\u2020\u2021\u2660\u25c7\u2663\u2666\u00a7\u00b6 ,')
            if len(name) < 2:
                continue
            affil_m = re.search(r'ltx_role_affiliation[^>]*>(.*?)</span>', content, re.DOTALL)
            affil = re.sub(r'<[^>]+>', '', affil_m.group(1)).strip() if affil_m else ''
            email = ''
            name_parts = name.lower().split()
            if name_parts:
                for e in emails_found:
                    e_lower = e.lower()
                    if name_parts[-1] in e_lower or name_parts[0] in e_lower:
                        email = e
                        break
            results.append({'name': name, 'institution': affil, 'email': email})
        if any(r['institution'] for r in results):
            return results

    # Strategy 2: Marker-based affiliations
    def math_to_text(s):
        return re.sub(
            r'<math[^>]*>.*?<annotation encoding="application/x-tex"[^>]*>(.*?)</annotation>.*?</math>',
            lambda m: m.group(1).replace('\\ddagger', '\u2021').replace('\\dagger', '\u2020')
                .replace('\\diamond', '\u22c4').replace('\\ast', '*').replace('~', '')
                .replace('{}^{', '').replace('{', '').replace('}', '').strip(),
            s, flags=re.DOTALL)

    processed = math_to_text(authors_html)
    processed = re.sub(r'<sup[^>]*>(.*?)</sup>',
                        lambda m: f'\u27e8{re.sub(r"<[^>]+>", "", m.group(1)).strip()}\u27e9',
                        processed, flags=re.DOTALL)
    processed = re.sub(r'<span class="ltx_note_outer">.*?</span></span>', '', processed, flags=re.DOTALL)
    processed = re.sub(r'<br[^>]*/?>','\n', processed)
    text = re.sub(r'<[^>]+>', '', processed)
    text = re.sub(r'[ \t]+', ' ', text)
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    affil_map = {}
    affil_lines = set()
    inst_keywords = [
        'university', 'institute', 'college', 'lab', 'research', 'microsoft', 'google',
        'meta', 'nvidia', 'amazon', 'deepmind', 'openai', 'adobe', 'tsinghua', 'peking',
        'stanford', 'mit', 'berkeley', 'carnegie', 'oxford', 'cambridge', 'huawei', 'baidu',
        'bytedance', 'tencent', 'alibaba', 'deepseek', 'school', 'department', 'center',
        'centre', 'academy', 'technology', 'sciences', 'politecnico', 'technische',
        'hong kong', 'singapore', 'eth', 'epfl', 'inria', 'cnrs', 'kaist',
    ]
    for i, line in enumerate(lines):
        matches = re.findall(r'\u27e8([^\u27e9]+)\u27e9\s*([A-Z][^\u27e8]*?)(?=\s*\u27e8|$)', line)
        found = False
        for sym, inst in matches:
            inst = inst.strip().rstrip(' ,;.')
            if len(inst) > 5 and any(kw in inst.lower() for kw in inst_keywords):
                affil_map[sym] = inst
                found = True
        if found:
            affil_lines.add(i)

    for i, line in enumerate(lines):
        if i in affil_lines:
            continue
        if any(skip in line.lower() for skip in [
            'equal contribution', 'corresponding', 'footnote', 'work done', 'https://', 'http://', '@'
        ]):
            continue
        parts = re.findall(
            r'([A-Z][a-zA-Z\'\-]+(?:\s+(?:de|van|von|der|del|la|el)\s+)?(?:\s+[A-Za-z\'\-]+)+)\s*((?:\u27e8[^\u27e9]*\u27e9\s*)*)',
            line)
        for name, markers in parts:
            name = name.strip().rstrip('*\u2020\u2021\u2660\u25c7\u2663\u2666\u00a7\u00b6 ,')
            if len(name) < 3 or name.lower() in ('related papers', 'equal contribution', 'corresponding author'):
                continue
            syms = re.findall(r'\u27e8([^\u27e9]+)\u27e9', markers)
            institution = ''
            for sym in syms:
                if sym in affil_map:
                    institution = affil_map[sym]
                    break
                for char in sym:
                    if char in affil_map:
                        institution = affil_map[char]
                        break
                if institution:
                    break
            email = ''
            name_parts = name.lower().split()
            if name_parts:
                for e in emails_found:
                    e_lower = e.lower()
                    if name_parts[-1] in e_lower or name_parts[0] in e_lower:
                        email = e
                        break
            results.append({'name': name, 'institution': institution, 'email': email})

    return results


# ── Core pipeline: arXiv enrich (streaming) ──────────────────

def enrich_papers_arxiv(papers, arxiv_cache=None, author_data=None, yield_cb=None):
    """Enrich papers with arXiv HTML data. Streams progress via yield_cb.

    Args:
        papers: list of paper dicts with chinese_authors field
        arxiv_cache: dict of title -> arxiv_id (mutable, updated in-place)
        author_data: dict of name_lower -> author info (mutable, updated in-place)
        yield_cb: callable(event_dict) for SSE streaming

    Returns (author_data, arxiv_cache, stats)
    """
    if arxiv_cache is None:
        arxiv_cache = {}
    if author_data is None:
        author_data = {}

    def emit(evt):
        if yield_cb:
            yield_cb(evt)

    session = _new_session()
    total = len(papers)
    parsed = 0
    no_arxiv = 0
    failed = 0
    rate_limited = 0
    consecutive_fail = 0

    for pi, paper in enumerate(papers):
        title = paper['title']

        # Register Chinese authors from paper metadata
        for idx, aname in enumerate(paper.get('chinese_authors', paper.get('authors', []))):
            if not has_chinese_surname(aname):
                continue
            key = aname.lower().strip()
            if key not in author_data:
                author_data[key] = {
                    'name': aname,
                    'institution': paper.get('org', '') if idx == 0 else '',
                    'email': '',
                    'papers': [],
                    'source': 'paperdigest' if idx == 0 and paper.get('org') else 'none',
                }
            existing_titles = {pp.get('title', '') for pp in author_data[key]['papers']}
            if title not in existing_titles:
                author_data[key]['papers'].append({
                    'title': title,
                    'rank': paper.get('rank', ''),
                    'first_author': idx == 0,
                })

        # Session recycling
        if pi > 0 and pi % 80 == 0:
            try:
                session.close()
            except Exception:
                pass
            session = _new_session()

        # Search arXiv ID
        arxiv_id = arxiv_cache.get(title)
        if not arxiv_id:
            arxiv_id = search_arxiv_id(title, session)
            if arxiv_id and arxiv_id != 'RATE_LIMITED':
                arxiv_cache[title] = arxiv_id
            elif arxiv_id == 'RATE_LIMITED':
                rate_limited += 1
                consecutive_fail += 1
                if consecutive_fail >= 3:
                    emit({'type': 'log', 'message': 'arXiv rate limited, stopping', 'level': 'error'})
                    break
                time.sleep(10)
                continue
            else:
                no_arxiv += 1
                time.sleep(2)

        # Fetch HTML
        if arxiv_id and arxiv_id != 'RATE_LIMITED':
            r = _safe_get(session, f'https://arxiv.org/html/{arxiv_id}',
                          cookies=ARXIV_COOKIES, headers=BROWSER_HEADERS, timeout=(5, 20))

            if r is None:
                consecutive_fail += 1
                failed += 1
                if consecutive_fail >= 5:
                    emit({'type': 'log', 'message': f'{consecutive_fail} consecutive timeouts, stopping', 'level': 'error'})
                    break
                time.sleep(5)
                continue

            if r.status_code == 200 and len(r.text) > 1000:
                consecutive_fail = 0
                html_authors = parse_arxiv_html(r.text)
                if html_authors:
                    _merge_html_authors(html_authors, author_data)
                    parsed += 1
                else:
                    no_arxiv += 1
            elif r.status_code == 429:
                rate_limited += 1
                consecutive_fail += 1
                if consecutive_fail >= 3:
                    emit({'type': 'log', 'message': 'arXiv rate limited, stopping', 'level': 'error'})
                    break
                time.sleep(15)
                continue
            elif r.status_code == 404:
                no_arxiv += 1
                consecutive_fail = 0
            else:
                failed += 1
                consecutive_fail = 0

            time.sleep(4)  # gentle pace

        # Progress reporting
        if (pi + 1) % 10 == 0 or pi == total - 1:
            emit({
                'type': 'progress',
                'current': pi + 1,
                'total': total,
                'message': f'[{pi + 1}/{total}] parsed: {parsed}, no_arxiv: {no_arxiv}',
            })

    stats = {
        'total': total,
        'parsed': parsed,
        'no_arxiv': no_arxiv,
        'failed': failed,
        'rate_limited': rate_limited,
    }
    return author_data, arxiv_cache, stats


def _merge_html_authors(html_authors, author_data):
    """Merge arXiv HTML author data into the author_data dict."""
    for ad in html_authors:
        ad_name = ad['name'].lower().strip()
        ad_inst = ad.get('institution', '')
        ad_email = ad.get('email', '')

        if ad_name in author_data:
            if ad_inst and not author_data[ad_name]['institution']:
                author_data[ad_name]['institution'] = ad_inst
                author_data[ad_name]['source'] = 'arxiv_html'
            if ad_email and not author_data[ad_name]['email']:
                author_data[ad_name]['email'] = ad_email
            continue

        # Fuzzy match: same last name + same first initial
        ad_parts = ad['name'].split()
        if len(ad_parts) >= 2:
            ad_last = ad_parts[-1].lower()
            ad_first = ad_parts[0][0].lower()
            for key, auth in author_data.items():
                kparts = auth['name'].split()
                if len(kparts) >= 2:
                    klast = kparts[-1].lower().rstrip(',;.')
                    kfirst = kparts[0][0].lower()
                    if klast == ad_last and kfirst == ad_first:
                        if ad_inst and not auth['institution']:
                            auth['institution'] = ad_inst
                            auth['source'] = 'arxiv_html'
                        if ad_email and not auth['email']:
                            auth['email'] = ad_email
                        break


# ── Build classified results ──────────────────────────────────

def build_results(author_data):
    """Classify all authors and return structured results.

    Returns dict with:
      industry: list of member dicts
      academic: list of member dicts
      unknown: list of member dicts
      stats: {industry_count, academic_count, unknown_count, with_email, with_institution}
    """
    industry = []
    academic = []
    unknown = []

    for key, auth in author_data.items():
        cat = classify_author(auth.get('institution', ''), auth.get('email', ''))
        papers = auth.get('papers', [])
        member = {
            'name': auth['name'],
            'institution': auth.get('institution', ''),
            'email': auth.get('email', ''),
            'classification': cat,
            'paper_count': len(papers),
            'papers': papers,
            'source': auth.get('source', ''),
            'role': '',
            'research_area': papers[0]['title'] if papers else '',
            'personal_page': '',
        }
        if cat == 'industry':
            industry.append(member)
        elif cat == 'academic':
            academic.append(member)
        else:
            unknown.append(member)

    # Sort by paper count desc
    for lst in [industry, academic, unknown]:
        lst.sort(key=lambda m: m['paper_count'], reverse=True)

    stats = {
        'industry_count': len(industry),
        'academic_count': len(academic),
        'unknown_count': len(unknown),
        'with_email': sum(1 for a in author_data.values() if a.get('email')),
        'with_institution': sum(1 for a in author_data.values() if a.get('institution')),
    }
    return {'industry': industry, 'academic': academic, 'unknown': unknown, 'stats': stats}


# ── Role normalization (for OpenReview profiles) ──────────────

def _normalize_role(position_str):
    if not position_str:
        return "Other"
    p = position_str.lower()
    if "phd" in p or "doctoral" in p:
        return "PhD Student"
    if "postdoc" in p or "post-doc" in p:
        return "Postdoc"
    if "master" in p or "ms " in p or "m.s." in p:
        return "Master Student"
    if "professor" in p or "faculty" in p or "lecturer" in p:
        return "Professor"
    if "undergrad" in p:
        return "Undergraduate"
    if "visiting" in p:
        return "Visiting Scholar"
    if "engineer" in p:
        return "Research Engineer"
    if "research" in p or "scientist" in p:
        return "Research Scientist"
    if "intern" in p:
        return "Research Scientist"
    return "Other"


def map_to_member(paper, profile):
    """Transform a paper + author profile into discoveredMembers format (OpenReview path)."""
    name = paper["authors"][0] if paper.get("authors") else "Unknown"
    tier = paper.get("venue_tier", "")
    title = paper.get("title", "")

    if profile:
        role = _normalize_role(profile.get("position", ""))
        institution = profile.get("institution", "")
        emails = profile.get("emails", [])
        homepage = profile.get("homepage", "")
        gscholar = profile.get("gscholar", "")
        start_year = profile.get("start_year")
        end_year = profile.get("end_year")

        expected_graduation = None
        if end_year:
            expected_graduation = end_year
        elif start_year:
            if role == "PhD Student":
                expected_graduation = start_year + 5
            elif role == "Master Student":
                expected_graduation = start_year + 2

        industry_exp = []
        for h in profile.get("history", [])[1:]:
            inst_lower = h.get("institution", "").lower()
            if any(k in inst_lower for k in INDUSTRY_KEYWORDS[:15]):
                industry_exp.append(f"{h.get('position', '')} @ {h.get('institution', '')}")

        member = {
            "name": name,
            "role": role,
            "email": emails[0] if emails else "",
            "research_area": title,
            "personal_page": homepage or gscholar or "",
            "expected_graduation": expected_graduation,
            "institution": institution,
            "advisor": "",
            "source": f"{tier}: {title[:60]}",
            "paper_title": title,
            "paper_venue": tier,
            "industry_experience": industry_exp,
        }
    else:
        member = {
            "name": name,
            "role": "Other",
            "email": "",
            "research_area": title,
            "personal_page": "",
            "expected_graduation": None,
            "institution": "",
            "advisor": "",
            "source": f"{tier}: {title[:60]}",
            "paper_title": title,
            "paper_venue": tier,
            "industry_experience": [],
        }
    return member
