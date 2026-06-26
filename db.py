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
                     ("start_date", "TEXT"), ("end_date", "TEXT"), ("source", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE experiences ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass  # column already exists

    for col, typ in [("description", "TEXT"), ("activities", "TEXT"), ("source", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE educations ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass

    # 实验室归属（个人主页提取）
    try:
        conn.execute("ALTER TABLE people ADD COLUMN lab TEXT")
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

    # GitHub 身份验证等级: verified_link / verified_email / llm_confirmed / uncertain / rejected / not_found
    try:
        conn.execute("ALTER TABLE people ADD COLUMN github_verified TEXT")
    except sqlite3.OperationalError:
        pass

    # 界别: academic / industry。与入口渠道(source_type)解耦——source_type 记录怎么进来的, sector 记录他是谁
    try:
        conn.execute("ALTER TABLE people ADD COLUMN sector TEXT")
    except sqlite3.OperationalError:
        pass

    # GitHub 画像摘要（LLM 从快照提取的衍生数据，可重跑覆盖）
    try:
        conn.execute("ALTER TABLE people ADD COLUMN github_summary TEXT")
    except sqlite3.OperationalError:
        pass

    # Backfill: mark existing lab_sourcer imports as academic
    conn.execute("UPDATE people SET source_type='academic' WHERE notes='lab_sourcer' AND source_type IS NULL")

    # 界别回填规则: 学术标记优先, 其次有公司即工业界
    conn.execute("""
        UPDATE people SET sector = 'academic' WHERE sector IS NULL AND (
            source_type = 'academic'
            OR (institution IS NOT NULL AND institution != '')
            OR (advisor IS NOT NULL AND advisor != '')
            OR company LIKE '%University%' OR company LIKE '%Institute of Technology%'
            OR company LIKE '%College%' OR company LIKE '%Academy of Sciences%'
            OR title LIKE '%PhD Student%' OR title LIKE '%Postdoc%' OR title LIKE '%Professor%'
        )""")
    conn.execute("""
        UPDATE people SET sector = 'industry'
        WHERE sector IS NULL AND company IS NOT NULL AND company != ''""")

    conn.executescript("""
    CREATE INDEX IF NOT EXISTS idx_people_company ON people(company);
    CREATE INDEX IF NOT EXISTS idx_people_location ON people(location);
    CREATE INDEX IF NOT EXISTS idx_people_status ON people(status);
    CREATE INDEX IF NOT EXISTS idx_people_source_type ON people(source_type);
    CREATE INDEX IF NOT EXISTS idx_people_institution ON people(institution);
    CREATE INDEX IF NOT EXISTS idx_people_advisor ON people(advisor);

    -- ── 标签系统 ──

    CREATE TABLE IF NOT EXISTS tags (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        name      TEXT NOT NULL UNIQUE,
        category  TEXT NOT NULL DEFAULT 'custom'
    );

    CREATE TABLE IF NOT EXISTS person_tags (
        person_id  INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        source     TEXT NOT NULL DEFAULT 'manual',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (person_id, tag_id)
    );

    CREATE INDEX IF NOT EXISTS idx_person_tags_person ON person_tags(person_id);
    CREATE INDEX IF NOT EXISTS idx_person_tags_tag ON person_tags(tag_id);
    CREATE INDEX IF NOT EXISTS idx_tags_category ON tags(category);

    -- ── 顶会发表记录："顶会"徽章由此表推导，支持按会议+年份筛选 ──

    CREATE TABLE IF NOT EXISTS publications (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        venue       TEXT NOT NULL,            -- ICML / NeurIPS / ICLR / CVPR ...
        year        INTEGER,
        title       TEXT NOT NULL DEFAULT '',
        is_first_author INTEGER NOT NULL DEFAULT 0,
        source      TEXT NOT NULL DEFAULT '', -- conference_scraper / s2 / manual
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_publications_person ON publications(person_id);
    CREATE INDEX IF NOT EXISTS idx_publications_venue ON publications(venue, year);
    CREATE INDEX IF NOT EXISTS idx_people_sector ON people(sector);

    -- ── 提取层：LLM 从快照提取的全量 JSON，带版本，宁可多不要少 ──

    CREATE TABLE IF NOT EXISTS extractions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        source      TEXT NOT NULL,            -- homepage / github / combined
        version     INTEGER NOT NULL DEFAULT 1,
        model       TEXT NOT NULL DEFAULT '',
        json        TEXT NOT NULL,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_extractions_person ON extractions(person_id, source);

    -- ── 规范层：项目 与 人际关系（图谱的点和边）──

    CREATE TABLE IF NOT EXISTS projects (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        url         TEXT,
        description TEXT,
        direction   TEXT,
        tech        TEXT,
        period      TEXT,
        source      TEXT NOT NULL DEFAULT '', -- homepage / github / manual
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_projects_person ON projects(person_id);

    CREATE TABLE IF NOT EXISTS collaborations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        collaborator_name      TEXT NOT NULL,
        collaborator_person_id INTEGER REFERENCES people(id),  -- 延迟对齐：在库则关联，不在库留名
        relation    TEXT NOT NULL DEFAULT 'collaborator',      -- advisor/advisee/coauthor/labmate/colleague/mentioned
        context     TEXT NOT NULL DEFAULT '',                  -- 哪篇论文/哪个实验室/什么场合
        collaborator_url TEXT,
        source      TEXT NOT NULL DEFAULT '',                  -- homepage / icml_coauthor / s2 / inferred
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_collab_person ON collaborations(person_id);
    CREATE INDEX IF NOT EXISTS idx_collab_name ON collaborations(collaborator_name);

    -- ── 原始快照层：抓取的网页/API 原文，只增不改，结构化提取可随时重跑 ──

    CREATE TABLE IF NOT EXISTS web_snapshots (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        source      TEXT NOT NULL,            -- github_profile / github_readme / homepage / scholar
        url         TEXT NOT NULL DEFAULT '',
        raw_text    TEXT NOT NULL DEFAULT '',
        fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_snapshots_person ON web_snapshots(person_id, source);

    CREATE TABLE IF NOT EXISTS enrichment_cache (
        cache_key   TEXT PRIMARY KEY,        -- 如 match_reason:<pid>:<query_hash>
        value       TEXT NOT NULL DEFAULT '',
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
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
    person["publications"] = [
        dict(r) for r in conn.execute(
            "SELECT venue, year, title, is_first_author FROM publications "
            "WHERE person_id = ? ORDER BY year DESC, is_first_author DESC", (person_id,)
        ).fetchall()
    ]
    person["tags"] = [
        dict(r) for r in conn.execute(
            """SELECT t.id, t.name, t.category, pt.source, pt.created_at
               FROM person_tags pt JOIN tags t ON pt.tag_id = t.id
               WHERE pt.person_id = ?
               ORDER BY t.category, t.name""",
            (person_id,),
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

    where_clauses = ["(source_type = 'academic' OR source_type = 'industry')"]
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

    if filters.get("venue"):
        ids = _person_ids_for_venue(filters["venue"])
        if not ids:
            where_clauses.append("1 = 0")
        else:
            ph = ",".join("?" * len(ids))
            where_clauses.append(f"id IN ({ph})")
            params.extend(sorted(ids))

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
                   expected_graduation, status, created_at,
                   (SELECT GROUP_CONCAT(pub.venue, '|') FROM publications pub
                    WHERE pub.person_id = people.id) AS venues
            FROM people WHERE {where_sql}
            ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        params + [limit, offset]
    ).fetchall()
    conn.close()

    out = []
    for r in rows:
        d = dict(r)
        d["venues"] = canon_venues(d.get("venues"))
        out.append(d)
    return out, total


def get_academic_filters() -> dict:
    """Get distinct values for filter dropdowns on the academic page."""
    conn = get_conn()
    institutions = [r[0] for r in conn.execute(
        "SELECT DISTINCT institution FROM people WHERE source_type IN ('academic','industry') AND institution IS NOT NULL ORDER BY institution"
    ).fetchall()]
    advisors = [r[0] for r in conn.execute(
        "SELECT DISTINCT advisor FROM people WHERE source_type IN ('academic','industry') AND advisor IS NOT NULL ORDER BY advisor"
    ).fetchall()]
    roles = [r[0] for r in conn.execute(
        "SELECT DISTINCT title FROM people WHERE source_type IN ('academic','industry') AND title IS NOT NULL ORDER BY title"
    ).fetchall()]
    conn.close()
    return {"institutions": institutions, "advisors": advisors, "roles": roles}


# ── 标签系统 ──

def ensure_tag(name: str, category: str = "custom", conn=None) -> int:
    """Get or create a tag by name. Returns tag_id."""
    close = False
    if conn is None:
        conn = get_conn()
        close = True
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row:
        tag_id = row["id"]
    else:
        cur = conn.execute("INSERT INTO tags (name, category) VALUES (?, ?)", (name, category))
        conn.commit()
        tag_id = cur.lastrowid
    if close:
        conn.close()
    return tag_id


def cache_get(key: str, max_age_days: int = 30) -> str | None:
    """通用富化缓存读取；超过 max_age_days 视为失效返回 None。"""
    conn = get_conn()
    row = conn.execute(
        f"SELECT value FROM enrichment_cache WHERE cache_key = ? "
        f"AND created_at > datetime('now', '-{int(max_age_days)} days')",
        (key,),
    ).fetchone()
    conn.close()
    return row["value"] if row else None


def cache_set(key: str, value: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO enrichment_cache (cache_key, value, created_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP)",
        (key, value),
    )
    conn.commit()
    conn.close()


def add_person_tag(person_id: int, tag_name: str, category: str = "custom", source: str = "manual", conn=None):
    """Add a tag to a person. Creates the tag if it doesn't exist. Skips if already tagged."""
    close = False
    if conn is None:
        conn = get_conn()
        close = True
    tag_id = ensure_tag(tag_name, category, conn)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO person_tags (person_id, tag_id, source) VALUES (?, ?, ?)",
            (person_id, tag_id, source),
        )
        conn.commit()
    except Exception:
        pass
    if close:
        conn.close()


def get_person_tags(person_id: int) -> list[dict]:
    """Get all tags for a person."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT t.id, t.name, t.category, pt.source, pt.created_at
           FROM person_tags pt JOIN tags t ON pt.tag_id = t.id
           WHERE pt.person_id = ?
           ORDER BY t.category, t.name""",
        (person_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def remove_person_tag(person_id: int, tag_id: int):
    """Remove a tag from a person."""
    conn = get_conn()
    conn.execute("DELETE FROM person_tags WHERE person_id = ? AND tag_id = ?", (person_id, tag_id))
    conn.commit()
    conn.close()


def search_by_tag(tag_name: str, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    """Find all people with a given tag."""
    conn = get_conn()
    total = conn.execute(
        """SELECT COUNT(*) FROM person_tags pt
           JOIN tags t ON pt.tag_id = t.id
           WHERE t.name = ?""",
        (tag_name,),
    ).fetchone()[0]
    rows = conn.execute(
        """SELECT p.* FROM people p
           JOIN person_tags pt ON p.id = pt.person_id
           JOIN tags t ON pt.tag_id = t.id
           WHERE t.name = ?
           ORDER BY p.updated_at DESC LIMIT ? OFFSET ?""",
        (tag_name, limit, offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def get_all_tags() -> list[dict]:
    """Get all tags with person counts."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT t.id, t.name, t.category, COUNT(pt.person_id) as count
           FROM tags t LEFT JOIN person_tags pt ON t.id = pt.tag_id
           GROUP BY t.id ORDER BY count DESC, t.name""",
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── GitHub 身份验证 + 快照层 ──


def get_unverified_github_people(limit: int = 50) -> list[dict]:
    """取 github_url 非空且尚未验证过身份的人。"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT id FROM people
           WHERE github_url IS NOT NULL AND github_url != ''
             AND github_verified IS NULL
           ORDER BY id LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [get_person(r["id"]) for r in rows]


def set_github_verified(person_id: int, level: str, evidence: str = ""):
    """写入验证等级；evidence 追加到 notes 之外单独存快照，这里只更新等级。"""
    conn = get_conn()
    conn.execute(
        "UPDATE people SET github_verified = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (level, person_id),
    )
    conn.commit()
    conn.close()


def set_personal_page(person_id: int, url: str):
    """补全个人主页字段（仅在原值为空时写入）。"""
    conn = get_conn()
    conn.execute(
        """UPDATE people SET personal_page = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = ? AND (personal_page IS NULL OR personal_page = '')""",
        (url, person_id),
    )
    conn.commit()
    conn.close()


def add_snapshot(person_id: int, source: str, url: str, raw_text: str):
    """存原始快照。同 person+source+url 当天重复抓取只保留最新一份。"""
    conn = get_conn()
    conn.execute(
        """DELETE FROM web_snapshots
           WHERE person_id = ? AND source = ? AND url = ? AND date(fetched_at) = date('now')""",
        (person_id, source, url),
    )
    conn.execute(
        "INSERT INTO web_snapshots (person_id, source, url, raw_text) VALUES (?, ?, ?, ?)",
        (person_id, source, url, raw_text),
    )
    conn.commit()
    conn.close()


def get_snapshots(person_id: int, source: str = "") -> list[dict]:
    """取某人的快照，可按 source 过滤。"""
    conn = get_conn()
    if source:
        rows = conn.execute(
            "SELECT * FROM web_snapshots WHERE person_id = ? AND source = ? ORDER BY fetched_at DESC",
            (person_id, source),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM web_snapshots WHERE person_id = ? ORDER BY fetched_at DESC",
            (person_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_github_verified_people() -> list[dict]:
    """取所有已做过 GitHub 身份验证的人（复核页用）。"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, first_name || ' ' || last_name AS name, company, title,
                  linkedin_url, github_url, email, personal_page, github_verified
           FROM people WHERE github_verified IS NOT NULL ORDER BY id"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── 视角化列表（Pool lens: all / academic / industry / conf / oss）──

# 会议名归一化：原始 venue 字符串 → 干净缩写。长/更具体的关键词排前面（如 naacl 在 acl 前）
_VENUE_MAP = [
    ("neurips", "NeurIPS"), ("nips", "NeurIPS"), ("iclr", "ICLR"), ("icml", "ICML"),
    ("cvpr", "CVPR"), ("iccv", "ICCV"), ("eccv", "ECCV"), ("wacv", "WACV"),
    ("emnlp", "EMNLP"), ("naacl", "NAACL"), ("coling", "COLING"), ("acl", "ACL"),
    ("aaai", "AAAI"), ("ijcai", "IJCAI"), ("aistats", "AISTATS"), ("uai", "UAI"),
    ("kdd", "KDD"), ("sigir", "SIGIR"), ("siggraph", "SIGGRAPH"), ("colm", "COLM"),
    ("interspeech", "Interspeech"), ("icassp", "ICASSP"), ("iros", "IROS"),
    ("icra", "ICRA"), ("corl", "CoRL"), ("acm multimedia", "ACM MM"), ("acmmm", "ACM MM"),
    ("thewebconf", "WWW"), ("www", "WWW"), ("tpami", "TPAMI"), ("jmlr", "JMLR"),
    ("nature", "Nature"), ("science", "Science"), ("miccai", "MICCAI"), ("mlsys", "MLSys"),
]
# 顶会展示优先级（越靠前越先显示）
_VENUE_RANK = {v: i for i, v in enumerate(dict.fromkeys(c for _, c in _VENUE_MAP))}


def canon_venues(raw: str, limit: int = 4) -> str:
    """原始 venue 串（'|' 分隔）→ 归一化顶会缩写，去噪去重，按优先级排序，逗号拼接。"""
    if not raw:
        return ""
    found = []
    for part in raw.split("|"):
        low = part.lower()
        hit = next((canon for kw, canon in _VENUE_MAP if kw in low), None)
        if hit and hit not in found:
            found.append(hit)
    found.sort(key=lambda v: _VENUE_RANK.get(v, 99))
    return ",".join(found[:limit])


def _person_ids_for_venue(venue: str) -> set[int]:
    """归一化后命中 venue 的所有 person_id（用 canon_venues 同一套映射，口径与徽章一致）。"""
    target = (venue or "").strip().lower()
    if not target:
        return set()
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT person_id, venue FROM publications WHERE TRIM(COALESCE(venue,'')) <> ''"
    ).fetchall()
    conn.close()
    ids = set()
    for pid, raw in rows:
        canon = canon_venues(raw)
        if canon and any(c.strip().lower() == target for c in canon.split(",")):
            ids.add(pid)
    return ids


def get_venue_counts() -> list[dict]:
    """学术板块的会议清单：每个归一化顶会下有多少（去重）候选人。按人数倒序、顶会优先。"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT DISTINCT pub.person_id, pub.venue
           FROM publications pub JOIN people p ON p.id = pub.person_id
           WHERE (p.source_type = 'academic' OR p.source_type = 'industry')
             AND TRIM(COALESCE(pub.venue,'')) <> ''"""
    ).fetchall()
    conn.close()
    sets: dict[str, set] = {}
    for pid, raw in rows:
        canon = canon_venues(raw)
        if not canon:
            continue
        for v in canon.split(","):
            sets.setdefault(v, set()).add(pid)
    out = [{"venue": v, "people": len(s)} for v, s in sets.items()]
    out.sort(key=lambda x: (-x["people"], _VENUE_RANK.get(x["venue"], 99)))
    return out


LENS_WHERE = {
    "academic": "p.sector = 'academic'",
    "industry": "p.sector = 'industry'",
    "conf": "EXISTS (SELECT 1 FROM publications pub WHERE pub.person_id = p.id)",
    "oss": "p.github_verified IN ('verified_link', 'verified_email', 'llm_confirmed', 'import_high')",
}


def list_people(lens: str = "", query: str = "", limit: int = 30, offset: int = 0) -> tuple[list[dict], int]:
    """统一人才列表：lens 是同一池子上的筛选窗口，附带徽章所需字段。"""
    where, params = [], []
    if lens in LENS_WHERE:
        where.append(LENS_WHERE[lens])
    if query:
        for term in query.split():
            frag, ps = _like_clause(term)
            where.append(frag.replace("first_name", "p.first_name").replace("last_name", "p.last_name")
                         .replace("company", "p.company").replace("title", "p.title")
                         .replace("headline", "p.headline").replace("location", "p.location"))
            params.extend(ps)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = get_conn()
    total = conn.execute(f"SELECT COUNT(*) FROM people p {where_sql}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT p.id, p.first_name, p.last_name, p.linkedin_url, p.email, p.github_url,
                   p.title, p.headline, p.company, p.location, p.industry, p.status, p.notes,
                   p.sector, p.github_verified, p.institution, p.created_at, p.updated_at,
                   (SELECT GROUP_CONCAT(pub.venue, '|') FROM publications pub
                    WHERE pub.person_id = p.id) AS venues
            FROM people p {where_sql}
            ORDER BY p.updated_at DESC LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["venues"] = canon_venues(d.get("venues"))
        out.append(d)
    return out, total


def get_badge_fields(person_ids: list[int]) -> dict[int, dict]:
    """给搜索结果补徽章字段（sector / github_verified / venues），一次查询。"""
    if not person_ids:
        return {}
    conn = get_conn()
    ph = ",".join("?" * len(person_ids))
    rows = conn.execute(
        f"""SELECT p.id, p.sector, p.github_verified,
                   (SELECT GROUP_CONCAT(pub.venue, '|') FROM publications pub
                    WHERE pub.person_id = p.id) AS venues
            FROM people p WHERE p.id IN ({ph})""",
        person_ids,
    ).fetchall()
    conn.close()
    result = {}
    for r in rows:
        d = dict(r)
        d["venues"] = canon_venues(d.get("venues"))
        result[r["id"]] = d
    return result
