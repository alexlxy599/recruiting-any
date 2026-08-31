"""个人主页深度补全管线（v0.8③ + 图谱数据收敛）。

用法:
    python3.12 enrich_homepage.py --fetch              # 抓取所有有主页的人 → 快照层（含未验证者，先锁内容）
    python3.12 enrich_homepage.py --extract --limit 5  # LLM 全量提取（只对已验证者），规范化入库
    python3.12 enrich_homepage.py --extract            # 夜批全量

三层流转: 主页HTML → web_snapshots(原始) → extractions(全量JSON) → 规范层
规范层落点: people(advisor/lab/research_area), educations/experiences(source='homepage_ai'),
            projects, collaborations, publications, tags(direction)。
原则: 宁可多不要少（JSON 全存）；先验证再归属（未验证者只抓快照不提取）；
      提取出的事实带 source，不覆盖 manual/linkedin 来源的数据。
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
from verify_github import fetch_homepage, load_env, LLM_BASE, LLM_MODEL, LLM_KEY, IS_OPENROUTER

load_env()

# 已验证的 GitHub 归属等级 —— 只对这些人做提取,避免把别人的资料归到候选人头上。
# homepage_link* 是 ingest/discover_github.py 的发现路径:候选人自己主页上挂的
# GitHub 链接,且 profile 有反向印证(双向链接/邮箱一致/姓名全等)。
VERIFIED = ("verified_link", "verified_email", "llm_confirmed", "import_high",
            "homepage_link_strong", "homepage_link")
EXTRACT_VERSION = 2


# ── 抓取 ──

def fetch_all(workers: int = 10):
    conn = db.get_conn()
    rows = conn.execute(
        """SELECT id, personal_page FROM people
           WHERE personal_page IS NOT NULL AND personal_page != ''
             AND id NOT IN (SELECT DISTINCT person_id FROM web_snapshots WHERE source='homepage')"""
    ).fetchall()
    conn.close()
    print(f"待抓取主页: {len(rows)}", flush=True)

    lock = threading.Lock()
    stats = {"ok": 0, "fail": 0}

    def work(r):
        url = r["personal_page"]
        if not url.startswith("http"):
            url = "https://" + url
        html, text = fetch_homepage(url)
        if text and len(text) > 200:
            db.add_snapshot(r["id"], "homepage", url, text)
            return True
        return False

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, r) for r in rows]
        for i, f in enumerate(as_completed(futs), 1):
            with lock:
                stats["ok" if f.result() else "fail"] += 1
                if i % 50 == 0:
                    print(f"[{i}/{len(rows)}] {stats}", flush=True)
    print("抓取完成:", stats, flush=True)


# ── 提取 ──

EXTRACT_PROMPT = """You are building a structured talent profile from a researcher/engineer's personal homepage.
Extract EVERYTHING valuable. Be exhaustive — missing information is worse than extra information.
Keep original wording where it matters (self description, research statements).

## Person (from our records, for context only)
Name: {name} | Affiliation: {affiliation}

## Homepage text
{homepage}

## GitHub bio (supplementary)
{github_bio}

