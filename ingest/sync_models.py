"""把模型清单与匹配结果同步进库(models / person_models)。

前置:
    python3.12 ingest/match_models.py     # 产出 data/raw/model_matches.json

用法:
    python3.12 ingest/sync_models.py          # dry-run
    python3.12 ingest/sync_models.py --commit
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

REG = "config/model_registry.json"   # 清单是配置,不含 PII,进版本库
MATCH = "data/raw/model_matches.json"

# 技术栈从证据文本推断。只在证据里明确出现时才打,推不出就留空 ——
# 宁可空着,也不要让人以为这个标签是核实过的。
STACK_RULES = [
    ("后训练/RLHF", r"post[- ]?train|后训练|rlhf|dpo|grpo|preference optimi|对齐微调"),
    ("RL",          r"\breinforcement learning\b|\brl\b|强化学习"),
    ("预训练",       r"pre[- ]?train|预训练|pretraining"),
    ("多模态",       r"multimodal|multi[- ]modal|多模态|vision[- ]language|\bvlm\b|\bmllm\b"),
    ("视频生成",     r"video generation|视频生成|text[- ]to[- ]video|video diffusion"),
    ("图像生成",     r"image generation|图像生成|text[- ]to[- ]image|文生图"),
    ("3D/世界模型",  r"world model|世界模型|\b3d\b|\b4d\b|gaussian splat|neural render"),
    ("推理加速/量化", r"quantiz|量化|效率|efficien|inference accel|推理加速|distill|蒸馏"),
    ("Agent",       r"\bagent|智能体|tool use|工具使用"),
    ("评测",         r"benchmark|评测|evaluat"),
    ("数据",         r"data (curation|pipeline|sourcing)|数据(治理|流水线|采集)|synthetic data|合成数据"),
    ("Infra/训练系统", r"\binfra\b|training system|训练系统|serving|分布式训练"),
    ("安全对齐",     r"safety|alignment|安全|对齐|red[- ]team"),
]


def infer_stack(text: str) -> str:
    hits = [name for name, pat in STACK_RULES if re.search(pat, text, re.I)]
    return ", ".join(hits[:3])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()

    reg = json.load(open(REG, encoding="utf-8"))
    matches = json.load(open(MATCH, encoding="utf-8"))

    plan_models = reg["families"]
    plan_links = []
    for fam_name, people in matches.items():
        for pid, v in people.items():
            plan_links.append({
                "pid": int(pid), "family": fam_name, "role": v["role"],
                "evidence": v["evidence"], "field": v["field"],
                "stack": infer_stack(v["evidence"]),
            })

    counted = [l for l in plan_links if l["role"] in db.MODEL_COUNTED_ROLES]
    print(f"模型族 {len(plan_models)} 个")
    print(f"归属关系 {len(plan_links)} 条,其中计入覆盖(core/build) {len(counted)} 条")
    print("  角色分布:", dict(Counter(l["role"] for l in plan_links)))
    stacked = [l for l in counted if l["stack"]]
    print(f"  推断出技术栈的 {len(stacked)}/{len(counted)}(推不出的留空,不猜)")
    print("\n前 5 条样例:")
    for l in counted[:5]:
        print(f"  #{l['pid']:<5} {l['family'][:20]:22s} [{l['role']:5s}] "
              f"栈={l['stack'] or '—':24s} 来源={l['field']}")

    if not a.commit:
        print("\n(dry-run,未写库。加 --commit 执行)")
        return

    # 单连接完成,避免 db.py 里逐条 commit 的函数造成自锁(GitHub 那轮踩过)
    conn = db.get_conn()
    for m in plan_models:
        conn.execute(
            """INSERT INTO models (family, org, category, source) VALUES (?,?,?,?)
               ON CONFLICT(family) DO UPDATE SET org=excluded.org,
                 category=excluded.category, source=excluded.source,
                 updated_at=CURRENT_TIMESTAMP""",
            (m["name"], m["org"], m["cat"], m["source"]))
    mid = {r["family"]: r["id"] for r in conn.execute("SELECT id, family FROM models")}

    conn.execute("DELETE FROM person_models")     # 全量重建,匹配器可重跑
    ok = 0
    for l in plan_links:
        m_id = mid.get(l["family"])
        if not m_id:
            continue
        conn.execute(
            """INSERT INTO person_models
                 (person_id, model_id, role, tech_stack, evidence, source_field)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(person_id, model_id) DO UPDATE SET
                 role=excluded.role, tech_stack=excluded.tech_stack,
                 evidence=excluded.evidence, source_field=excluded.source_field,
                 observed_at=CURRENT_TIMESTAMP""",
            (l["pid"], m_id, l["role"], l["stack"], l["evidence"], l["field"]))
        ok += 1
    conn.commit()
    conn.close()
    print(f"\n已写入 models {len(plan_models)} 行 / person_models {ok} 行")


if __name__ == "__main__":
    main()
