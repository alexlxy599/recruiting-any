"""从个人主页发现 GitHub 账号 —— 只写高可信度的。

与 verify_github.py 的分工:
    本脚本  = 发现(库里没有 github_url 的人,去主页找)
    verify_github.py = 验证(已有 github_url 的人,判断是不是本人)

为什么不做姓名搜索:GitHub search 对中文拼音名精度极低("Wei Zhang" 上百个账号),
且 search 接口限流 30次/分钟。自述链接 + 反向印证的精度高一个量级。

可信度分档(只有 high 才写库):
    high   主页挂了 github.com/X,且 X 的 profile 至少有一项反向印证:
           blog 指回该主页 / email 与库内一致 / name 与库内姓名匹配
    medium 主页挂了链接,但 profile 无任何可印证信息 → 存疑,只记录不写库
    low    只能从 repo 链接反推 owner → 丢弃

用法:
    python3.12 ingest/discover_github.py --limit 150          # 试跑,dry-run
    python3.12 ingest/discover_github.py                      # 全量 dry-run
    python3.12 ingest/discover_github.py --commit             # 写库
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

load_dotenv()
UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120 Safari/537.36")}
OUT = "data/raw/github_discovery.json"

# github.com/<login> —— 排除组织/功能路径与 repo 二级路径
RESERVED = {
    "orgs","organizations","settings","about","features","pricing","topics","collections",
    "trending","events","sponsors","readme","explore","marketplace","apps","login","join",
    "search","notifications","issues","pulls","codespaces","new","blog","site","security",
    "enterprise","team","customer-stories","github","microsoft","google","facebook","apple",
}
GH_RE = re.compile(r"github\.com/([A-Za-z0-9][A-Za-z0-9\-]{0,38})(?:/([A-Za-z0-9._\-]+))?", re.I)


def norm_url(u):
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://(www\.)?", "", u).rstrip("/")
    return u


def norm_name(s):
    return re.sub(r"[^a-z]", "", (s or "").lower())


def extract_handles(html):
    """返回 [(login, 是否出现在 repo 路径中)]，按出现次数排序。"""
    seen = {}
    for m in GH_RE.finditer(html):
        login, sub = m.group(1), m.group(2)
        if login.lower() in RESERVED:
            continue
        d = seen.setdefault(login, {"n": 0, "profile_only": False})
        d["n"] += 1
        if not sub:
            d["profile_only"] = True
    return sorted(seen.items(), key=lambda kv: (-kv[1]["profile_only"], -kv[1]["n"]))


def gh_headers():
    tok = (os.environ.get("GITHUB_TOKEN") or "").strip()
    h = {"Accept": "application/vnd.github+json", **UA}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def fetch_profile(login, headers):
    ck = f"ghprofile:{login}"
    c = db.cache_get(ck)
    if c:
        try:
            return json.loads(c)
        except Exception:
            pass
    try:
        r = requests.get(f"https://api.github.com/users/{login}", headers=headers, timeout=20)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    j = r.json()
    if j.get("type") != "User":          # 组织账号不是候选人
        return None
    keep = {k: j.get(k) for k in
            ("login","name","company","blog","email","location","bio","followers","public_repos")}
    db.cache_set(ck, json.dumps(keep, ensure_ascii=False))
    return keep


def corroborate(person, prof, page_url):
    """返回 (命中的印证信号列表)。任一命中即 high。"""
    hits = []
    # 1. blog 指回本人主页(双向链接)
    if prof.get("blog") and norm_url(prof["blog"]) == norm_url(page_url):
        hits.append("blog↔主页")
    # 2. 公开邮箱与库内一致
    pe = (person["email"] or "").strip().lower()
    if pe and prof.get("email") and prof["email"].strip().lower() == pe:
        hits.append("email一致")
    # 3. 姓名匹配(去空格全等,或姓名互换后全等)
    fn, ln = person["first_name"] or "", person["last_name"] or ""
    cands = {norm_name(f"{fn}{ln}"), norm_name(f"{ln}{fn}")}
    gn = norm_name(prof.get("name") or "")
    if gn and len(gn) >= 4 and gn in cands:
        hits.append("姓名匹配")
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    conn = db.get_conn()
    rows = conn.execute("""
        SELECT id, first_name, last_name, email, personal_page, company, institution
        FROM people
        WHERE (github_url IS NULL OR TRIM(github_url)='' OR github_url LIKE 'pipeline://%')
          AND personal_page LIKE 'http%'
        ORDER BY id""").fetchall()
    people = [dict(r) for r in rows]
    if a.limit:
        people = people[:a.limit]
    print(f"待处理 {len(people)} 人(缺 GitHub 且有主页)\n")

    headers = gh_headers()
    r = requests.get("https://api.github.com/rate_limit", headers=headers, timeout=15)
    core = r.json().get("resources", {}).get("core", {})
    print(f"GitHub 额度 {core.get('remaining')}/{core.get('limit')}\n")

    results, stats = [], {"页面失败":0, "无链接":0, "profile失败":0, "high":0, "medium":0}

    def work(p):
        try:
            resp = requests.get(p["personal_page"], headers=UA, timeout=20)
            if resp.status_code != 200:
                return p, None, "页面失败"
            html = resp.text
        except requests.RequestException:
            return p, None, "页面失败"
        hs = extract_handles(html)
        if not hs:
            return p, None, "无链接"
        return p, hs[:3], None

    pages = []
    done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for f in as_completed([ex.submit(work, p) for p in people]):
            p, hs, err = f.result()
            done += 1
            if err:
                stats[err] += 1
            else:
                pages.append((p, hs))
            if done % 100 == 0:
                print(f"  抓主页 {done}/{len(people)}", flush=True)

    print(f"\n主页抓取完成:找到候选 handle 的 {len(pages)} 人,"
          f"页面失败 {stats['页面失败']},无链接 {stats['无链接']}\n")

    for i, (p, hs) in enumerate(pages, 1):
        best = None
        for login, meta in hs:
            prof = fetch_profile(login, headers)
            if not prof:
                continue
            hits = corroborate(p, prof, p["personal_page"])
            if hits:
                best = (login, prof, hits)
                break
            if best is None:
                best = (login, prof, [])
        if not best:
            stats["profile失败"] += 1
            continue
        login, prof, hits = best
        conf = "high" if hits else "medium"
        stats[conf] += 1
        results.append({
            "id": p["id"],
            "name": f"{p['first_name'] or ''} {p['last_name'] or ''}".strip(),
            "login": login, "conf": conf, "signals": hits,
            "gh_name": prof.get("name") or "", "gh_company": prof.get("company") or "",
            "gh_blog": prof.get("blog") or "", "page": p["personal_page"],
        })
        if i % 100 == 0:
            print(f"  查 profile {i}/{len(pages)}  high={stats['high']}", flush=True)

    os.makedirs("data/raw", exist_ok=True)
    json.dump(results, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    high = [r for r in results if r["conf"] == "high"]
    print(f"\n=== 结果 ===")
    for k, v in stats.items():
        print(f"  {k:10s} {v}")
    print(f"\n可写库(high) {len(high)} 人  → {OUT}")
    from collections import Counter
    print("印证信号分布:", dict(Counter(s for r in high for s in r["signals"])))
    print("\n── high 样本(前 12)──")
    for r in high[:12]:
        print(f"  #{r['id']:<5} {r['name'][:16]:18s} @{r['login']:20s} {'+'.join(r['signals'])}")

    # ── 冲突检测 ──
    # 一个 GitHub 账号只能属于一个人。撞车说明:要么发现错了,要么库里这两条是同一个人。
    # 实测多为后者(库内重复记录),两种情况都不能写 —— 写了就制造歧义。
    from collections import Counter as _C, defaultdict as _dd
    owned = {}
    for r0 in conn.execute("SELECT id, github_url FROM people "
                           "WHERE github_url LIKE 'https://github.com/%'"):
        owned.setdefault(r0[1].rstrip("/").split("/")[-1].lower(), []).append(r0[0])
    in_batch = _C(r["login"].lower() for r in high)

    conflicts, writable = [], []
    for r in high:
        lg = r["login"].lower()
        if lg in owned:
            r["conflict"] = f"已属于库内 #{owned[lg]}"
            conflicts.append(r)
        elif in_batch[lg] > 1:
            r["conflict"] = "本批内多人指向同一账号"
            conflicts.append(r)
        else:
            writable.append(r)

    if conflicts:
        json.dump(conflicts, open("data/raw/github_conflicts.json","w",encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"\n!! 冲突 {len(conflicts)} 条已剔除 → data/raw/github_conflicts.json")
        print("   (多为库内重复人记录,建议单独走去重流程)")
    print(f"实际可写 {len(writable)} 人")

    if not a.commit:
        print("\n(dry-run,未写库。加 --commit 写入)")
        return
    high = writable
    # 用独立的 verified 值,不复用 import_high/verified_link —— 那两个是别的判定路径的
    # 含义,混用会让 /pool/github-review 的分档失真,也没法回溯这批是怎么来的。
    for r in high:
        conn.execute("UPDATE people SET github_url=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (f"https://github.com/{r['login']}", r["id"]))
        strong = {"blog↔主页", "email一致"} & set(r["signals"])
        db.set_github_verified(r["id"], "homepage_link_strong" if strong else "homepage_link")
    conn.commit()
    print(f"\n已写入 {len(high)} 人")
    print("  verified 值: homepage_link_strong(双向链接/邮箱印证) / homepage_link(自述链接+姓名匹配)")


if __name__ == "__main__":
    main()
