"""ICML 2026 量化 cohort 的 arXiv 补全。

两步：
  1. 按标题在 arXiv API 反查 arxiv_id，用标题相似度卡阈值（防误匹配到同名老论文）
  2. 抓 arXiv HTML 全文头部，留存作者/机构/邮箱原文块，供后续精确归属

只做抓取和留存，不做归属判断 —— 机构块格式太杂（共享脚注、上标编号、
逗号分隔多单位），交给后面的人/模型读原文映射，比正则硬猜准。

用法:
    python3.12 ingest/enrich_quant_arxiv.py --lookup    # 第一步
    python3.12 ingest/enrich_quant_arxiv.py --fetch     # 第二步
"""
import argparse
import difflib
import json
import os
import re
import time
import urllib.parse

import requests

IN = "data/raw/icml2026_quant_cohort.json"
IDS = "data/raw/icml2026_quant_arxiv.json"
HTML = "data/raw/icml2026_quant_arxiv_html.json"

S = requests.Session()
S.headers["User-Agent"] = "RecruitingAny/1.0 (academic talent research; contact via repo)"

ARXIV_DELAY = 3.2   # arXiv API 要求 >3s

# 判定同一篇论文的两条独立证据。只靠标题会两头出错：
#   预印本改过题 → 同一篇被拒（LiftQuant sim=0.58）
#   撞缩写      → 不同工作被收（两篇都叫 MixQuant，sim=0.44）
# 作者列表重合是更硬的证据 —— 六个人名同时撞车的概率极低。
TITLE_STRONG = 0.75   # 标题够像，单独成立
AUTH_STRONG = 0.50    # 作者过半重合，单独成立
TITLE_WEAK = 0.35     # 标题+作者都弱不收
AUTH_WEAK = 0.34


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def norm_person(s):
    return re.sub(r"[^a-z]", "", (s or "").lower())


def lookup(title, authors):
    """查 arXiv，用标题相似度 + 作者重合度双证据。返回 dict 或 None。"""
    q = re.sub(r"[^\w\s]", " ", title.split(":")[0])[:80]
    ours = {norm_person(a) for a in authors if a}
    cands = []
    net_failed = False
    for field in ("ti", "all"):
        url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(
            {"search_query": f'{field}:"{q}"', "max_results": 5})
        r = None
        # 退避重试。不重试就会把限流/超时记成"查无此文" —— 传输故障不该
        # 变成数据结论，这两种情况后续处理方式完全不同。
        for attempt in range(4):
            try:
                r = S.get(url, timeout=40)
                if r.status_code == 200:
                    break
                if r.status_code in (429, 503):
                    time.sleep(15 + attempt * 20)
                    continue
                break
            except requests.RequestException:
                r = None
                time.sleep(15 + attempt * 20)
        if r is None or r.status_code != 200:
            net_failed = True
            continue
        for e in re.findall(r"<entry>(.*?)</entry>", r.text, re.S):
            i = re.search(r"<id>http://arxiv\.org/abs/([^<]+)</id>", e)
            t = re.search(r"<title>(.*?)</title>", e, re.S)
            if not (i and t):
                continue
            mt = re.sub(r"\s+", " ", t.group(1)).strip()
            theirs = {norm_person(n) for n in re.findall(r"<name>([^<]+)</name>", e)}
            tsim = difflib.SequenceMatcher(None, norm(title), norm(mt)).ratio()
            asim = len(ours & theirs) / max(1, min(len(ours), len(theirs))) if ours else 0.0
            cands.append({"arxiv": i.group(1), "matched": mt,
                          "tsim": round(tsim, 2), "asim": round(asim, 2),
                          "n_shared": len(ours & theirs)})
        if cands:
            break
        time.sleep(ARXIV_DELAY)
    if not cands:
        # 区分"网络没通"和"确实查无此文" —— 前者要重跑，后者是结论
        return {"verdict": "error"} if net_failed else None
    cands.sort(key=lambda c: (c["asim"], c["tsim"]), reverse=True)
    best = cands[0]
    if best["asim"] >= AUTH_STRONG or best["tsim"] >= TITLE_STRONG:
        best["verdict"] = "accept"
    elif best["tsim"] >= TITLE_WEAK and best["asim"] >= AUTH_WEAK:
        best["verdict"] = "accept"
    else:
        best["verdict"] = "reject"
    return best


