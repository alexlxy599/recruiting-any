"""把 arXiv 作者块解析成 作者 → 机构 / 邮箱 的归属表。

arXiv(ar5iv) 的头部格式相当规整：
    姓名 [邮箱] Affiliation: 机构 [Affiliation: 第二机构] [Correspondence to: 邮箱]
所以按姓名定位切段就能拿到归属，不需要 LLM。

难点在姓名对齐 —— icml.cc 上的写法和 arXiv 上经常不一致：
    HuangJunTao → Juntao Huang     (姓名黏连且颠倒)
    Lilaiyi     → Laiyi Li
    XuanAng Liu → Xuan Ang Liu
所以匹配要过三道：原样 / 姓名互换 / 去空格后比对。对不上的显式报出来，
不静默丢弃 —— 对不上的人正是最该人工看一眼的。

用法:
    python3.12 ingest/attribute_quant_authors.py
"""
import json
import re

BLOCKS = "data/raw/icml2026_quant_authorblocks.json"
OUT = "data/raw/icml2026_quant_attribution.json"

HIT_SZ = "School of Computer Science and Technology, Harbin Institute of Technology, Shenzhen"

# 人工核对过的修正。每条都注明依据 —— 自动解析在这几处会错，且错法各不相同，
# 与其把规则堆进正则（会带出新的误伤），不如把已核实的结论显式钉住。
CORRECTIONS = {
    # icml.cc 上姓名黏连/颠倒，arXiv 给了正确写法
    "HuangJunTao":   {"name": "Juntao Huang", "institution": HIT_SZ,
                      "why": "icml.cc 写作 HuangJunTao；arXiv 2605.00539 作 Juntao Huang"},
    "Lilaiyi":       {"name": "Laiyi Li", "institution": HIT_SZ,
                      "why": "icml.cc 写作 Lilaiyi；arXiv 2605.00539 作 Laiyi Li"},
    "BingWang Wang": {"name": "Bing Wang", "institution": "Huawei Technologies Ltd",
                      "why": "icml.cc 写作 BingWang Wang；arXiv 2605.00539 作 Bing Wang"},
    # 名字出现在别人的脚注里("Anbang Yao conceived the project")，被抢先匹配，位置和机构都错了
    "Anbang Yao":    {"institution": "Intel Labs China", "email": "anbang.yao@intel.com",
                      "corresponding": True, "is_last": True,
                      "why": "解析误匹配到 Shigeng Wang 脚注；实为 CAT-Q 末位通讯作者"},
    # arXiv 版无 Affiliation；同篇另两位(Zhixuan Chen / Dawei Yang)均为 Houmo AI
    "Xu Chen":       {"name": "Chen Xu", "institution": "Houmo AI (推断)",
                      "confidence": "low",
                      "why": "icml.cc 作 Xuchen，arXiv 作 Chen Xu，姓名归属存疑；"
                             "机构按同篇合著者推断，未证实"},
}


def squash(s):
    return re.sub(r"[^a-z]", "", (s or "").lower())


def name_variants(name):
    """生成一个人名的可能写法，用于在 arXiv 块里定位。"""
    parts = name.split()
    v = {squash(name)}
    if len(parts) >= 2:
        v.add(squash(" ".join(parts[::-1])))              # 姓名互换
        v.add(squash(parts[-1] + parts[0]))
        v.add(squash(parts[0] + parts[-1]))
    return v


def find_positions(block, authors):
    """返回 [(pos, author, matched_text)]，按出现位置排序。"""
    sq = squash(block)
    # squash 后的下标 → 原文下标
    idx_map, j = [], 0
    for i, ch in enumerate(block):
        if ch.isalpha():
            idx_map.append(i)
    hits, missed = [], []
    for a in authors:
        best = None
        for v in name_variants(a):
            if len(v) < 4:
                continue
            p = sq.find(v)
            if p >= 0 and (best is None or p < best[0]):
                best = (p, v)
        if best is None:
            missed.append(a)
            continue
        real = idx_map[best[0]] if best[0] < len(idx_map) else 0
        hits.append((real, a, best[1]))
    hits.sort()
    return hits, missed


AFF_RE = re.compile(r"Affiliation:\s*(.+?)(?=\s*(?:Affiliation:|Correspondence to:|Email:|$))", re.S)
MAIL_RE = re.compile(r"[\w.\-+]+@[\w.\-]+\.\w+")


def clean_org(s):
    s = re.sub(r"\s+", " ", s).strip(" ,.;")
    s = re.sub(r"\.\s*(This work|Qualcomm AI Research is an initiative).*$", "", s, flags=re.I)
    return s.strip(" ,.;")


