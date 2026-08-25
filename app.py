import json
import os
import time
import requests as _req
from dotenv import load_dotenv

load_dotenv()
from flask import Flask, render_template, request, Response, stream_with_context, jsonify, redirect
import anthropic
from openai import OpenAI
from duckduckgo_search import DDGS
from db import (init_db, get_sender_config, save_sender_config, add_history, get_history, delete_history, clear_history,
                search_people, search_people_boolean, search_people_fts, get_person, count_people, upsert_person,
                add_experiences, add_educations, get_conn, rebuild_fts, update_fts_for_person,
                search_academic, get_academic_filters, get_venue_counts,
                add_person_tag, get_person_tags, get_all_tags, search_by_tag, remove_person_tag,
                cache_get, cache_set)
from ai.embedder import semantic_search
from csrankings import get_institutions, get_ai_faculty, get_all_faculty, AREA_LABELS
from scraper import scrape_lab, scrape_department_page
from agent_scraper import run_agent_search
from fast_scraper import fast_scrape_lab
from enrich_academic import enrich_from_personal_pages
from conference_scraper import (
    fetch_papers_openreview, fetch_author_profile, map_to_member, CONFERENCE_VENUES,
    parse_paperdigest_json, parse_paperdigest_csv, filter_chinese_authors,
    enrich_papers_arxiv, build_results, classify_author,
)

app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
    """Allow Chrome extension on linkedin.com to call our local API."""
    origin = request.headers.get("Origin", "")
    if origin and ("linkedin.com" in origin or "127.0.0.1" in origin or "localhost" in origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Api-Key"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


init_db()
rebuild_fts()

# ── Paper lookup: Semantic Scholar + arXiv fallback ──

S2_API = "https://api.semanticscholar.org/graph/v1"
ARXIV_API = "http://export.arxiv.org/api/query"


def search_paper(title: str) -> dict | None:
    """Search paper by title. Try Semantic Scholar first, fall back to arXiv."""
    result = _search_s2(title)
    if result:
        return result
    return _search_arxiv(title)


def _search_s2(title: str) -> dict | None:
    """Search Semantic Scholar by paper title."""
    for attempt in range(3):
        try:
            resp = _req.get(f"{S2_API}/paper/search", params={
                "query": title,
                "limit": 1,
                "fields": "title,abstract,venue,year,authors,authors.name,authors.affiliations,externalIds",
            }, timeout=15)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                if data:
                    return data[0]
                return None
            if resp.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            return None
        except Exception:
            if attempt < 2:
                time.sleep(1)
            continue
    return None


def _search_arxiv(title: str) -> dict | None:
    """Fallback: search arXiv API by exact title phrase."""
    import xml.etree.ElementTree as ET
    try:
        # Use quoted title for exact phrase match
        query = f'ti:"{title}"'
        resp = _req.get(ARXIV_API, params={
            "search_query": query,
            "max_results": 1,
            "sortBy": "relevance",
        }, timeout=15)
        if resp.status_code != 200:
            return None

        ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        root = ET.fromstring(resp.text)
        entry = root.find("atom:entry", ns)
        if entry is None:
            return None

        found_title = (entry.findtext("atom:title", "", ns) or "").strip().replace("\n", " ")
        # Verify it's actually a match (not a random result)
        if not _titles_match(title, found_title):
            return None

        abstract = (entry.findtext("atom:summary", "", ns) or "").strip().replace("\n", " ")

        authors = []
        for author_el in entry.findall("atom:author", ns):
            name = (author_el.findtext("atom:name", "", ns) or "").strip()
            affiliation = ""
            aff_el = author_el.find("arxiv:affiliation", ns)
            if aff_el is not None and aff_el.text:
                affiliation = aff_el.text.strip()
            if name:
                authors.append({"name": name, "affiliations": [affiliation] if affiliation else []})

        # Extract arxiv category as venue hint
        categories = entry.findall("atom:category", ns)
        venue_parts = [c.get("term", "") for c in categories[:2]]

        return {
            "title": found_title,
            "abstract": abstract,
            "venue": " / ".join(venue_parts) if venue_parts else "",
            "year": (entry.findtext("atom:published", "", ns) or "")[:4] or None,
            "authors": authors,
        }
    except Exception:
        return None


def _titles_match(query: str, found: str) -> bool:
    """Check if two titles are close enough (case-insensitive, ignore punctuation)."""
    import re
    def normalize(s):
        return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()
    q = normalize(query)
    f = normalize(found)
    # Check if one contains the other or they share >80% words
    q_words = set(q.split())
    f_words = set(f.split())
    if not q_words:
        return False
    overlap = len(q_words & f_words) / max(len(q_words), len(f_words))
    return overlap > 0.7


def get_paper_authors(paper: dict) -> list[dict]:
    """Extract all authors from a paper result."""
    authors = []
    for a in paper.get("authors", []):
        name = a.get("name", "").strip()
        if not name:
            continue
        affiliations = a.get("affiliations") or []
        authors.append({
            "name": name,
            "affiliation": affiliations[0] if affiliations else "",
        })
    return authors


def _clean_llm_output(text: str) -> str:
    """Clean up LLM output: take only the first complete message if model outputted duplicates."""
    text = text.strip()
    # Remove leading "好的，消息如下：" if model echoed the prefill
    for prefix in ("好的，消息如下：", "好的,消息如下:", "好的，消息如下:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    # If model outputted multiple versions separated by double newlines starting with 您好/你好/Hi,
    # keep only the last complete version (usually the refined one)
    import re
    versions = re.split(r'\n{2,}(?=(?:您好|你好|Hi[,，]|Hello))', text)
    if len(versions) > 1:
        text = versions[-1].strip()
    # Remove trailing meta-commentary (字数统计, 计算字数, etc.)
    text = re.split(r'\n+(?:字数|计算|长度|注[：:]|说明|备注)', text)[0].strip()
    return text


# ── System prompt templates ──

SYSTEM_PROMPT_ZH = """你帮{sender_name}写 LinkedIn 私信，用于人才招聘或技术交流邀请。

发件人信息：
- {sender_name}，{sender_team}，负责高端人才招聘
- 联系方式：{sender_contact}

风格要求——模仿以下真实话术的语气和结构：
示例1: "Hi，我是{sender_team}负责人才招聘的 {sender_name}，关注到今年ICLR收录了您的论文，祝贺祝贺！我们多个部门和实验室对您研究非常感兴趣。他们这周也会参加ICLR会议并举办活动晚宴，希望可以有机会约您当面/线上交流。"
示例2: "您好，我是{sender_team}的{sender_name}。我们正寻找像您一样的技术领军专家，负责引领相关项目的研发、优化、落地、探索等。您的研究背景和丰富经验，正是我们的理想人选。"
示例3: "您好，我是{sender_team}的{sender_name}，我们团队关注到您在ICML 2025上的作品，备受启发，希望现场与您交流。"

核心原则：
1. 简短自然，像真人发消息，3-6句话，不要写成模板
2. 开头自我介绍 + 具体触发点（对方的论文/项目/会议/技术贡献，越具体越好）
3. 核心诉求：约当面交流或线上沟通，不是直接推销岗位
4. 如有相关会议，提及"我们也会参加XX会议，希望当面交流"
5. 结尾简洁："期待您的回复。" 或 "祝好，谢谢！"
6. 署名：{sender_name} {sender_team}
7. 可附微信：{sender_contact}
8. 长度 80-150 字（不含署名），宁短勿长
9. 只输出消息正文，不加任何解释或思考过程"""

SYSTEM_PROMPT_EN = """You help {sender_name} write LinkedIn DMs for talent recruiting and technical exchange invitations.

Sender info:
- {sender_name}, {sender_team}, responsible for senior talent acquisition
- Contact: {sender_contact}

Style — mimic this natural, human tone:
Example: "Hi [Name], I'm {sender_name} from {sender_team}. We noticed your work on [specific project/paper] — really impressive. Our team is looking for leading experts like you to drive R&D in [area]. We'll be at [conference] — would love to meet in person or chat online. Looking forward to hearing from you!"

Core principles:
1. Short and natural, like a real person messaging — 3-6 sentences, NOT a template
2. Open with self-intro + specific trigger (their paper/project/conference/contribution — the more specific the better)
3. Core ask: meet in person or chat, NOT a hard sell on a job
4. If a relevant conference is coming up, mention "we'll also be at XX, hope to meet there"
5. End simply: "Looking forward to your reply!" or "Best regards,"
6. Sign off: {sender_name}, {sender_team}
7. Optional WeChat: {sender_contact}
8. Length: 60-100 words (excluding signature), shorter is better
9. Output only the message body, no explanations"""

# ── Academic mode system prompts ──

ACADEMIC_PROMPT_ZH = """你帮{sender_name}写 LinkedIn 私信，用于学术合作交流邀请（不是招聘）。

发件人信息：
- {sender_name}，{sender_team}，负责学术合作与技术交流
- 联系方式：{sender_contact}

风格要求——模仿以下真实话术：
示例1: "Hi，我是{sender_team}负责学术合作与技术交流的 {sender_name}，关注到今年ICCV收录了您的论文，祝贺祝贺！我们多个部门和实验室对您研究非常感兴趣。届时我们也会去参加会议，如果您也参会，希望可以有机会约您当面/线上交流。"
示例2: "您好，我是{sender_team}的{sender_name}，我们团队关注到您在ICML 2025上的作品，备受启发，希望现场与您交流。您也可以通过微信与我保持联系，我的微信号是{sender_contact}。"

核心原则：
1. 语气是学术同行交流，不是HR招聘，"祝贺祝贺"比"诚邀您"自然
2. 具体提及对方的论文标题、发表会议、研究方向
3. 表达"我们团队/多个部门对您的研究感兴趣"
4. 如有相关会议，提及线下交流机会
5. 结尾简洁自然
6. 署名：{sender_name}, {sender_team}
7. 长度 80-150 字（不含署名），宁短勿长
8. 只输出消息正文，不加任何解释或思考过程"""

ACADEMIC_PROMPT_EN = """You help {sender_name} write LinkedIn DMs for academic collaboration outreach (NOT recruiting).

Sender info:
- {sender_name}, {sender_team}, responsible for academic collaboration & technical exchange
- Contact: {sender_contact}

Style — natural and collegial, like a peer reaching out:
Example: "Hi [Name], I'm {sender_name} from {sender_team}. Congrats on your [paper title] being accepted at [conference]! Several teams at our lab are really interested in your research on [topic]. We'll also be attending the conference — would love to meet in person and discuss potential collaboration. Looking forward to connecting!"

Core principles:
1. Collegial tone — peer-to-peer, NOT HR. "Congrats!" feels natural, "We cordially invite..." does not
2. Reference the specific paper, conference, and research direction
3. Express genuine interest from "our team / multiple departments"
4. If a relevant conference is upcoming, mention meeting there
5. End naturally
6. Sign off: {sender_name}, {sender_team}
7. Length: 60-100 words (excluding signature), shorter is better
8. Output only the message body, no explanations"""


CUSTOM_PROMPT_ZH = """你帮{sender_name}写 LinkedIn 私信，用于人才招聘或技术交流邀请。

发件人信息：
- {sender_name}，{sender_team}
- 联系方式：{sender_contact}

风格要求——模仿以下真实话术的自然语气：
示例1: "您好，我是{sender_team}的{sender_name}。我们正寻找像您一样的技术领军专家，负责引领相关项目的研发、优化、落地、探索等。您的研究背景和丰富经验，正是我们的理想人选。"
示例2: "您好，我是{sender_team}的{sender_name}，happy to connect. 我们团队关注到您在该领域的深厚积累，备受启发，希望能与您交流合作机会。"
示例3: "Hi，我是{sender_team}负责人才招聘的{sender_name}，注意到您的技术背景和丰富经验，想和您聊聊可能的合作机会。"

核心原则：
1. 简短自然，像真人发消息，3-6句话
2. 根据对方的背景信息（职位、公司、技术方向）个性化内容
3. 核心诉求：建立联系、约交流，不是直接推销岗位
4. 可以提及工作地点灵活、薪资竞争力等吸引点
5. 结尾简洁自然
6. 署名：{sender_name}, {sender_team}
7. 可附微信：{sender_contact}
8. 长度 80-150 字（不含署名），宁短勿长
9. 只输出消息正文，不加任何解释或思考过程"""

CUSTOM_PROMPT_EN = """You help {sender_name} write LinkedIn DMs for talent recruiting or technical exchange.

Sender info:
- {sender_name}, {sender_team}
- Contact: {sender_contact}

Style — natural and direct, like a real person messaging:
Example: "Hi [Name], I'm {sender_name} from {sender_team}. Your background in [their area] really caught our attention. We're looking for leading experts like you to drive R&D in related projects. Would love to connect and discuss. Looking forward to hearing from you!"

Core principles:
1. Short and natural, 3-6 sentences, NOT a template
2. Personalize based on their background (title, company, tech direction)
3. Core ask: establish connection, chat about opportunities — NOT a hard sell
4. Can mention flexible location and competitive compensation as hooks
5. End naturally
6. Sign off: {sender_name}, {sender_team}
7. Optional WeChat: {sender_contact}
8. Length: 60-100 words (excluding signature), shorter is better
9. Output only the message body, no explanations"""


def build_system_prompt(config: dict) -> str:
    mode     = config.get("mode", "community")
    language = config.get("language", "zh")
    sender   = get_sender_config()

    sender_name    = sender["name"] or "Recruiter"
    sender_team    = sender["team"] or "Talent Team"
    sender_contact = sender["contact"] or ""

    if mode == "academic":
        template = ACADEMIC_PROMPT_ZH if language == "zh" else ACADEMIC_PROMPT_EN
    elif mode == "custom":
        template = CUSTOM_PROMPT_ZH if language == "zh" else CUSTOM_PROMPT_EN
    else:
        template = SYSTEM_PROMPT_ZH if language == "zh" else SYSTEM_PROMPT_EN

    prompt = template.format(
        sender_name=sender_name,
        sender_team=sender_team,
        sender_contact=sender_contact,
    )

    community   = config.get("community", "").strip()
    conference  = config.get("conference", "").strip()
    focus_areas = config.get("focus_areas", "").strip()
    custom_note = config.get("custom_note", "").strip()

    extra = []
    if language == "zh":
        if community:   extra.append(f"目标社区：{community}")
        if conference:  extra.append(f"目标会议：{conference}")
        if focus_areas: extra.append(f"重点技术方向：{focus_areas}")
        if custom_note: extra.append(f"额外话术要求：{custom_note}")
        if extra:
            prompt += "\n\n【本次配置】\n" + "\n".join(extra)
    else:
        if community:   extra.append(f"Target community: {community}")
        if conference:  extra.append(f"Target conference: {conference}")
        if focus_areas: extra.append(f"Focus areas: {focus_areas}")
        if custom_note: extra.append(f"Additional requirements: {custom_note}")
        if extra:
            prompt += "\n\n[Session config]\n" + "\n".join(extra)

    return prompt


GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")


def _gh_headers():
    h = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def _gh_get(url, params=None, timeout=10):
    return _req.get(url, params=params, headers=_gh_headers(), timeout=timeout)


def search_github(name: str, community: str, github_login: str = "") -> str:
    """Search GitHub for a user with signal-based repo ranking.

    Returns structured context with confidence level:
    - HIGH: core active projects with commit details
    - MEDIUM: some activity but limited
    - LOW: mostly forks/stale repos — only mention tech direction, no project names
    """
    try:
        if github_login:
            login = github_login
        else:
            q = f"{name} {community}" if community else name
            resp = _gh_get("https://api.github.com/search/users",
                           params={"q": q, "per_page": 1})
            if resp.status_code != 200 or not resp.json().get("items"):
                return ""
            login = resp.json()["items"][0]["login"]

        # 1. User profile
        profile = _gh_get(f"https://api.github.com/users/{login}").json()
        bio = profile.get("bio") or ""
        company = profile.get("company") or ""
        followers = profile.get("followers", 0)

        # 2. Recent events (last 90 days) — best signal for real activity
        events_resp = _gh_get(f"https://api.github.com/users/{login}/events",
                              params={"per_page": 100})
        active_repos = {}  # repo_full_name -> {pushes, prs, last_active}
        if events_resp.status_code == 200:
            for ev in events_resp.json():
                repo_name = ev.get("repo", {}).get("name", "")
                if not repo_name:
                    continue
                ev_type = ev.get("type", "")
                created = ev.get("created_at", "")[:10]
                if repo_name not in active_repos:
                    active_repos[repo_name] = {"pushes": 0, "prs": 0, "last_active": created}
                if ev_type == "PushEvent":
                    commits_count = len(ev.get("payload", {}).get("commits", []))
                    active_repos[repo_name]["pushes"] += max(commits_count, 1)  # At least 1 per push event
                elif ev_type in ("PullRequestEvent",):
                    active_repos[repo_name]["prs"] += 1
                if created > active_repos[repo_name]["last_active"]:
                    active_repos[repo_name]["last_active"] = created

        # 3. Repos sorted by last push — filter to recent 2 years
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=730)).isoformat()[:10]

        repos_resp = _gh_get(f"https://api.github.com/users/{login}/repos",
                             params={"sort": "pushed", "direction": "desc", "per_page": 30})
        repos_data = repos_resp.json() if repos_resp.status_code == 200 else []

        # Classify repos into tiers
        core_projects = []      # S: own repo + recent commits
        contributions = []      # A: active in others' repos (from events)
        historical = []         # C: own repo but stale
        languages = set()

        for r in repos_data:
            repo_name = r.get("full_name", "")
            is_fork = r.get("fork", False)
            pushed_at = (r.get("pushed_at") or "")[:10]
            stars = r.get("stargazers_count", 0)
            lang = r.get("language") or ""
            desc = (r.get("description") or "")[:100]

            if lang:
                languages.add(lang)

            # Skip forks with no activity from this user
            if is_fork:
                event_data = active_repos.get(repo_name, {})
                if event_data.get("pushes", 0) == 0 and event_data.get("prs", 0) == 0:
                    continue  # D tier: fork with zero commits — ignore

            event_data = active_repos.get(repo_name, {})
            recent_commits = event_data.get("pushes", 0)

            entry = {
                "name": r.get("name", ""),
                "full_name": repo_name,
                "lang": lang,
                "stars": stars,
                "desc": desc,
                "pushed_at": pushed_at,
                "recent_commits": recent_commits,
                "is_fork": is_fork,
            }

            if not is_fork and pushed_at >= cutoff:
                if recent_commits > 0:
                    core_projects.append(entry)  # S tier
                else:
                    core_projects.append(entry)  # Own + recent push but no events (events only cover 90 days)
            elif not is_fork:
                historical.append(entry)  # C tier
            elif is_fork and recent_commits > 0:
                contributions.append(entry)  # B tier (active fork)

        # Check events for contributions to repos NOT in their own repo list (A tier)
        own_repos = {r.get("full_name", "") for r in repos_data}
        for repo_name, data in active_repos.items():
            if repo_name not in own_repos and data["pushes"] + data["prs"] > 0:
                contributions.append({
                    "name": repo_name.split("/")[-1],
                    "full_name": repo_name,
                    "recent_commits": data["pushes"],
                    "prs": data["prs"],
                    "last_active": data["last_active"],
                })

        # Sort by activity
        core_projects.sort(key=lambda x: (x.get("recent_commits", 0), x.get("stars", 0)), reverse=True)
        contributions.sort(key=lambda x: x.get("recent_commits", 0) + x.get("prs", 0), reverse=True)

        # Determine confidence level based on real activity signals
        total_recent_commits = sum(p.get("recent_commits", 0) for p in core_projects[:5])
        total_contributions = sum(c.get("recent_commits", 0) + c.get("prs", 0) for c in contributions[:5])
        has_starred_projects = any(p.get("stars", 0) >= 10 for p in core_projects)

        if (total_recent_commits >= 10 and len(core_projects) >= 2) or \
           (total_recent_commits >= 5 and has_starred_projects):
            confidence = "HIGH"
        elif total_recent_commits >= 3 or total_contributions >= 2 or \
             (core_projects and has_starred_projects):
            confidence = "MEDIUM"
        elif core_projects or contributions or historical:
            confidence = "LOW"
        else:
            confidence = "NONE"

        # Build structured output for LLM
        parts = []
        parts.append(f"GitHub: {login} | Followers: {followers}")
        if bio:
            parts.append(f"Bio: {bio}")
        if company:
            parts.append(f"Company: {company}")
        parts.append(f"Signal confidence: {confidence}")

        if confidence == "HIGH":
            parts.append("\n=== Core Active Projects (mention these specifically) ===")
            for p in core_projects[:4]:
                commits_info = f", {p['recent_commits']} recent commits" if p.get("recent_commits") else ""
                parts.append(f"  - {p['name']} ({p.get('lang', '')}, {p.get('stars', 0)} stars{commits_info}): {p.get('desc', '')}")
            if contributions[:2]:
                parts.append("\n=== Community Contributions (shows collaboration) ===")
                for c in contributions[:2]:
                    parts.append(f"  - {c['full_name']} ({c.get('recent_commits', 0)} commits, {c.get('prs', 0)} PRs)")

        elif confidence == "MEDIUM":
            parts.append("\n=== Active Projects (can mention by name but don't go deep) ===")
            for p in (core_projects + contributions)[:3]:
                parts.append(f"  - {p.get('name', '')} ({p.get('lang', '')}): {p.get('desc', '')}")

        elif confidence == "LOW":
            # Only provide tech direction, no project names
            if languages:
                parts.append(f"\nTech direction (DO NOT mention specific project names): {', '.join(sorted(languages))}")
            if historical[:1]:
                parts.append(f"Has some background in: {historical[0].get('desc', '') or historical[0].get('lang', '')}")
            parts.append("NOTE: Low signal — write a general message about their tech direction, do NOT name any repos.")

        else:
            parts.append("NOTE: No meaningful GitHub activity found. Do NOT reference GitHub in the message.")

        return "\n".join(parts)

    except Exception:
        return ""