Output ONLY a JSON object (no other text) with this structure. Use null/[] when absent. Do NOT invent facts.
{{
  "self_description": "<their about/bio text, condensed but faithful, in original language>",
  "summary_zh": "<2-4句中文画像: 谁、在哪、做什么方向、亮点>",
  "current": {{"role": "", "institution": "", "lab": "", "advisor": ""}},
  "research_directions": ["<specific directions, e.g. 'LLM reasoning', not just 'AI'>"],
  "education": [{{"school": "", "degree": "", "field": "", "start_year": null, "end_year": null}}],
  "experience": [{{"organization": "", "title": "", "type": "work|internship", "start_year": null, "end_year": null}}],
  "projects": [{{"name": "", "url": null, "description": "", "direction": ""}}],
  "publications": [{{"title": "", "venue": "", "year": null, "coauthors": ["<ALL listed coauthor names>"]}}],
  "people_mentioned": [{{"name": "", "relation": "advisor|coauthor|labmate|colleague|mentor|mentioned", "context": "", "url": null}}],
  "links": {{"github": null, "linkedin": null, "scholar": null, "twitter": null, "others": []}},
  "signals": {{"job_market": null, "graduation": null, "news": ["<dated updates, esp. internships/job moves>"]}},
  "awards": [], "talks": [], "service": [],
  "extra": "<anything valuable that fits nowhere above>"
}}
Limit publications to the 20 most recent/important (with ALL their coauthors). Include ALL people mentioned with relations."""


def llm_extract(prompt: str) -> dict | None:
    body = {"model": LLM_MODEL, "temperature": 0.1, "max_tokens": 12288,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}}
    if IS_OPENROUTER:
        # 关键：关掉思考，避免把输出预算烧在内部推理上（提取任务无需 reasoning）
        body["reasoning"] = {"enabled": False}
    r = requests.post(
        f"{LLM_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_KEY}",
                 "HTTP-Referer": "https://recruiting-any.local", "X-Title": "Recruiting Any"},
        json=body, timeout=300)
    data = r.json()
    if "choices" not in data:
        raise RuntimeError(data.get("error", data))
    content = re.sub(r"(?s)<think>.*?</think>", "",
                     data["choices"][0]["message"].get("content") or "").strip()
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return None
    raw = m.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return _salvage_json(raw)


def _salvage_json(raw: str) -> dict | None:
    """超长/截断的 JSON 抢救：截到最后一个完整对象处，补齐闭合括号。"""
    for cut in (len(raw), raw.rfind("}") + 1):
        s = raw[:cut]
        # 在字符串外平衡括号
        depth_obj = depth_arr = 0
        in_str = esc = False
        for ch in s:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == "{":
                    depth_obj += 1
                elif ch == "}":
                    depth_obj -= 1
                elif ch == "[":
                    depth_arr += 1
                elif ch == "]":
                    depth_arr -= 1
        fixed = s.rstrip().rstrip(",")
        fixed += "]" * max(depth_arr, 0) + "}" * max(depth_obj, 0)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            continue
    return None


def _s(v):
    """任何模型输出值 → 干净字符串（列表 join、dict 取值、None→None）。"""
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, (list, tuple)):
        parts = [_s(x) for x in v]
        return ", ".join(p for p in parts if p) or None
    if isinstance(v, dict):
        return ", ".join(f"{k}: {_s(val)}" for k, val in v.items() if _s(val)) or None
    return str(v)


def _i(v):
    """→ int 或 None。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def normalize(conn, pid: int, ext: dict):
    """提取 JSON → 规范层。只补空、不覆盖 manual/linkedin 数据；本源数据先清后写，可重跑。"""
    cur = ext.get("current") or {}
    # 注意: summary_zh 属于主页画像（从 extractions 读），不写进 github_summary（那是 GitHub 卡片的字段）
    conn.execute(
        """UPDATE people SET
             advisor = COALESCE(NULLIF(advisor,''), ?),
             lab = COALESCE(NULLIF(lab,''), ?),
             research_area = COALESCE(NULLIF(research_area,''), ?),
             updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (_s(cur.get("advisor")), _s(cur.get("lab")),
         _s(ext.get("research_directions")), pid))

    # 时间线：只在该人没有 LinkedIn 来源数据时补充，且按 source 清重
    conn.execute("DELETE FROM educations WHERE person_id=? AND source='homepage_ai'", (pid,))
    has_edu = conn.execute(
        "SELECT COUNT(*) FROM educations WHERE person_id=?", (pid,)).fetchone()[0]
    if not has_edu:
        for e in (ext.get("education") or [])[:6]:
            if not isinstance(e, dict):
                continue
            conn.execute(
                """INSERT INTO educations (person_id, school, degree, field, start_year, end_year, source)
                   VALUES (?,?,?,?,?,?, 'homepage_ai')""",
                (pid, _s(e.get("school")) or "", _s(e.get("degree")) or "", _s(e.get("field")),
                 _i(e.get("start_year")), _i(e.get("end_year"))))

    conn.execute("DELETE FROM experiences WHERE person_id=? AND source='homepage_ai'", (pid,))
    has_exp = conn.execute(
        "SELECT COUNT(*) FROM experiences WHERE person_id=?", (pid,)).fetchone()[0]
    if not has_exp:
        for i, e in enumerate((ext.get("experience") or [])[:8]):
            if not isinstance(e, dict):
                continue
            conn.execute(
                """INSERT INTO experiences (person_id, position, is_current, title, company,
                                            start_year, end_year, source)
                   VALUES (?,?,?,?,?,?,?, 'homepage_ai')""",
                (pid, i, 1 if i == 0 else 0, _s(e.get("title")) or "",
                 _s(e.get("organization")) or "", _i(e.get("start_year")), _i(e.get("end_year"))))

    conn.execute("DELETE FROM projects WHERE person_id=? AND source='homepage'", (pid,))
    for p in (ext.get("projects") or [])[:10]:
        if isinstance(p, dict) and _s(p.get("name")):
            conn.execute(
                """INSERT INTO projects (person_id, name, url, description, direction, source)
                   VALUES (?,?,?,?,?, 'homepage')""",
                (pid, _s(p.get("name"))[:120], _s(p.get("url")),
                 (_s(p.get("description")) or "")[:300], _s(p.get("direction"))))

    # 关系边：主页提及的人 + 论文共同作者（带上下文，延迟对齐 collaborator_person_id）
    conn.execute("DELETE FROM collaborations WHERE person_id=? AND source='homepage'", (pid,))
    seen = set()
    for m in (ext.get("people_mentioned") or [])[:30]:
        if not isinstance(m, dict):
            continue
        nm = _s(m.get("name"))
        if nm and nm.lower() not in seen:
            seen.add(nm.lower())
            conn.execute(
                """INSERT INTO collaborations (person_id, collaborator_name, relation, context,
                                               collaborator_url, source)
                   VALUES (?,?,?,?,?, 'homepage')""",
                (pid, nm[:80], _s(m.get("relation")) or "mentioned",
                 (_s(m.get("context")) or "")[:200], _s(m.get("url"))))
    for pub in (ext.get("publications") or [])[:20]:
        if not isinstance(pub, dict):
            continue
        for co in (pub.get("coauthors") or [])[:15]:
            co = _s(co)
            if co and co.lower() not in seen:
                seen.add(co.lower())
                conn.execute(
                    """INSERT INTO collaborations (person_id, collaborator_name, relation, context, source)
                       VALUES (?,?, 'coauthor', ?, 'homepage')""",
                    (pid, co[:80], (_s(pub.get("title")) or "")[:150]))

    # 论文（主页来源，按标题去重）
    for pub in (ext.get("publications") or [])[:20]:
        if not isinstance(pub, dict):
            continue
        t = _s(pub.get("title"))
        if not t:
            continue
        dup = conn.execute(
            "SELECT 1 FROM publications WHERE person_id=? AND LOWER(title)=LOWER(?)", (pid, t)).fetchone()
        if not dup:
            conn.execute(
                """INSERT INTO publications (person_id, venue, year, title, source)
                   VALUES (?,?,?,?, 'homepage')""",
                (pid, (_s(pub.get("venue")) or "unknown")[:40], _i(pub.get("year")), t[:300]))

    # 方向标签
    for d in (ext.get("research_directions") or [])[:5]:
        d = _s(d)
        if d and len(d) < 60:
            db.add_person_tag(pid, d.strip(), category="domain", source="ai", conn=conn)


def extract_all(limit: int = 10000):
    conn = db.get_conn()
    rows = conn.execute(
        f"""SELECT p.id, p.first_name || ' ' || p.last_name AS name,
                   COALESCE(p.institution, p.company, '') AS affiliation
            FROM people p
            WHERE p.github_verified IN {VERIFIED}
              AND p.id IN (SELECT DISTINCT person_id FROM web_snapshots WHERE source='homepage')
              AND p.id NOT IN (SELECT person_id FROM extractions
                               WHERE source='homepage' AND version=?)
            ORDER BY p.id LIMIT ?""",
        (EXTRACT_VERSION, limit)).fetchall()
    conn.close()
    workers = 6 if "openrouter" in LLM_BASE.lower() else 1
    print(f"待提取: {len(rows)} 人 (model={LLM_MODEL}, 并发 {workers})", flush=True)

    db_lock = threading.Lock()
    counter = {"done": 0, "ok": 0}

    def work(r):
        snaps = db.get_snapshots(r["id"], "homepage")
        homepage = snaps[0]["raw_text"][:7000] if snaps else ""
        gh_bio = ""
        gp = db.get_snapshots(r["id"], "github_profile")
        if gp:
            try:
                gh_bio = json.loads(gp[0]["raw_text"]).get("bio") or ""
            except Exception:
                pass
        prompt = EXTRACT_PROMPT.format(name=r["name"], affiliation=r["affiliation"],
                                       homepage=homepage, github_bio=gh_bio)
        try:
            ext = llm_extract(prompt)
        except Exception as e:
            return r, None, str(e)[:80]
        return r, ext, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, r) for r in rows]
        for fut in as_completed(futs):
            r, ext, err = fut.result()
            with db_lock:
                counter["done"] += 1
                n = counter["done"]
                if not ext:
                    print(f"[{n}/{len(rows)}] #{r['id']} {r['name']} 失败 {err or '解析'}", flush=True)
                    continue
                try:
                    conn = db.get_conn()
                    conn.execute(
                        """INSERT INTO extractions (person_id, source, version, model, json)
                           VALUES (?, 'homepage', ?, ?, ?)""",
                        (r["id"], EXTRACT_VERSION, LLM_MODEL, json.dumps(ext, ensure_ascii=False)))
                    normalize(conn, r["id"], ext)
                    conn.commit()
                    conn.close()
                except Exception as e:
                    print(f"[{n}/{len(rows)}] #{r['id']} {r['name']} 入库失败: {str(e)[:80]}", flush=True)
                    continue
                counter["ok"] += 1
                if n % 25 == 0 or n == len(rows):
                    print(f"[{n}/{len(rows)}] ✓{counter['ok']} | 最新 #{r['id']} {r['name']} "
                          f"论文{len(ext.get('publications') or [])} 提及{len(ext.get('people_mentioned') or [])}人", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--limit", type=int, default=10000)
    args = ap.parse_args()
    db.init_db()
    if args.fetch:
        fetch_all()
    if args.extract:
        extract_all(args.limit)
