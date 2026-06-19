"""引用扩张发现引擎（Semantic Scholar）。

给一个库内高价值种子 → 顺着"谁引用了他的工作"伸进全球学术图 →
挖出库外、在这条线上有分量的人，按"引用深度 × 自身影响力"排序。

用法: python3.12 discover_s2.py <person_id>
可选: 设环境变量 S2_API_KEY 提高限流额度。
"""

import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

S2 = "https://api.semanticscholar.org/graph/v1"
S2_KEY = os.environ.get("S2_API_KEY", "")
HDR = {"x-api-key": S2_KEY} if S2_KEY else {}


def s2(method, path, **kw):
    """带退避重试的 S2 调用（含网络/SSL 异常重试）。"""
    for attempt in range(6):
        try:
            r = requests.request(method, f"{S2}{path}", headers=HDR, timeout=30, **kw)
        except requests.RequestException as e:
            wait = 3 + attempt * 3
            print(f"  网络错误({type(e).__name__})，等 {wait}s...", flush=True)
            time.sleep(wait)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            wait = 3 + attempt * 4
            print(f"  限流，等 {wait}s...", flush=True)
            time.sleep(wait)
            continue
        return None
    return None


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def find_seed_author(person):
    """用种子的论文标题反查其 S2 authorId（经论文消歧，比名字搜索可靠）。"""
    name = f"{person['first_name']} {person['last_name']}"
    titles = [p["title"] for p in person.get("publications", []) if p.get("title")]
    titles.sort(key=len, reverse=True)  # 长标题更独特
    for t in titles[:5]:
        data = s2("GET", "/paper/search",
                  params={"query": t[:120], "limit": 3,
                          "fields": "title,authors,year,citationCount"})
        time.sleep(2)
        if not data or not data.get("data"):
            continue
        for paper in data["data"]:
            if norm(paper["title"])[:40] != norm(t)[:40]:
                continue
            for a in paper.get("authors", []):
                if norm(a.get("name", "")) == norm(name) and a.get("authorId"):
                    return a["authorId"], paper["title"]
    return None, None