def search_person(name: str, url: str, community: str, github_login: str = "") -> str:
    """Combine GitHub API + DuckDuckGo search for background info."""
    parts = []

    # GitHub API search
    gh_info = search_github(name, community, github_login)
    if gh_info:
        parts.append(gh_info[:800])

    # DuckDuckGo search
    community_kw = community if community else "AI open source"
    queries = [
        f"{name} {community_kw} GitHub contributions",
        f"{name} AI engineer researcher",
    ]
    snippets = []
    try:
        with DDGS() as ddgs:
            for q in queries:
                for r in ddgs.text(q, max_results=3):
                    snippets.append(r.get("body", ""))
                if len(snippets) >= 5:
                    break
    except Exception:
        pass
    if snippets:
        parts.append("\n".join(snippets[:5]))

    result = "\n\n".join(parts)
    # 限制总长度，避免超出本地模型上下文窗口
    return result[:2000] if len(result) > 2000 else result


def generate_academic_message(name: str, affiliation: str, paper_title: str,
                              abstract: str, venue: str,
                              api_key: str = "", config: dict = None) -> str:
    """Generate a collaboration message for an academic author."""
    config = config or {}

    provider, api_key, model_name, base_url = _resolve_llm_config(api_key, config)

    if not api_key:
        return "⚠️ 未设置 API Key，请在页面顶部输入。"

    system_prompt = build_system_prompt(config)
    user_prompt = f"""请为以下研究者生成一条个性化 LinkedIn 合作交流消息：

姓名：{name}
机构：{affiliation or "未知"}
论文标题：{paper_title}
发表会议：{venue or "顶级学术会议"}
论文摘要：{abstract or "（无摘要）"}"""

    try:
        if provider == "anthropic":
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model=model_name,
                max_tokens=600,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return msg.content[0].text.strip()
        else:
            kwargs = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            client = OpenAI(**kwargs)
            resp = client.chat.completions.create(
                model=model_name,
                max_tokens=600,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                    {"role": "assistant", "content": "好的，消息如下：\n\n"},
                ],
            )
            msg = resp.choices[0].message
            text = (msg.content or "").strip()
            if not text:
                text = getattr(msg, "reasoning_content", "") or ""
                if not text and hasattr(msg, "model_extra") and msg.model_extra:
                    text = msg.model_extra.get("reasoning_content", "") or ""
                text = text.strip()
            return _clean_llm_output(text)
    except Exception as e:
        return f"⚠️ 生成失败：{e}"


def _resolve_llm_config(api_key: str = "", config: dict = None) -> tuple[str, str, str, str]:
    """决定用哪个 LLM 后端。返回 (provider, api_key, model_name, base_url)。

    优先级：前端传入 > .env 配置 > 本地默认。
    """
    config = config or {}
    provider = config.get("provider", "").strip()
    model_name = config.get("model_name", "").strip()
    base_url = config.get("base_url", "").strip()

    ai_mode = os.environ.get("AI_MODE", "anthropic")

    # 如果前端没指定 provider，根据 .env 决定
    if not provider:
        provider = "openai" if ai_mode == "openai_compat" else "anthropic"

    # API key 回退
    if not api_key:
        if provider == "anthropic":
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        else:
            api_key = os.environ.get("OPENAI_API_KEY", "")

    # base_url 回退
    if not base_url and provider != "anthropic":
        base_url = os.environ.get("OPENAI_BASE_URL", "")

    # model 回退
    if not model_name:
        model_name = os.environ.get("DEFAULT_MODEL", "")
        if not model_name:
            model_name = "claude-opus-4-6" if provider == "anthropic" else "glm-4"

    return provider, api_key, model_name, base_url


def generate_message(name: str, url: str, context: str,
                     api_key: str = "", config: dict = None) -> str:
    config = config or {}
    community = config.get("community", "开源社区")

    provider, api_key, model_name, base_url = _resolve_llm_config(api_key, config)

    if not api_key:
        return "⚠️ 未设置 API Key，请在页面顶部输入。"

    system_prompt = build_system_prompt(config)
    language = config.get("language", "zh")
    if language == "zh":
        user_prompt = f"""请为以下人员生成一条个性化 LinkedIn 招聘消息（必须使用中文撰写）：

姓名：{name}
LinkedIn：{url}
背景信息：
{context if context else f"（未找到详细信息，根据 {community} 社区贡献者身份撰写）"}

关于 GitHub 信息的使用规则：
- 如果标注 "Signal confidence: HIGH"：大胆提及具体项目名和技术细节
- 如果标注 "Signal confidence: MEDIUM"：可以提项目名，但不要深入技术细节
- 如果标注 "Signal confidence: LOW"：只提技术方向（如"分布式系统"），不要提任何项目名
- 如果标注 "Signal confidence: NONE" 或无 GitHub 信息：完全不提 GitHub
注意：请用中文撰写消息。"""
    else:
        user_prompt = f"""Generate a personalized LinkedIn recruiting message for:

Name: {name}
LinkedIn: {url}
Background:
{context if context else f"(No details found, write based on their role as a {community} community contributor)"}

Rules for using GitHub information:
- If "Signal confidence: HIGH": Boldly reference specific project names and technical details
- If "Signal confidence: MEDIUM": Mention project names but keep it brief, no deep technical details
- If "Signal confidence: LOW": Only mention tech direction (e.g. "distributed systems"), do NOT name any repos
- If "Signal confidence: NONE" or no GitHub info: Do NOT reference GitHub at all"""

    try:
        if provider == "anthropic":
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model=model_name,
                max_tokens=600,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return msg.content[0].text.strip()

        else:
            kwargs = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            client = OpenAI(**kwargs)
            resp = client.chat.completions.create(
                model=model_name,
                max_tokens=600,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                    {"role": "assistant", "content": "好的，消息如下：\n\n"},
                ],
            )
            msg = resp.choices[0].message
            text = (msg.content or "").strip()
            if not text:
                text = getattr(msg, "reasoning_content", "") or ""
                if not text and hasattr(msg, "model_extra") and msg.model_extra:
                    text = msg.model_extra.get("reasoning_content", "") or ""
                text = text.strip()
            return _clean_llm_output(text)

    except Exception as e:
        return f"⚠️ 生成失败：{e}"