def do_lookup(only_missing=False):
    papers = json.load(open(IN, encoding="utf-8"))
    prev = {}
    if only_missing and os.path.exists(IDS):
        for p in json.load(open(IDS, encoding="utf-8")):
            prev[p["title"]] = p

    out = []
    for n, (title, poster, tier, authors) in enumerate(papers, 1):
        old = prev.get(title)
        # 已命中的、以及已判定"确实没有预印本"的，不重跑；只补网络失败的
        if old and (old.get("arxiv") or old.get("verdict") == "no_match"):
            out.append(old)
            print(f"  [{n:2}/{len(papers)}] 跳过(已有结果) {title[:44]}", flush=True)
            continue
        r = lookup(title, authors)
        time.sleep(ARXIV_DELAY)
        rec = {"title": title, "poster": poster, "tier": tier, "authors": authors,
               "arxiv": None}
        if r and r.get("verdict") == "error":
            rec["verdict"] = "network_error"
            print(f"  [{n:2}/{len(papers)}] !!  网络失败(需重跑)  {title[:44]}", flush=True)
            out.append(rec)
            continue
        if r:
            rec.update({"tsim": r["tsim"], "asim": r["asim"], "n_shared": r["n_shared"]})
        if r and r["verdict"] == "accept":
            rec["arxiv"] = r["arxiv"]
            print(f"  [{n:2}/{len(papers)}] OK  {r['arxiv']:13s} "
                  f"t={r['tsim']:.2f} a={r['asim']:.2f}({r['n_shared']}人)  {title[:40]}", flush=True)
        else:
            rec["verdict"] = "no_match"
            rec["rejected"] = r["matched"][:70] if r else None
            info = (f"t={r['tsim']:.2f} a={r['asim']:.2f} → {r['matched'][:38]}"
                    if r else "查无此文")
            print(f"  [{n:2}/{len(papers)}] --  {title[:36]}  {info}", flush=True)
        out.append(rec)
    os.makedirs("data/raw", exist_ok=True)
    json.dump(out, open(IDS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ok = sum(1 for o in out if o.get("arxiv"))
    err = sum(1 for o in out if o.get("verdict") == "network_error")
    print(f"\n命中 {ok}/{len(out)} | 确实无预印本 {len(out)-ok-err} | "
          f"网络失败待重跑 {err}  →  {IDS}")


HEAD_LIMIT = 6000   # 机构信息都在头部，正文不要


def fetch_html():
    recs = json.load(open(IDS, encoding="utf-8"))
    todo = [r for r in recs if r.get("arxiv")]
    out = {}
    for n, r in enumerate(todo, 1):
        aid = r["arxiv"]
        text = None
        for url in (f"https://arxiv.org/html/{aid}", f"https://arxiv.org/abs/{aid}"):
            try:
                resp = S.get(url, timeout=25)
            except requests.RequestException as e:
                print(f"  [{n:2}/{len(todo)}] {aid} FAIL {type(e).__name__}", flush=True)
                continue
            if resp.status_code != 200:
                continue
            html = resp.text
            # 去脚本样式，压平标签，只留头部
            html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
            plain = re.sub(r"<[^>]+>", " ", html)
            plain = re.sub(r"&#x?\w+;", " ", plain)
            plain = re.sub(r"\s+", " ", plain).strip()
            if len(plain) > 400:
                text = plain[:HEAD_LIMIT]
                out[aid] = {"title": r["title"], "poster": r["poster"],
                            "authors": r["authors"], "src": url, "head": text}
                print(f"  [{n:2}/{len(todo)}] {aid} OK {len(text)}b  {url.split('/')[-2]}", flush=True)
                break
        if not text:
            print(f"  [{n:2}/{len(todo)}] {aid} 无正文", flush=True)
        time.sleep(1.5)
    json.dump(out, open(HTML, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n抓到 {len(out)}/{len(todo)}  →  {HTML}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookup", action="store_true")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--resume", action="store_true", help="只补网络失败的")
    a = ap.parse_args()
    if a.lookup or a.resume:
        do_lookup(only_missing=a.resume)
    if a.fetch:
        fetch_html()
    if not (a.lookup or a.fetch):
        ap.print_help()
