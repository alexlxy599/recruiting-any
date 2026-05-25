import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sender_config (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        name        TEXT NOT NULL DEFAULT '',
        team        TEXT NOT NULL DEFAULT '',
        contact     TEXT NOT NULL DEFAULT '',
        updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    INSERT OR IGNORE INTO sender_config (id) VALUES (1);

    CREATE TABLE IF NOT EXISTS history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        url         TEXT NOT NULL DEFAULT '',
        community   TEXT NOT NULL DEFAULT '',
        language    TEXT NOT NULL DEFAULT 'zh',
        message     TEXT NOT NULL,
        person_id   INTEGER REFERENCES people(id),
        status      TEXT NOT NULL DEFAULT 'sent',
        reply       TEXT,
        replied_at  TIMESTAMP,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- ── 人才库核心表 ──

    CREATE TABLE IF NOT EXISTS people (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        first_name    TEXT NOT NULL DEFAULT '',
        last_name     TEXT NOT NULL DEFAULT '',
        linkedin_url  TEXT NOT NULL UNIQUE,
        email         TEXT,
        github_url    TEXT,
        title         TEXT,
        headline      TEXT,
        company       TEXT,
        location      TEXT,
        industry      TEXT,
        notes         TEXT,
        status        TEXT NOT NULL DEFAULT 'new',
        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS experiences (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        position    INTEGER NOT NULL DEFAULT 0,
        is_current  INTEGER NOT NULL DEFAULT 0,
        title       TEXT NOT NULL DEFAULT '',
        company     TEXT NOT NULL DEFAULT '',
        start_year  INTEGER,
        end_year    INTEGER
    );

    CREATE TABLE IF NOT EXISTS educations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        school      TEXT NOT NULL DEFAULT '',
        degree      TEXT NOT NULL DEFAULT '',
        field       TEXT,
        start_year  INTEGER,
        end_year    INTEGER
    );

    CREATE INDEX IF NOT EXISTS idx_experiences_person ON experiences(person_id);
    CREATE INDEX IF NOT EXISTS idx_educations_person ON educations(person_id);
    """)

    # Add columns if not exist (safe migration)
    for col, typ in [("description", "TEXT"), ("location", "TEXT"),
                     ("start_date", "TEXT"), ("end_date", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE experiences ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass  # column already exists

    for col, typ in [("description", "TEXT"), ("activities", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE educations ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass

    # Academic talent pool columns
    for col, typ in [("source_type", "TEXT"), ("advisor", "TEXT"),
                     ("institution", "TEXT"), ("personal_page", "TEXT"),
                     ("expected_graduation", "INTEGER"), ("research_area", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE people ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass

    # Backfill: mark existing lab_sourcer imports as academic
    conn.execute("UPDATE people SET source_type='academic' WHERE notes='lab_sourcer' AND source_type IS NULL")

    conn.executescript("""
    CREATE INDEX IF NOT EXISTS idx_people_company ON people(company);
    CREATE INDEX IF NOT EXISTS idx_people_location ON people(location);
    CREATE INDEX IF NOT EXISTS idx_people_status ON people(status);
    CREATE INDEX IF NOT EXISTS idx_people_source_type ON people(source_type);
    CREATE INDEX IF NOT EXISTS idx_people_institution ON people(institution);
    CREATE INDEX IF NOT EXISTS idx_people_advisor ON people(advisor);
    """)

    # FTS5 全文搜索索引（按时效性分列，支持加权排序）
    # 先 drop 再建，确保 schema 变更生效
    try:
        conn.execute("DROP TABLE IF EXISTS people_fts")
    except Exception:
        pass
    conn.executescript("""
    CREATE VIRTUAL TABLE IF NOT EXISTS people_fts USING fts5(
        person_id UNINDEXED,
        current_work,
        recent_work,
        profile,
        education,
        basic_info,
        older_work,
        tokenize='unicode61'
    );
    """)

    conn.commit()
    conn.close()


# ── sender config ──

def get_sender_config():
    conn = get_conn()
    row = conn.execute("SELECT name, team, contact FROM sender_config WHERE id = 1").fetchone()
    conn.close()
    return dict(row) if row else {"name": "", "team": "", "contact": ""}


def save_sender_config(name: str, team: str, contact: str):
    conn = get_conn()
    conn.execute(
        "UPDATE sender_config SET name=?, team=?, contact=?, updated_at=CURRENT_TIMESTAMP WHERE id=1",
        (name, team, contact),
    )
    conn.commit()
    conn.close()


# ── history ──

def add_history(name: str, url: str, community: str, language: str, message: str, person_id: int | None = None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO history (name, url, community, language, message, person_id) VALUES (?, ?, ?, ?, ?, ?)",
        (name, url, community, language, message, person_id),
    )
    conn.commit()
    conn.close()


def get_history(search: str = "", limit: int = 50, offset: int = 0):
    conn = get_conn()
    if search:
        rows = conn.execute(
            "SELECT * FROM history WHERE name LIKE ? OR message LIKE ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (f"%{search}%", f"%{search}%", limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM history ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_history(record_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM history WHERE id = ?", (record_id,))
    conn.commit()
    conn.close()


def clear_history():
    conn = get_conn()
    conn.execute("DELETE FROM history")
    conn.commit()
    conn.close()


# ── people CRUD ──

def upsert_person(data: dict) -> tuple[int, str]:
    """Insert or update a person by linkedin_url.

    Returns (person_id, action) where action is 'created' or 'updated' or 'skipped'.
    """
    conn = get_conn()
    linkedin_url = data["linkedin_url"]

    existing = conn.execute(
        "SELECT id, first_name, last_name, email, github_url, title, headline, company, location, industry "
        "FROM people WHERE linkedin_url = ?",
        (linkedin_url,),
    ).fetchone()

    if existing:
        # Check if anything actually changed
        changed = False
        for col in ("first_name", "last_name", "email", "github_url", "title", "headline", "company", "location", "industry"):
            new_val = data.get(col) or ""
            old_val = existing[col] or ""
            if new_val and new_val != old_val:
                changed = True
                break

        if not changed:
            person_id = existing["id"]
            conn.close()
            return person_id, "skipped"

        conn.execute(
            """UPDATE people SET first_name=?, last_name=?, email=?, github_url=?,
               title=?, headline=?, company=?, location=?, industry=?,
               updated_at=CURRENT_TIMESTAMP
               WHERE linkedin_url=?""",
            (
                data.get("first_name", "") or existing["first_name"],
                data.get("last_name", "") or existing["last_name"],
                data.get("email") or existing["email"],
                data.get("github_url") or existing["github_url"],
                data.get("title") or existing["title"],
                data.get("headline") or existing["headline"],
                data.get("company") or existing["company"],
                data.get("location") or existing["location"],
                data.get("industry") or existing["industry"],
                linkedin_url,
            ),
        )
        person_id = existing["id"]
        action = "updated"
    else:
        cur = conn.execute(
            """INSERT INTO people (first_name, last_name, linkedin_url, email, github_url,
               title, headline, company, location, industry)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("first_name", ""),
                data.get("last_name", ""),
                linkedin_url,
                data.get("email"),
                data.get("github_url"),
                data.get("title"),
                data.get("headline"),
                data.get("company"),
                data.get("location"),
                data.get("industry"),
            ),
        )
        person_id = cur.lastrowid
        action = "created"

    conn.commit()
    conn.close()
    return person_id, action