@app.route("/")
def index():
    return redirect("/pool")


@app.route("/outreach")
def outreach_page():
    return render_template("index.html")


# ── Pool (人才库) ──

@app.route("/pool")
def pool_page():
    return render_template("people.html")


def _match_reason(person: dict, query: str) -> str:
    """「为什么推荐」：用 1-2 句指出候选人命中搜索意图的具体证据。按 (pid, query) 缓存 30 天。"""
    import hashlib, re
    key = f"match_reason:{person.get('id')}:{hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    parts = []
    for f in ("title", "company", "institution", "headline", "research_area",
              "location", "github_summary", "industry"):
        if person.get(f):
            parts.append(f"{f}: {person[f]}")
    for e in (person.get("experiences") or [])[:6]:
        if e.get("title") or e.get("company"):
            parts.append(f"经历: {e.get('title','')} @ {e.get('company','')}")
    for ed in (person.get("educations") or [])[:4]:
        if ed.get("school"):
            parts.append(f"教育: {ed.get('school','')} {ed.get('field') or ''}")
    for pub in (person.get("publications") or [])[:8]:
        parts.append(f"论文({pub.get('venue','')}): {pub.get('title','')}")
    tags = [t.get("name", "") for t in (person.get("tags") or []) if t.get("name")]
    if tags:
        parts.append("标签: " + ", ".join(tags))
    blob = "\n".join(parts)[:1500]   # 收短：本地推理模型 prompt 越长思考越停不下来
    if not blob:
        return ""

    provider, api_key, model_name, base_url = _resolve_llm_config("", {})
    if not api_key and provider == "anthropic":
        return ""
    sys_p = ("你是招聘检索助手。给定一个搜索意图和一位候选人的背景，用中文一句话（最多两句）"
             "说明这位候选人为什么匹配该搜索——必须指向背景里的具体证据（研究方向/论文/技能/经历/机构），"
             "不要泛泛而谈，也不要复述全部背景。若关联较弱，直说‘关联较弱’并指出最接近的一点。")
    user_p = f"搜索意图：{query}\n\n候选人背景：\n{blob}\n\n输出匹配理由："
    try:
        # max_tokens 给足：本地推理模型(deepseek/qwen-thinking)会先消耗几百思考 token，给少了正文为空
        if provider == "anthropic":
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(model=model_name, max_tokens=1024, system=sys_p,
                                         messages=[{"role": "user", "content": user_p}])
            reason = msg.content[0].text.strip()
        else:
            kwargs = {"api_key": api_key or "lm-studio"}
            if base_url:
                kwargs["base_url"] = base_url
            client = OpenAI(**kwargs)
            resp = client.chat.completions.create(
                model=model_name, max_tokens=4096,   # 给足：推理模型先耗千余思考 token，少了正文为空
                messages=[{"role": "system", "content": sys_p},
                          {"role": "user", "content": user_p}])
            reason = (resp.choices[0].message.content or "").strip()
            reason = re.sub(r"(?is)<think>.*?</think>", "", reason).strip()  # 防御:剥离残留思考标签
    except Exception as e:
        app.logger.warning(f"match_reason failed for {person.get('id')}: {e}")
        return ""
    if reason:                 # 只缓存非空结果，避免把失败/空回复钉死在缓存里
        cache_set(key, reason)
    return reason


@app.route("/pool/<int:person_id>")
@app.route("/people/<int:person_id>")
def person_page(person_id):
    person = get_person(person_id)
    if not person:
        return "Not found", 404

    # 「为什么推荐」：仅当从搜索点进来（带 ?q=）时计算
    search_query = request.args.get("q", "").strip()
    match_reason = _match_reason(person, search_query) if search_query else ""

    # GitHub 画像：从快照层组装（profile JSON + README + 个人主页摘要）
    github = None
    from db import get_snapshots
    snaps = get_snapshots(person_id)
    profile_snap = next((s for s in snaps if s["source"] == "github_profile"), None)
    if profile_snap:
        try:
            gh_profile = json.loads(profile_snap["raw_text"])
        except (json.JSONDecodeError, TypeError):
            gh_profile = {}
        repos = []
        repos_snap = next((s for s in snaps if s["source"] == "github_repos"), None)
        if repos_snap:
            try:
                repos = json.loads(repos_snap["raw_text"])
            except (json.JSONDecodeError, TypeError):
                pass
        languages = sorted({r["language"] for r in repos if r.get("language")})
        github = {
            "verified": person.get("github_verified"),
            "profile": gh_profile,
            "summary": person.get("github_summary"),
            "repos": repos[:5],
            "languages": languages[:8],
            "homepage_url": next((s["url"] for s in snaps if s["source"] == "homepage"), ""),
            "fetched_at": (profile_snap["fetched_at"] or "")[:10],
        }
    # 个人主页画像：最新一版提取 JSON
    homepage_profile = None
    conn = get_conn()
    ext_row = conn.execute(
        "SELECT json FROM extractions WHERE person_id=? AND source='homepage' "
        "ORDER BY version DESC, created_at DESC LIMIT 1", (person_id,)).fetchone()
    collabs = [dict(r) for r in conn.execute(
        "SELECT collaborator_name, collaborator_person_id, relation, context "
        "FROM collaborations WHERE person_id=? ORDER BY "
        "CASE relation WHEN 'advisor' THEN 0 WHEN 'mentor' THEN 1 WHEN 'labmate' THEN 2 ELSE 3 END",
        (person_id,)).fetchall()]
    conn.close()
    if ext_row:
        try:
            homepage_profile = json.loads(ext_row["json"])
            homepage_profile["collabs"] = collabs[:20]
            # News 原文 → 结构化时间线 [(yyyy-mm, 正文)]，按时间倒序
            import re as _re
            months = {m: i + 1 for i, m in enumerate(
                ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
            timeline = []
            for item in (homepage_profile.get("signals") or {}).get("news", [])[:15]:
                m = _re.match(r"^\s*([A-Za-z]{3,9})\.?,?\s*(?:(\d{1,2}),?\s*)?(\d{4})\s*[:\-–]\s*(.+)$",
                              item.strip())
                if m:
                    mon = months.get(m.group(1)[:3].lower())
                    key = f"{m.group(3)}-{mon:02d}" if mon else m.group(3)
                    timeline.append({"date": key, "text": m.group(4).strip()})
                else:
                    m2 = _re.match(r"^\s*(\d{4})[./-](\d{1,2})\s*[:\-–]?\s*(.+)$", item.strip())
                    if m2:
                        timeline.append({"date": f"{m2.group(1)}-{int(m2.group(2)):02d}",
                                         "text": m2.group(3).strip()})
                    else:
                        timeline.append({"date": "", "text": item.strip()})
            timeline.sort(key=lambda x: x["date"], reverse=True)
            homepage_profile["timeline"] = timeline
        except (json.JSONDecodeError, TypeError):
            pass
    wordcloud = _build_wordcloud(person, homepage_profile)
    has_discovery = os.path.exists(os.path.join(
        os.path.dirname(__file__), "data", "discoveries", f"{person_id}.json"))
    return render_template("person.html", person=person, github=github,
                           homepage_profile=homepage_profile, wordcloud=wordcloud,
                           has_discovery=has_discovery,
                           search_query=search_query, match_reason=match_reason)


# 泛词黑名单：太宽泛、无区分度的方向词不进词云
_WC_GENERIC = {
    "ai", "a.i.", "artificial intelligence", "machine learning", "ml", "deep learning", "dl",
    "computer science", "cs", "data science", "research", "science", "technology", "engineering",
    "algorithm", "algorithms", "neural network", "neural networks", "model", "models", "modeling",
    "learning", "system", "systems", "method", "methods", "approach", "computing", "computation",
    "mathematics", "math", "statistics", "optimization", "data", "analysis", "application", "applications",
    "intelligence", "informatics", "information", "general", "various", "etc",
}


def _build_wordcloud(person, hp):
    """研究方向词云：具体方向词，过滤泛词，按陈述+语料频次定权重。"""
    import re as _re
    terms = {}

    def add(t, base):
        if not isinstance(t, str):
            return
        t = _re.sub(r"\s+", " ", t).strip().strip(".,;")
        low = t.lower()
        if not t or low in _WC_GENERIC or len(t) < 2 or len(t) > 42 or t.isdigit():
            return
        terms.setdefault(low, {"text": t, "weight": 0})
        terms[low]["weight"] += base

    for d in (hp or {}).get("research_directions") or []:
        add(d, 3)
    for tag in person.get("tags", []):
        if tag.get("category") in ("domain", "skill"):
            add(tag.get("name"), 2)

    # 语料：摘要 + 自述 + 论文标题 + 项目，统计方向词出现频次作为额外权重
    corpus_parts = [(hp or {}).get("summary_zh") or "", (hp or {}).get("self_description") or ""]
    corpus_parts += [p.get("title", "") for p in person.get("publications", [])]
    for pr in (hp or {}).get("projects") or []:
        if isinstance(pr, dict):
            corpus_parts += [pr.get("name") or "", pr.get("description") or ""]
    corpus = " ".join(corpus_parts).lower()
    for low, obj in terms.items():
        obj["weight"] += min(corpus.count(low), 5)

    return sorted(terms.values(), key=lambda x: -x["weight"])[:28]


@app.route("/pool/github-review")
def github_review_page():
    import re as _re
    from db import get_github_verified_people

    # 判定依据存在验证报告里（reason 是衍生分析，不进 people 表）
    reasons = {}
    report_path = os.path.join(os.path.dirname(__file__), "data", "github_verify_report.json")
    if os.path.exists(report_path):
        for r in json.load(open(report_path, encoding="utf-8")):
            reasons[r["id"]] = r.get("reason", "")

    people = get_github_verified_people()
    for p in people:
        m = _re.search(r"github\.com/([^/?#]+)", p.get("github_url") or "", _re.I)
        p["login"] = m.group(1) if m else ""
        m = _re.search(r"linkedin\.com/in/([^/?#]+)", p.get("linkedin_url") or "", _re.I)
        p["linkedin_slug"] = m.group(1) if m else ""
        p["verified"] = p["github_verified"]
        p["reason"] = reasons.get(p["id"], "")

    def pick(*levels):
        return [p for p in people if p["verified"] in levels]

    groups = [
        {"title": "铁证确认", "desc": "邮箱匹配或 GitHub/个人主页挂了本人 LinkedIn，理论上不会错",
         "people": pick("verified_email", "verified_link")},
        {"title": "LLM 确认", "desc": "本地模型比对库内经历与 GitHub 证据后确认，建议抽查",
         "people": pick("llm_confirmed")},
        {"title": "待复核", "desc": "空壳账号或证据不足，需要人工判断",
         "people": pick("uncertain")},
        {"title": "已排除 / 死链", "desc": "确认错误归属，或 GitHub 账号已不存在",
         "people": pick("rejected", "not_found")},
    ]
    counts = {
        "verified": len(groups[0]["people"]),
        "llm_confirmed": len(groups[1]["people"]),
        "uncertain": len(groups[2]["people"]),
        "rejected": len(pick("rejected")),
        "not_found": len(pick("not_found")),
    }
    return render_template("github_review.html",
                           groups=[g for g in groups if g["people"]],
                           counts=counts, total=len(people))


# Legacy redirects
@app.route("/people")
def people_redirect():
    return redirect("/pool")


@app.route("/discover")
def discover_page():
    return render_template("lab_sourcer.html")


@app.route("/api/people", methods=["GET"])
def api_search_people():
    from db import list_people, get_badge_fields

    query = request.args.get("q", "").strip()
    lens = request.args.get("lens", "").strip()
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 30))
    offset = (page - 1) * per_page

    # 视角（lens）是同一池子上的筛选窗口；带 lens 时统一走 list_people（LIKE 检索）
    if lens or not query:
        results, total = list_people(lens=lens, query=query, limit=per_page, offset=offset)
        return jsonify({"data": results, "total": total, "page": page, "per_page": per_page,
                        "mode": lens or "all"})

    # 自动判断搜索模式
    import re
    has_boolean = bool(re.search(r'\b(AND|OR|NOT)\b', query))
    words = query.split()

    if has_boolean:
        # Boolean search: Google AND PhD, engineer NOT intern
        results, total = search_people_boolean(query, limit=per_page, offset=offset)
        mode = "boolean"
    elif len(words) >= 4 and not has_boolean:
        # 长自然语言 → 语义搜索
        sem_results = semantic_search(query, limit=per_page + offset)
        person_ids = [r["person_id"] for r in sem_results[offset:offset + per_page]]
        results = []
        for pid in person_ids:
            p = get_person(pid)
            if p:
                results.append({k: p[k] for k in (
                    "id", "first_name", "last_name", "linkedin_url", "email", "github_url",
                    "title", "headline", "company", "location", "industry", "status",
                    "created_at", "updated_at"
                )})
        total = len(sem_results)
        mode = "semantic"
    else:
        # 短关键词 → FTS5 加权全文搜索
        results, total = search_people_fts(query, limit=per_page, offset=offset)
        mode = "fts"

    # 搜索结果补徽章字段（sector / github_verified / venues）
    badges = get_badge_fields([r["id"] for r in results])
    for r in results:
        r.update(badges.get(r["id"], {}))

    return jsonify({"data": results, "total": total, "page": page, "per_page": per_page, "mode": mode})


