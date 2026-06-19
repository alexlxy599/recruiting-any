"""批量验证 people.github_url 的身份归属（roadmap v0.8 管线 ①+② 步）。

用法:
    python verify_github.py [--limit 50]

每人验证流程（API 成本最小化，匿名 GitHub 限流 60 次/小时下每人仅 1 次 API 调用）:
    1. GitHub profile（API）→ bio / company / location / blog
    2. profile README（raw.githubusercontent.com，不计 API 限流）
    3. 个人主页（blog 字段指向的站外页面，直接 HTTP 抓取）
    4. 决定性信号: 上述任何文本中出现该候选人的 LinkedIn slug → verified_link
    5. 否则 LLM 仲裁: 库内档案 vs GitHub+主页证据 → llm_confirmed / rejected / uncertain

所有抓到的原文存入 web_snapshots（原始与衍生分离，提取可随时重跑）。
验证等级写入 people.github_verified；发现的个人主页回填 people.personal_page。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse

import requests

import db

# ── 配置 ──

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_env():
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
# 多 token 轮换：GITHUB_TOKENS=tok1,tok2,... 配额按账号叠加
_TOKEN_POOL = [t.strip() for t in os.environ.get("GITHUB_TOKENS", "").split(",") if t.strip()] \
    or ([GITHUB_TOKEN] if GITHUB_TOKEN else [])
_token_i = 0


def _next_token() -> str:
    global _token_i
    if not _TOKEN_POOL:
        return ""
    _token_i = (_token_i + 1) % len(_TOKEN_POOL)
    return _TOKEN_POOL[_token_i]
# 批处理 LLM：优先用 EXTRACT_* 专用配置（如 OpenRouter），回退到主 app 的本地 LM Studio
LLM_BASE = os.environ.get("EXTRACT_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:1234/v1")
LLM_MODEL = os.environ.get("EXTRACT_MODEL") or os.environ.get("DEFAULT_MODEL", "")
LLM_KEY = os.environ.get("EXTRACT_API_KEY") or os.environ.get("OPENAI_API_KEY", "lm-studio")
IS_OPENROUTER = "openrouter" in LLM_BASE.lower()

UA = {"User-Agent": "Mozilla/5.0 (talent-pool-verifier)"}


# ── 抓取 ──

def gh_headers():
    h = {"Accept": "application/vnd.github+json", **UA}
    tok = _next_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def fetch_profile(login: str) -> tuple[dict | None, int]:
    """返回 (profile, rate_remaining)。404 → ({}, n)，限流/出错 → (None, n)。"""
    r = requests.get(f"https://api.github.com/users/{login}", headers=gh_headers(), timeout=10)
    remaining = int(r.headers.get("X-RateLimit-Remaining", "0"))
    if r.status_code == 404:
        return {}, remaining
    if r.status_code != 200:
        return None, remaining
    return r.json(), remaining


def fetch_profile_readme(login: str) -> str:
    try:
        r = requests.get(
            f"https://raw.githubusercontent.com/{login}/{login}/HEAD/README.md",
            headers=UA, timeout=10)
        return r.text[:8000] if r.status_code == 200 else ""
    except requests.RequestException:
        return ""


def fetch_commit_emails(login: str) -> set[str]:
    """从公开 PushEvent 里收集该账号的 commit author email（1 次 API 调用）。"""
    try:
        r = requests.get(f"https://api.github.com/users/{login}/events",
                         params={"per_page": 100}, headers=gh_headers(), timeout=10)
        if r.status_code != 200:
            return set()
        emails = set()
        for ev in r.json():
            if ev.get("type") == "PushEvent":
                for c in ev.get("payload", {}).get("commits", []):
                    em = (c.get("author", {}).get("email") or "").lower()
                    if em and not em.endswith("users.noreply.github.com"):
                        emails.add(em)
        return emails
    except requests.RequestException:
        return set()


def fetch_homepage(url: str) -> tuple[str, str]:
    """返回 (raw_html, plain_text)。"""
    try:
        r = requests.get(url, headers=UA, timeout=12, allow_redirects=True)
        if r.status_code != 200 or "text/html" not in r.headers.get("Content-Type", "text/html"):
            return "", ""
        html = r.text[:200_000]
        text = re.sub(r"(?is)<(script|style|nav|footer).*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return html, text[:8000]
    except requests.RequestException:
        return "", ""


# ── 信号判定 ──

def linkedin_slug(linkedin_url: str) -> str:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", linkedin_url or "", re.I)
    return urllib.parse.unquote(m.group(1)).lower().rstrip("/") if m else ""


def slug_in(slug: str, *texts: str) -> bool:
    if not slug:
        return False
    return any(slug in (t or "").lower() for t in texts)


def normalize_blog(blog: str) -> str:
    blog = (blog or "").strip()
    if not blog:
        return ""
    if not blog.startswith("http"):
        blog = "https://" + blog
    return blog


# ── LLM 仲裁 ──

ADJUDICATE_PROMPT = """You are verifying whether a GitHub account belongs to a specific person from a talent database.

