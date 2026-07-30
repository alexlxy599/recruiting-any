"""人才库数据导入（自包含，含 GitHub 快照 / 主页画像 / 关系网）。

【怎么用】
1. 把 import_talent.py 和 talent_export.jsonl 一起放到项目根目录（有 app.py / db.py 的文件夹）。
2. 运行：  python3.12 import_talent.py        （先看统计：加 --dry-run）
3. 跑完启动：python3.12 app.py，打开 http://127.0.0.1:5055 ，
   候选人列表、词云、GitHub 信息、关系网都会有。

自动建表并兼容老代码（把 people.linkedin_url 改为可空）。只按 linkedin_url 去重
（email 在本数据里是污染字段，不可作唯一标识）。面向空库一次性导入。
关系网的 collaborator_person_id 会做 老id→新id 重映射，保证连对人。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

# JSONL 路径由命令行传入

PEOPLE_COLS = ["first_name", "last_name", "linkedin_url", "email", "github_url", "title",
               "headline", "company", "location", "industry", "notes", "status", "source_type",
               "advisor", "institution", "personal_page", "expected_graduation", "research_area",
               "github_verified", "sector", "github_summary", "lab"]


def ensure_linkedin_nullable(conn):
    cols = [dict(r) for r in conn.execute("PRAGMA table_info(people)").fetchall()]
    if not any(c["name"] == "linkedin_url" and c["notnull"] for c in cols):
        return
    defs = []
    for c in cols:
        if c["name"] == "id":
            defs.append("id INTEGER PRIMARY KEY AUTOINCREMENT"); continue
        d = f'{c["name"]} {c["type"]}'
        if c["name"] == "linkedin_url":
            d += " UNIQUE"
        else:
            if c["notnull"]:
                d += " NOT NULL"
            if c["dflt_value"] is not None:
                d += f' DEFAULT {c["dflt_value"]}'
        defs.append(d)
    names = ", ".join(c["name"] for c in cols)
    conn.executescript(f"BEGIN; CREATE TABLE people_new ({', '.join(defs)}); "
                       f"INSERT INTO people_new ({names}) SELECT {names} FROM people; "
                       f"DROP TABLE people; ALTER TABLE people_new RENAME TO people; COMMIT;")
    conn.commit()
    print("  已将 people.linkedin_url 调整为可空")


def existing_id(conn, rec):
    li = (rec.get("linkedin_url") or "").strip().lower()
    if li:
        r = conn.execute("SELECT id FROM people WHERE LOWER(linkedin_url)=?", (li,)).fetchone()
        if r:
            return r["id"]
    return None


def insert_person(conn, rec):
    """插入/合并 person，返回 (pid, action)。新人同时插经历/教育/快照/画像/项目。"""
    pid = existing_id(conn, rec)
    cols = [c for c in PEOPLE_COLS if c in rec]
    if pid:
        sets = ", ".join(f"{c}=COALESCE({c}, ?)" for c in cols)
        conn.execute(f"UPDATE people SET {sets} WHERE id=?", [rec.get(c) for c in cols] + [pid])
        action = "merged"
    else:
        ph = ", ".join("?" * len(cols))
        cur = conn.execute(f"INSERT INTO people ({', '.join(cols)}) VALUES ({ph})",
                           [rec.get(c) for c in cols])
        pid = cur.lastrowid
        action = "added"
        for e in rec.get("experiences", []):
            conn.execute("INSERT INTO experiences (person_id, position, is_current, title, company, "
                         "start_year, end_year, description, location) VALUES (?,?,?,?,?,?,?,?,?)",
                         (pid, e.get("position"), e.get("is_current"), e.get("title"), e.get("company"),
                          e.get("start_year"), e.get("end_year"), e.get("description"), e.get("location")))
        for ed in rec.get("educations", []):
            conn.execute("INSERT INTO educations (person_id, school, degree, field, start_year, end_year) "
                         "VALUES (?,?,?,?,?,?)",
                         (pid, ed.get("school"), ed.get("degree"), ed.get("field"),
                          ed.get("start_year"), ed.get("end_year")))
        for s in rec.get("snapshots", []):       # GitHub / 主页抓取原文
            conn.execute("INSERT INTO web_snapshots (person_id, source, url, raw_text, fetched_at) "
                         "VALUES (?,?,?,?,?)",
                         (pid, s.get("source"), s.get("url"), s.get("raw_text"), s.get("fetched_at")))
        for x in rec.get("extractions", []):     # 主页结构化画像(词云)
            conn.execute("INSERT INTO extractions (person_id, source, version, model, json, created_at) "
                         "VALUES (?,?,?,?,?,?)",
                         (pid, x.get("source"), x.get("version"), x.get("model"), x.get("json"),
                          x.get("created_at")))
        for pr in rec.get("projects", []):
            conn.execute("INSERT INTO projects (person_id, name, url, description, direction, tech, "
                         "period, source) VALUES (?,?,?,?,?,?,?,?)",
                         (pid, pr.get("name"), pr.get("url"), pr.get("description"), pr.get("direction"),
                          pr.get("tech"), pr.get("period"), pr.get("source")))
    for pub in rec.get("publications", []):
        if not conn.execute("SELECT 1 FROM publications WHERE person_id=? AND venue=? AND year=? AND title=?",
                            (pid, pub.get("venue"), pub.get("year"), pub.get("title"))).fetchone():
            conn.execute("INSERT INTO publications (person_id, venue, year, title, is_first_author, source) "
                         "VALUES (?,?,?,?,?,?)",
                         (pid, pub.get("venue"), pub.get("year"), pub.get("title"),
                          pub.get("is_first_author"), pub.get("source")))
    for t in rec.get("tags", []):
        if t.get("name"):
            db.add_person_tag(pid, t["name"], category=t.get("category", "custom"),
                              source=t.get("source", "import"), conn=conn)
    return pid, action


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="talent_export.jsonl 路径")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    global JSONL; JSONL = args.file
    if not os.path.exists(JSONL):
        sys.exit(f"找不到 {JSONL}（请把 talent_export.jsonl 和本脚本放在同一目录）")
    records = [json.loads(l) for l in open(JSONL, encoding="utf-8") if l.strip()]
    print(f"读取 {len(records)} 条候选人记录")
    if args.dry_run:
        s = records[0] if records else {}
        print("样例:", {k: s.get(k) for k in ("first_name", "last_name", "company")},
              f"| 快照{len(s.get('snapshots', []))} 画像{len(s.get('extractions', []))} "
              f"关系{len(s.get('collaborations', []))}")
        print("[dry-run] 未写库")
        return

    db.init_db()
    conn = db.get_conn()
    ensure_linkedin_nullable(conn)

    # Pass 1: 建全部人 + 各自的经历/教育/快照/画像/项目/论文/标签，记录 老id→新id
    idmap, pending, stats = {}, [], {"added": 0, "merged": 0}
    for i, rec in enumerate(records, 1):
        pid, action = insert_person(conn, rec)
        stats[action] += 1
        if "_id" in rec:
            idmap[rec["_id"]] = pid
        if action == "added" and rec.get("collaborations"):
            pending.append((pid, rec["collaborations"]))
        if i % 1000 == 0:
            conn.commit(); print(f"  ...建人 {i}/{len(records)}")
    conn.commit()

    # Pass 2: 关系网（重映射 collaborator_person_id）
    nlink = 0
    for new_pid, clist in pending:
        for c in clist:
            old = c.get("collaborator_person_id")
            new_cid = idmap.get(old) if old is not None else None
            conn.execute("INSERT INTO collaborations (person_id, collaborator_name, "
                         "collaborator_person_id, relation, context, collaborator_url, source) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (new_pid, c.get("collaborator_name"), new_cid, c.get("relation"),
                          c.get("context"), c.get("collaborator_url"), c.get("source")))
            nlink += 1
    conn.commit(); conn.close()

    print(f"导入完成: 新增 {stats['added']} 人, 合并 {stats['merged']} 人, 关系网 {nlink} 条")
    print("重建全文索引...")
    db.rebuild_fts()
    print("done — 运行 python3.12 app.py 即可查看(含词云/GitHub/关系网)")


if __name__ == "__main__":
    main()
