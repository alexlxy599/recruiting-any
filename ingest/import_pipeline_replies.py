"""导入两份外部追踪表到人才库,并与现有人员去重整合。

  1. 管道情况.xlsx   — 深度管道(推荐/面试/offer 阶段),无 LinkedIn 列
  2. 人才库回复情况.xlsx — 外联回复追踪,约半数有 LinkedIn

去重策略(按可靠性降序,遵循 CLAUDE.md「linkedin_url 是唯一主键」原则):
  a. LinkedIn slug 精确匹配(/in/xxx,忽略国家子域)
  b. email 匹配 + 姓名一致性校验(email 可能污染,姓名对不上则降级为歧义)
  c. 姓名精确匹配(库中唯一命中才算,多命中→歧义人工审)
  d. 都没有 → 新建,linkedin_url 用合成 scheme(pipeline:// 或 reply://,
     沿用库里 lab-sourcer:// 的惯例)

用法:
  uv run --python 3.11 --with openpyxl python ingest/import_pipeline_replies.py          # dry-run
  uv run --python 3.11 --with openpyxl python ingest/import_pipeline_replies.py --write  # 落库
"""
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402
import openpyxl  # noqa: E402

PIPELINE_XLSX = "/Users/alex/Desktop/管道情况.xlsx"
REPLIES_XLSX = "/Users/alex/Desktop/人才库回复情况.xlsx"
WRITE = "--write" in sys.argv

# ---------- 状态映射 ----------
PIPELINE_STATUS = {
    "01 部门识别/流转中": "replied",
    "02 待首次业务交流": "replied",
    "03 技术面试中": "interview",
    "04 业务终面中": "interview",
    "面试通过/谈薪中": "decision",
    "05 Offer审批中": "decision",
    "07 已发offer/已入职": "decision",
    "08 长期跟踪": "replied",
    "09 流程终止": "archived",
}

OWNER_FIX = {"fiona": "Fiona", "fIona": "Fiona", "lulu": "lulu",
             "涛哥": "黄伦涛", "huang luntao": "黄伦涛"}


def norm_owner(raw):
    if not raw:
        return []
    parts = re.split(r"[/、,]", str(raw))
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        out.append(OWNER_FIX.get(p.lower(), OWNER_FIX.get(p, p)))
    return out


def reply_status(prog: str) -> str:
    """进展文本 → pipeline 状态(粗分类,原文完整保留在 history.reply)。"""
    if not prog or not prog.strip():
        return "contacted"
    p = prog.strip()
    if any(k in p for k in ("暂无回复", "未出席", "没约上")):
        return "contacted"
    if any(k in p for k in ("非目标", "怪人", "不合适", "难以匹配")):
        return "archived"
    if any(k in p for k in ("面试中", "综面", "审批", "待业务交流", "部门流转")):
        return "interview"
    return "replied"  # 其余都有实质互动


# ---------- 姓名 / URL 归一 ----------
def li_slug(url):
    if not url:
        return None
    m = re.search(r"linkedin\.com/in/([^/?#]+)", str(url).lower())
    return m.group(1).strip() if m else None


def norm_name(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).strip().lower()
    return re.sub(r"\s+", " ", s)


def split_name(full):
    """英文名:末词为姓;中文名:首字为姓。"""
    full = str(full).strip()
    if re.search(r"[一-鿿]", full):
        return (full[1:], full[0]) if len(full) >= 2 else (full, "")
    parts = full.split()
    if len(parts) >= 2:
        return " ".join(parts[:-1]), parts[-1]
    return full, ""


from pypinyin import lazy_pinyin  # noqa: E402


def name_tokens(s) -> frozenset:
    """姓名 → 归一 token 集。中文转拼音,去括号,忽略顺序。"""
    if not s:
        return frozenset()
    s = unicodedata.normalize("NFKC", str(s))  # 全角→半角,再剥括号
    s = re.sub(r"[()]", " ", s)
    toks = set()
    for part in norm_name(s).split():
        if re.search(r"[一-鿿]", part):
            toks.update(p.lower() for p in lazy_pinyin(part))
        else:
            toks.add(part)
    return frozenset(toks)


def name_compat(a, b) -> bool:
    """两个姓名是否兼容:token 集互为子集,或交集 >= 2。"""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return False
    return ta <= tb or tb <= ta or len(ta & tb) >= 2


def slugify(s):
    s = norm_name(s)
    s = re.sub(r"[^a-z0-9一-鿿]+", "-", s).strip("-")
    return s or "unknown"


# ---------- 现有库索引 ----------
conn = db.get_conn()
existing = conn.execute(
    "SELECT id, first_name, last_name, linkedin_url, email, company, status FROM people"
).fetchall()
conn.close()

