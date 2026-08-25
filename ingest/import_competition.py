"""竞赛线导入：数学/信息学竞赛获奖名单 → people + tags。

独立于会议线的第二条 sourcing 线（QR 岗一级信号）。
输入格式（CSV）：competition,year,award,subject,name,school

与会议线的关键差异：
  1. 姓名是中文。存库时 last_name=姓（含复姓识别）、first_name=名。
     与会议线（拼音名）天然不撞，同线内去重按 姓名+学校。
  2. 一人多奖极常见（丘赛按科目设奖）——合并成一个人，
     tag 记最高奖，notes 列全所有奖项。
  3. 奖档写成结构化 tag：competition:<赛事><年>-<最高奖>，category='background'，
     另挂 route:qr。信号档(S/A)不落库，按 skill 规则现算。

用法:
    python3.12 ingest/import_competition.py data/raw/yau2025_awards.csv           # dry-run
    python3.12 ingest/import_competition.py data/raw/yau2025_awards.csv --commit  # 写库
"""
import csv
import re
import sys
import os
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

COMPOUND_SURNAMES = (
    "欧阳", "司马", "上官", "诸葛", "端木", "司徒", "令狐", "皇甫",
    "尉迟", "长孙", "慕容", "宇文", "轩辕", "东方", "独孤", "南宫",
)
MEDAL_RANK = {"金": 0, "银": 1, "铜": 2, "优胜": 3, "优秀": 3}


def split_cn_name(name):
    """中文名拆 姓/名；西文名按空格拆。返回 (first_name, last_name)。"""
    name = name.strip()
    if re.search(r"[a-zA-Z]", name):                     # 西文名（留学生等）
        parts = name.split()
        return " ".join(parts[:-1]), parts[-1]
    for cs in COMPOUND_SURNAMES:
        if name.startswith(cs):
            return name[len(cs):], cs
    return name[1:], name[:1]


def load(path):
    """CSV → {(name, school): {awards: [...], best: 最高奖}}"""
    people = defaultdict(lambda: {"awards": [], "competition": "", "year": ""})
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["name"].strip(), row["school"].strip())
            d = people[key]
            d["competition"] = row["competition"].strip()
            d["year"] = row["year"].strip()
            d["awards"].append(f"{row['subject'].strip()}·{row['award'].strip()}")
            d.setdefault("medals", []).append(row["award"].strip())
    for d in people.values():
        d["best"] = min(d["medals"], key=lambda m: MEDAL_RANK.get(m, 9))
    return people


def existing_person(conn, name, school):
    """同线去重：姓名(去空格)完全一致，再看学校是否相容。"""
    sq = re.sub(r"\s", "", name)
    for r in conn.execute("SELECT id, first_name, last_name, institution FROM people"):
        full = re.sub(r"\s", "", f"{r['last_name'] or ''}{r['first_name'] or ''}")
        full2 = re.sub(r"\s", "", f"{r['first_name'] or ''}{r['last_name'] or ''}")
        if sq in (full, full2):
            inst = r["institution"] or ""
            if not inst or not school or school[:2] in inst or inst[:2] in school:
                return r["id"]
    return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    path = sys.argv[1]
    commit = "--commit" in sys.argv
    people = load(path)

    conn = db.get_conn()
    plan = []
    for (name, school), d in sorted(people.items(), key=lambda x: MEDAL_RANK.get(x[1]["best"], 9)):
        fn, ln = split_cn_name(name)
        pid = existing_person(conn, name, school)
        tag = f"competition:{d['competition']}{d['year']}-{d['best']}"
        plan.append({
            "action": "更新" if pid else "新增",
            "pid": pid,
            "first_name": fn, "last_name": ln,
            "institution": school,
            "tag": tag,
            "notes": f"{d['competition']}{d['year']}获奖: " + "；".join(sorted(set(d["awards"]))),
        })

    print(f"解析 {sum(1 for _ in people)} 人（{len(plan)} 条计划）\n")
    print("── 前 3 条解析结果（dry-run 必看）──")
    for p in plan[:3]:
        print(f"  [{p['action']}] {p['last_name']}{p['first_name']} | {p['institution']}")
        print(f"        tag: {p['tag']} + route:qr")
        print(f"        notes: {p['notes'][:80]}")
    n_new = sum(1 for p in plan if p["action"] == "新增")
    n_upd = len(plan) - n_new
    from collections import Counter
    print(f"\n新增 {n_new} / 更新 {n_upd}")
    print("奖档分布:", dict(Counter(p["tag"].split("-")[-1] for p in plan)))
    print("学校分布:", dict(Counter(p["institution"] for p in plan).most_common(8)))

    if not commit:
        print("\n(dry-run，未写库。加 --commit 正式导入)")
        return

    done_new = done_upd = 0
    for p in plan:
        pid, action = db.upsert_person({
            "first_name": p["first_name"], "last_name": p["last_name"],
            "institution": p["institution"],
            "source_type": "competition", "sector": "academic",
            "research_area": "mathematics",
            "notes": p["notes"],
        })
        db.add_person_tag(pid, p["tag"], category="background", source="manual")
        db.add_person_tag(pid, "route:qr", category="custom", source="manual")
        if action == "inserted":
            done_new += 1
        else:
            done_upd += 1
    print(f"\n已写库: 新增 {done_new} / 更新 {done_upd}")


if __name__ == "__main__":
    main()