@app.route("/demo")
def demo_page():
    """精品 Demo 人才池：丰富度 Top 100，演示用。"""
    from db import canon_venues
    conn = get_conn()
    rows = conn.execute(
        """SELECT p.id, p.first_name, p.last_name, COALESCE(p.institution, p.company, '') AS aff,
                  p.advisor, p.lab, p.sector, p.github_summary
           FROM people p JOIN person_tags pt ON pt.person_id = p.id
           JOIN tags t ON t.id = pt.tag_id WHERE t.name = '__demo__'""").fetchall()
    people = []
    for r in rows:
        d = dict(r)
        d["dirs"] = [x["name"] for x in conn.execute(
            """SELECT t.name FROM person_tags pt JOIN tags t ON t.id=pt.tag_id
               WHERE pt.person_id=? AND t.category IN ('domain','skill')
               ORDER BY length(t.name) LIMIT 4""", (r["id"],)).fetchall()]
        vraw = conn.execute(
            "SELECT GROUP_CONCAT(venue,'|') FROM publications WHERE person_id=?", (r["id"],)).fetchone()[0]
        d["venues"] = canon_venues(vraw, 3).split(",") if vraw else []
        d["venues"] = [v for v in d["venues"] if v]
        d["conns"] = conn.execute(
            "SELECT COUNT(*) FROM collaborations WHERE person_id=? AND collaborator_person_id IS NOT NULL",
            (r["id"],)).fetchone()[0]
        ext = conn.execute(
            "SELECT json FROM extractions WHERE person_id=? AND source='homepage' ORDER BY version DESC LIMIT 1",
            (r["id"],)).fetchone()
        summ = ""
        if ext:
            try:
                summ = json.loads(ext["json"]).get("summary_zh") or ""
            except (json.JSONDecodeError, TypeError):
                pass
        d["summary"] = summ or (r["github_summary"] or "")
        people.append(d)
    conn.close()
    # 按方向多样性 + 连线数排序展示
    people.sort(key=lambda x: -(len(x["dirs"]) + x["conns"] * 0.1))
    n_inst = len({p["aff"] for p in people if p["aff"]})
    total_conns = sum(p["conns"] for p in people)
    return render_template("demo.html", people=people, n_inst=n_inst, total_conns=total_conns)


@app.route("/discover/<int:person_id>")
def person_discover_page(person_id):
    """引用扩张发现结果（读缓存）。"""
    path = os.path.join(os.path.dirname(__file__), "data", "discoveries", f"{person_id}.json")
    if not os.path.exists(path):
        return f"该候选人暂无发现结果。先运行: python3.12 discover_s2.py {person_id}", 404
    data = json.load(open(path, encoding="utf-8"))
    return render_template("discover.html", d=data)


@app.route("/api/people/<int:person_id>/graph", methods=["GET"])
def api_person_graph(person_id):
    """关系网络图数据：中心=本人，周围=合作者，含合作者之间的二度连线。"""
    person = get_person(person_id)
    if not person:
        return jsonify({"error": "not found"}), 404

    REL_PRIORITY = {"advisor": 0, "co-advisor": 0, "supervisor": 0, "mentor": 1,
                    "labmate": 2, "student": 3, "colleague": 4, "coauthor": 5, "mentioned": 6}
    REL_LABEL = {"advisor": "导师", "co-advisor": "co-advisor", "supervisor": "导师",
                 "mentor": "mentor", "labmate": "同实验室", "student": "学生",
                 "colleague": "同事", "coauthor": "共著", "mentioned": "提及"}

    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        """SELECT collaborator_name, collaborator_person_id, relation, context
           FROM collaborations WHERE person_id=?""", (person_id,)).fetchall()]

    # 按名字去重，保留优先级最高的关系
    best = {}
    for r in rows:
        nm = (r["collaborator_name"] or "").strip()
        if not nm or nm.lower() == f"{person['first_name']} {person['last_name']}".lower():
            continue
        rel = (r["relation"] or "coauthor").lower()
        pr = REL_PRIORITY.get(rel, 5)
        if nm not in best or pr < best[nm]["pr"]:
            best[nm] = {"name": nm, "pid": r["collaborator_person_id"], "rel": rel,
                        "pr": pr, "context": r["context"] or ""}

    # 排序：关系优先级 → 在库优先 → 取前 36 个，保证可读
    collabs = sorted(best.values(), key=lambda x: (x["pr"], x["pid"] is None))[:36]

    nodes = [{"id": "center", "name": f"{person['first_name']} {person['last_name']}",
              "rel": "self", "pid": person_id, "in_db": True,
              "aff": person.get("institution") or person.get("company") or ""}]
    links = [{"source": "center", "target": f"n{i}", "rel": c["rel"]}
             for i, c in enumerate(collabs)]
    id_by_pid = {}
    for i, c in enumerate(collabs):
        nid = f"n{i}"
        nodes.append({"id": nid, "name": c["name"], "rel": c["rel"],
                      "pid": c["pid"], "in_db": c["pid"] is not None, "context": c["context"]})
        if c["pid"]:
            id_by_pid[c["pid"]] = nid

    # 二度连线：本人的在库合作者之间，若彼此也有合作关系，连起来 → 形成网络
    db_pids = [p for p in id_by_pid]
    if len(db_pids) > 1:
        ph = ",".join("?" * len(db_pids))
        for r in conn.execute(
            f"""SELECT DISTINCT person_id, collaborator_person_id FROM collaborations
                WHERE person_id IN ({ph}) AND collaborator_person_id IN ({ph})""",
            db_pids + db_pids).fetchall():
            a, b = id_by_pid.get(r["person_id"]), id_by_pid.get(r["collaborator_person_id"])
            if a and b and a != b:
                links.append({"source": a, "target": b, "rel": "peer", "peer": True})
    conn.close()
    return jsonify({"nodes": nodes, "links": links, "rel_label": REL_LABEL})


@app.route("/api/pool/lens-counts", methods=["GET"])
def api_lens_counts():
    from db import LENS_WHERE
    conn = get_conn()
    counts = {"all": conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]}
    for lens, cond in LENS_WHERE.items():
        counts["conf" if lens == "conf" else lens] = conn.execute(
            f"SELECT COUNT(*) FROM people p WHERE {cond}").fetchone()[0]
    conn.close()
    return jsonify(counts)


@app.route("/api/people/<int:person_id>", methods=["GET"])
def api_get_person(person_id):
    person = get_person(person_id)
    if not person:
        return jsonify({"error": "not found"}), 404
    return jsonify(person)


@app.route("/api/people/insights", methods=["GET"])
def api_people_insights():
    """Return aggregated insights for the talent pool dashboard."""
    conn = get_conn()

    total = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]

    # Top companies
    companies = [dict(r) for r in conn.execute(
        "SELECT company as label, COUNT(*) as value FROM people "
        "WHERE company IS NOT NULL AND company != '' "
        "GROUP BY company ORDER BY value DESC LIMIT 12"
    ).fetchall()]

    # Top locations (simplified)
    locations = [dict(r) for r in conn.execute(
        "SELECT location as label, COUNT(*) as value FROM people "
        "WHERE location IS NOT NULL AND location != '' "
        "GROUP BY location ORDER BY value DESC LIMIT 10"
    ).fetchall()]

    # Top titles
    titles = [dict(r) for r in conn.execute(
        "SELECT title as label, COUNT(*) as value FROM people "
        "WHERE title IS NOT NULL AND title != '' "
        "GROUP BY title ORDER BY value DESC LIMIT 10"
    ).fetchall()]

    # Top schools
    schools = [dict(r) for r in conn.execute(
        "SELECT school as label, COUNT(*) as value FROM educations "
        "WHERE school != '' AND school NOT LIKE '%Master%' AND school NOT LIKE '%Doctor%' "
        "AND school NOT LIKE '%Bachelor%' AND school NOT LIKE '%PhD%' "
        "GROUP BY school ORDER BY value DESC LIMIT 10"
    ).fetchall()]

    # Degree distribution
    degrees = [dict(r) for r in conn.execute(
        """SELECT
            CASE
                WHEN degree LIKE '%PhD%' OR degree LIKE '%Doctor%' THEN 'PhD'
                WHEN degree LIKE '%Master%' OR degree LIKE '%MS%' OR degree LIKE '%MA%' THEN 'Master'
                WHEN degree LIKE '%Bachelor%' OR degree LIKE '%BS%' OR degree LIKE '%BA%' THEN 'Bachelor'
                WHEN degree LIKE '%MBA%' THEN 'MBA'
                ELSE 'Other'
            END as label,
            COUNT(*) as value
        FROM educations WHERE degree != ''
        GROUP BY label ORDER BY value DESC"""
    ).fetchall()]

    # Status distribution
    statuses = [dict(r) for r in conn.execute(
        "SELECT status as label, COUNT(*) as value FROM people GROUP BY status ORDER BY value DESC"
    ).fetchall()]

    # Seniority distribution (from title keywords)
    seniority = [dict(r) for r in conn.execute(
        """SELECT
            CASE
                WHEN title LIKE '%Director%' OR title LIKE '%VP%' OR title LIKE '%Vice President%' THEN 'Director / VP'
                WHEN title LIKE '%Principal%' OR title LIKE '%Distinguished%' OR title LIKE '%Fellow%' THEN 'Principal+'
                WHEN title LIKE '%Staff%' THEN 'Staff'
                WHEN title LIKE '%Senior%' OR title LIKE '%Sr.%' OR title LIKE '%Sr %' THEN 'Senior'
                WHEN title LIKE '%Manager%' OR title LIKE '%Lead%' OR title LIKE '%Head%' THEN 'Manager / Lead'
                WHEN title LIKE '%Intern%' THEN 'Intern'
                ELSE 'IC'
            END as label,
            COUNT(*) as value
        FROM people WHERE title IS NOT NULL AND title != ''
        GROUP BY label ORDER BY value DESC"""
    ).fetchall()]

    # Outreach funnel
    enriched = conn.execute("SELECT COUNT(*) FROM people WHERE notes = 'linkedin_enriched'").fetchone()[0]
    contacted = conn.execute("SELECT COUNT(DISTINCT person_id) FROM history WHERE person_id IS NOT NULL").fetchone()[0]
    replied = conn.execute("SELECT COUNT(DISTINCT person_id) FROM history WHERE person_id IS NOT NULL AND status = 'replied'").fetchone()[0]
    interviewing = conn.execute("SELECT COUNT(*) FROM people WHERE status = 'interviewing'").fetchone()[0]

    # 已补充(enriched)不是外联阶段,放进漏斗会打破单调性
    # (已联系 435 / 已补充 9 → 转化率 4833%),它有独立的"补充进度"卡片
    funnel = [
        {"label": "总库", "value": total},
        {"label": "已联系", "value": contacted},
        {"label": "已回复", "value": replied},
        {"label": "面试中", "value": interviewing},
    ]

    # Research / work directions — detailed word cloud (count distinct candidates per topic)
    raw_topics = conn.execute(
        """SELECT LOWER(TRIM(t.name)) AS label, COUNT(DISTINCT pt.person_id) AS value
           FROM person_tags pt JOIN tags t ON t.id = pt.tag_id
           WHERE t.category IN ('domain', 'skill') AND TRIM(t.name) != ''
           GROUP BY LOWER(TRIM(t.name))"""
    ).fetchall()
    research_topics = _build_topic_cloud(raw_topics)

    conn.close()
    return jsonify({
        "total": total,
        "companies": companies,
        "locations": locations,
        "titles": titles,
        "schools": schools,
        "degrees": degrees,
        "statuses": statuses,
        "seniority": seniority,
        "funnel": funnel,
        "enriched": enriched,
        "research_topics": research_topics,
    })


