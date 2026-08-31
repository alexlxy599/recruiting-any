"""按 2026-08 个人主页/GitHub 核实结果,刷新过期的机构归属。

背景:这批人多来自 ICML 2026 论文导入,institution 存的是**论文发表时**的单位。
主页显示的是**当前**单位 —— 两者都是事实,不是脏数据。所以:
  - institution/company/title 更新为当前
  - 原 institution 追加进 notes,保留可回溯
  - 工业界的同时补 company 并把 sector 从 academic 改为 industry

依据:每个人的个人主页原文(data/raw/homepage_raw.json)与 GitHub profile,
2026-08-26 人工核实。

用法:
    python3.12 migrations/002_refresh_stale_affiliations.py           # dry-run
    python3.12 migrations/002_refresh_stale_affiliations.py --commit
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

STAMP = "2026-08-26主页核实"

# pid: (新institution, 新company或None, 新title, 新sector或None保持不变)
UPDATES = {
    6243: ("Workday AI Research", "Workday", "Founding Member, AI Research", "industry"),
    3008: ("Tsinghua University", None, "PhD Student", None),
    2999: ("University of Toronto", None, "PhD Student", None),
    3196: ("The University of Hong Kong", "Reka AI", "Assistant Professor / Co-founder", None),
    3839: ("Shanghai Jiao Tong University", None, "PhD Student", None),
    3414: ("Princeton University", None, "PhD Student", None),
    3069: ("Moonshot AI (former)", "Moonshot AI", "Researcher (former)", "industry"),
    3264: ("University of Utah", None, "Assistant Professor", None),
    3100: ("Stealth Startup", "Stealth Startup", "Founder (ex-xAI)", "industry"),
    3491: ("Stanford University", None, "PhD Student", None),
    3871: ("Moonshot AI", "Moonshot AI", "Researcher (Soochow Univ PhD candidate)", "industry"),
    3433: ("Aurora", "Aurora", "Researcher (CMU Robotics PhD)", "industry"),
    3287: ("Westlake University", None, "ENCODE Lab", None),
    3377: ("Beijing Jiaotong University", None, "教授 / 博导", None),
    3670: ("University of Macau", None, "Postdoctoral Fellow", None),
    3043: ("The University of Texas at Austin", None, "Postdoctoral Researcher", None),
    3247: ("TikTok (ByteDance) Sydney", "ByteDance", "Machine Learning Engineer", "industry"),
    3295: ("National University of Singapore", None, "PhD Student", None),
    3899: ("Nanyang Technological University", None, "PhD Student (MMLab@NTU)", None),
    3157: ("Stanford University", None, "PhD Candidate", None),
    3270: ("Tencent Hunyuan", "Tencent", "Senior Researcher (NTU PhD in progress)", "industry"),
    3086: ("Virginia Tech", None, "Postdoc Fellow", None),
    3193: ("Huawei", "Huawei", "Researcher / Group Lead (天才少年)", "industry"),
    3750: ("Meta SuperIntelligence Lab", "Meta", "AI Research Scientist", "industry"),
    4055: ("University of California, Santa Cruz", None, "", None),
    3472: ("Shanghai Jiao Tong University", None, "PhD Student", None),
    3857: ("South China University of Technology", None, "", None),
    3132: ("Zhejiang University", None, "ZJU-100 青年教授", None),
    3593: ("Henan University of Technology", None, "讲师 (Lecturer)", None),
    3741: ("Columbia University", None, "CS PhD", None),
    2989: ("University of Washington", None, "PhD Student", None),
    3427: ("Monash University", None, "PhD Candidate", None),
    3952: ("University of Washington", None, "", None),
    4156: ("South China University of Technology", None, "", None),
    3243: ("University of Chicago", None, "PhD (graduated)", None),
    3320: ("独立内容创作者", None, "AI短剧创作者 / Apache Dubbo PMC", "other"),
    3305: ("Duke University", None, "MS in Statistical Science", None),
    3325: ("East China Normal University", None, "", None),
    3574: ("The Chinese University of Hong Kong", None, "", None),
    3907: ("National University of Singapore", None, "Undergraduate", None),
}


def main():
    commit = "--commit" in sys.argv
    conn = db.get_conn()
    plan = []
    for pid, (inst, co, title, sector) in UPDATES.items():
        r = conn.execute("SELECT id, first_name, last_name, institution, company, title, "
                         "sector, notes FROM people WHERE id=?", (pid,)).fetchone()
        if not r:
            print(f"  !! #{pid} 不存在,跳过")
            continue
        old_inst = r["institution"] or ""
        if old_inst == inst and (r["sector"] or "") == (sector or r["sector"] or ""):
            continue
        note = (r["notes"] or "").strip()
        tag = f"{STAMP}:原institution={old_inst or '(空)'}"
        new_note = f"{note} ｜ {tag}" if note else tag
        plan.append({
            "pid": pid, "name": f"{r['first_name'] or ''} {r['last_name'] or ''}".strip(),
            "old_inst": old_inst, "new_inst": inst,
            "old_sector": r["sector"] or "", "new_sector": sector or r["sector"] or "",
            "co": co, "title": title, "notes": new_note,
        })

    print(f"=== 计划更新 {len(plan)} 人 (dry-run) ===\n")
    for p in plan[:5]:
        print(f"#{p['pid']} {p['name']}")
        print(f"   institution: {p['old_inst'][:44]!r}")
        print(f"             → {p['new_inst']!r}")
        if p["co"]:
            print(f"   company    → {p['co']!r}")
        if p["title"]:
            print(f"   title      → {p['title']!r}")
        if p["old_sector"] != p["new_sector"]:
            print(f"   sector     : {p['old_sector']} → {p['new_sector']}")
        print(f"   notes      → ...{p['notes'][-56:]!r}")
        print()
    print(f"(以上为前 5 条,共 {len(plan)} 条)\n")

    n_sector = sum(1 for p in plan if p["old_sector"] != p["new_sector"])
    n_co = sum(1 for p in plan if p["co"])
    print(f"其中: 改 sector {n_sector} 人 (academic→industry 等) | 补 company {n_co} 人")

    if not commit:
        print("\n未写库。确认无误后加 --commit 执行。")
        return

    ok = 0
    for p in plan:
        # 列名与值显式配对 —— 不用下标算术拼 SET 子句,错一位就会写错列
        pairs = [("institution", p["new_inst"]),
                 ("sector", p["new_sector"]),
                 ("notes", p["notes"])]
        if p["co"]:
            pairs.append(("company", p["co"]))
        if p["title"]:
            pairs.append(("title", p["title"]))
        sql = ("UPDATE people SET "
               + ", ".join(f"{c}=?" for c, _ in pairs)
               + ", updated_at=CURRENT_TIMESTAMP WHERE id=?")
        conn.execute(sql, [v for _, v in pairs] + [p["pid"]])
        ok += 1
    conn.commit()
    print(f"\n已更新 {ok} 人。回滚依据保留在 notes 的 '{STAMP}:原institution=' 里。")


if __name__ == "__main__":
    main()
