#!/usr/bin/env python3
"""CSV 导入脚本：把候选人数据导入人才库。

用法：
    python ingest/import_csv.py data/raw/people.csv            # dry-run（默认）
    python ingest/import_csv.py data/raw/people.csv --commit    # 正式写库
"""

import argparse
import csv
import os
import sys

# 让 import db 能找到项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import init_db, upsert_person, add_experiences, add_educations


def parse_experience(raw: str) -> list[dict]:
    """解析 Experience 字段："Title@Company\\nTitle@Company\\n..."

    返回按 position 排列的列表，position=0 是当前职位。
    """
    if not raw or not raw.strip():
        return []
    lines = [line.strip() for line in raw.strip().split("\n") if line.strip()]
    results = []
    for i, line in enumerate(lines):
        if "@" in line:
            title, company = line.split("@", 1)
        else:
            title, company = line, ""
        results.append({
            "position": i,
            "is_current": 1 if i == 0 else 0,
            "title": title.strip(),
            "company": company.strip(),
        })
    return results


def parse_education(raw: str) -> list[dict]:
    """解析 Education 字段："University - Program - Degree Type"

    格式不固定，用 " - " 分割，第一段是学校，后面是学位信息。
    """
    if not raw or not raw.strip():
        return []
    parts = [p.strip() for p in raw.split(" - ")]
    school = parts[0]
    degree = " - ".join(parts[1:]) if len(parts) > 1 else ""
    return [{"school": school, "degree": degree}]


def row_to_person(row: dict) -> dict:
    """把 CSV 行映射到 people 表字段。"""
    github = (row.get("Github") or "").strip()
    return {
        "first_name": (row.get("First Name") or "").strip(),
        "last_name": (row.get("Last Name") or "").strip(),
        "linkedin_url": (row.get("Linkedin URL") or "").strip(),
        "email": (row.get("Personal Email") or "").strip() or None,
        "github_url": github or None,
        "title": (row.get("Title") or "").strip() or None,
        "headline": (row.get("Headline") or "").strip() or None,
        "company": (row.get("Company") or "").strip() or None,
        "location": (row.get("Location") or "").strip() or None,
        "industry": (row.get("Industry") or "").strip() or None,
    }


def preview_row(row: dict, idx: int):
    """打印一行解析预览。"""
    person = row_to_person(row)
    exps = parse_experience(row.get("Experience", ""))
    edus = parse_education(row.get("Education", ""))

    print(f"\n{'='*60}")
    print(f"  #{idx+1}  {person['first_name']} {person['last_name']}")
    print(f"  LinkedIn: {person['linkedin_url']}")
    print(f"  Title:    {person['title']} @ {person['company']}")
    print(f"  Location: {person['location']}")
    print(f"  Email:    {person['email'] or '—'}")
    print(f"  GitHub:   {person['github_url'] or '—'}")
    if edus:
        print(f"  Education: {edus[0]['school']}  {edus[0]['degree']}")
    print(f"  Experience ({len(exps)} 条):")
    for exp in exps[:5]:
        marker = "→" if exp["is_current"] else " "
        print(f"    {marker} [{exp['position']}] {exp['title']} @ {exp['company']}")
    if len(exps) > 5:
        print(f"    ... 还有 {len(exps) - 5} 条")


def main():
    parser = argparse.ArgumentParser(description="导入候选人 CSV 到人才库")
    parser.add_argument("csv_file", help="CSV 文件路径")
    parser.add_argument("--commit", action="store_true", help="正式写入数据库（默认 dry-run）")
    parser.add_argument("--preview", type=int, default=3, help="dry-run 时预览几行（默认 3）")
    args = parser.parse_args()

    if not os.path.exists(args.csv_file):
        print(f"文件不存在: {args.csv_file}")
        sys.exit(1)

    # 读取 CSV
    with open(args.csv_file, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"读取 {len(rows)} 行")

    # 过滤无 LinkedIn URL 的行
    valid_rows = [r for r in rows if (r.get("Linkedin URL") or "").strip()]
    skipped_no_url = len(rows) - len(valid_rows)
    if skipped_no_url:
        print(f"跳过 {skipped_no_url} 行（无 LinkedIn URL）")

    print(f"有效记录: {len(valid_rows)} 条")

    if not args.commit:
        # Dry-run: 预览前 N 行
        print(f"\n{'='*60}")
        print(f"  DRY-RUN 模式 — 预览前 {args.preview} 行，不写数据库")
        print(f"  确认无误后加 --commit 正式导入")
        for i, row in enumerate(valid_rows[:args.preview]):
            preview_row(row, i)
        print(f"\n{'='*60}")
        print(f"共 {len(valid_rows)} 条待导入。运行以下命令正式写库：")
        print(f"  python ingest/import_csv.py '{args.csv_file}' --commit")
        return

    # 正式导入
    init_db()
    stats = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

    for i, row in enumerate(valid_rows):
        try:
            person_data = row_to_person(row)
            person_id, action = upsert_person(person_data)
            stats[action] += 1

            if action in ("created", "updated"):
                exps = parse_experience(row.get("Experience", ""))
                edus = parse_education(row.get("Education", ""))
                if exps:
                    add_experiences(person_id, exps)
                if edus:
                    add_educations(person_id, edus)

            if (i + 1) % 500 == 0:
                print(f"  进度: {i+1}/{len(valid_rows)}")

        except Exception as e:
            stats["errors"] += 1
            print(f"  第 {i+1} 行出错 ({row.get('First Name', '')} {row.get('Last Name', '')}): {e}")

    print(f"\n导入完成！")
    print(f"  新增: {stats['created']} 人")
    print(f"  更新: {stats['updated']} 人")
    print(f"  跳过: {stats['skipped']} 人（数据无变化）")
    if stats["errors"]:
        print(f"  出错: {stats['errors']} 行")


if __name__ == "__main__":
    main()
