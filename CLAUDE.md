# 人才库系统（Talent Pool）

本地优先的招聘人才库：管理 2-3 千人的候选人数据，按需召回、智能匹配、自动外联。

> **项目背景**：本项目基于话术定制器（huashu-dingzhiqi）扩展而来。原项目只能临时输入候选人生成话术，本项目把它升级成一套完整的人才管理系统——候选人数据持久化存储，外联只是其中一个功能模块。

---

## 这个系统是干什么的

招聘工作流的完整闭环：

```
导入候选人 → 智能打标签 → 搜索/筛选/匹配 → 生成外联话术 → 追踪回复
```

**核心场景**：
1. **管人**：3000+ 候选人的本地数据库，公司/职位/教育/标签多维管理
2. **找人**：自然语言搜索 + 结构化筛选 + JD 智能匹配
3. **联系人**：基于候选人完整背景生成个性化 LinkedIn 话术（中英双语，社区招聘/学术合作两种模式）
4. **跟进**：状态机追踪每个候选人的 pipeline（新建 → 已联系 → 回复 → 面试 → 决策）

---

## 一句话架构

**SQLite 存所有数据 → Flask 暴露 Web + JSON API → AI Provider 抽象层支持 Anthropic / OpenAI 兼容 / 本地 Ollama → Web UI 是主界面，CLI 是脚本入口。**

数据不出本机，只有 LLM 调用走云端（可切换成本地模型完全离线）。

---

## 数据模型（必读，所有功能都围绕它）

数据库文件：`data.db`（SQLite + WAL + FTS5）。schema 定义在 `db.py:init_db()`。

### 人才库核心表

- **`people`** — 候选人，1 人 1 行，按 `linkedin_url` 去重（UNIQUE）。当前职位、公司、地点、邮箱、GitHub、状态都在这。
- **`experiences`** — 工作经历，按 `position` 排序（0 = 最新职位，对应 CSV Experience 字段第一行）
- **`educations`** — 教育背景
- **`tags` + `person_tags`** — 标签字典 + 关联，`category` 字段区分 `skill` / `seniority` / `domain` / `company_tier` / `background` / `custom`，`source` 字段区分 `manual` / `ai`
- **`embeddings_meta`** — 向量元数据（向量本身存 LanceDB，文件在 `data/vectors/`）
- **`enrichment_cache`** — GitHub / Semantic Scholar / 搜索结果缓存，默认 30 天有效，**所有外部 API 调用必须先查缓存**

### 外联追踪表

- **`history`** — 每条话术记录，含 `person_id` 外键关联到人才库、`status` 状态、`reply` 回复内容、`replied_at` 回复时间
- **`sender_config`** — 发件人配置（单例，id=1）

### 重要 schema 约定

1. `experiences.position = 0 AND is_current = 1` → 当前职位。这是判断"现在在哪"的唯一依据。
2. CSV 原始数据里 Experience 字段是 `"Title@Company"` 多行字符串，**没有时间信息**。`start_year` / `end_year` 默认 NULL，除非未来用 ContactOut 等 enrich API 补全。
3. 所有数据库操作都在 `db.py`，命名规范：`upsert_*` / `get_*` / `add_*` / `search_*` / `cache_*`。**不要绕过 `db.py` 直接写 SQL**——除非是 ad-hoc 分析。

---

## 目录结构

```
.
├── app.py                  # Flask 主入口（人才库 + 话术 + 学术/会议/实验室 sourcing API）
├── db.py                   # 数据层：schema + 所有 CRUD
├── data.db                 # SQLite 主库（gitignored）
├── data/
│   ├── raw/                # 原始 CSV 等导入文件
│   └── vectors/            # LanceDB 向量库
├── ingest/                 # 数据导入
│   ├── import_csv.py       # CSV → DB
│   ├── import_icml.py      # ICML 论文 / 作者导入
│   └── build_coauthor_graph.py  # 共同作者关系图
├── migrations/             # 破坏性 schema 变更迁移脚本
│   └── 001_linkedin_nullable.py
├── ai/
│   └── embedder.py         # 向量化 + 语义搜索（semantic_search）
├── csrankings.py           # CSRankings 院校 / faculty 数据
├── scraper.py              # 实验室主页抓取（基础版）
├── fast_scraper.py         # 实验室主页抓取（快速版）
├── agent_scraper.py        # 实验室主页抓取（agent 版）
├── conference_scraper.py   # 会议论文抓取
├── discover_s2.py          # Semantic Scholar 候选人发现
├── enrich_academic.py      # 学术背景补全（个人主页）
├── enrich_homepage.py      # 个人主页 enrich
├── enrich_github_repos.py  # GitHub 仓库 enrich
├── verify_github.py        # GitHub 信号验证
├── verify_medium.py        # Medium 信号验证
├── templates/              # Jinja2 模板
│   ├── index.html / academic.html / people.html / person.html
│   ├── demo.html / discover.html / github_review.html / lab_sourcer.html
├── .env                    # API key + AI_MODE 等配置
├── CLAUDE.md               # 本文件
└── README.md
```

