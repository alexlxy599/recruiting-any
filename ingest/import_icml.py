"""ICML 总表导入：只导 GH置信度=high 的人，同步登记界别(sector)和 ICML 发表记录。

用法:
    python3.12 ingest/import_icml.py --dry-run   # 解析并打印样例，不写库
    python3.12 ingest/import_icml.py             # 正式导入

去重: 同表内按 github_url 聚合（一人多篇论文 → 多条 publications）；
与库内按 github_url → linkedin_url → 姓名+机构 三级匹配，命中则补字段不重复建人。
所有导入行 notes='icml2026'，方便整体回溯。
"""

import argparse
import os
import sys

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

XLSX = "/Users/alex/Desktop/ICML 2026 总表0610.xlsx"
SHEET = "全部华人作者"
VENUE, YEAR = "ICML", 2026

ACADEMIC_HINTS = ("university", "institute", "college", "academy", "school", ".edu", "eth z", "epfl", "mila")


def norm_url(u: str) -> str:
    return (u or "").strip().rstrip("/").lower()


def split_name(name: str) -> tuple[str, str]:
    parts = (name or "").strip().split()
    if len(parts) <= 1:
        return name.strip(), ""
    return " ".join(parts[:-1]), parts[-1]


def infer_sector(typ: str, institution: str, org: str, email: str) -> str | None:
    if typ == "学术界":
        return "academic"
    if typ == "工业界":
        return "industry"
    blob = f"{institution} {org} {email}".lower()
    if any(h in blob for h in ACADEMIC_HINTS):
        return "academic"
    if org:
        return "industry"
    return None


def login_matches_name(login: str, name: str) -> bool:
    """姓名拼音与 GitHub login 是否吻合（liujiaheng ↔ Jiaheng Liu）。"""
    import re
    lg = re.sub(r"[^a-z0-9]", "", login.lower())
    parts = [re.sub(r"[^a-z]", "", w.lower()) for w in name.split() if w]
    if len(parts) < 2:
        return False
    first, last = "".join(parts[:-1]), parts[-1]
    candidates = [first + last, last + first, first[0] + last, last + first[0],
                  "".join(p[0] for p in parts[:-1]) + last]
    return any(c and c in lg for c in candidates if len(c) >= 4) or first in lg or (len(last) >= 4 and last in lg)


def resolve_owner(gh: str, names: list[str]) -> str | None:
    """同一 GitHub 挂多个作者名时找真正主人；大小写变体视为同一人。"""
    uniq = {}
    for n in names:
        uniq.setdefault(n.lower(), []).append(n)
    if len(uniq) == 1:
        variants = list(uniq.values())[0]
        return next((v for v in variants if v.istitle()), variants[0])
    login = gh.rstrip("/").split("/")[-1]
    matches = [vs[0] for vs in uniq.values() if login_matches_name(login, vs[0])]
    return matches[0] if len(matches) == 1 else None  # 找不到唯一主人 → 整组丢弃


def parse_sheet() -> tuple[list[dict], int]:
    wb = openpyxl.load_workbook(XLSX, read_only=True)
    ws = wb[SHEET]
    rows = ws.iter_rows(values_only=True)
    idx = {h: i for i, h in enumerate(next(rows))}

    raw = []  # 先收集所有高置信行
    for r in rows:
        def g(col):
            v = r[idx[col]]
            return str(v).strip() if v is not None else ""

        if g("GH置信度").lower() != "high":
            continue
        gh = norm_url(g("GitHub"))
        if not gh or "github.com/" not in gh:
            continue
        raw.append((gh, g("Author"), r, g))

    # 解决"一个 GitHub 挂多个作者名"的源表错误
    from collections import defaultdict
    gh_names = defaultdict(list)
    for gh, author, _, _ in raw:
        gh_names[gh].append(author)
    owners = {gh: resolve_owner(gh, names) for gh, names in gh_names.items()}
    dropped = sum(1 for o in owners.values() if o is None)

    people = {}
    for gh, author, r, g in raw:
        owner = owners[gh]
        if owner is None or author.lower() != owner.lower():
            continue  # 该行的 GitHub 归属判定为错误挂载，跳过

        if gh not in people:
            first, last = split_name(owner)
            typ = g("Type")
            org = g("Org Label")
            sector = infer_sector(typ, g("Institution"), org, g("Email"))
            people[gh] = {
                "first_name": first, "last_name": last,
                "github_url": gh,
                "linkedin_url": norm_url(g("LinkedIn")) or None,
                "email": g("Email") or g("GH Email") or None,
                "personal_page": g("个人主页") or None,
                "institution": org if sector == "academic" else g("Institution"),
                "company": org if sector == "industry" else None,
                "sector": sector,
                "source_type": sector,           # Academic/Industry 视图按它过滤
                "github_verified": "import_high",  # 来源表的高置信，与本地验证等级区分
                "notes": "icml2026",
                "papers": [],
            }
        people[gh]["papers"].append({
            "title": g("Title")[:300],
            "is_first_author": 1 if str(r[idx["Position"]]) == "1" else 0,
        })
    return list(people.values()), dropped