by_slug, by_email, by_name = {}, {}, {}
for r in existing:
    s = li_slug(r["linkedin_url"])
    if s:
        by_slug[s] = r["id"]
    if r["email"]:
        by_email.setdefault(r["email"].strip().lower(), []).append(r["id"])
    n = norm_name(f"{r['first_name']} {r['last_name']}")
    if n:
        by_name.setdefault(n, []).append(r["id"])
name_of = {r["id"]: norm_name(f"{r['first_name']} {r['last_name']}") for r in existing}
by_tokens = {}
for r in existing:
    t = name_tokens(f"{r['first_name']} {r['last_name']}")
    if t:
        by_tokens.setdefault(t, []).append(r["id"])
company_of = {r["id"]: (r["company"] or "").strip().lower() for r in existing}


def _company_filter(ids, company):
    """多命中时用公司字段消歧,唯一命中才返回。"""
    if not company:
        return None
    c = str(company).strip().lower()
    hits = [i for i in ids if company_of.get(i) and (company_of[i] in c or c in company_of[i])]
    return hits[0] if len(hits) == 1 else None


def match_person(name, email=None, linkedin=None, company=None):
    """返回 (person_id | None, how)。歧义返回 (None, 'ambiguous:...')"""
    s = li_slug(linkedin)
    if s and s in by_slug:
        return by_slug[s], "linkedin"
    if email:
        ids = by_email.get(str(email).strip().lower(), [])
        if len(ids) == 1:
            # email 污染防线:姓名要兼容(拼音归一后互为子集/交集>=2)
            if name_compat(name, name_of.get(ids[0], "")):
                return ids[0], "email+name"
            return None, f"ambiguous:email命中id={ids[0]}但姓名不符({name} vs {name_of.get(ids[0])})"
        if len(ids) > 1:
            return None, f"ambiguous:email命中{len(ids)}人"
    ids = by_tokens.get(name_tokens(name), [])
    if len(ids) == 1:
        return ids[0], "name"
    if len(ids) > 1:
        hit = _company_filter(ids, company)
        if hit:
            return hit, "name+company"
        return None, f"ambiguous:姓名命中{len(ids)}人 ids={ids}"
    return None, "new"