> **现状提示**：项目重心已从「外联话术」扩展到**学术/会议/实验室 sourcing**（lab-sourcer、conference、discover、academic 等大量路由）。
> 原计划中的 `ai/provider.py`（统一 LLM 接口）、`ai/tagger.py`（批量打标签）、`ai/matcher.py`（JD 匹配）、`outreach/generator.py`、`cli/tp.py` **尚未拆出**，话术生成与外部 LLM 调用目前仍在 `app.py` 内。下次重构时按上面布局拆。

---

## AI 调用规范

**所有 LLM 调用必须走 `ai/provider.py` 的统一接口，不要直接 `import anthropic`**（现有 `app.py` 里的两处直接调用是历史遗留，下次重构时也走 provider）。

### 三种后端切换

- `AI_MODE=anthropic`（默认）→ Claude API，最高质量，用于：单条话术、JD 匹配、关键决策
- `AI_MODE=openai_compat` + `OPENAI_BASE_URL` → 智谱 GLM / DeepSeek / 任何 OpenAI 兼容服务
- `AI_MODE=local` + `OLLAMA_URL` → 本地 Ollama，免费、隐私、慢，用于：批量打标签、批量向量化、不敏感的探索性任务

### 模型版本

- Anthropic 默认模型：`claude-opus-4-7`（写代码时优先读 `DEFAULT_ANTHROPIC_MODEL` 环境变量，没设置再回退）
- 不要硬编码 `claude-opus-4-6` 这种老型号，会过时

### Prompt 模板

System prompts 集中在 `ai/prompts/` 下，按 `{purpose}_{language}.md` 命名。已有的 4 个（community/academic × zh/en）从 `app.py` 抽出来放这里。

---

## 检索的三种姿势（按代价从低到高）

要找人时，按这个顺序挑：

1. **结构化筛选**（`db.search_people(filters={...})`）—— 零成本，毫秒级。能精确表达的条件（公司、地点、学校、标签）都走这。
2. **FTS5 全文搜索**（`db.search_people(query="...")`）—— 零成本，毫秒级。关键词在 headline/title/notes 里。
3. **语义搜索**（`ai/embedder.py:semantic_search`）—— 需要先 embed 查询，查 LanceDB，再回查 SQLite。用于"找有 X 背景、做 Y 方向、最好在 Z 类公司"这种模糊意图。

**永远先 1 → 2 → 3**。能用 SQL 解决的不要让 LLM 解决。

---

## 常见任务怎么做

### "帮我导入这份 CSV"
1. 用 `ingest/import_csv.py`，按字段映射调 `db.upsert_person` + `db.add_experiences` + `db.add_educations`
2. **必须 dry-run 一次**，打印前 3 行解析结果让用户 review 后再正式写库
3. Experience 字段拆行后倒序入库时 `position` 从 0 开始递增，`position=0` 同时 `is_current=1`
4. 跳过：linkedin_url 为空的行、已存在且数据完全一致的行
5. 输出统计：新增 X 人 / 更新 Y 人 / 跳过 Z 人

### "找一下符合 JD 的候选人"
1. 解析 JD（如果用户没提，让用户贴或指定文件路径）
2. 先抽硬约束（地点、最低职级、必须的公司类型）→ 走结构化筛选缩小范围
3. JD 文本 → embed → 在缩小后的范围内做语义匹配
4. Top 20 候选 → 让 LLM 输出匹配理由
5. **输出格式：表格，含 `tp show <id>` 命令方便后续查看**

### "给某人生成话术"
1. 优先从库里读：`db.get_person(person_id)` 拿完整背景
2. 检查 `enrichment_cache`，如果有 30 天内的 GitHub/论文数据直接用
3. 没有再调外部 API，**调完必须 `cache_enrichment` 存回去**
4. 复用 `outreach/generator.py` 的 `generate_message` / `generate_academic_message`
5. 调用 `db.add_history` 时**必须传 `person_id`**，把外联和人才库打通

### "批量打标签"
1. 用本地 Ollama（`AI_MODE=local`）跑，成本零
2. 输出严格 JSON，每人 3-5 个标签 + 置信度
3. 写入时 `source='ai'`，**不要覆盖 `source='manual'` 的标签**
4. 批量任务用 `tp tag --auto`，进度条 + 断点续传

