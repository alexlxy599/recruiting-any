"""手动处理 import_pipeline_replies.py 产生的 15 个歧义记录。
经人工比对公司/院校/LinkedIn,全部判定为库中无对应人 → 新建。
唯一例外:Hao Cheng(回复/Microsoft)与 Hao Cheng(管道/Microsoft)为同一人,
         只新建一次,两条 history 都挂到同一 person_id。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

WRITE = "--write" in sys.argv

PIPELINE_RECORDS = [
    dict(name="杨旸",       company="Meta",               school="西北大学（美）",  status="09 流程终止",   owner="Boshen",
         track="无", email="yang.angela06@gmail.com"),
    dict(name="杨帆",       company="Waymo",              school="哥伦比亚大学",   status="09 流程终止",   owner="Boshen",  track="无"),
    dict(name="Chenkai Wang",company="Meta",              school="康奈尔大学/牛津大学", status="08 长期跟踪",owner="Boshen/Alex", track="无"),
    dict(name="王珂",        company="斯坦福/南大",         school="UC Davis",      status="08 长期跟踪",   owner="Boshen",  track="无"),
    dict(name="Hao Cheng",  company="Microsoft",          school="华盛顿大学",     status="08 长期跟踪",   owner="Alex",    track="无"),
    dict(name="Kai Wang",   company="Amazon",             school="布朗大学",       status="08 长期跟踪",   owner="Alex",    track="无"),
    dict(name="周航",        company="阿尔伯塔大学（博后）",school="中国科学技术大学",status="02 待首次业务交流",owner="黄伦涛",  track="无"),
]

REPLY_RECORDS = [
    dict(name="Yang Li",     company="Amazon",    linkedin="", prog="无法出席，但可以线上交流"),
    dict(name="Jingyi Zhang",company="Corbel",    linkedin="", prog="暂无兴趣"),
    dict(name="Hao Wu",      company="Waterloo PostDoc", linkedin="", prog="沟通中"),
    dict(name="Yang Liu",    company="Shopify",   linkedin="", prog="暂无兴趣"),
    dict(name="Yadi Cao",    company="UCSD博后",  linkedin="", prog="沟通中"),
    dict(name="Xi Liu",      company="Meta",      linkedin="", prog="暂无兴趣"),
    dict(name="Zhenyu Liao", company="Amazon",    linkedin="",
         prog="做AI for Math/RL方向，中长期一定回国，短期内有好机会也可以考虑。答应给简历，对华为Sheng teng很了解。"),
    # Hao Cheng(回复/Microsoft) 与管道那条同一人,复用管道新建的 person_id,只写 history
    dict(name="Hao Cheng",   company="Microsoft", linkedin="",
         prog="linkedin主动私信，想了解机会，在约时间", is_reply_for_pipeline="Hao Cheng"),
]

import re, unicodedata
from pypinyin import lazy_pinyin

def slugify(s):
    s = unicodedata.normalize("NFKC", str(s)).strip().lower()
    s = re.sub(r"[^a-z0-9一-鿿]+", "-", s).strip("-")
    return s or "unknown"

def split_name(full):
    full = str(full).strip()
    if re.search(r"[一-鿿]", full):
        return (full[1:], full[0]) if len(full) >= 2 else (full, "")
    parts = full.split()
    return (" ".join(parts[:-1]), parts[-1]) if len(parts) >= 2 else (full, "")

# 先收集本批新建的 name → pid 映射(避免 Hao Cheng 重复新建)
created_pids = {}

def upsert_new(name, company, email=None, linkedin=None, school=None, notes=""):
    if name in created_pids:
        return created_pids[name]
    first, last = split_name(name)
    url = linkedin if linkedin and "linkedin.com" in linkedin else f"pipeline://{slugify(name)}-{slugify(company)}"
    data = {"first_name": first, "last_name": last, "linkedin_url": url,
            "company": company, "email": email}
    if not WRITE:
        print(f"  [dry] 新建 {name} @ {company}  url={url}")
        return None
    pid, action = db.upsert_person(data)
    if school:
        db.add_educations(pid, [{"school": school}])
    if notes:
        c = db.get_conn()
        old = c.execute("SELECT notes FROM people WHERE id=?", (pid,)).fetchone()[0] or ""
        merged = (old + "\n\n" if old else "") + notes
        c.execute("UPDATE people SET notes=? WHERE id=?", (merged, pid))
        c.commit(); c.close()
    created_pids[name] = pid
    print(f"  {action}: {name} @ {company}  id={pid}")
    return pid

print(f"{'DRY-RUN' if not WRITE else 'WRITE'} — 处理 {len(PIPELINE_RECORDS)} 管道 + {len(REPLY_RECORDS)} 回复歧义记录")

for r in PIPELINE_RECORDS:
    notes = f"[管道状态] {r['status']}\n[owner] {r['owner']}"
    pid = upsert_new(r["name"], r["company"], email=r.get("email"),
                     school=r.get("school"), notes=notes)
    if pid and r.get("track") and r["track"] != "无":
        db.add_history(name=r["name"], url="", community="pipeline",
                       language="zh", message=r["track"], person_id=pid)

for r in REPLY_RECORDS:
    ref_name = r.get("is_reply_for_pipeline")
    if ref_name:
        # 复用同批管道新建的 pid
        pid = created_pids.get(ref_name)
        if not pid and WRITE:
            c = db.get_conn()
            row = c.execute(
                "SELECT id FROM people WHERE first_name||' '||last_name LIKE ? AND company LIKE ?",
                (f"%{ref_name.split()[0]}%", "%Microsoft%")
            ).fetchone()
            c.close()
            pid = row[0] if row else None
    else:
        pid = upsert_new(r["name"], r["company"], linkedin=r.get("linkedin"))
    if pid and WRITE:
        prog = r.get("prog", "")
        db.add_history(name=r["name"], url=r.get("linkedin",""), community="回复追踪",
                       language="zh", message="(歧义补录)", person_id=pid)
        if prog:
            c = db.get_conn()
            c.execute("UPDATE history SET reply=? WHERE id=(SELECT MAX(id) FROM history WHERE person_id=?)",
                      (prog, pid))
            c.commit(); c.close()
        s = "replied" if any(k in prog for k in ("沟通中","答应","简历","电话","约时间")) else "contacted"
        db.update_person_status(pid, s)
    elif not pid:
        print(f"  [skip] {r['name']} 回复 history — 找不到管道对应 pid")

if WRITE:
    c = db.get_conn()
    print("\n最终统计:")
    print("  总人数:", c.execute("SELECT COUNT(*) FROM people").fetchone()[0])
    print("  history:", c.execute("SELECT COUNT(*) FROM history").fetchone()[0])
    print("  状态分布:", dict(c.execute("SELECT status, COUNT(*) FROM people GROUP BY status").fetchall()))
    c.close()
else:
    print("\n确认后加 --write 落库")
