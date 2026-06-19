"""对已验证 GitHub 身份的人抓取 repo 列表并生成画像摘要（v0.8 管线 ②原始抓取 + ③结构化提取）。

用法:
    python enrich_github_repos.py            # 抓 repo 快照（每人 1 次 API 调用）
    python enrich_github_repos.py --summarize  # 用本地 LLM 生成中文画像摘要（建议验证批跑完后再跑）

repo 原始 JSON 存 web_snapshots(source='github_repos')；
摘要写 people.github_summary，方向词追加为 tags(source='ai')。
"""

import argparse
import json
import os
import re

import requests

import db
from verify_github import gh_headers, load_env, LLM_BASE, LLM_MODEL, LLM_KEY

load_env()


def top_repos(login: str) -> list[dict]:
    r = requests.get(f"https://api.github.com/users/{login}/repos",
                     params={"sort": "pushed", "per_page": 100},
                     headers=gh_headers(), timeout=15)
    if r.status_code != 200:
        return []
    repos = [{
        "name": x.get("name", ""),
        "description": (x.get("description") or "")[:160],
        "language": x.get("language") or "",
        "stars": x.get("stargazers_count", 0),
        "pushed_at": (x.get("pushed_at") or "")[:10],
    } for x in r.json() if not x.get("fork")]
    repos.sort(key=lambda x: (x["stars"], x["pushed_at"]), reverse=True)
    return repos[:8]


def fetch_repos(people: list[dict]):
    from verify_github import fetch_profile

    for i, p in enumerate(people, 1):
        m = re.search(r"github\.com/([^/?#]+)", p["github_url"] or "", re.I)
        if not m:
            continue
        login = m.group(1)

        # profile 快照（详情页 GitHub 卡片的基础），缺了才抓
        if not db.get_snapshots(p["id"], "github_profile"):
            profile, remaining = fetch_profile(login)
            if profile:
                db.add_snapshot(p["id"], "github_profile", f"https://github.com/{login}",
                                json.dumps(profile, ensure_ascii=False))
            if remaining == 0:
                print("GitHub API 限流耗尽，停止", flush=True)
                return

        if db.get_snapshots(p["id"], "github_repos"):
            continue
        repos = top_repos(login)
        if repos:
            db.add_snapshot(p["id"], "github_repos",
                            f"https://github.com/{login}?tab=repositories",
                            json.dumps(repos, ensure_ascii=False))
        print(f"[{i}/{len(people)}] #{p['id']} @{login}: {len(repos)} repos", flush=True)


SUMMARY_PROMPT = """根据以下 GitHub 信息，为这位候选人写一份招聘视角的画像摘要。

{evidence}

只输出 JSON，不要其他文字:
{{"summary": "<2-3句中文，概括他的技术方向、代表性工作和活跃度。基于事实，不要吹捧>", "directions": ["<技术方向词，2-4个，如 LLM推理优化 / 目标检测>"]}}"""


def _evidence(pid: int) -> str:
    parts = []
    for s in db.get_snapshots(pid):
        if s["source"] == "github_profile":
            try:
                prof = json.loads(s["raw_text"])
                parts.append("Profile: " + json.dumps(
                    {k: prof.get(k) for k in ("name", "bio", "company", "followers") if prof.get(k)},
                    ensure_ascii=False))
            except Exception:
                pass
        elif s["source"] == "github_repos":
            parts.append("Top repos: " + s["raw_text"][:2500])
        elif s["source"] == "github_readme":
            parts.append("README: " + s["raw_text"][:1500])
    return "\n\n".join(parts)


def summarize(people: list[dict], workers: int = 6):
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from enrich_homepage import llm_extract  # 已含关思考 + JSON 模式 + 抢救

    lock = threading.Lock()
    cnt = {"done": 0, "ok": 0}

    def work(p):
        ev = _evidence(p["id"])
        if not ev:
            return p, None
        try:
            return p, llm_extract(SUMMARY_PROMPT.format(evidence=ev))
        except Exception:
            return p, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, p) for p in people]
        for fut in as_completed(futs):
            p, out = fut.result()
            with lock:
                cnt["done"] += 1
                if out and out.get("summary"):
                    conn = db.get_conn()
                    conn.execute("UPDATE people SET github_summary=? WHERE id=?", (out["summary"], p["id"]))
                    conn.commit()
                    conn.close()
                    for d in (out.get("directions") or [])[:5]:
                        if isinstance(d, str) and len(d) < 60:
                            db.add_person_tag(p["id"], d.strip(), category="domain", source="ai")
                    cnt["ok"] += 1
                if cnt["done"] % 50 == 0:
                    print(f"[{cnt['done']}/{len(people)}] ✓{cnt['ok']}", flush=True)
    print(f"完成: {cnt['ok']}/{len(people)} 出摘要", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summarize", action="store_true")
    ap.add_argument("--limit", type=int, default=1000)
    args = ap.parse_args()

    db.init_db()
    conn = db.get_conn()
    if args.summarize:
        # 有 repo 快照但还没出摘要的人
        rows = conn.execute(
            """SELECT id, first_name, last_name, github_url FROM people
               WHERE github_verified IN ('verified_link','verified_email','llm_confirmed','import_high')
                 AND (github_summary IS NULL OR github_summary='')
                 AND id IN (SELECT person_id FROM web_snapshots WHERE source='github_repos')
               ORDER BY id LIMIT ?""", (args.limit,)).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, first_name, last_name, github_url FROM people
               WHERE github_verified IN ('verified_link','verified_email','llm_confirmed','import_high')
               ORDER BY id LIMIT ?""", (args.limit,)).fetchall()
    conn.close()
    people = [dict(r) for r in rows]
    print(f"待处理: {len(people)} 人", flush=True)

    if args.summarize:
        summarize(people)
    else:
        fetch_repos(people)


if __name__ == "__main__":
    main()