### "看某人的完整档案"
1. `db.get_person(id)` 一次拉全：基本信息 + 所有经历 + 所有教育 + 所有标签
2. 顺便查 `history` 表，看是否联系过、回复过

---

## 重要约定

- **数据库迁移**：现阶段直接改 `init_db()` 里的 `CREATE TABLE IF NOT EXISTS`。如果是破坏性变更（删列、改类型），必须写迁移脚本到 `migrations/` 目录，不要直接 drop 表。
- **CSV / 敏感数据不要 commit**。`.gitignore` 已经包含 `data.db`、`data/raw/*.csv`、`.env`。
- **API key 永远从环境变量读**，不要写死。前端传过来的 key 走 `X-Api-Key` header。
- **写新功能时，能加测试就加**——`pytest` 风格，放 `tests/` 目录。db 操作用临时 sqlite 文件测，不要污染 `data.db`。
- **外联模块的所有操作都要关联到 person_id**，没有 person_id 的外联是脱离人才库的，应该补一条 `people` 记录而不是临时塞。

---

## 不要做的事

- 不要把 LinkedIn URL 之外的字段当主键
- 不要在 `experiences` 里塞 JSON blob，已经是结构化表了
- 不要为了"看起来简洁"省掉 `enrichment_cache`，GitHub API 限流真的会让你头疼
- 不要直接用 `requests` 调 LLM，走 `ai/provider.py`
- 不要在生产代码里 print 大段 prompt 或候选人 PII，要 log 用 logging 模块控制级别
- 不要把 docx/pptx 当成数据库——CSV 导入完就归档到 `data/raw/archive/`

---

## 当前进度（保持更新）

### 已完成
- [x] v1 话术生成（社区 + 学术，中英双语），SSE 流式输出
- [x] v1 SQLite 存 sender + history
- [x] v2 schema 扩展：people / experiences / educations / tags / cache 表
- [x] v2 FTS5 全文搜索 + 触发器自动同步
- [x] v2 db.py 新增人才库操作函数，全部向后兼容
- [x] CSV 导入脚本（`ingest/import_csv.py`）
- [x] 人才库 Web 界面（`/pool`、`/people`、`/people/<id>` 检索页 + 详情页）
- [x] 把"生成话术"接到人才库（`/api/generate-for-person/<person_id>`，按 person_id 拉数据）
- [x] 向量化 + 语义搜索（`ai/embedder.py:semantic_search`）
- [x] 标签管理（手动增删查，`/api/person/<id>/tags`）
- [x] 精品 Demo 人才池（`/demo`，Top 100 演示）
- [x] 数据库迁移机制（`migrations/`，首个：linkedin_url 可空）
- [x] **实验室 sourcing**（`/lab-sourcer`：CSRankings faculty → 主页抓取 → 导入，含基础/快速/agent 三种抓取 + enrich）
- [x] **会议论文 sourcing**（`/api/conference/*`：上传/抓取/筛选/enrich/作者提取）
- [x] **学术候选人发现**（`/discover` + `discover_s2.py`，Semantic Scholar）
- [x] **学术 pipeline 管理**（`/academic` + `/api/academic/people/<id>/status` 状态追踪）
- [x] GitHub / 主页信号 enrich + 验证（`enrich_*` / `verify_*` / `/pool/github-review`）
- [x] 共同作者关系图（`ingest/build_coauthor_graph.py` + `/api/people/<id>/graph`）
- [x] 洞察接口（`/api/people/insights`、`/api/pool/lens-counts`）

### 进行中 / 待做
- [ ] AI Provider 统一接口（把 `app.py` 里直接 `import anthropic`/`openai` 的调用迁过来）
- [ ] AI 自动打标签（批量，本地模型，`ai/tagger.py`）
- [ ] JD 匹配功能（`ai/matcher.py`）
- [ ] 把话术生成抽到 `outreach/generator.py`
- [ ] CLI 工具 `tp`（`cli/tp.py`）
- [ ] 完整可视化分析 dashboard（目前只有零散 insights 接口）

### 下一步建议

代码已铺得很开但 `app.py` 偏臃肿（2800+ 行）。建议优先：
1. **抽 `ai/provider.py`**（统一三路 LLM 调用，消除 app.py 里的直连 anthropic/openai）
2. **抽 `outreach/generator.py`**（把话术生成从 app.py 拆出）
3. **AI 自动打标签 + JD 匹配**（让人才库从"能存能搜"升级到"能智能召回"）
4. 其余按需推进