def main():
    data = json.load(open(BLOCKS, encoding="utf-8"))
    result, unmatched, no_aff = {}, [], []

    for aid, r in data.items():
        block, authors = r["block"], r["authors"]
        hits, missed = find_positions(block, authors)
        for a in missed:
            unmatched.append({"arxiv": aid, "title": r["title"], "author": a})

        for k, (pos, author, _) in enumerate(hits):
            end = hits[k + 1][0] if k + 1 < len(hits) else len(block)
            seg = block[pos:end]
            orgs = [clean_org(m) for m in AFF_RE.findall(seg)]
            orgs = [o for o in orgs if o and len(o) > 2]
            mails = MAIL_RE.findall(seg)
            rec = {
                "arxiv": aid,
                "title": r["title"],
                "poster": r["poster"],
                "institution": orgs[0] if orgs else "",
                "institution_2": orgs[1] if len(orgs) > 1 else "",
                "email": mails[0] if mails else "",
                "corresponding": "Correspondence to:" in seg,
                "pos": k,
                "n_authors": len(hits),
            }
            if not orgs:
                no_aff.append({"arxiv": aid, "author": author})
            prev = result.get(author)
            # 同名多篇时保留信息更全的那条
            if not prev or (rec["institution"] and not prev["institution"]):
                result[author] = rec

    # ── 邮箱归属校验 ──
    # "Correspondence to: X" 出现在谁的段落里，取决于姓名匹配的位置，一旦匹配
    # 错位（例如名字出现在别人的脚注里），通讯邮箱就会挂到无关的人头上。
    # 后果是把邮件发给错的人，必须挡住：邮箱与姓名对不上就摘掉并标记。
    def affinity(name, email):
        local = re.split(r"[@+]", email)[0].lower()
        local_sq = re.sub(r"[^a-z]", "", local)
        toks = [squash(t) for t in name.split() if len(t) > 1]
        for t in toks:
            if t and (t in local_sq or local_sq.startswith(t[:4])):
                return True
        # 姓+名首字母 / 名首字母+姓 这类缩写
        if len(toks) >= 2:
            a, b = toks[0], toks[-1]
            if local_sq in (b + a[0], a[0] + b, b[0] + a, a + b[0],
                            a[0] + b[0], b[0] + a[0]):
                return True
            # J.Y. Chung → jyc：所有名的首字母 + 姓
            if local_sq == "".join(t[0] for t in toks[:-1]) + b[0]:
                return True
        return False

    stripped = []
    for author, rec in result.items():
        if not rec.get("email"):
            continue
        if affinity(author, rec["email"]):
            continue
        # 同篇里有别人跟这个邮箱对得上 → 邮箱是那个人的，摘掉
        owner = next((o for o, r2 in result.items()
                      if r2.get("arxiv") == rec.get("arxiv") and o != author
                      and affinity(o, rec["email"])), None)
        if owner:
            stripped.append(f"{author} ✂ {rec['email']} (实为 {owner} 的)")
            rec["email"] = ""
            rec["corresponding"] = False
        else:
            rec["email_uncertain"] = True
            stripped.append(f"{author} ? {rec['email']} (对不上姓名，保留但标记)")

    # 应用人工修正。改名的把旧键搬到新键，避免留下两条。
    applied = []
    for old, fix in CORRECTIONS.items():
        rec = result.pop(old, None)
        if rec is None and "name" not in fix:
            continue
        rec = rec or {"arxiv": "", "title": "", "poster": "", "institution_2": "",
                      "email": "", "corresponding": False, "pos": None, "n_authors": None}
        for k, v in fix.items():
            if k not in ("name", "why"):
                rec[k] = v
        rec["corrected"] = fix["why"]
        result[fix.get("name", old)] = rec
        applied.append(f"{old} → {fix.get('name', old)}")
        unmatched[:] = [u for u in unmatched if u["author"] != old]
        no_aff[:] = [u for u in no_aff if u["author"] != old]

    json.dump({"attribution": result, "unmatched": unmatched, "no_affiliation": no_aff},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"邮箱校验: 处理 {len(stripped)} 条")
    for x in stripped:
        print(f"   {x}")
    print()
    print(f"应用人工修正 {len(applied)} 条: {', '.join(applied)}\n")
    n = len(result)
    print(f"归属 {n} 人  →  {OUT}")
    print(f"  有机构 {sum(1 for v in result.values() if v['institution'])}")
    print(f"  有邮箱 {sum(1 for v in result.values() if v['email'])}")
    print(f"  通讯作者 {sum(1 for v in result.values() if v['corresponding'])}")
    print(f"\n姓名对不上(需人工看) {len(unmatched)}:")
    for u in unmatched:
        print(f"   {u['author']:22s} {u['title'][:44]}")
    print(f"\n有名字但没机构 {len(no_aff)}:")
    for u in no_aff[:15]:
        print(f"   {u['author']:22s} {u['arxiv']}")


if __name__ == "__main__":
    main()
