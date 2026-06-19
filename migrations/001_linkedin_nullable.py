"""迁移 001: people.linkedin_url 从 NOT NULL UNIQUE 改为可空 UNIQUE。

背景: 会议来源的候选人（如 ICML 总表）大多没有 LinkedIn，原约束无法落库。
SQLite 的 UNIQUE 允许多个 NULL，去重逻辑改为多级:
有 LinkedIn 按 LinkedIn，否则按 github_url，再否则按 姓名+机构（在导入脚本中实现）。

运行: python3.12 migrations/001_linkedin_nullable.py（先备份 data.db）
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import DB_PATH


def migrate():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    cols = [dict(r) for r in conn.execute("PRAGMA table_info(people)").fetchall()]
    if not any(c["name"] == "linkedin_url" and c["notnull"] for c in cols):
        print("linkedin_url 已可空，跳过")
        return

    col_defs = []
    for c in cols:
        if c["name"] == "id":
            col_defs.append("id INTEGER PRIMARY KEY AUTOINCREMENT")
            continue
        d = f'{c["name"]} {c["type"]}'
        if c["name"] == "linkedin_url":
            d += " UNIQUE"  # 去掉 NOT NULL，保留唯一约束（NULL 可重复）
        else:
            if c["notnull"]:
                d += " NOT NULL"
            if c["dflt_value"] is not None:
                d += f' DEFAULT {c["dflt_value"]}'
        col_defs.append(d)

    col_names = ", ".join(c["name"] for c in cols)
    conn.executescript(f"""
    BEGIN;
    CREATE TABLE people_new ({", ".join(col_defs)});
    INSERT INTO people_new ({col_names}) SELECT {col_names} FROM people;
    DROP TABLE people;
    ALTER TABLE people_new RENAME TO people;
    COMMIT;
    """)

    # 重建被 DROP 掉的索引
    conn.executescript("""
    CREATE INDEX IF NOT EXISTS idx_people_company ON people(company);
    CREATE INDEX IF NOT EXISTS idx_people_location ON people(location);
    CREATE INDEX IF NOT EXISTS idx_people_status ON people(status);
    CREATE INDEX IF NOT EXISTS idx_people_source_type ON people(source_type);
    CREATE INDEX IF NOT EXISTS idx_people_institution ON people(institution);
    CREATE INDEX IF NOT EXISTS idx_people_advisor ON people(advisor);
    CREATE INDEX IF NOT EXISTS idx_people_sector ON people(sector);
    """)
    conn.commit()

    n = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    nullable_ok = not any(
        c["name"] == "linkedin_url" and c["notnull"]
        for c in [dict(r) for r in conn.execute("PRAGMA table_info(people)").fetchall()]
    )
    print(f"迁移完成: {n} 人, linkedin_url 可空 = {nullable_ok}")
    conn.close()


if __name__ == "__main__":
    migrate()