# 高层/工具类词——词云里滤掉，只留细粒度方向
_TOPIC_STOP = {
    "machine learning", "deep learning", "artificial intelligence", "ai", "ml", "dl",
    "neural networks", "neural network", "data science", "computer science", "statistics",
    "mathematics", "algorithms", "algorithm", "programming", "software engineering",
    "software development", "research", "big data", "data mining", "data analysis",
    "python", "c++", "java", "javascript", "pytorch", "tensorflow", "keras", "sql",
    "linux", "git", "docker", "kubernetes", "matlab", "scala", "rust", "golang",
    "python开发", "开发", "vision", "systems", "agents",
}
# 中英 / 同义归并到统一键
_TOPIC_ALIAS = {
    "计算机视觉": "computer vision", "深度学习": "deep learning", "强化学习": "reinforcement learning",
    "机器学习": "machine learning", "自然语言处理": "nlp", "大语言模型": "large language models",
    "多模态": "multimodal learning", "多模态学习": "multimodal learning", "生成模型": "generative models",
    "扩散模型": "diffusion models", "机器人": "robotics", "机器人学": "robotics", "具身智能": "embodied ai",
    "三维视觉": "3d vision", "优化": "optimization", "图形学": "computer graphics",
    "llms": "large language models", "llm": "large language models", "vlms": "vision-language models",
    "vlm": "vision-language models", "natural language processing": "nlp",
    "vision-language": "vision-language models", "language models": "large language models",
    "llm agent": "llm agents", "video generation": "video generation",
}
_TOPIC_ACRONYM = {
    "llm": "LLM", "llms": "LLMs", "nlp": "NLP", "vlm": "VLM", "vlms": "VLMs", "ai": "AI",
    "3d": "3D", "2d": "2D", "rl": "RL", "gan": "GAN", "gans": "GANs", "rlhf": "RLHF",
    "cv": "CV", "hci": "HCI", "ar": "AR", "vr": "VR", "slam": "SLAM", "ocr": "OCR",
    "mllm": "MLLM", "moe": "MoE", "gnn": "GNN", "ssl": "SSL",
}


def _topic_display(key: str) -> str:
    """统一键 → 展示标签：中文原样，英文按词首字母大写、已知缩写保持大写。"""
    if any("一" <= ch <= "鿿" for ch in key):
        return key
    return " ".join(_TOPIC_ACRONYM.get(w, w.capitalize()) for w in key.split())


def _build_topic_cloud(raw_topics, limit: int = 20):
    """归并大小写/中英别名、滤掉高层与工具词，按候选人数取 top N。"""
    agg = {}
    for r in raw_topics:
        key = _TOPIC_ALIAS.get(r["label"], r["label"]).strip()
        if not key or len(key) < 2 or key in _TOPIC_STOP:
            continue
        agg[key] = agg.get(key, 0) + r["value"]
    items = sorted(agg.items(), key=lambda kv: -kv[1])[:limit]
    return [{"label": _topic_display(k), "value": v} for k, v in items]


@app.route("/api/people/enrich-from-linkedin", methods=["POST"])
def api_enrich_from_linkedin():
    """Receive LinkedIn profile data from Chrome extension and upsert into talent pool."""
    body = request.json or {}
    linkedin_url = (body.get("linkedin_url") or "").strip()
    if not linkedin_url:
        return jsonify({"error": "linkedin_url required"}), 400

    # Build person data
    person_data = {
        "linkedin_url": linkedin_url,
        "first_name": (body.get("first_name") or "").strip(),
        "last_name": (body.get("last_name") or "").strip(),
        "headline": (body.get("headline") or "").strip(),
        "location": (body.get("location") or "").strip(),
    }

    # Extract title and company from headline if possible
    headline = person_data["headline"]
    if headline and " at " in headline:
        parts = headline.split(" at ", 1)
        person_data["title"] = parts[0].strip()
        person_data["company"] = parts[1].strip()
    elif headline and " @ " in headline:
        parts = headline.split(" @ ", 1)
        person_data["title"] = parts[0].strip()
        person_data["company"] = parts[1].strip()

    # Also accept about as notes (truncated)
    about = (body.get("about") or "").strip()
    if about:
        person_data["industry"] = about[:200]

    # Upsert person
    person_id, action = upsert_person(person_data)

    # Update experiences if provided and person was created or updated
    experiences = body.get("experiences", [])
    if experiences and action in ("created", "updated"):
        add_experiences(person_id, experiences)

    # Update educations if provided
    educations = body.get("educations", [])
    if educations and action in ("created", "updated"):
        add_educations(person_id, educations)

    return jsonify({"person_id": person_id, "action": action})


@app.route("/api/people/<int:person_id>/enrich-from-paste", methods=["POST"])
def api_enrich_from_paste(person_id):
    """Parse pasted LinkedIn text with LLM and update person profile."""
    person = get_person(person_id)
    if not person:
        return jsonify({"error": "not found"}), 404

    body = request.json or {}
    raw_text = (body.get("text") or "").strip()
    if not raw_text:
        return jsonify({"error": "text required"}), 400

    api_key = request.headers.get("X-Api-Key", "")

    # Resolve LLM config
    provider, api_key, model_name, base_url = _resolve_llm_config(api_key, {})
    if not api_key:
        return jsonify({"error": "未设置 API Key"}), 400

    # Clean LinkedIn copy-paste noise to reduce token count
    import re as _re
    # Remove common LinkedIn UI text
    noise_patterns = [
        r'See more$', r'see more$', r'Show all \d+.*$', r'Show \d+ more.*$',
        r'^\s*Like$', r'^\s*Comment$', r'^\s*Repost$', r'^\s*Send$',
        r'^\s*Report / Block$', r'^\s*Follow$', r'^\s*Connect$', r'^\s*Message$',
        r'^\s*More$', r'^\s*Open to$', r'^\s*Add profile section$',
        r'^\s*Enhance profile$', r'^\s*\d+ connections?$',
        r'^\s*\d+ followers?$', r'^\s*\d+ mutual connections?$',
        r'^\s*•$', r'^\s*…$', r'^\s*Reactions?$',
        r'^\s*Activity$', r'^\s*Analytics$', r'^\s*Resources$',
        r'^\s*Agree$', r'^\s*Celebrate$', r'^\s*Love$', r'^\s*Insightful$',
        r'^\s*Funny$', r'^\s*Support$', r'^\s*Save$',
        r'^\s*Report this', r'^\s*Copy link', r'^\s*Embed this',
        r'^\s*People also viewed', r'^\s*People you may know',
        r'^\s*Similar profiles', r'^\s*Other similar profiles',
        r'^\s*Add a comment', r'^\s*Post a comment',
        r'^\s*Powered by', r'^\s*Sign in', r'^\s*Join now',
        r'^https?://', r'^\s*\d+ comments?$', r'^\s*\d+ likes?$',
        r'^\s*Messaging$', r'^\s*Notifications$', r'^\s*My Network$',
        r'^\s*Home$', r'^\s*Jobs$', r'^\s*Search$',
    ]
    lines = raw_text.split('\n')
    cleaned_lines = []
    seen = set()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Skip noise
        skip = False
        for pat in noise_patterns:
            if _re.match(pat, stripped, _re.IGNORECASE):
                skip = True
                break
        if skip:
            continue
        # Deduplicate identical lines
        if stripped in seen:
            continue
        seen.add(stripped)
        cleaned_lines.append(stripped)
    raw_text = '\n'.join(cleaned_lines)

    # Truncate to stay within local model context
    raw_text = raw_text[:15000]
    print(f"[Enrich] Cleaned text length: {len(raw_text)} chars", flush=True)

    parse_prompt = """从以下 LinkedIn 页面粘贴的文本中提取结构化信息。严格输出 JSON，不要输出其他内容。

JSON 格式：
{
  "headline": "完整的 headline",
  "location": "所在地",
  "about": "个人简介/About 部分原文",
  "experiences": [
    {
      "title": "职位名称",
      "company": "公司名称",
      "location": "工作地点",
      "start_date": "开始时间，如 Jan 2020",
      "end_date": "结束时间，如 Present 或 Dec 2023",
      "description": "工作职责描述原文",
      "is_current": 1
    }
  ],
  "educations": [
    {
      "school": "学校名称",
      "degree": "学位，如 Ph.D., Master's, Bachelor's",
      "field": "专业方向",
      "start_year": 2015,
      "end_year": 2019,
      "activities": "活动/社团等"
    }
  ]
}

规则：
- experiences 按时间从近到远排序，最新的 is_current=1，其余为0
- 同一公司多个职位要分开列出，每个职位一条记录
- description 保留原文，不要缩写
- 日期保留原始格式如 "Jan 2020"、"Sep 2018 - Present"
- 如果某个字段找不到，用空字符串、null 或空数组
- 只输出 JSON"""

    try:
        if provider == "anthropic":
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model=model_name, max_tokens=1500,
                system=parse_prompt,
                messages=[{"role": "user", "content": raw_text}],
            )
            result_text = msg.content[0].text.strip()
        else:
            kwargs = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            client = OpenAI(**kwargs)
            resp = client.chat.completions.create(
                model=model_name, max_tokens=1500,
                messages=[
                    {"role": "system", "content": parse_prompt},
                    {"role": "user", "content": raw_text},
                    {"role": "assistant", "content": "{"},
                ],
            )
            msg = resp.choices[0].message
            result_text = (msg.content or "").strip()
            # Qwen: content may be empty, real output in reasoning_content
            if not result_text:
                result_text = getattr(msg, "reasoning_content", "") or ""
                if not result_text and hasattr(msg, "model_extra") and msg.model_extra:
                    result_text = msg.model_extra.get("reasoning_content", "") or ""
                result_text = result_text.strip()

        # Prepend { from assistant prefill
        if not result_text.startswith('{'):
            result_text = '{' + result_text
        print(f"[Enrich] LLM response ({len(result_text)} chars): {result_text[:500]}")

        # Extract JSON from response - handle markdown code blocks
        # Strip markdown code fences
        result_text = _re.sub(r'```json\s*', '', result_text)
        result_text = _re.sub(r'```\s*', '', result_text)
        # Find first complete JSON object (model may output duplicates)
        # Use a stack-based approach to find the first balanced {}
        parsed = None
        depth = 0
        start = -1
        for i, ch in enumerate(result_text):
            if ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        parsed = json.loads(result_text[start:i+1])
                        break
                    except json.JSONDecodeError:
                        start = -1
                        continue
        if not parsed:
            return jsonify({"error": "AI 未返回有效 JSON", "raw": result_text[:300]}), 500

        import sys
        print(f"[Enrich] Parsed: {len(parsed.get('experiences', []))} exps, {len(parsed.get('educations', []))} edus", flush=True)
        print(f"[Enrich] Educations: {parsed.get('educations', [])}", flush=True)
        print(f"[Enrich] Full parsed keys: {list(parsed.keys())}", flush=True)

        # Update person basic fields (overwrite with richer data from LinkedIn)
        update_data = {"linkedin_url": person.get("linkedin_url", "")}
        update_data["first_name"] = person["first_name"]
        update_data["last_name"] = person["last_name"]
        if parsed.get("headline"):
            update_data["headline"] = parsed["headline"]
        if parsed.get("location"):
            update_data["location"] = parsed["location"]
        if parsed.get("about"):
            update_data["industry"] = parsed["about"][:500]

        exps = parsed.get("experiences", [])
        if exps:
            update_data["title"] = exps[0].get("title", "")
            update_data["company"] = exps[0].get("company", "")

        # Mark as enriched
        conn = get_conn()
        conn.execute("UPDATE people SET notes = 'linkedin_enriched' WHERE id = ? AND (notes IS NULL OR notes = '')", (person_id,))
        conn.commit()
        conn.close()

        upsert_person(update_data)

        # Replace experiences with richer LinkedIn data
        if exps:
            conn = get_conn()
            conn.execute("DELETE FROM experiences WHERE person_id = ?", (person_id,))
            for i, exp in enumerate(exps):
                conn.execute(
                    """INSERT INTO experiences
                    (person_id, position, is_current, title, company, location, start_date, end_date, description)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (person_id, i, 1 if exp.get("is_current") else 0,
                     exp.get("title") or "", exp.get("company") or "",
                     exp.get("location") or "", exp.get("start_date") or "",
                     exp.get("end_date") or "", exp.get("description") or ""))
            conn.commit()
            conn.close()

        # Replace educations with richer data
        edus = parsed.get("educations", [])
        if edus:
            conn = get_conn()
            conn.execute("DELETE FROM educations WHERE person_id = ?", (person_id,))
            for edu in edus:
                conn.execute(
                    """INSERT INTO educations
                    (person_id, school, degree, field, start_year, end_year, activities)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (person_id, edu.get("school") or "", edu.get("degree") or "",
                     edu.get("field") or "", edu.get("start_year"), edu.get("end_year"),
                     edu.get("activities") or ""))
            conn.commit()
            conn.close()

        # 更新 FTS 索引
        update_fts_for_person(person_id)

        return jsonify({"ok": True, "parsed": parsed})

    except json.JSONDecodeError:
        return jsonify({"error": "AI 返回内容无法解析为 JSON"}), 500
    except Exception as e:
        return jsonify({"error": f"解析失败：{e}"}), 500


@app.route("/api/people/check-duplicate", methods=["POST"])
def api_check_duplicate():
    """检查 LinkedIn URL 是否已存在。"""
    body = request.json or {}
    linkedin_url = (body.get("linkedin_url") or "").strip()
    if not linkedin_url:
        return jsonify({"exists": False})
    conn = get_conn()
    row = conn.execute("SELECT id, first_name, last_name, title, company FROM people WHERE linkedin_url = ?",
                       (linkedin_url,)).fetchone()
    conn.close()
    if row:
        return jsonify({"exists": True, "person": dict(row)})
    return jsonify({"exists": False})


