"""向量化 + 语义搜索模块。

用 fastembed（本地 ONNX）生成 embedding，LanceDB 存储和检索。
"""

import os
import sys

import lancedb
from fastembed import TextEmbedding

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VECTORS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "vectors")
MODEL_NAME = "BAAI/bge-small-en-v1.5"  # 384 维，体积小速度快，英文为主够用

_model = None


def get_model() -> TextEmbedding:
    global _model
    if _model is None:
        _model = TextEmbedding(MODEL_NAME)
    return _model


def get_db() -> lancedb.DBConnection:
    os.makedirs(VECTORS_DIR, exist_ok=True)
    return lancedb.connect(VECTORS_DIR)


def person_to_text(person: dict) -> str:
    """把候选人信息拼成一段文本用于 embedding。"""
    parts = []
    name = f"{person.get('first_name', '')} {person.get('last_name', '')}".strip()
    if name:
        parts.append(name)
    if person.get("title"):
        parts.append(person["title"])
    if person.get("company"):
        parts.append(f"at {person['company']}")
    if person.get("headline"):
        parts.append(person["headline"])
    if person.get("location"):
        parts.append(person["location"])

    # 工作经历
    experiences = person.get("experiences", [])
    if experiences:
        exp_lines = []
        for exp in experiences[:8]:
            exp_lines.append(f"{exp.get('title', '')} at {exp.get('company', '')}")
        parts.append("Experience: " + "; ".join(exp_lines))

    # 教育背景
    educations = person.get("educations", [])
    if educations:
        edu_lines = []
        for edu in educations:
            line = edu.get("school", "")
            if edu.get("degree"):
                line += f" - {edu['degree']}"
            edu_lines.append(line)
        parts.append("Education: " + "; ".join(edu_lines))

    return ". ".join(parts)


def build_index(people: list[dict], batch_size: int = 256):
    """对所有候选人建立向量索引。

    people: list of dicts from db.get_person() (含 experiences, educations)
    """
    model = get_model()
    db = get_db()

    # 准备文本
    texts = []
    records = []
    for p in people:
        text = person_to_text(p)
        texts.append(text)
        records.append({
            "person_id": p["id"],
            "text": text[:500],  # 存一份截断文本方便调试
        })

    # 批量 embedding
    print(f"正在向量化 {len(texts)} 条记录...")
    embeddings = list(model.embed(texts, batch_size=batch_size))

    for i, emb in enumerate(embeddings):
        records[i]["vector"] = emb.tolist()

    # 写入 LanceDB（覆盖旧表）
    table_name = "people"
    if table_name in db.table_names():
        db.drop_table(table_name)
    db.create_table(table_name, records)
    print(f"向量索引已建立: {len(records)} 条，存储于 {VECTORS_DIR}")


def semantic_search(query: str, limit: int = 30) -> list[dict]:
    """语义搜索：返回 [{person_id, text, _distance}, ...]，按相似度排序。"""
    model = get_model()
    db = get_db()

    if "people" not in db.table_names():
        return []

    table = db.open_table("people")
    query_embedding = list(model.embed([query]))[0].tolist()

    results = (
        table.search(query_embedding)
        .limit(limit)
        .to_list()
    )

    return [{"person_id": r["person_id"], "text": r["text"], "_distance": r["_distance"]} for r in results]


if __name__ == "__main__":
    """命令行用法: python ai/embedder.py [--rebuild]"""
    import argparse
    from db import init_db, get_person, count_people, get_conn

    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true", help="重建向量索引")
    parser.add_argument("--search", type=str, help="测试语义搜索")
    args = parser.parse_args()

    init_db()

    if args.rebuild:
        total = count_people()
        print(f"从数据库加载 {total} 人...")
        conn = get_conn()
        ids = [r[0] for r in conn.execute("SELECT id FROM people").fetchall()]
        conn.close()

        people = []
        for i, pid in enumerate(ids):
            p = get_person(pid)
            if p:
                people.append(p)
            if (i + 1) % 500 == 0:
                print(f"  加载: {i+1}/{total}")
        build_index(people)

    if args.search:
        results = semantic_search(args.search, limit=10)
        if not results:
            print("未建立索引，先运行: python ai/embedder.py --rebuild")
        else:
            print(f"\n搜索: {args.search}")
            print(f"{'='*60}")
            for r in results:
                p = get_person(r["person_id"])
                if p:
                    print(f"  [{r['_distance']:.3f}] {p['first_name']} {p['last_name']} — {p['title']} @ {p['company']}")