def existing_person_id(conn, p: dict) -> int | None:
    row = conn.execute(
        "SELECT id FROM people WHERE LOWER(RTRIM(github_url,'/')) = ?", (p["github_url"],)
    ).fetchone()
    if row:
        return row["id"]
    if p["linkedin_url"]:
        row = conn.execute("SELECT id FROM people WHERE LOWER(linkedin_url) = ?",
                           (p["linkedin_url"],)).fetchone()
        if row:
            return row["id"]
    row = conn.execute(
        """SELECT id FROM people WHERE first_name = ? AND last_name = ?
           AND (institution = ? OR company = ?)""",
        (p["first_name"], p["last_name"], p["institution"], p["company"]),
    ).fetchone()
    return row["id"] if row else None


def upsert(conn, p: dict) -> str:
    pid = existing_person_id(conn, p)
    if pid:
        # 已在库：只补空字段 + 记 publication，不覆盖既有数据
        conn.execute(
            """UPDATE people SET
                 github_url = COALESCE(github_url, ?),
                 email = COALESCE(email, ?),
                 personal_page = COALESCE(personal_page, ?),
                 institution = COALESCE(institution, ?),
                 sector = COALESCE(sector, ?),
                 github_verified = COALESCE(github_verified, ?),
                 updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (p["github_url"], p["email"], p["personal_page"], p["institution"],
             p["sector"], p["github_verified"], pid),
        )
        action = "merged"
    else:
        cur = conn.execute(
            """INSERT INTO people (first_name, last_name, linkedin_url, email, github_url,
                                   personal_page, institution, company, sector, source_type,
                                   github_verified, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (p["first_name"], p["last_name"], p["linkedin_url"], p["email"], p["github_url"],
             p["personal_page"], p["institution"], p["company"], p["sector"], p["source_type"],
             p["github_verified"], p["notes"]),
        )
        pid = cur.lastrowid
        action = "added"

    for paper in p["papers"]:
        dup = conn.execute(
            "SELECT 1 FROM publications WHERE person_id = ? AND venue = ? AND year = ? AND title = ?",
            (pid, VENUE, YEAR, paper["title"]),
        ).fetchone()
        if not dup:
            conn.execute(
                """INSERT INTO publications (person_id, venue, year, title, is_first_author, source)
                   VALUES (?, ?, ?, ?, ?, 'icml_sheet_0610')""",
                (pid, VENUE, YEAR, paper["title"], paper["is_first_author"]),
            )
    return action


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    people, dropped = parse_sheet()
    sectors = {}
    for p in people:
        sectors[p["sector"]] = sectors.get(p["sector"], 0) + 1
    n_papers = sum(len(p["papers"]) for p in people)
    print(f"解析: {len(people)} 人（界别: {sectors}），{n_papers} 条 ICML 发表记录，"
          f"丢弃归属不明的 GitHub 组: {dropped}")

    print("\n== 样例（前 3 人）==")
    for p in people[:3]:
        show = {k: v for k, v in p.items() if k != "papers"}
        print(show)
        print(f"  papers: {len(p['papers'])} 篇, 一作 {sum(x['is_first_author'] for x in p['papers'])} 篇,"
              f" 例: {p['papers'][0]['title'][:60]}")

    if args.dry_run:
        print("\n[dry-run] 未写库")
        return

    db.init_db()
    conn = db.get_conn()
    stats = {"added": 0, "merged": 0}
    for p in people:
        stats[upsert(conn, p)] += 1
    conn.commit()
    conn.close()

    print(f"\n导入完成: 新增 {stats['added']} 人, 合并进已有档案 {stats['merged']} 人")
    print("重建全文索引...")
    db.rebuild_fts()
    print("done")


if __name__ == "__main__":
    main()