@app.route("/api/people", methods=["POST"])
def api_add_person():
    """添加候选人，支持查重。返回 {person_id, action}。"""
    body = request.json or {}
    linkedin_url = (body.get("linkedin_url") or "").strip()
    if not linkedin_url:
        return jsonify({"error": "linkedin_url is required"}), 400

    person_data = {
        "first_name": (body.get("first_name") or "").strip(),
        "last_name": (body.get("last_name") or "").strip(),
        "linkedin_url": linkedin_url,
        "email": (body.get("email") or "").strip() or None,
        "github_url": (body.get("github_url") or "").strip() or None,
        "title": (body.get("title") or "").strip() or None,
        "headline": (body.get("headline") or "").strip() or None,
        "company": (body.get("company") or "").strip() or None,
        "location": (body.get("location") or "").strip() or None,
        "industry": (body.get("industry") or "").strip() or None,
    }

    person_id, action = upsert_person(person_data)

    # 处理经历
    experiences_raw = (body.get("experience") or "").strip()
    if experiences_raw:
        lines = [l.strip() for l in experiences_raw.split("\n") if l.strip()]
        exps = []
        for i, line in enumerate(lines):
            if "@" in line:
                title, company = line.split("@", 1)
            else:
                title, company = line, ""
            exps.append({"position": i, "is_current": 1 if i == 0 else 0,
                         "title": title.strip(), "company": company.strip()})
        if exps:
            add_experiences(person_id, exps)

    # 处理教育
    education_raw = (body.get("education") or "").strip()
    if education_raw:
        parts = [p.strip() for p in education_raw.split(" - ")]
        school = parts[0]
        degree = " - ".join(parts[1:]) if len(parts) > 1 else ""
        add_educations(person_id, [{"school": school, "degree": degree}])

    return jsonify({"person_id": person_id, "action": action})


@app.route("/api/generate-for-person/<int:person_id>", methods=["POST"])
def api_generate_for_person(person_id):
    """基于人才库中的候选人完整背景生成话术。"""
    person = get_person(person_id)
    if not person:
        return jsonify({"error": "not found"}), 404

    body = request.json or {}
    config = body.get("config", {})
    api_key = request.headers.get("X-Api-Key", "")

    # 构建背景信息（按权重分层）
    name = f"{person['first_name']} {person['last_name']}".strip()
    url = person.get("linkedin_url", "")
    community = config.get("community", "")
    language = config.get("language", "zh")
    custom_note = config.get("custom_note", "").strip()

    context_parts = []
    exps = person.get("experiences") or []
    edus = person.get("educations") or []

    # ── 第一层：核心 Hook 区（最高权重） ──
    if exps:
        current = exps[0]
        context_parts.append(f"【当前职位】{current['title']} @ {current['company']}")
        desc = (current.get("description") or "").strip()
        if desc:
            context_parts.append(f"【当前工作内容（重点参考）】\n{desc}")
        date_str = ""
        if current.get("start_date"):
            date_str = current["start_date"]
            if current.get("end_date"):
                date_str += f" – {current['end_date']}"
        if date_str or current.get("location"):
            meta = " | ".join(filter(None, [date_str, current.get("location") or ""]))
            context_parts.append(f"任职时间/地点：{meta}")

    if custom_note:
        context_parts.append(f"【招聘方需求重点】{custom_note}\n→ 请找到候选人当前工作内容与此需求的具体结合点，作为消息的核心吸引力。")

    # ── 第二层：佐证信息 ──
    if len(exps) > 1:
        prev = exps[1]
        prev_line = f"{prev['title']} @ {prev['company']}"
        prev_desc = (prev.get("description") or "").strip()
        if prev_desc:
            prev_line += f"\n  {prev_desc[:200]}"
        context_parts.append(f"【上一段经历】{prev_line}")

    # 教育：仅高级学位（PhD/硕士），本科压缩
    if edus:
        advanced = [e for e in edus if any(k in (e.get("degree") or "").lower() for k in ("phd", "ph.d", "doctor", "master", "m.s", "m.a", "博士", "硕士"))]
        other = [e for e in edus if e not in advanced]
        edu_lines = []
        for e in advanced:
            parts = [e.get("school") or "", e.get("degree") or "", e.get("field") or ""]
            yr = ""
            if e.get("start_year") and e.get("end_year"):
                yr = f" ({e['start_year']}-{e['end_year']})"
            elif e.get("end_year"):
                yr = f" ({e['end_year']})"
            edu_lines.append(" — ".join(filter(None, parts)) + yr)
        for e in other:
            edu_lines.append(f"{e.get('school') or ''}, {e.get('degree') or ''}")
        if edu_lines:
            context_parts.append("【教育背景】\n" + "\n".join(edu_lines))

    if person.get("headline"):
        context_parts.append(f"Headline: {person['headline']}")

    # About（industry 字段存了 about 内容）
    about = person.get("industry") or ""
    if about:
        context_parts.append(f"About: {about[:200]}")

    # ── 第三层：背景概览（压缩） ──
    if len(exps) > 2:
        older_companies = list(dict.fromkeys(e["company"] for e in exps[2:] if e.get("company")))
        if older_companies:
            context_parts.append(f"【更早经历】曾在 {'、'.join(older_companies)} 任职")

    # GitHub
    github_login = ""
    if person.get("github_url"):
        context_parts.append(f"GitHub: {person['github_url']}")
        github_login = person["github_url"].rstrip("/").split("/")[-1]

    context = "\n\n".join(context_parts)

    # 如果是学术模式
    mode = config.get("mode", "community")

    def stream():
        yield f"data: {json.dumps({'status': 'searching', 'name': name})}\n\n"

        # 如果有 GitHub，补充搜索
        full_context = context
        if github_login and mode == "community":
            gh_info = search_github(name, community, github_login)
            if gh_info:
                full_context += f"\n\nGitHub 详情:\n{gh_info[:800]}"

        # 限制总 context 长度，避免超出本地模型上下文窗口
        if len(full_context) > 2000:
            full_context = full_context[:2000] + "\n...(已截断)"

        yield f"data: {json.dumps({'status': 'generating', 'name': name})}\n\n"

        message = generate_message(name, url, full_context, api_key, config)

        # 存入历史，关联 person_id
        add_history(name, url, community, language, message, person_id=person_id)

        yield f"data: {json.dumps({'status': 'done', 'name': name, 'url': url, 'message': message})}\n\n"
        yield f"data: {json.dumps({'status': 'complete'})}\n\n"

    return Response(stream_with_context(stream()), mimetype="text/event-stream")


# ── Sender config API ──

@app.route("/api/sender", methods=["GET"])
def api_get_sender():
    return jsonify(get_sender_config())


@app.route("/api/sender", methods=["POST"])
def api_save_sender():
    body = request.json or {}
    save_sender_config(
        body.get("name", ""),
        body.get("team", ""),
        body.get("contact", ""),
    )
    return jsonify({"ok": True})


# ── History API ──

@app.route("/api/history", methods=["GET"])
def api_get_history():
    search = request.args.get("search", "")
    return jsonify(get_history(search=search))


@app.route("/api/history/<int:record_id>", methods=["DELETE"])
def api_delete_history(record_id):
    delete_history(record_id)
    return jsonify({"ok": True})


@app.route("/api/history/clear", methods=["POST"])
def api_clear_history():
    clear_history()
    return jsonify({"ok": True})


# ── Paper lookup API ──

@app.route("/api/lookup-paper", methods=["POST"])
def api_lookup_paper():
    """Look up a paper title via Semantic Scholar, return authors + abstract."""
    body = request.json or {}
    title = body.get("title", "").strip()
    if not title:
        return jsonify({"error": "missing title"}), 400

    paper = search_paper(title)
    if not paper:
        return jsonify({"error": "not found", "title": title}), 404

    authors = get_paper_authors(paper)
    return jsonify({
        "title":    paper.get("title", title),
        "abstract": paper.get("abstract", ""),
        "venue":    paper.get("venue", ""),
        "year":     paper.get("year"),
        "authors":  authors,
    })


# ── Academic generate API ──

@app.route("/api/generate-academic", methods=["POST"])
def generate_academic():
    body     = request.json or {}
    papers   = body.get("papers", [])
    config   = body.get("config", {})
    api_key  = request.headers.get("X-Api-Key", "")
    language = config.get("language", "zh")
    conference = config.get("conference", "")

    config["mode"] = "academic"

    def stream():
        for paper in papers:
            title    = paper.get("title", "")
            abstract = paper.get("abstract", "")
            venue    = paper.get("venue", "") or conference
            authors  = paper.get("authors", [])

            for author in authors:
                name = author.get("name", "").strip()
                if not name:
                    continue
                affiliation = author.get("affiliation", "")

                yield f"data: {json.dumps({'status': 'generating', 'name': name, 'paper': title})}\n\n"

                message = generate_academic_message(
                    name, affiliation, title, abstract, venue, api_key, config
                )

                add_history(name, "", conference, language, message)

                yield f"data: {json.dumps({'status': 'done', 'name': name, 'paper': title, 'affiliation': affiliation, 'message': message})}\n\n"

                time.sleep(0.1)  # gentle pacing for API rate limits

        yield f"data: {json.dumps({'status': 'complete'})}\n\n"

    return Response(stream_with_context(stream()), mimetype="text/event-stream")


# ── Generate API ──

@app.route("/api/generate", methods=["POST"])
def generate():
    body      = request.json or {}
    people    = body.get("people", [])
    config    = body.get("config", {})
    api_key   = request.headers.get("X-Api-Key", "")
    community = config.get("community", "")
    language  = config.get("language", "zh")

    mode = config.get("mode", "community")

    def stream():
        for person in people:
            name   = person.get("name", "").strip()
            url    = person.get("url",  "").strip()
            github = person.get("github", "").strip()
            person_context = person.get("context", "").strip()
            if not name:
                continue

            yield f"data: {json.dumps({'status': 'searching',  'name': name})}\n\n"

            if mode == "custom":
                # Custom mode: use provided context directly, skip GitHub search
                context = person_context or ""
            else:
                context = search_person(name, url, community, github)

            yield f"data: {json.dumps({'status': 'generating', 'name': name})}\n\n"
            message = generate_message(name, url, context, api_key, config)

            # Save to history
            add_history(name, url, community, language, message)

            yield f"data: {json.dumps({'status': 'done', 'name': name, 'url': url, 'message': message})}\n\n"

        yield f"data: {json.dumps({'status': 'complete'})}\n\n"

    return Response(stream_with_context(stream()), mimetype="text/event-stream")


# ── Lab Sourcer ──

@app.route("/lab-sourcer")
def lab_sourcer_redirect():
    return redirect("/discover")


@app.route("/api/institutions")
def api_institutions():
    return jsonify(get_institutions())


@app.route("/api/faculty", methods=["POST"])
def api_faculty():
    data = request.json or {}
    institution = data.get("institution", "").strip()
    mode = data.get("mode", "ai").strip()  # "ai" or "all"
    if not institution:
        return jsonify({"error": "No institution provided"}), 400
    if mode == "all":
        faculty = get_all_faculty(institution)
    else:
        faculty = get_ai_faculty(institution)
    for f in faculty:
        f["area_labels"] = [AREA_LABELS.get(a, a) for a in f.get("areas", [])]
    return jsonify(faculty)


import queue
import threading


def _scrape_lab_threaded(homepage, name, api_key, base_url, model):
    q = queue.Queue()
    result = [None, None]

    def run():
        try:
            members = scrape_lab(
                homepage, name,
                api_key=api_key, base_url=base_url, model=model,
                status_cb=lambda msg: q.put(("status", msg))
            )
            result[0] = members
        except Exception as e:
            result[1] = str(e)
        q.put(("done", None))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return q, result


@app.route("/api/lab-sourcer/scrape-batch", methods=["POST"])
def api_lab_scrape_batch():
    data = request.json or {}
    professors = data.get("professors", [])
    api_key = data.get("api_key", "")
    base_url = data.get("base_url", "")
    model = data.get("model", "")

    if not professors:
        return jsonify({"error": "No professors provided"}), 400

    def generate():
        total = len(professors)
        all_results = []
        for i, prof in enumerate(professors):
            name = prof.get("name", "")
            homepage = prof.get("homepage", "")
            yield f"data: {json.dumps({'type': 'progress', 'current': i+1, 'total': total, 'professor': name})}\n\n"

            if not homepage:
                yield f"data: {json.dumps({'type': 'skip', 'professor': name, 'reason': 'No homepage'})}\n\n"
                continue

            q, result = _scrape_lab_threaded(homepage, name, api_key, base_url, model)
            while True:
                try:
                    kind, msg = q.get(timeout=2)
                except Exception:
                    yield f"data: {json.dumps({'type': 'status', 'message': f'Still processing {name}...'})}\n\n"
                    continue
                if kind == "status":
                    yield f"data: {json.dumps({'type': 'status', 'message': msg})}\n\n"
                elif kind == "done":
                    break

            if result[1]:
                yield f"data: {json.dumps({'type': 'error', 'professor': name, 'message': result[1]})}\n\n"
            else:
                members = result[0] or []
                all_results.append({"professor": name, "members": members})
                yield f"data: {json.dumps({'type': 'result', 'professor': name, 'count': len(members), 'members': members})}\n\n"

        yield f"data: {json.dumps({'type': 'done', 'total_professors': total, 'total_members': sum(len(r['members']) for r in all_results)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/lab-sourcer/scrape-dept", methods=["POST"])
