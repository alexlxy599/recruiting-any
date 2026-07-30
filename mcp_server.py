"""Recruiting Any 人才库 MCP Server。

把人才库以 MCP 标准协议暴露给任何 MCP 客户端(WorkBuddy / Claude Code / CodeBuddy)。
设计原则:
- 白名单工具,不暴露裸 SQL、不暴露删除操作
- 默认不返回联系方式(email/linkedin),需显式 include_contact=True
- 写操作仅限:标记状态 + 记外联历史(状态是动作的副作用)

运行:uv run --python 3.11 --with mcp mcp_server.py
"""
import db
from mcp.server import MCPServer

mcp = MCPServer("recruiting-any",
                description="本地招聘人才库:搜索候选人、查档案、记录外联、推进 pipeline 状态")

# 档案摘要要脱敏的字段(联系方式按需获取,减少 PII 流入外部 LLM)
CONTACT_FIELDS = ("email", "linkedin_url", "github_url", "personal_page")


def _slim(person: dict, include_contact: bool = False) -> dict:
    """裁剪 get_person 的完整档案,控制 token 和 PII。"""
    out = {
        "id": person["id"],
        "name": f"{person['first_name']} {person['last_name']}".strip(),
        "title": person.get("title"),
        "company": person.get("company"),
        "location": person.get("location"),
        "headline": person.get("headline"),
        "status": person.get("status"),
        "source_type": person.get("source_type"),
        "tags": [t["name"] for t in person.get("tags", [])],
        "experiences": [
            {"title": e.get("title"), "company": e.get("company"), "is_current": e.get("is_current")}
            for e in person.get("experiences", [])
        ],
        "educations": [
            {"school": e.get("school"), "degree": e.get("degree"), "field": e.get("field")}
            for e in person.get("educations", [])
        ],
        "publications_count": len(person.get("publications", [])),
        "outreach_history": [
            {"created_at": h.get("created_at"), "language": h.get("language"),
             "status": h.get("status"), "message_preview": (h.get("message") or "")[:120]}
            for h in person.get("history", [])
        ],
    }
    if include_contact:
        for f in CONTACT_FIELDS:
            out[f] = person.get(f)
    return out


@mcp.tool()
def search_people(query: str = "", limit: int = 20) -> list[dict]:
    """搜索人才库候选人。query 支持姓名/公司/职位/地点关键词(FTS 全文搜索)。
    返回精简列表(不含联系方式),用 get_person 看完整档案。"""
    limit = min(limit, 50)
    if query:
        rows, _total = db.search_people_fts(query, limit=limit)
    else:
        rows = db.search_people(limit=limit)
    return [
        {"id": r["id"],
         "name": f"{r['first_name']} {r['last_name']}".strip(),
         "title": r.get("title"), "company": r.get("company"),
         "location": r.get("location"), "status": r.get("status")}
        for r in rows
    ]


@mcp.tool()
def get_person(person_id: int, include_contact: bool = False) -> dict:
    """查看候选人完整档案:经历、教育、标签、论文数、外联历史。
    include_contact=True 时额外返回 email/linkedin/github(仅在准备发送时使用)。"""
    person = db.get_person(person_id)
    if not person:
        return {"error": f"person {person_id} not found"}
    return _slim(person, include_contact=include_contact)


@mcp.tool()
def log_outreach(person_id: int, message: str, language: str = "en",
                 channel: str = "linkedin") -> dict:
    """记录一次外联:把话术存入 history,并自动把候选人状态推进为 contacted。
    发送完成后必须调用本工具,否则系统无法追踪。"""
    person = db.get_person(person_id)
    if not person:
        return {"error": f"person {person_id} not found"}
    name = f"{person['first_name']} {person['last_name']}".strip()
    db.add_history(name=name, url=person.get("linkedin_url") or "",
                   community=channel, language=language,
                   message=message, person_id=person_id)
    db.update_person_status(person_id, "contacted")
    return {"ok": True, "person": name, "new_status": "contacted"}


@mcp.tool()
def set_status(person_id: int, status: str) -> dict:
    """更新候选人 pipeline 状态。
    合法值:new / drafted / contacted / replied / interview / decision / archived。"""
    try:
        changed = db.update_person_status(person_id, status)
    except ValueError as e:
        return {"error": str(e)}
    return {"ok": changed, "person_id": person_id, "status": status}


@mcp.tool()
def pool_stats() -> dict:
    """人才库总览:总人数、按状态/来源分布。用于回答"库里现在什么情况"。"""
    conn = db.get_conn()
    total = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    by_status = dict(conn.execute(
        "SELECT status, COUNT(*) FROM people GROUP BY status").fetchall())
    by_source = dict(conn.execute(
        "SELECT COALESCE(source_type,'unknown'), COUNT(*) FROM people GROUP BY source_type").fetchall())
    conn.close()
    return {"total": total, "by_status": by_status, "by_source": by_source}


if __name__ == "__main__":
    mcp.run()  # stdio transport
