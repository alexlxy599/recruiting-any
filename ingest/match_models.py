"""把库内人员与模型族做归属匹配。

为什么不能用子串匹配(首版的教训):
  1. 子串套嵌   "Vidu" 命中 30 人,全部来自 "indi(vidu)al"
  2. 同名产品   "Cosmos" 命中 "@azure cosmos" —— 那是微软数据库,不是 NVIDIA 世界模型
  3. 使用≠建造  "Expert in Gemini/ChatGPT" 是使用者,不该算作该模型的人才

因此三层过滤:词边界 → 负向上下文排除 → 关系分级。
只有 core / build 两级算进覆盖度,use / mention 不算。

用法:
    python3.12 ingest/match_models.py            # 跑匹配并输出报告
    python3.12 ingest/match_models.py --verify   # 抽样打印证据供人工核对
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

REG = "data/raw/model_registry.json"
OUT = "data/raw/model_matches.json"

# 别名。用 \b 词边界,故短名(Wan/Step/Muse)必须配限定词,否则误伤
ALIAS = {
 "Claude":[r"claude"], "GPT":[r"gpt-?[45]", r"gpt image"], "Sora":[r"\bsora\b"],
 "Gemini":[r"gemini"], "Gemma":[r"gemma"], "Veo":[r"\bveo ?[23]"], "Nano Banana":[r"nano banana"],
 "Imagen":[r"\bimagen\b"], "VideoPoet":[r"videopoet"], "MAGVIT":[r"magvit"], "VideoPrism":[r"videoprism"],
 "Grok":[r"\bgrok\b"], "Grok Imagine":[r"grok[- ]imagine"],
 "Llama":[r"\bllama ?[0-9]", r"\bllama\b"], "Muse":[r"muse[- ](image|video|spark|glimmer)"],
 "MetaCLIP":[r"metaclip"], "SAM":[r"\bsam ?3\b", r"sam ?3d"], "DINOv2":[r"dinov2"], "Movie Gen":[r"movie gen"],
 "Qwen":[r"\bqwen"], "Qwen-Image":[r"qwen[- ]image"], "Wan":[r"\bwan ?2\.", r"\bwan ?[23]\b"],
 "Doubao / Seed":[r"doubao", r"豆包", r"\bseed ?[12]\.", r"bytedance seed", r"字节 ?seed"],
 "Seedance":[r"seedance"], "Seedream":[r"seedream"], "LLaVA":[r"llava"],
 "Hunyuan":[r"hunyuan", r"混元"], "Hunyuan3D":[r"hunyuan ?3d"], "HunyuanVideo":[r"hunyuanvideo"],
 "Nemotron":[r"nemotron"], "Cosmos":[r"\bcosmos\b"], "Edify":[r"\bedify\b"],
 "GR00T / Isaac":[r"gr00t", r"isaac ?gr"], "Lyra":[r"\blyra ?[0-9]"],
 "Kimi":[r"\bkimi\b", r"moonshot", r"月之暗面"], "DeepSeek":[r"deepseek"],
 "GLM":[r"\bglm-?[0-9]", r"智谱"], "MiniMax":[r"minimax"], "MiMo":[r"\bmimo-?v?[0-9]"],
 "ERNIE":[r"\bernie\b", r"文心"], "Ling / Ring":[r"\bling ?3\.", r"\bring-?2\."],
 "Step":[r"stepfun", r"\bstep ?3"], "LongCat":[r"longcat"],
 "Kling":[r"\bkling\b", r"可灵"], "Vidu":[r"\bvidu\b"], "PixVerse":[r"pixverse"], "SkyReels":[r"skyreels"],
 "Nova":[r"amazon nova", r"\bnova ?[12]\."], "Apple Foundation Models":[r"apple foundation model", r"\bafm ?[0-9]?\b"],
 "MAI-Image":[r"mai-?image"], "Phi":[r"\bphi-?[34]\b"],
 "Mistral / Magistral / Devstral":[r"\bmistral\b", r"magistral", r"devstral"],
 "FLUX":[r"\bflux\.[12]", r"\bflux ?\[" ], "Command":[r"command ?[ar]\b"],
 "Granite":[r"\bgranite ?[0-9]"], "Olmo / Molmo":[r"\bolmo\b", r"\bmolmo\b"],
 "Unified-IO":[r"unified-?io"], "Inkling":[r"\binkling\b", r"thinking machines"],
 "LTX":[r"\bltx-?[0-9]"], "Ray / Uni":[r"\bray ?3\b", r"\buni-?1\b"], "Pika":[r"\bpika\b"],
 "Meshy":[r"\bmeshy\b"], "Roblox Reality":[r"roblox reality"], "Nex-N":[r"nex-?n", r"nex ?agi"],
 "Hermes":[r"\bhermes ?[34]"], "Mercury":[r"\bmercury\b"],
 "WizardLM / WizardCoder":[r"wizardlm", r"wizardcoder"], "BitNet":[r"bitnet"],
 "InternVL":[r"internvl"], "WorldGen":[r"worldgen"],
}

# 负向上下文:命中这些说明是同名的别的东西
NEGATIVE = {
 "Cosmos":[r"azure ?cosmos", r"cosmos ?db"],
 "Mercury":[r"mercury ?(retrograde|planet)"],
 "Nova":[r"supernova", r"nova ?scotia"],
 "Pika":[r"pikachu"],
 "Grok":[r"\bgrokking\b"],
 "Ray / Uni":[r"\bray ?tracing", r"\bray-?ban"],
}

# 关系分级。按顺序判,先命中者胜
# 关系分级。按顺序判,先命中者胜。
# 注意 build 里的「机构模式」——首版只写了动词(work on/负责),漏掉了最强的信号:
# 「在某模型的组/团队里」本身就是归属证据。抽样核对时发现 "SAM 3D Team"、
# "NVIDIA Cosmos Lab"、"DeepMind (Gemini)" 全被误判成 mention,故补入。
_M = r"[\w\- ]{0,18}"     # 模型名与 Team/Lab 之间允许的间隔
ROLE_RULES = [
 ("core", r"(core (contributor|author)|核心(贡献者|作者)|主导|领导|创建者|creator of|"
          r"I led|we led|led the (development|team)|(paper )?lead author|first author|一作|"
          r"lead(ing)?[\w\s\-]{0,24}(development|research|effort|team)|发明|提出了|\"title\"\s*:\s*\"[^\"]{0,30}(lead|head|director|负责人)\b)"),
 ("build", r"(work(ing)? on|contribut(e|ing|ed|or) to|参与(研发|开发)?|负责|发布|we release|"
           r"I propose|member of" + _M + r"team|研发|做过|开发了|part of|"
           # 机构模式:X Team / X Lab / (X) / @ X —— 在这个模型的组里
           r"part of)"),
 ("use",   r"(expert in|experience with|使用|用过|based on|built (on|with)|powered by|"
           r"evaluat(e|ed) on|compared (to|with)|benchmark(ed)? on|调用)"),
]
# 机构模式必须**紧贴模型名**才算数。放进大窗口会误判:
# "Health AI team at Google ... Gemini" 里的 team 属于 Health AI,不属于 Gemini。
# organization/lab 是我们自己提取 schema 里的字段名 —— 模型名出现在这两个字段的值里,
# 等于「这个模型就是他的所属组织」,是最硬的归属证据(例:#4444 的
# "title":"Post-train Lead","organization":"Apple Foundation Models")。
INSTITUTIONAL = r"(team|lab|group|组|团队|\)|@|\"(organization|lab)\"\s*:)"
COUNTED = ("core", "build")
WIN = 160    # 动词模式:宽窗口
NEAR = 28    # 机构模式:紧窗口,只看模型名前后一小段


def build_corpus(conn):
    """人 → 文本。分片段存,便于定位证据来源。"""
    c = defaultdict(list)
    for r in conn.execute("SELECT person_id, json FROM extractions WHERE source='homepage'"):
        if r[1]: c[r[0]].append(("画像", r[1]))
    for r in conn.execute("SELECT person_id, name, COALESCE(description,'') FROM projects"):
        c[r[0]].append(("项目", f"{r[1]} {r[2]}"))
    for r in conn.execute("SELECT id, COALESCE(research_area,''), COALESCE(headline,''), COALESCE(notes,'') FROM people"):
        for lbl, t in (("方向", r[1]), ("headline", r[2]), ("notes", r[3])):
            if t: c[r[0]].append((lbl, t))
    return c


def classify(ctx, near):
    """ctx = 宽窗口(判动词),near = 紧贴模型名的片段(判机构归属)。"""
    for role, pat in ROLE_RULES:
        if re.search(pat, ctx, re.I):
            return role
    # 动词没命中,再看模型名是否直接嵌在 Team/Lab/组 里
    if re.search(INSTITUTIONAL, near, re.I):
        return "build"
    return "mention"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()

    reg = json.load(open(REG, encoding="utf-8"))
    conn = db.get_conn()
    corpus = build_corpus(conn)

    matches = defaultdict(dict)   # family -> pid -> {role, evidence, field}
    for fam in reg["families"]:
        name = fam["name"]
        pats = ALIAS.get(name)
        if not pats:
            continue
        negs = NEGATIVE.get(name, [])
        for pid, frags in corpus.items():
            best = None
            for field, txt in frags:
                low = txt.lower()
                for p in pats:
                    for m in re.finditer(p, low, re.I):
                        s, e = m.start(), m.end()
                        ctx = txt[max(0, s - WIN):e + WIN]
                        near = txt[max(0, s - NEAR):e + NEAR]
                        if any(re.search(n, ctx, re.I) for n in negs):
                            continue                       # 同名别物,跳过
                        role = classify(ctx, near)
                        rank = {"core": 0, "build": 1, "use": 2, "mention": 3}[role]
                        if best is None or rank < best[0]:
                            best = (rank, role, re.sub(r"\s+", " ", ctx).strip()[:200], field)
            if best:
                matches[name][pid] = {"role": best[1], "evidence": best[2], "field": best[3]}

    json.dump({k: {str(p): v for p, v in d.items()} for k, d in matches.items()},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("=== 覆盖度(只计 core/build)===")
    rows = []
    for fam in reg["families"]:
        d = matches.get(fam["name"], {})
        counted = [p for p, v in d.items() if v["role"] in COUNTED]
        rows.append((fam, len(counted), len(d) - len(counted)))
    rows.sort(key=lambda x: -x[1])
    for fam, n, dropped in rows:
        if n:
            print(f"  {fam['name'][:24]:26s} {fam['org'][:20]:22s} {n:3d} 人"
                  + (f"   (滤掉 {dropped} 条 use/mention)" if dropped else ""))
    tot = sum(n for _, n, _ in rows)
    drop = sum(d for _, _, d in rows)
    print(f"\n有效归属 {tot} 条,过滤掉 {drop} 条(使用/提及)")
    print(f"覆盖 {sum(1 for _,n,_ in rows if n)}/{len(rows)} 个模型族")

    if a.verify:
        print("\n=== 抽样证据(人工核对)===")
        for name in ("Cosmos", "Gemini", "Vidu", "Doubao / Seed", "SAM"):
            d = matches.get(name, {})
            print(f"\n--- {name} ---")
            for pid, v in list(d.items())[:4]:
                print(f"  #{pid} [{v['role']}] ({v['field']}) {v['evidence'][:130]}")


if __name__ == "__main__":
    main()