def add_experiences(person_id: int, experiences: list[dict]):
    """Replace all experiences for a person."""
    conn = get_conn()
    conn.execute("DELETE FROM experiences WHERE person_id = ?", (person_id,))
    for exp in experiences:
        conn.execute(
            "INSERT INTO experiences (person_id, position, is_current, title, company, start_year, end_year) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                person_id,
                exp.get("position", 0),
                exp.get("is_current", 0),
                exp.get("title", ""),
                exp.get("company", ""),
                exp.get("start_year"),
                exp.get("end_year"),
            ),
        )
    conn.commit()
    conn.close()


def add_educations(person_id: int, educations: list[dict]):
    """Replace all educations for a person."""
    conn = get_conn()
    conn.execute("DELETE FROM educations WHERE person_id = ?", (person_id,))
    for edu in educations:
        conn.execute(
            "INSERT INTO educations (person_id, school, degree, field, start_year, end_year) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                person_id,
                edu.get("school", ""),
                edu.get("degree", ""),
                edu.get("field"),
                edu.get("start_year"),
                edu.get("end_year"),
            ),
        )
    conn.commit()
    conn.close()


def get_person(person_id: int) -> dict | None:
    """Get a person with all their experiences and educations."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    if not row:
        conn.close()
        return None

    person = dict(row)
    person["experiences"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM experiences WHERE person_id = ? ORDER BY position", (person_id,)
        ).fetchall()
    ]
    person["educations"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM educations WHERE person_id = ? ORDER BY id", (person_id,)
        ).fetchall()
    ]
    person["history"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM history WHERE person_id = ? ORDER BY created_at DESC", (person_id,)
        ).fetchall()
    ]
    conn.close()
    return person


SEARCH_COLS = ("first_name", "last_name", "company", "title", "headline", "location")


def _like_clause(term: str) -> tuple[str, list[str]]:
    """为单个关键词生成 (sql_fragment, params)，在多列中 OR 匹配。"""
    like = f"%{term}%"
    parts = [f"{col} LIKE ?" for col in SEARCH_COLS]
    return f"({' OR '.join(parts)})", [like] * len(SEARCH_COLS)


def search_people_boolean(query: str, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    """Boolean search：支持 AND / OR / NOT。

    例：Google AND PhD, Microsoft OR Amazon, engineer NOT intern
    返回 (results, total_count)。
    """
    import re

    # 先按 AND/OR/NOT 拆 token（保留运算符）
    tokens = re.split(r'\s+(AND|OR|NOT)\s+', query.strip(), flags=re.IGNORECASE)
    tokens = [t.strip() for t in tokens if t.strip()]

    if not tokens:
        return search_people("", limit, offset), count_people()

    # 构建 WHERE
    clauses = []
    params = []
    pending_op = "AND"  # 默认 AND

    for token in tokens:
        upper = token.upper()
        if upper in ("AND", "OR", "NOT"):
            pending_op = upper
            continue

        frag, p = _like_clause(token)

        if pending_op == "NOT":
            clauses.append(f"NOT {frag}")
            params.extend(p)
            pending_op = "AND"
        elif pending_op == "OR" and clauses:
            # 把上一个 clause 和当前 OR 合并
            prev = clauses.pop()
            prev_params_count = len(SEARCH_COLS)
            clauses.append(f"({prev} OR {frag})")
            params.extend(p)
            pending_op = "AND"
        else:
            clauses.append(frag)
            params.extend(p)
            pending_op = "AND"

    where = " AND ".join(clauses) if clauses else "1"

    conn = get_conn()
    total = conn.execute(f"SELECT COUNT(*) FROM people WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM people WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def search_people(query: str = "", limit: int = 50, offset: int = 0) -> list[dict]:
    """简单关键词搜索。"""
    conn = get_conn()
    if query:
        frag, params = _like_clause(query)
        rows = conn.execute(
            f"SELECT * FROM people WHERE {frag} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM people ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_people(query: str = "") -> int:
    conn = get_conn()
    if query:
        frag, params = _like_clause(query)
        count = conn.execute(f"SELECT COUNT(*) FROM people WHERE {frag}", params).fetchone()[0]
    else:
        count = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    conn.close()
    return count


# ── FTS5 全文搜索 ──

def rebuild_fts():
    """重建整个 FTS5 索引。启动时或批量导入后调用。"""
    conn = get_conn()
    conn.execute("DELETE FROM people_fts")

    people = conn.execute("SELECT id, first_name, last_name, headline, location, industry FROM people").fetchall()

    for p in people:
        pid = p["id"]
        exps = conn.execute(
            "SELECT title, company, description, position, is_current FROM experiences WHERE person_id = ? ORDER BY position",
            (pid,),
        ).fetchall()
        edus = conn.execute(
            "SELECT school, degree, field FROM educations WHERE person_id = ?",
            (pid,),
        ).fetchall()

        # 当前工作（position=0 或 is_current=1）
        current_parts = []
        recent_parts = []
        older_parts = []
        for exp in exps:
            text = " ".join(filter(None, [exp["title"], exp["company"], exp["description"]]))
            if exp["position"] == 0 or exp["is_current"]:
                current_parts.append(text)
            elif exp["position"] == 1:
                recent_parts.append(text)
            else:
                older_parts.append(" ".join(filter(None, [exp["title"], exp["company"]])))

        current_work = " ".join(current_parts)
        recent_work = " ".join(recent_parts)
        older_work = " ".join(older_parts)

        profile = " ".join(filter(None, [p["headline"], p["industry"], p["location"]]))
        education = " ".join(
            " ".join(filter(None, [e["school"], e["degree"], e["field"]])) for e in edus
        )
        basic_info = f"{p['first_name']} {p['last_name']}"

        conn.execute(
            "INSERT INTO people_fts (person_id, current_work, recent_work, profile, education, basic_info, older_work) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, current_work, recent_work, profile, education, basic_info, older_work),
        )

    conn.commit()
    conn.close()


def update_fts_for_person(person_id: int):
    """更新单个候选人的 FTS 索引。enrichment 或编辑后调用。"""
    conn = get_conn()
    # 先删旧记录
    conn.execute("DELETE FROM people_fts WHERE person_id = ?", (person_id,))

    p = conn.execute(
        "SELECT id, first_name, last_name, headline, location, industry FROM people WHERE id = ?",
        (person_id,),
    ).fetchone()
    if not p:
        conn.close()
        return

    exps = conn.execute(
        "SELECT title, company, description, position, is_current FROM experiences WHERE person_id = ? ORDER BY position",
        (person_id,),
    ).fetchall()
    edus = conn.execute(
        "SELECT school, degree, field FROM educations WHERE person_id = ?",
        (person_id,),
    ).fetchall()

    current_parts, recent_parts, older_parts = [], [], []
    for exp in exps:
        text = " ".join(filter(None, [exp["title"], exp["company"], exp["description"]]))
        if exp["position"] == 0 or exp["is_current"]:
            current_parts.append(text)
        elif exp["position"] == 1:
            recent_parts.append(text)
        else:
            older_parts.append(" ".join(filter(None, [exp["title"], exp["company"]])))

    conn.execute(
        "INSERT INTO people_fts (person_id, current_work, recent_work, profile, education, basic_info, older_work) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            person_id,
            " ".join(current_parts),
            " ".join(recent_parts),
            " ".join(filter(None, [p["headline"], p["industry"], p["location"]])),
            " ".join(" ".join(filter(None, [e["school"], e["degree"], e["field"]])) for e in edus),
            f"{p['first_name']} {p['last_name']}",
            " ".join(older_parts),
        ),
    )
    conn.commit()
    conn.close()


import re as _re

# 中英技术词典：中文搜索词 → 英文关键词（支持多个同义词）
_ZH_EN_DICT = {
    # 技术方向
    "推荐系统": "recommendation",
    "推荐": "recommendation",
    "搜索": "search",
    "搜索引擎": "search engine",
    "自然语言处理": "NLP natural language processing",
    "计算机视觉": "computer vision",
    "机器学习": "machine learning",
    "深度学习": "deep learning",
    "强化学习": "reinforcement learning",
    "大模型": "LLM large language model",
    "大语言模型": "LLM large language model",
    "生成式": "generative",
    "人工智能": "AI artificial intelligence",
    "数据科学": "data science",
    "数据工程": "data engineering",
    "数据库": "database",
    "分布式系统": "distributed system",
    "分布式": "distributed",
    "云计算": "cloud computing",
    "后端": "backend",
    "前端": "frontend",
    "全栈": "full stack",
    "移动端": "mobile",
    "安卓": "Android",
    "嵌入式": "embedded",
    "操作系统": "operating system",
    "编译器": "compiler",
    "图形学": "graphics rendering",
    "音视频": "audio video streaming",
    "语音识别": "speech recognition ASR",
    "语音合成": "speech synthesis TTS",
    "图像识别": "image recognition",
    "目标检测": "object detection",
    "图神经网络": "graph neural network GNN",
    "知识图谱": "knowledge graph",
    "多模态": "multimodal",
    "自动驾驶": "autonomous driving self-driving",
    "机器人": "robotics robot",
    "芯片": "chip semiconductor",
    "算法": "algorithm",
    "架构": "architecture",
    "基础设施": "infrastructure",
    "安全": "security",
    "隐私": "privacy",
    "区块链": "blockchain",
    "量化": "quantization quantitative",
    "优化": "optimization",
    "检索": "retrieval search",
    "排序": "ranking",
    "广告": "ads advertising",
    "风控": "risk control fraud detection",
    "推理": "inference reasoning",
    "训练": "training",
    "微调": "fine-tuning",
    "预训练": "pre-training",
    "向量": "vector embedding",
    # 职位
    "工程师": "engineer",
    "研究员": "researcher scientist",
    "经理": "manager",
    "总监": "director",
    "架构师": "architect",
    "技术负责人": "tech lead",
    "实习": "intern internship",
    # 学位
    "博士": "PhD doctoral",
    "硕士": "master",
    "本科": "bachelor",
    # 公司/行业
    "谷歌": "Google",
    "微软": "Microsoft",
    "亚马逊": "Amazon",
    "脸书": "Facebook Meta",
    "苹果": "Apple",
    "英伟达": "NVIDIA",
    "字节跳动": "ByteDance TikTok",
    "腾讯": "Tencent",
    "阿里巴巴": "Alibaba",
    "阿里": "Alibaba",
    "百度": "Baidu",
    "华为": "Huawei",
    "金融": "finance financial",
    "医疗": "healthcare medical",
    "游戏": "gaming game",
    "电商": "e-commerce",
    "社交": "social",
}


def _translate_query(query: str) -> list[str]:
    """将中文搜索词翻译为英文关键词列表。每个中文词可能产生多个同义英文词。

    返回所有应搜索的英文词列表（用 OR 关系搜索）。
    """
    result = query
    all_terms = []
    # 按词典长度降序匹配，确保"推荐系统"优先于"推荐"
    for zh, en in sorted(_ZH_EN_DICT.items(), key=lambda x: -len(x[0])):
        if zh in result:
            result = result.replace(zh, "")
            all_terms.extend(en.split())
    # 剩余非中文词也加入
    for word in result.split():
        if word.strip():
            all_terms.append(word.strip())
    return all_terms


def _has_chinese(text: str) -> bool:
    return bool(_re.search(r'[\u4e00-\u9fff]', text))


def search_people_fts(query: str, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    """混合搜索：FTS5（英文/前缀） + LIKE（中文/子串），合并去重，FTS 结果优先。
    支持中文搜索词自动翻译为英文。

    权重：current_work=10, recent_work=5, profile=3, education=3, basic_info=2, older_work=1
    """
    conn = get_conn()

    # 中文搜索词翻译为英文词列表
    if _has_chinese(query):
        terms = _translate_query(query)
    else:
        terms = query.strip().split()

    if not terms:
        rows = conn.execute(
            "SELECT * FROM people ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
        conn.close()
        return [dict(r) for r in rows], total

    # ── 1. FTS5 搜索（英文词、前缀匹配） ──
    # 翻译后的同义词用 OR，用户输入的多个独立词用 OR（宽搜索）
    fts_query = " OR ".join(f'"{t}"*' for t in terms)
    fts_ids = []
    try:
        rows = conn.execute(
            """SELECT people_fts.person_id,
                      bm25(people_fts, 10.0, 5.0, 3.0, 3.0, 2.0, 1.0) as score
               FROM people_fts
               WHERE people_fts MATCH ?
               ORDER BY score""",
            (fts_query,),
        ).fetchall()
        fts_ids = [r[0] for r in rows]
    except Exception:
        pass

    # ── 2. LIKE 搜索（中文、子串匹配，带权重） ──
    # 对每个 term 在多表多列中搜索，按列权重打分
    like_scores = {}  # person_id -> score
    for term in terms:
        like = f"%{term}%"

        # 当前工作（position=0）的 title/company/description — 权重 10
        cur_rows = conn.execute(
            "SELECT person_id FROM experiences WHERE position = 0 AND (title LIKE ? OR company LIKE ? OR description LIKE ?)",
            (like, like, like),
        ).fetchall()
        for r in cur_rows:
            like_scores[r[0]] = like_scores.get(r[0], 0) + 10

        # 上一段经历（position=1） — 权重 5
        prev_rows = conn.execute(
            "SELECT person_id FROM experiences WHERE position = 1 AND (title LIKE ? OR company LIKE ? OR description LIKE ?)",
            (like, like, like),
        ).fetchall()
        for r in prev_rows:
            like_scores[r[0]] = like_scores.get(r[0], 0) + 5

        # people 表基础字段 — 权重 3
        people_frag, people_params = _like_clause(term)
        p_rows = conn.execute(
            f"SELECT id FROM people WHERE {people_frag}", people_params,
        ).fetchall()
        for r in p_rows:
            like_scores[r[0]] = like_scores.get(r[0], 0) + 3

        # 教育 — 权重 3
        edu_rows = conn.execute(
            "SELECT person_id FROM educations WHERE school LIKE ? OR degree LIKE ? OR field LIKE ?",
            (like, like, like),
        ).fetchall()
        for r in edu_rows:
            like_scores[r[0]] = like_scores.get(r[0], 0) + 3

        # 更早经历（position>=2） — 权重 1
        old_rows = conn.execute(
            "SELECT DISTINCT person_id FROM experiences WHERE position >= 2 AND (title LIKE ? OR company LIKE ? OR description LIKE ?)",
            (like, like, like),
        ).fetchall()
        for r in old_rows:
            like_scores[r[0]] = like_scores.get(r[0], 0) + 1

    # ── 3. 合并去重：FTS 在前，LIKE 补充 ──
    seen = set(fts_ids)
    # LIKE 结果按分数降序排，追加到 FTS 结果后面
    like_sorted = sorted(
        ((pid, score) for pid, score in like_scores.items() if pid not in seen),
        key=lambda x: -x[1],
    )
    all_ids = fts_ids + [pid for pid, _ in like_sorted]

    total = len(all_ids)
    page_ids = all_ids[offset:offset + limit]

    if not page_ids:
        conn.close()
        return [], 0

    # 批量拿 people 数据，保持排序
    placeholders = ",".join("?" * len(page_ids))
    rows = conn.execute(
        f"SELECT * FROM people WHERE id IN ({placeholders})", page_ids,
    ).fetchall()
    conn.close()

    # 按 page_ids 顺序排列
    by_id = {dict(r)["id"]: dict(r) for r in rows}
    results = [by_id[pid] for pid in page_ids if pid in by_id]

    return results, total


# ── Academic talent pool ──

def search_academic(filters: dict = None, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    """Search academic candidates (source_type='academic') with filters.

    Filters: q (text search), institution, advisor, role, grad_min, grad_max, research_area, status
    """
    filters = filters or {}
    conn = get_conn()

    where_clauses = ["source_type = 'academic'"]
    params = []

    if filters.get("q"):
        q = f"%{filters['q']}%"
        where_clauses.append("(first_name || ' ' || last_name LIKE ? OR research_area LIKE ? OR headline LIKE ?)")
        params.extend([q, q, q])

    if filters.get("institution"):
        where_clauses.append("institution LIKE ?")
        params.append(f"%{filters['institution']}%")

    if filters.get("advisor"):
        where_clauses.append("advisor LIKE ?")
        params.append(f"%{filters['advisor']}%")

    if filters.get("role"):
        where_clauses.append("title = ?")
        params.append(filters["role"])

    if filters.get("grad_min"):
        where_clauses.append("expected_graduation >= ?")
        params.append(int(filters["grad_min"]))

    if filters.get("grad_max"):
        where_clauses.append("expected_graduation <= ?")
        params.append(int(filters["grad_max"]))

    if filters.get("research_area"):
        where_clauses.append("research_area LIKE ?")
        params.append(f"%{filters['research_area']}%")

    if filters.get("status"):
        where_clauses.append("status = ?")
        params.append(filters["status"])

    where_sql = " AND ".join(where_clauses)

    # Count
    count_row = conn.execute(f"SELECT COUNT(*) FROM people WHERE {where_sql}", params).fetchone()
    total = count_row[0] if count_row else 0

    # Fetch
    rows = conn.execute(
        f"""SELECT id, first_name, last_name, email, title, headline, company,
                   institution, advisor, research_area, personal_page,
                   expected_graduation, status, created_at
            FROM people WHERE {where_sql}
            ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        params + [limit, offset]
    ).fetchall()
    conn.close()

    return [dict(r) for r in rows], total


def get_academic_filters() -> dict:
    """Get distinct values for filter dropdowns on the academic page."""
    conn = get_conn()
    institutions = [r[0] for r in conn.execute(
        "SELECT DISTINCT institution FROM people WHERE source_type='academic' AND institution IS NOT NULL ORDER BY institution"
    ).fetchall()]
    advisors = [r[0] for r in conn.execute(
        "SELECT DISTINCT advisor FROM people WHERE source_type='academic' AND advisor IS NOT NULL ORDER BY advisor"
    ).fetchall()]
    roles = [r[0] for r in conn.execute(
        "SELECT DISTINCT title FROM people WHERE source_type='academic' AND title IS NOT NULL ORDER BY title"
    ).fetchall()]
    conn.close()
    return {"institutions": institutions, "advisors": advisors, "roles": roles}