# ---------- 解析文件 ----------
def load_rows(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = list(wb.active.iter_rows(values_only=True))
    hdr = [str(h).strip() if h else "" for h in rows[0]]
    data = [dict(zip(hdr, r)) for r in rows[1:]
            if any(v is not None and str(v).strip() for v in r)]
    return data


def cell(row, key):
    v = row.get(key)
    return str(v).strip() if v is not None and str(v).strip() else None


stats = {"matched": 0, "created": 0, "ambiguous": 0}
ambiguous_report = []
samples = []
actions = []  # (kind, payload) 收集后统一执行


# ---------- 文件 1:管道情况 ----------
for row in load_rows(PIPELINE_XLSX):
    name = cell(row, "姓名")
    if not name:
        continue
    email = cell(row, "联系方式")
    if email and "@" not in email:
        email = None
    pid, how = match_person(name, email=email, company=cell(row, "工作经历"))

    status = PIPELINE_STATUS.get(cell(row, "状态归类") or "", "replied")
    first, last = split_name(name)
    notes_parts = [f"[管道状态] {cell(row,'状态归类')}"]
    for label, key in [("推荐部门", "最新推荐部门"), ("定级", "定级"),
                       ("原薪酬", "原薪酬"), ("期望薪酬", "期望薪酬"),
                       ("微信", "微信号"), ("介绍", "候选人情况介绍"), ("备注", "备注")]:
        v = cell(row, key)
        if v:
            notes_parts.append(f"[{label}] {v}")
    person_data = {
        "first_name": first, "last_name": last,
        "linkedin_url": None,  # 匹配到就不动原 URL;新建才用合成
        "email": email,
        "company": cell(row, "工作经历"),
        "headline": cell(row, "岗位/擅长领域"),
        "location": cell(row, "目前所在地"),
    }
    edu = {"school": cell(row, "毕业院校"), "degree": cell(row, "学历"),
           "field": cell(row, "专业"), "end_year": row.get("毕业时间")}
    ugrad = cell(row, "本科学校")
    track = "\n".join(x for x in (cell(row, "推荐记录"), cell(row, "沟通跟踪记录")) if x)
    owners = norm_owner(cell(row, "跟踪人"))

    if how.startswith("ambiguous"):
        stats["ambiguous"] += 1
        ambiguous_report.append(("管道", name, how))
        continue
    if pid is None:
        person_data["linkedin_url"] = f"pipeline://{slugify(name)}"
        stats["created"] += 1
    else:
        stats["matched"] += 1
    actions.append(("pipeline", dict(pid=pid, person=person_data, status=status,
                                     notes="\n".join(notes_parts), edu=edu, ugrad=ugrad,
                                     track=track, owners=owners, name=name, how=how,
                                     raw_status=cell(row, "状态归类"))))
    if len(samples) < 3:
        samples.append(actions[-1])

# ---------- 文件 2:回复情况 ----------
for row in load_rows(REPLIES_XLSX):
    name = cell(row, "Name")
    if not name:
        continue
    linkedin = cell(row, "LinkedIn")
    email = cell(row, "邮箱")
    if email and "@" not in email:
        email = None
    pid, how = match_person(name, email=email, linkedin=linkedin, company=cell(row, "company"))

    prog = cell(row, "进展")
    status = reply_status(prog or "")
    first, last = split_name(name)
    notes_parts = []
    for label, key in [("备注", "备注"), ("微信", "微信"), ("简历", "简历有无")]:
        v = cell(row, key)
        if v:
            notes_parts.append(f"[{label}] {v}")
    person_data = {
        "first_name": first, "last_name": last,
        "linkedin_url": linkedin if li_slug(linkedin) else None,
        "email": email,
        "company": cell(row, "company"),
    }
    if how.startswith("ambiguous"):
        stats["ambiguous"] += 1
        ambiguous_report.append(("回复", name, how))
        continue
    if pid is None:
        if not person_data["linkedin_url"]:
            person_data["linkedin_url"] = f"reply://{slugify(name)}-{slugify(cell(row,'company') or '')}"
        stats["created"] += 1
    else:
        stats["matched"] += 1
    actions.append(("reply", dict(pid=pid, person=person_data, status=status,
                                  notes="\n".join(notes_parts), prog=prog,
                                  channel=cell(row, "联系平台") or "", name=name, how=how)))
    if len(samples) < 6:
        samples.append(actions[-1])


# ---------- 汇报 ----------
print(f"{'=' * 20} {'DRY-RUN(未写库)' if not WRITE else '写入模式'} {'=' * 20}")
print(f"匹配到现有人员: {stats['matched']}  新建: {stats['created']}  歧义待人工: {stats['ambiguous']}")
print("\n--- 样例(前6条) ---")
for kind, a in samples:
    tgt = f"→ 已有 id={a['pid']} ({a['how']})" if a["pid"] else "→ 新建"
    print(f"[{kind}] {a['name']} {tgt}  status={a['status']}")
if ambiguous_report:
    print("\n--- 歧义清单(不会写入,需人工确认) ---")
    for src, name, why in ambiguous_report:
        print(f"[{src}] {name}: {why}")

if not WRITE:
    print("\n确认无误后加 --write 落库")
    sys.exit(0)

# ---------- 落库 ----------
n_hist = 0
for kind, a in actions:
    p = a["person"]
    if a["pid"]:
        # 已有人员:补充字段(upsert_person 不覆盖已有非空值以外逻辑,这里查原 URL)
        c = db.get_conn()
        url = c.execute("SELECT linkedin_url FROM people WHERE id=?", (a["pid"],)).fetchone()[0]
        c.close()
        p["linkedin_url"] = url
    upsert_data = {k: v for k, v in p.items() if v}
    upsert_data["linkedin_url"] = p["linkedin_url"] or ""  # 键必须在(库里存在 url 为空串的行)
    pid, _ = db.upsert_person(upsert_data)
    if kind == "reply":  # 管道数据暂不改状态,原始状态归类只留在 notes 里回溯
        db.update_person_status(pid, a["status"])

    # notes 追加(不覆盖)
    if a["notes"]:
        c = db.get_conn()
        old = c.execute("SELECT notes FROM people WHERE id=?", (pid,)).fetchone()[0] or ""
        if a["notes"] not in old:
            merged = (old + "\n\n" if old else "") + a["notes"]
            c.execute("UPDATE people SET notes=? WHERE id=?", (merged, pid))
            c.commit()
        c.close()

    if kind == "pipeline":
        if a["edu"]["school"]:
            db.add_educations(pid, [a["edu"]])
        if a["ugrad"] and a["ugrad"] != a["edu"]["school"]:
            db.add_educations(pid, [{"school": a["ugrad"], "degree": "本科"}])
        for o in a["owners"]:
            db.add_person_tag(pid, f"owner:{o}", category="custom", source="manual")
        if a["track"]:
            db.add_history(name=a["name"], url=p.get("linkedin_url") or "",
                           community="pipeline", language="zh",
                           message=a["track"], person_id=pid)
            n_hist += 1
    else:  # reply
        db.add_history(name=a["name"], url=p.get("linkedin_url") or "",
                       community=a["channel"] or "reply-tracking", language="zh",
                       message="(导入自 人才库回复情况.xlsx)", person_id=pid)
        c = db.get_conn()
        c.execute("UPDATE history SET status=?, reply=? WHERE id=(SELECT MAX(id) FROM history)",
                  ("replied" if a["status"] in ("replied", "interview") else "sent", a["prog"]))
        c.commit()
        c.close()
        n_hist += 1

print(f"\n完成:写入 history {n_hist} 条")
c = db.get_conn()
print("人才库状态分布:", dict(c.execute("SELECT status, COUNT(*) FROM people GROUP BY status").fetchall()))
c.close()