## Person record (from LinkedIn)
{record}

## Evidence gathered from GitHub account "{login}" and linked pages
{evidence}

Compare facts: employer history, education, location, research/tech direction, name. Career trajectories rarely coincide between different people; a name match alone proves nothing.

Respond with ONLY a JSON object, no other text:
{{"verdict": "confirmed" | "rejected" | "uncertain", "reason": "<one sentence citing the specific matching or conflicting facts>"}}

- "confirmed": at least two independent facts match (e.g. same employer AND same field), with no conflicts
- "rejected": clear conflict (different career, different person's name, incompatible location/field)
- "uncertain": not enough evidence either way"""


def llm_adjudicate(record: str, login: str, evidence: str) -> tuple[str, str]:
    prompt = ADJUDICATE_PROMPT.format(record=record, login=login, evidence=evidence[:6000])
    try:
        r = requests.post(
            f"{LLM_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_KEY}"},
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 6144,
            },
            timeout=180,
        )
        content = r.json()["choices"][0]["message"]["content"]
        content = re.sub(r"(?s)<think>.*?</think>", "", content).strip()
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return "uncertain", "LLM 输出无法解析"
        out = json.loads(m.group(0))
        verdict = out.get("verdict", "uncertain")
        if verdict not in ("confirmed", "rejected", "uncertain"):
            verdict = "uncertain"
        return verdict, out.get("reason", "")
    except Exception as e:
        return "uncertain", f"LLM 调用失败: {e}"


def build_record(p: dict) -> str:
    lines = [
        f"Name: {p['first_name']} {p['last_name']}".strip(),
        f"Current: {p.get('title') or ''} @ {p.get('company') or ''}",
        f"Headline: {p.get('headline') or ''}",
        f"Location: {p.get('location') or ''}",
    ]
    if p.get("institution"):
        lines.append(f"Institution: {p['institution']}")
    if p.get("research_area"):
        lines.append(f"Research area: {p['research_area']}")
    for e in (p.get("experiences") or [])[:5]:
        lines.append(f"Experience: {e.get('title','')} @ {e.get('company','')}")
    for e in (p.get("educations") or [])[:3]:
        lines.append(f"Education: {e.get('degree','')} {e.get('field') or ''} @ {e.get('school','')}")
    return "\n".join(l for l in lines if l.split(":", 1)[-1].strip(" @"))


def build_evidence(profile: dict, readme: str, homepage_url: str, homepage_text: str) -> str:
    parts = []
    if profile:
        parts.append("GitHub profile: " + json.dumps({
            k: profile.get(k) for k in
            ("name", "company", "location", "bio", "blog", "email", "followers", "public_repos")
            if profile.get(k)
        }, ensure_ascii=False))
    if readme:
        parts.append(f"GitHub profile README:\n{readme[:2500]}")
    if homepage_text:
        parts.append(f"Personal homepage ({homepage_url}):\n{homepage_text[:3000]}")
    return "\n\n".join(parts) or "(no evidence beyond the username)"


# ── 主流程 ──

def verify_person(p: dict) -> dict:
    pid = p["id"]
    name = f"{p['first_name']} {p['last_name']}".strip()
    m = re.search(r"github\.com/([^/?#]+)", p["github_url"] or "", re.I)
    if not m:
        db.set_github_verified(pid, "not_found")
        return {"id": pid, "name": name, "login": "", "verdict": "not_found",
                "method": "bad_url", "reason": p["github_url"]}
    login = m.group(1)

    profile, remaining = fetch_profile(login)
    if profile is None:
        return {"id": pid, "name": name, "login": login, "verdict": "SKIPPED",
                "method": "api_error", "reason": f"rate remaining={remaining}", "remaining": remaining}
    if profile == {}:
        db.set_github_verified(pid, "not_found")
        return {"id": pid, "name": name, "login": login, "verdict": "not_found",
                "method": "404", "reason": "GitHub 账号不存在", "remaining": remaining}

    db.add_snapshot(pid, "github_profile", f"https://github.com/{login}",
                    json.dumps(profile, ensure_ascii=False))
    readme = fetch_profile_readme(login)
    if readme:
        db.add_snapshot(pid, "github_readme",
                        f"https://github.com/{login}/{login}", readme)

    # 信号 1: profile 公开邮箱与库内邮箱一致 → verified_email
    person_email = (p.get("email") or "").lower().strip()
    if person_email and (profile.get("email") or "").lower() == person_email:
        db.set_github_verified(pid, "verified_email")
        return {"id": pid, "name": name, "login": login, "verdict": "verified_email",
                "method": "profile_email", "reason": person_email, "remaining": remaining}

    slug = linkedin_slug(p.get("linkedin_url", ""))
    blog = normalize_blog(profile.get("blog", ""))

    # 个人主页：blog 字段非社交链接时抓取
    homepage_url, homepage_html, homepage_text = "", "", ""
    if blog and not re.search(r"(github\.com|twitter\.com|x\.com)", blog, re.I):
        if "linkedin.com" in blog.lower():
            if slug_in(slug, blog):
                db.set_github_verified(pid, "verified_link")
                return {"id": pid, "name": name, "login": login, "verdict": "verified_link",
                        "method": "blog=linkedin", "reason": blog, "remaining": remaining}
        else:
            homepage_url = blog
            homepage_html, homepage_text = fetch_homepage(blog)
            if homepage_text:
                db.add_snapshot(pid, "homepage", homepage_url, homepage_text)
                db.set_personal_page(pid, homepage_url)

    # 信号 2: LinkedIn slug 出现在任何证据里（主页搜 HTML 原文，链接在 href 里）
    if slug_in(slug, profile.get("bio", ""), blog, readme, homepage_html):
        db.set_github_verified(pid, "verified_link")
        return {"id": pid, "name": name, "login": login, "verdict": "verified_link",
                "method": "linkedin_backlink", "reason": f"slug '{slug}' 出现在 GitHub/主页",
                "remaining": remaining}

    # 信号 3: commit author email 与库内邮箱一致 → verified_email（多 1 次 API 调用）
    commit_emails = fetch_commit_emails(login) if person_email else set()
    if person_email and person_email in commit_emails:
        db.set_github_verified(pid, "verified_email")
        return {"id": pid, "name": name, "login": login, "verdict": "verified_email",
                "method": "commit_email", "reason": person_email, "remaining": remaining}

    # 信号 4: LLM 仲裁（commit 邮箱域名也作为证据，如 @google.com 对应现雇主）
    record = build_record(p)
    evidence = build_evidence(profile, readme, homepage_url, homepage_text)
    if commit_emails:
        evidence += "\n\nCommit author emails on this account: " + ", ".join(sorted(commit_emails)[:5])
    verdict, reason = llm_adjudicate(record, login, evidence)
    level = {"confirmed": "llm_confirmed", "rejected": "rejected"}.get(verdict, "uncertain")
    db.set_github_verified(pid, level)
    return {"id": pid, "name": name, "login": login, "verdict": level,
            "method": "llm", "reason": reason, "remaining": remaining,
            "had_homepage": bool(homepage_text), "had_readme": bool(readme)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    db.init_db()
    people = db.get_unverified_github_people(args.limit)
    print(f"待验证: {len(people)} 人 (model={LLM_MODEL}, github_token={'yes' if GITHUB_TOKEN else 'no'})")

    results = []
    for i, p in enumerate(people, 1):
        t0 = time.time()
        r = verify_person(p)
        results.append(r)
        print(f"[{i}/{len(people)}] #{r['id']} {r['name']} @{r['login']} → {r['verdict']}"
              f" ({r['method']}, {time.time()-t0:.0f}s) {r.get('reason','')[:80]}", flush=True)
        if r["verdict"] == "SKIPPED" and r.get("remaining", 1) == 0:
            print("GitHub API 限流耗尽，提前停止。", flush=True)
            break
        time.sleep(1)

    report_path = os.path.join(ROOT, "data", "github_verify_report.json")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    json.dump(results, open(report_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("\n=== 汇总 ===")
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    print(f"报告: {report_path}")


if __name__ == "__main__":
    main()