def api_lab_scrape_dept():
    data = request.json or {}
    url = data.get("url", "").strip()
    api_key = data.get("api_key", "")
    base_url = data.get("base_url", "")
    model = data.get("model", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        faculty = scrape_department_page(url, api_key=api_key, base_url=base_url, model=model)
        return jsonify(faculty)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/lab-sourcer/scrape-selected", methods=["POST"])
def api_lab_scrape_selected():
    data = request.json or {}
    professors = data.get("professors", [])
    api_key = data.get("api_key", "")
    base_url = data.get("base_url", "")
    model = data.get("model", "")

    if not professors:
        return jsonify({"error": "No professors selected"}), 400

    def generate():
        total = len(professors)
        all_results = []
        for i, prof in enumerate(professors):
            name = prof.get("name", "").strip()
            homepage = prof.get("personal_page", "").strip() or prof.get("homepage", "").strip()
            yield f"data: {json.dumps({'type': 'progress', 'current': i+1, 'total': total, 'professor': name})}\n\n"

            if not homepage:
                yield f"data: {json.dumps({'type': 'skip', 'professor': name, 'reason': 'No homepage URL'})}\n\n"
                continue

            q, result = _scrape_lab_threaded(homepage, name, api_key, base_url, model)
            while True:
                try:
                    kind, msg = q.get(timeout=2)
                except Exception:
                    yield f"data: {json.dumps({'type': 'status', 'message': f'Still processing {name}...'})}\n\n"
                    continue
                if kind == "status":
                    yield f"data: {json.dumps({'type': 'status', 'message': msg})}\n\n"
                elif kind == "done":
                    break

            if result[1]:
                yield f"data: {json.dumps({'type': 'error', 'professor': name, 'message': result[1]})}\n\n"
            else:
                members = result[0] or []
                students = [m for m in members if m["name"].lower().strip() != name.lower().strip()]
                all_results.append({"professor": name, "members": students})
                yield f"data: {json.dumps({'type': 'result', 'professor': name, 'count': len(students), 'members': students})}\n\n"

        yield f"data: {json.dumps({'type': 'done', 'total_professors': total, 'total_members': sum(len(r['members']) for r in all_results)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/lab-sourcer/scrape-manual", methods=["POST"])
def api_lab_scrape_manual():
    data = request.json or {}
    professors = data.get("professors", [])
    api_key = data.get("api_key", "")
    base_url = data.get("base_url", "")
    model = data.get("model", "")

    if not professors:
        return jsonify({"error": "No professors provided"}), 400

    def generate():
        total = len(professors)
        all_results = []
        for i, prof in enumerate(professors):
            name = prof.get("name", "").strip()
            homepage = prof.get("homepage", "").strip()
            yield f"data: {json.dumps({'type': 'progress', 'current': i+1, 'total': total, 'professor': name})}\n\n"

            if not homepage:
                yield f"data: {json.dumps({'type': 'skip', 'professor': name, 'reason': 'No homepage'})}\n\n"
                continue

            q, result = _scrape_lab_threaded(homepage, name, api_key, base_url, model)
            while True:
                try:
                    kind, msg = q.get(timeout=2)
                except Exception:
                    yield f"data: {json.dumps({'type': 'status', 'message': f'Still processing {name}...'})}\n\n"
                    continue
                if kind == "status":
                    yield f"data: {json.dumps({'type': 'status', 'message': msg})}\n\n"
                elif kind == "done":
                    break

            if result[1]:
                yield f"data: {json.dumps({'type': 'error', 'professor': name, 'message': result[1]})}\n\n"
            else:
                members = result[0] or []
                all_results.append({"professor": name, "members": members})
                yield f"data: {json.dumps({'type': 'result', 'professor': name, 'count': len(members), 'members': members})}\n\n"

        yield f"data: {json.dumps({'type': 'done', 'total_professors': total, 'total_members': sum(len(r['members']) for r in all_results)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/lab-sourcer/import", methods=["POST"])