def expand(person_id, seed_id_override=None):
    person = db.get_person(person_id)
    name = f"{person['first_name']} {person['last_name']}"
    print(f"种子: {name} ({person.get('institution') or person.get('company') or ''})", flush=True)

    # authorId 缓存：定位一次后存下，避免每次都赌限流
    idcache = os.path.join(os.path.dirname(__file__), "data", "discoveries", f"{person_id}.authorid")
    seed_id, matched = seed_id_override, "（手动指定）"
    if not seed_id and os.path.exists(idcache):
        seed_id = open(idcache).read().strip(); matched = "（缓存）"
    if not seed_id:
        seed_id, matched = find_seed_author(person)
    if not seed_id:
        print("× 未能在 S2 定位该作者（论文标题对不上）"); return
    os.makedirs(os.path.dirname(idcache), exist_ok=True)
    open(idcache, "w").write(seed_id)
    print(f"S2 authorId={seed_id} {matched[:40]}", flush=True)

    papers = s2("GET", f"/author/{seed_id}/papers",
                params={"fields": "title,citationCount,year", "limit": 100})
    time.sleep(2)
    if not papers or not papers.get("data"):
        print("× 取不到论文"); return
    top = sorted(papers["data"], key=lambda p: p.get("citationCount") or 0, reverse=True)[:6]
    print(f"取其被引最高的 {len(top)} 篇，逐篇看引用者...", flush=True)

    # 已在库的名字（用于过滤"已知"）
    conn = db.get_conn()
    known = {norm(r["nm"]) for r in conn.execute(
        "SELECT first_name||' '||last_name AS nm FROM people").fetchall()}
    conn.close()

    cand = {}  # authorId -> {name, cites, infl, papers:set, ctx:(title,year)}
    for p in top:
        c = s2("GET", f"/paper/{p['paperId']}/citations",
               params={"fields": "isInfluential,citingPaper.title,citingPaper.year,citingPaper.authors",
                       "limit": 200})
        time.sleep(2.5)
        if not c:
            continue
        for item in c.get("data", []):
            cp = item.get("citingPaper", {}) or {}
            infl = item.get("isInfluential")
            for a in cp.get("authors", []) or []:
                aid = a.get("authorId")
                if not aid or aid == seed_id:
                    continue
                e = cand.setdefault(aid, {"name": a.get("name", ""), "cites": 0, "infl": 0,
                                          "papers": set(), "ctx": None, "ctx_year": 0})
                e["cites"] += 1
                e["infl"] += 1 if infl else 0
                e["papers"].add(p["title"][:40])
                # 记录"他引用后做的工作"：优先深度引用、其次最新
                yr = cp.get("year") or 0
                if cp.get("title") and (infl or yr > e["ctx_year"]):
                    e["ctx"] = cp["title"]; e["ctx_year"] = yr

    print(f"收集到 {len(cand)} 个引用者，批量取其背景...", flush=True)
    ids = list(cand.keys())
    details = {}
    for i in range(0, len(ids), 200):
        d = s2("POST", "/author/batch",
               params={"fields": "name,hIndex,citationCount,paperCount,affiliations,"
                                 "papers.title,papers.year,papers.citationCount"},
               json={"ids": ids[i:i+200]})
        time.sleep(2)
        for a in (d or []):
            if a:
                details[a["authorId"]] = a

    def rep_paper(det):
        """代表作：近 5 年内被引最高的论文标题。"""
        ps = [p for p in (det.get("papers") or []) if p.get("title")]
        if not ps:
            return ""
        ymax = max((p.get("year") or 0) for p in ps)
        recent = [p for p in ps if (p.get("year") or 0) >= ymax - 5] or ps
        best = max(recent, key=lambda p: p.get("citationCount") or 0)
        return best["title"]

    # 按名字合并 S2 拆分的重复实体
    merged = {}
    for aid, e in cand.items():
        det = details.get(aid, {})
        key = norm(e["name"])
        if not key:
            continue
        m = merged.setdefault(key, {"name": e["name"], "cites": 0, "infl": 0, "papers": set(),
                                    "h": 0, "cit": 0, "aff": "", "ctx": None, "ctx_year": 0, "rep": ""})
        m["cites"] += e["cites"]; m["infl"] += e["infl"]; m["papers"] |= e["papers"]
        m["h"] = max(m["h"], det.get("hIndex") or 0)
        m["cit"] = max(m["cit"], det.get("citationCount") or 0)
        aff = ", ".join((det.get("affiliations") or [])[:1])
        if aff and not m["aff"]:
            m["aff"] = aff
        if e.get("ctx") and e["ctx_year"] >= m["ctx_year"]:
            m["ctx"] = e["ctx"]; m["ctx_year"] = e["ctx_year"]
        rp = rep_paper(det)
        if rp and not m["rep"]:
            m["rep"] = rp

    # 打分：引用深度 × 自身影响力；过滤已在库 / 太资浅
    ranked = []
    for key, m in merged.items():
        if key in known or m["h"] < 3:
            continue
        link = m["infl"] * 3 + (m["cites"] - m["infl"])
        m["score"] = round(link * (1 + min(m["h"], 60) / 25), 1)
        m["papers"] = sorted(m["papers"])
        ranked.append(m)
    ranked.sort(key=lambda x: -x["score"])

    import json
    out = {"seed_id": person_id, "seed_name": name,
           "seed_aff": person.get("institution") or person.get("company") or "",
           "matched_paper": matched, "total": len(ranked), "candidates": ranked[:40]}
    os.makedirs(os.path.join(os.path.dirname(__file__), "data", "discoveries"), exist_ok=True)
    path = os.path.join(os.path.dirname(__file__), "data", "discoveries", f"{person_id}.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"\n=== 发现 {len(ranked)} 个库外候选人（Top 20）===", flush=True)
    for m in ranked[:20]:
        print(f"{m['score']:6.0f} {m['name'][:22]:<22} h{m['h']:>3} 引用{m['cit']:>6}  "
              f"{(m['aff'] or '—')[:30]} | 引种子{m['cites']}次({m['infl']}深度)")
    print(f"已存: {path}", flush=True)


if __name__ == "__main__":
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else 3049
    aid = sys.argv[2] if len(sys.argv) > 2 else None
    expand(pid, aid)
