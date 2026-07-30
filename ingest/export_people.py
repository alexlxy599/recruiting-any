"""导出人才库为可移植文件，方便分享给没有 data.db 的同事 / 复用到别的项目。

用法:
    python3.12 ingest/export_people.py            # 导出到 data/exports/

产出（含候选人隐私。这两个文件仍然 gitignore，私下传输；
      进 git 的只有 data/exports/talent.sql.gz 这份 SQL dump，且仓库必须保持 private）:
  talent_export.jsonl  每行一个候选人，含完整增强数据:
                       experiences/educations/publications/tags +
                       github 快照(web_snapshots)/主页画像(extractions)/项目(projects)/
                       关系网(collaborations)。配 import_people.py 可在别人机器上忠实重建。
  talent_basic.csv     扁平基本信息表(每人一行)，Excel 可开，复用到任何项目。

每条记录带 _id(原始 id)，仅用于导入时重映射关系网的 collaborator_person_id。
"""

import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "exports")
DROP_COLS = {"id", "created_at", "updated_at"}


def _group(rows, key="person_id"):
    out = {}
    for r in rows:
        d = dict(r)
        out.setdefault(d.pop(key), []).append(d)
    return out


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = db.get_conn()

    people = [dict(r) for r in conn.execute("SELECT * FROM people").fetchall()]
    exps = _group(conn.execute(
        "SELECT person_id, position, is_current, title, company, start_year, end_year, "
        "description, location FROM experiences").fetchall())
    edus = _group(conn.execute(
        "SELECT person_id, school, degree, field, start_year, end_year FROM educations").fetchall())
    pubs = _group(conn.execute(
        "SELECT person_id, venue, year, title, is_first_author, source FROM publications").fetchall())
    tags = _group(conn.execute(
        "SELECT pt.person_id, t.name, t.category, pt.source FROM person_tags pt "
        "JOIN tags t ON t.id = pt.tag_id").fetchall())
    snaps = _group(conn.execute(
        "SELECT person_id, source, url, raw_text, fetched_at FROM web_snapshots").fetchall())
    exts = _group(conn.execute(
        "SELECT person_id, source, version, model, json, created_at FROM extractions").fetchall())
    projs = _group(conn.execute(
        "SELECT person_id, name, url, description, direction, tech, period, source FROM projects").fetchall())
    collabs = _group(conn.execute(
        "SELECT person_id, collaborator_name, collaborator_person_id, relation, context, "
        "collaborator_url, source FROM collaborations").fetchall())
    conn.close()

    jsonl_path = os.path.join(OUT_DIR, "talent_export.jsonl")
    csv_path = os.path.join(OUT_DIR, "talent_basic.csv")

    with open(jsonl_path, "w", encoding="utf-8") as f:
        for p in people:
            pid = p["id"]
            rec = {k: v for k, v in p.items() if k not in DROP_COLS}
            rec["_id"] = pid                      # 原始 id，仅供关系网重映射
            rec["experiences"] = exps.get(pid, [])
            rec["educations"] = edus.get(pid, [])
            rec["publications"] = pubs.get(pid, [])
            rec["tags"] = tags.get(pid, [])
            rec["snapshots"] = snaps.get(pid, [])      # GitHub/主页抓取原文
            rec["extractions"] = exts.get(pid, [])     # 主页结构化画像(词云等)
            rec["projects"] = projs.get(pid, [])
            rec["collaborations"] = collabs.get(pid, [])  # 关系网
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    CSV_COLS = ["first_name", "last_name", "email", "linkedin_url", "github_url", "personal_page",
                "title", "company", "institution", "location", "sector", "advisor",
                "research_area", "expected_graduation", "status"]
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS + ["tags", "venues"])
        for p in people:
            pid = p["id"]
            tag_str = ", ".join(t["name"] for t in tags.get(pid, []))
            ven_str = ", ".join(sorted({pb["venue"] for pb in pubs.get(pid, []) if pb.get("venue")}))
            w.writerow([p.get(c, "") if p.get(c) is not None else "" for c in CSV_COLS] + [tag_str, ven_str])

    def mb(path):
        return round(os.path.getsize(path) / 1024 / 1024, 2)

    print(f"导出 {len(people)} 人(含 github快照/主页画像/关系网)")
    print(f"  {jsonl_path}  ({mb(jsonl_path)} MB)")
    print(f"  {csv_path}  ({mb(csv_path)} MB)")


if __name__ == "__main__":
    main()