def api_lab_import():
    """Import discovered lab members into the talent pool with dedup."""
    data = request.json or {}
    members = data.get("members", [])

    if not members:
        return jsonify({"error": "No members provided"}), 400

    imported = 0
    skipped = 0
    failed = 0
    details = []

    conn = get_conn()

    institution = data.get("institution", "")

    for m in members:
        name = (m.get("name") or "").strip()
        if not name:
            failed += 1
            continue

        email = (m.get("email") or "").strip()
        personal_page = (m.get("personal_page") or "").strip()
        role = (m.get("role") or "").strip()
        research_area = (m.get("research_area") or "").strip()
        source = (m.get("source") or "").strip()
        advisor = (m.get("advisor") or source).strip()
        expected_graduation = m.get("expected_graduation")
        # Per-member institution overrides top-level institution
        member_institution = (m.get("institution") or "").strip()
        if member_institution:
            institution_for_row = member_institution
        else:
            institution_for_row = institution

        # Split name into first/last
        name_parts = name.split()
        first_name = name_parts[0] if name_parts else ""
        last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

        # Dedup: check by email (if available) or by first_name + last_name
        existing = None
        if email:
            existing = conn.execute(
                "SELECT id FROM people WHERE email = ?", (email,)
            ).fetchone()
        if not existing:
            existing = conn.execute(
                "SELECT id FROM people WHERE LOWER(first_name) = LOWER(?) AND LOWER(last_name) = LOWER(?)",
                (first_name, last_name),
            ).fetchone()

        if existing:
            skipped += 1
            existing_id = existing["id"]
            details.append({"name": name, "status": "skipped", "reason": "duplicate", "person_id": existing_id})
            # Still add tags to existing person (e.g., new conference tag)
            member_tags = m.get("tags", [])
            for tag_info in member_tags:
                if isinstance(tag_info, str):
                    add_person_tag(existing_id, tag_info, category="conference", source="auto")
                elif isinstance(tag_info, dict):
                    add_person_tag(existing_id, tag_info["name"],
                                   category=tag_info.get("category", "custom"),
                                   source=tag_info.get("source", "auto"))
            continue

        try:
            # Use a placeholder linkedin_url since we don't have one
            placeholder_url = f"lab-sourcer://{name.lower().replace(' ', '-')}"
            cur = conn.execute(
                """INSERT INTO people (first_name, last_name, linkedin_url, email, title, headline, industry, notes,
                   source_type, advisor, institution, personal_page, expected_graduation, research_area)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (first_name, last_name, placeholder_url, email or None,
                 role, research_area, f"Source: {source}" if source else None,
                 "lab_sourcer",
                 m.get("source_type", "academic"), advisor or None, institution_for_row or None,
                 personal_page or None, expected_graduation, research_area or None),
            )
            person_id = cur.lastrowid
            imported += 1
            details.append({"name": name, "status": "imported", "person_id": person_id})

            # Update FTS index for the new person
            conn.commit()
            update_fts_for_person(person_id)

            # Auto-tag if tags provided
            member_tags = m.get("tags", [])
            for tag_info in member_tags:
                if isinstance(tag_info, str):
                    add_person_tag(person_id, tag_info, category="conference", source="auto")
                elif isinstance(tag_info, dict):
                    add_person_tag(person_id, tag_info["name"],
                                   category=tag_info.get("category", "custom"),
                                   source=tag_info.get("source", "auto"))
        except Exception as e:
            failed += 1
            details.append({"name": name, "status": "failed", "reason": str(e)})

    # Also handle top-level tags (applied to all imported members)
    global_tags = data.get("tags", [])
    if global_tags:
        for d in details:
            if d["status"] == "imported" and d.get("person_id"):
                for tag in global_tags:
                    if isinstance(tag, str):
                        add_person_tag(d["person_id"], tag, category="conference", source="auto")

    conn.commit()
    conn.close()

    return jsonify({
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "details": details,
    })


# ── Tag API ──

@app.route("/api/tags", methods=["GET"])
def api_tags():
    """Get all tags with counts."""
    return jsonify(get_all_tags())


@app.route("/api/person/<int:person_id>/tags", methods=["GET"])
def api_person_tags(person_id):
    """Get tags for a person."""
    return jsonify(get_person_tags(person_id))


@app.route("/api/person/<int:person_id>/tags", methods=["POST"])
def api_add_person_tag(person_id):
    """Add a tag to a person."""
    data = request.json or {}
    tag_name = data.get("name", "").strip()
    if not tag_name:
        return jsonify({"error": "Tag name required"}), 400
    category = data.get("category", "custom")
    source = data.get("source", "manual")
    add_person_tag(person_id, tag_name, category=category, source=source)
    return jsonify({"ok": True, "tags": get_person_tags(person_id)})


@app.route("/api/person/<int:person_id>/tags/<int:tag_id>", methods=["DELETE"])
def api_remove_person_tag(person_id, tag_id):
    """Remove a tag from a person."""
    remove_person_tag(person_id, tag_id)
    return jsonify({"ok": True, "tags": get_person_tags(person_id)})


@app.route("/api/lab-sourcer/batch-fast-scrape", methods=["POST"])
def api_lab_batch_fast_scrape():
    """Batch scrape: for multiple professors, either homepage-scrape or web-search. SSE.

    strategy: "fast" (homepage scrape) or "search" (web search via agent)
    """
    data = request.json or {}
    professors = data.get("professors", [])
    api_key = (data.get("api_key") or "").strip()
    base_url = (data.get("base_url") or "").strip()
    model = (data.get("model") or "").strip()
    strategy = (data.get("strategy") or "fast").strip()

    if not professors:
        return jsonify({"error": "No professors provided"}), 400
    if not api_key:
        return jsonify({"error": "No API key provided."}), 400

    def generate():
        total = len(professors)
        all_members = []
        key_preview = api_key[:12] if api_key else "(empty)"
        key_bytes = [hex(ord(c)) for c in api_key[:6]] if api_key else []
        url_preview = base_url[:30] if base_url else "(empty)"
        yield f"data: {json.dumps({'type': 'log', 'message': f'Config: strategy={strategy}, model={model}, base_url={url_preview}, key={key_preview}... (len={len(api_key)}, bytes={key_bytes})'})}\n\n"
        for i, prof in enumerate(professors):
            name = prof.get("name", "")
            homepage = prof.get("homepage", "")

            yield f"data: {json.dumps({'type': 'progress', 'current': i + 1, 'total': total, 'message': f'[{i+1}/{total}] {name}'})}\n\n"

            if strategy == "search":
                # Web search mode: use agent_scraper
                query = f"{name} lab members research group"
                if homepage:
                    query += f" site:{homepage.split('/')[2] if '/' in homepage else homepage}"
                try:
                    log_msgs = []
                    def batch_status_cb(msg):
                        log_msgs.append(msg)

                    members = run_agent_search(
                        query, api_key,
                        status_cb=batch_status_cb,
                        model=model,
                        base_url=base_url,
                        max_turns=15,
                    )
                    for m in members:
                        m["source"] = name
                        all_members.append(m)
                    yield f"data: {json.dumps({'type': 'members', 'data': members})}\n\n"
                    yield f"data: {json.dumps({'type': 'log', 'message': f'{name}: {len(members)} members found (web search)'})}\n\n"
                except Exception as e:
                    yield f"data: {json.dumps({'type': 'log', 'message': f'{name}: error - {e}', 'level': 'error'})}\n\n"
            else:
                # Fast mode: homepage scrape
                if not homepage:
                    yield f"data: {json.dumps({'type': 'log', 'message': f'Skipping {name}: no homepage', 'level': 'warn'})}\n\n"
                    continue
                try:
                    members = fast_scrape_lab(
                        homepage, name,
                        api_key=api_key, base_url=base_url, model=model,
                        status_cb=lambda msg: None,
                    )
                    for m in members:
                        m["source"] = name
                        all_members.append(m)
                    yield f"data: {json.dumps({'type': 'members', 'data': members})}\n\n"
                    yield f"data: {json.dumps({'type': 'log', 'message': f'{name}: {len(members)} members found'})}\n\n"
                except Exception as e:
                    yield f"data: {json.dumps({'type': 'log', 'message': f'{name}: error - {e}', 'level': 'error'})}\n\n"

        yield f"data: {json.dumps({'type': 'done', 'message': f'Complete: {len(all_members)} members from {total} professors'})}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/lab-sourcer/fast-scrape", methods=["POST"])
def api_lab_fast_scrape():
    """Two-stage lab scrape: fetch pages then LLM extract. Streams SSE."""
    data = request.json or {}
    homepage = (data.get("homepage") or "").strip()
    professor_name = (data.get("professor_name") or "").strip()
    api_key = (data.get("api_key") or "").strip()
    base_url = (data.get("base_url") or "").strip()
    model = (data.get("model") or "").strip()

    if not homepage:
        return jsonify({"error": "No homepage URL provided"}), 400
    if not api_key:
        return jsonify({"error": "No API key provided."}), 400

    q = queue.Queue()
    result_holder = [None, None]

    def run():
        try:
            members = fast_scrape_lab(
                homepage, professor_name,
                api_key=api_key, base_url=base_url, model=model,
                status_cb=lambda msg: q.put(("status", msg)),
            )
            result_holder[0] = members
        except Exception as e:
            result_holder[1] = str(e)
        q.put(("done", None))

    t = threading.Thread(target=run, daemon=True)
    t.start()

    def generate():
        while True:
            try:
                kind, payload = q.get(timeout=120)
            except Exception:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Timeout'})}\n\n"
                break
            if kind == "status":
                yield f"data: {json.dumps({'type': 'status', 'message': payload})}\n\n"
            elif kind == "done":
                if result_holder[1]:
                    yield f"data: {json.dumps({'type': 'error', 'message': result_holder[1]})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'result', 'members': result_holder[0] or []})}\n\n"
                break

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/lab-sourcer/agent-search", methods=["POST"])
def api_lab_agent_search():
    """Agentic web search — streams progress via SSE."""
    data = request.json or {}
    query = (data.get("query") or "").strip()
    api_key = (data.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")).strip()
    model = (data.get("model") or "claude-sonnet-4-20250514").strip()
    base_url = (data.get("base_url") or "").strip()

    if not query:
        return jsonify({"error": "No query provided"}), 400
    if not api_key:
        return jsonify({"error": "No API key provided."}), 400

    q = queue.Queue()
    result_holder = [None, None]  # [members, error]

    def run():
        try:
            members = run_agent_search(
                query, api_key,
                status_cb=lambda msg: q.put(("status", msg)),
                model=model,
                base_url=base_url,
            )
            result_holder[0] = members
        except Exception as e:
            result_holder[1] = str(e)
        q.put(("done", None))

    t = threading.Thread(target=run, daemon=True)
    t.start()

    def generate():
        while True:
            try:
                kind, payload = q.get(timeout=120)
            except Exception:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Timeout'})}\n\n"
                break
            if kind == "status":
                yield f"data: {json.dumps({'type': 'status', 'message': payload})}\n\n"
            elif kind == "done":
                if result_holder[1]:
                    yield f"data: {json.dumps({'type': 'error', 'message': result_holder[1]})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'result', 'members': result_holder[0] or []})}\n\n"
                break

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Enrich from Personal Pages ────────────────────────────────────────────

@app.route("/api/lab-sourcer/enrich-pages", methods=["POST"])
def api_enrich_pages():
    """Enrich members by fetching their personal pages."""
    data = request.json or {}
    members = data.get("members", [])
    api_key = data.get("api_key", "")
    base_url = data.get("base_url", "")
    model = data.get("model", "")

    if not members:
        return jsonify({"error": "No members provided"}), 400
    if not api_key:
        return jsonify({"error": "API key required"}), 400

    def generate():
        messages = []
        def status_cb(msg):
            messages.append(msg)

        enriched = enrich_from_personal_pages(
            members, api_key=api_key, base_url=base_url,
            model=model, status_cb=status_cb
        )

        # Stream status messages then final result
        for msg in messages:
            yield f"data: {json.dumps({'type': 'status', 'message': msg})}\n\n"
        yield f"data: {json.dumps({'type': 'result', 'members': enriched})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Conference Sourcing ───────────────────────────────────────────────────
# Multi-phase pipeline: papers → Chinese filter → arXiv HTML → classify

# In-memory state for the current conference sourcing session
_conf_session = {
    'papers': [],           # all loaded papers
    'chinese_papers': [],   # after Chinese filter
    'author_data': {},      # enriched author data
    'arxiv_cache': {},      # title -> arxiv_id
}


@app.route("/api/conference/papers", methods=["POST"])
def api_conference_papers():
    """Phase 1: Fetch paper list from OpenReview."""
    data = request.json or {}
    venue = data.get("venue", "")
    year = data.get("year", 2025)
    tiers = data.get("tiers", [])

    if venue not in CONFERENCE_VENUES:
        return jsonify({"error": f"Unknown venue: {venue}"}), 400
    if not tiers:
        return jsonify({"error": "No tiers selected"}), 400

    def generate():
        yield f"data: {json.dumps({'type': 'progress', 'message': f'Fetching {venue} {year} papers...'})}\n\n"

        def status_cb(msg):
            pass

        papers = fetch_papers_openreview(venue, year, tiers, status_cb=status_cb)
        _conf_session['papers'] = papers
        _conf_session['chinese_papers'] = []
        _conf_session['author_data'] = {}
        _conf_session['arxiv_cache'] = {}

        from collections import Counter
        tier_counts = Counter(p["venue_tier"] for p in papers)
        stats = {"total": len(papers), "by_tier": dict(tier_counts)}

        yield f"data: {json.dumps({'type': 'papers', 'data': papers, 'stats': stats})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'message': f'{len(papers)} unique first authors from {venue} {year}'})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/conference/upload", methods=["POST"])
def api_conference_upload():
    """Phase 1 (alt): Upload Paper Digest JSON or CSV file."""
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    content = file.read().decode("utf-8", errors="replace")
    filename = file.filename or ""

    try:
        if filename.endswith(".json"):
            raw = json.loads(content)
            if isinstance(raw, list):
                papers = parse_paperdigest_json(raw)
            elif isinstance(raw, dict) and 'papers' in raw:
                papers = parse_paperdigest_json(raw['papers'])
            else:
                return jsonify({"error": "Unrecognized JSON format"}), 400
        elif filename.endswith(".csv"):
            papers = parse_paperdigest_csv(content)
        else:
            # Try JSON first, then CSV
            try:
                raw = json.loads(content)
                papers = parse_paperdigest_json(raw if isinstance(raw, list) else raw.get('papers', []))
            except json.JSONDecodeError:
                papers = parse_paperdigest_csv(content)
    except Exception as e:
        return jsonify({"error": f"Parse error: {e}"}), 400

    _conf_session['papers'] = papers
    _conf_session['chinese_papers'] = []
    _conf_session['author_data'] = {}
    _conf_session['arxiv_cache'] = {}

    return jsonify({
        "total": len(papers),
        "sample": papers[:3],
        "message": f"Loaded {len(papers)} papers from {filename}",
    })


@app.route("/api/conference/filter", methods=["POST"])
def api_conference_filter():
    """Phase 2: Apply Chinese surname filter to loaded papers."""
    data = request.json or {}
    paper_indices = data.get("indices")  # optional subset

    papers = _conf_session.get('papers', [])
    if not papers:
        return jsonify({"error": "No papers loaded. Upload or fetch first."}), 400

    if paper_indices is not None:
        papers = [papers[i] for i in paper_indices if 0 <= i < len(papers)]

    chinese_papers, stats = filter_chinese_authors(papers)
    _conf_session['chinese_papers'] = chinese_papers

    # Build summary: papers with Chinese authors + their names
    summary = []
    for p in chinese_papers:
        summary.append({
            'title': p['title'],
            'authors': p['authors'],
            'chinese_authors': p['chinese_authors'],
            'org': p.get('org', ''),
            'rank': p.get('rank', ''),
        })

    return jsonify({
        'total_papers': stats['total'],
        'chinese_papers': stats['chinese'],
        'papers': summary,
    })


@app.route("/api/conference/enrich", methods=["POST"])
def api_conference_enrich():
    """Phase 3: arXiv HTML enrichment (SSE streaming)."""
    chinese_papers = _conf_session.get('chinese_papers', [])
    if not chinese_papers:
        return jsonify({"error": "No Chinese-authored papers. Run filter first."}), 400

    def generate():
        events = []

        def yield_cb(evt):
            events.append(evt)

        author_data = _conf_session.get('author_data', {})
        arxiv_cache = _conf_session.get('arxiv_cache', {})

        yield f"data: {json.dumps({'type': 'progress', 'current': 0, 'total': len(chinese_papers), 'message': f'Starting arXiv enrichment for {len(chinese_papers)} papers...'})}\n\n"

        session = None
        try:
            import socket as _socket
            _socket.setdefaulttimeout(30)
            session = requests.Session()
        except Exception:
            pass

        total = len(chinese_papers)
        parsed = 0
        no_arxiv = 0
        failed = 0
        rate_limited = 0
        consecutive_fail = 0

        for pi, paper in enumerate(chinese_papers):
            title = paper['title']

            # Register Chinese authors
            for idx, aname in enumerate(paper.get('chinese_authors', paper.get('authors', []))):
                from conference_scraper import has_chinese_surname as _hcs
                if not _hcs(aname):
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
                session = requests.Session()
                _socket.setdefaulttimeout(30)

            # Search arXiv ID
            from conference_scraper import search_arxiv_id, parse_arxiv_html, _safe_get, _merge_html_authors
            from conference_scraper import ARXIV_COOKIES, BROWSER_HEADERS

            arxiv_id = arxiv_cache.get(title)
            if not arxiv_id:
                arxiv_id = search_arxiv_id(title, session)
                if arxiv_id and arxiv_id != 'RATE_LIMITED':
                    arxiv_cache[title] = arxiv_id
                elif arxiv_id == 'RATE_LIMITED':
                    rate_limited += 1
                    consecutive_fail += 1
                    if consecutive_fail >= 3:
                        yield f"data: {json.dumps({'type': 'log', 'message': 'arXiv rate limited, stopping', 'level': 'error'})}\n\n"
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
                        yield f"data: {json.dumps({'type': 'log', 'message': f'{consecutive_fail} consecutive timeouts, stopping', 'level': 'error'})}\n\n"
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
                        yield f"data: {json.dumps({'type': 'log', 'message': 'arXiv rate limited, stopping', 'level': 'error'})}\n\n"
                        break
                    time.sleep(15)
                    continue
                elif r.status_code == 404:
                    no_arxiv += 1
                    consecutive_fail = 0
                else:
                    failed += 1
                    consecutive_fail = 0

                time.sleep(4)

            # Progress every 5 papers
            if (pi + 1) % 5 == 0 or pi == total - 1:
                yield f"data: {json.dumps({'type': 'progress', 'current': pi + 1, 'total': total, 'message': f'[{pi+1}/{total}] parsed: {parsed}, no_arxiv: {no_arxiv}'})}\n\n"

        # Save state
        _conf_session['author_data'] = author_data
        _conf_session['arxiv_cache'] = arxiv_cache

        # Build classified results
        results = build_results(author_data)
        yield f"data: {json.dumps({'type': 'results', 'data': results})}\n\n"
        s = results['stats']
        done_msg = f"Done. Parsed: {parsed}, Industry: {s['industry_count']}, Academic: {s['academic_count']}, Unknown: {s['unknown_count']}"
        yield f"data: {json.dumps({'type': 'done', 'message': done_msg})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/conference/results", methods=["GET"])
def api_conference_results():
    """Get current classified results without re-running enrichment."""
    author_data = _conf_session.get('author_data', {})
    if not author_data:
        return jsonify({"error": "No data. Run the pipeline first."}), 400
    results = build_results(author_data)
    return jsonify(results)


@app.route("/api/conference/authors", methods=["POST"])
def api_conference_authors():
    """Phase 2 (OpenReview path): Fetch author profiles and stream as members."""
    data = request.json or {}
    papers = data.get("papers", [])
    venue_label = data.get("venue_label", "")

    if not papers:
        return jsonify({"error": "No papers provided"}), 400

    def generate():
        total = len(papers)
        yield f"data: {json.dumps({'type': 'progress', 'current': 0, 'total': total, 'message': f'Fetching {total} author profiles...'})}\n\n"

        for i, paper in enumerate(papers):
            author_name = paper["authors"][0] if paper.get("authors") else "Unknown"
            author_id = paper["authorids"][0] if paper.get("authorids") else None

            yield f"data: {json.dumps({'type': 'progress', 'current': i + 1, 'total': total, 'message': f'[{i+1}/{total}] {author_name}'})}\n\n"

            profile = None
            if author_id and author_id.startswith("~"):
                profile = fetch_author_profile(author_id)

            member = map_to_member(paper, profile)
            if venue_label:
                member["source"] = f"{venue_label} {member['source']}"

            yield f"data: {json.dumps({'type': 'members', 'data': [member]})}\n\n"

            if not profile:
                yield f"data: {json.dumps({'type': 'log', 'message': f'{author_name}: no profile found', 'level': 'warn'})}\n\n"

        yield f"data: {json.dumps({'type': 'done', 'message': f'Complete: {total} authors processed'})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── Academic Talent Pool ──────────────────────────────────────────────────

@app.route("/academic")
def academic_redirect():
    return redirect("/pool?tab=academic")


@app.route("/api/academic/people", methods=["GET"])
def api_academic_people():
    """List academic candidates with filters."""
    filters = {
        "q": request.args.get("q", "").strip(),
        "institution": request.args.get("institution", "").strip(),
        "advisor": request.args.get("advisor", "").strip(),
        "role": request.args.get("role", "").strip(),
        "grad_min": request.args.get("grad_min", "").strip(),
        "grad_max": request.args.get("grad_max", "").strip(),
        "research_area": request.args.get("research_area", "").strip(),
        "venue": request.args.get("venue", "").strip(),
        "status": request.args.get("status", "").strip(),
    }
    # Remove empty filters
    filters = {k: v for k, v in filters.items() if v}

    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))
    group_by = request.args.get("group_by", "")

    people, total = search_academic(filters, limit=limit, offset=offset)

    if group_by == "advisor":
        groups = {}
        for p in people:
            adv = p.get("advisor") or "Unknown"
            groups.setdefault(adv, []).append(p)
        return jsonify({"groups": groups, "total": total})

    return jsonify({"people": people, "total": total})


@app.route("/api/academic/filters", methods=["GET"])
def api_academic_filters():
    """Get distinct values for filter dropdowns."""
    return jsonify(get_academic_filters())


@app.route("/api/academic/venues", methods=["GET"])
def api_academic_venues():
    """会议/期刊清单 + 每个顶会下的候选人数，给学术板块做会议筛选。"""
    return jsonify({"venues": get_venue_counts()})


@app.route("/api/academic/people/<int:person_id>/status", methods=["PATCH"])
def api_academic_update_status(person_id):
    """Update status of an academic candidate."""
    data = request.json or {}
    new_status = data.get("status", "").strip()
    if not new_status:
        return jsonify({"error": "No status provided"}), 400

    conn = get_conn()
    conn.execute("UPDATE people SET status = ? WHERE id = ? AND source_type = 'academic'",
                 (new_status, person_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/academic/people/<int:person_id>", methods=["DELETE"])
def api_academic_delete(person_id):
    """Delete an academic candidate."""
    conn = get_conn()
    conn.execute("DELETE FROM people WHERE id = ? AND source_type = 'academic'", (person_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("Starting: http://localhost:5055")
    app.run(debug=False, port=5055, threaded=True)
