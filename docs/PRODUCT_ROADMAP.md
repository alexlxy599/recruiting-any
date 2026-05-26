# Recruiting Any — Product Roadmap & PRD

> 最后更新：2026-05-25

## 一、产品定位

**一句话**：本地优先的 AI 招聘平台，覆盖"发现候选人 → 管理人才库 → 个性化外联"全链路。

**目标用户**：技术招聘人员（Recruiter / Sourcer），尤其是面向 AI/ML 领域的高端人才招聘。

**核心差异**：
- 数据不出本机，隐私可控
- 学术人才发现能力（实验室成员批量提取、会议论文候选人锁定）
- 信号驱动的个性化话术（不是模板群发，而是基于候选人真实活动的精准触达）

---

## 二、信息架构

```
┌─────────────────────────────────────────────────────┐
│  Discover         Pool              Outreach        │
│  /discover        /pool             /outreach       │
│                                                     │
│  ┌─────────┐   ┌──────────────┐   ┌─────────────┐ │
│  │CSRankings│   │ All          │   │ 消息生成     │ │
│  │Smart     │   │ Academic Tab │   │ 历史记录     │ │
│  │Dept Page │   │ Industry Tab │   │ 模板管理     │ │
│  │Conf Paper│   │              │   │              │ │
│  │Manual    │   │ Person Detail│   │              │ │
│  └─────────┘   └──────────────┘   └─────────────┘ │
└─────────────────────────────────────────────────────┘
```

---

## 三、已完成功能清单

### v0.1 — 话术生成器（原始版本）
- [x] LinkedIn 消息生成（中/英双语）
- [x] 开源社区模式 + 学术会议模式
- [x] GitHub API / Semantic Scholar / DuckDuckGo 背景抓取
- [x] 发件人配置持久化
- [x] 历史记录（搜索、复制、删除、导出 Excel）
- [x] 多 LLM 后端（Anthropic / OpenAI 兼容 / Ollama）

### v0.2 — 人才库
- [x] SQLite + FTS5 全文搜索
- [x] CSV 批量导入（2800+ 候选人）
- [x] 人才详情页（工作经历、教育、标签）
- [x] 布尔搜索（`Google AND PhD`）
- [x] 语义搜索（LanceDB 向量匹配）
- [x] Insights 面板（公司/学历/院校/职级分布 + 外联漏斗）
- [x] LinkedIn 粘贴补充档案
- [x] Chrome 插件一键抓取 LinkedIn 页面

### v0.3 — Lab Sourcer（学术人才发现）
- [x] CSRankings 院校 + AI 教授数据集成
- [x] 两阶段抓取器（HTTP 抓取 + 单次 LLM 提取）
- [x] Agent 模式 Web 搜索（Anthropic 原生 web_search）
- [x] 跨域实验室网站发现（教授主页 → 实验室 → People 页面）
- [x] JS 渲染页面 JSON 数据提取
- [x] 超长页面智能截断（保留 People 段落）
- [x] Icon-only 链接保留
- [x] 模型下拉选择器（按 Provider 分组）
- [x] 策略切换：Web Search（agent）/ Homepage Only（fast）

### v0.4 — 学术人才库 + 可编辑结果
- [x] Schema 迁移：source_type, advisor, institution, expected_graduation, research_area, personal_page
- [x] 毕业年份 LLM 提取（PhD start+5, Master start+2）
- [x] 结果表可编辑（contenteditable + role 下拉 + 行删除）
- [x] Import 写入全部学术字段
- [x] 学术人才筛选（导师/学校/角色/毕业年/方向/状态）
- [x] 按导师分组视图
- [x] Pipeline 状态管理（New → Contacted → Replied → Interview）
- [x] 二次 Enrich（批量抓取个人主页，LLM 提取 Scholar/论文/毕业年）

### v0.5 — 导航重构 + GitHub 信号
- [x] 三段式导航：Discover → Pool → Outreach
- [x] 人才库合并：All / Academic / Industry Tab
- [x] Conference Papers 模式占位
- [x] 旧 URL 301 重定向
- [x] GitHub 信号分层（Events API 活动分析）
- [x] 信心评分（HIGH/MEDIUM/LOW/NONE）控制话术精度
- [x] Repo 分类：核心项目 / 社区贡献 / 历史项目 / 忽略

---

## 四、Roadmap

### v0.6 — Conference Papers（会议论文候选人）🔜

**目标**：通过学术会议 accepted paper list 批量锁定一作 PhD/Postdoc。

**用户故事**：
> "NeurIPS 2025 论文列表出来了，我想找做 LLM alignment 方向的一作，自动识别哪些是 PhD 学生，哪些快毕业了。"

**功能设计**：
- 输入方式：
  - 粘贴会议 proceedings URL（如 openreview.net、aclanthology.org）
  - 上传 paper list CSV/JSON
  - 手动粘贴论文标题列表
- 自动提取：
  - 论文标题、作者列表、摘要
  - 一作 / 通讯作者标识
  - 通过 Semantic Scholar / DBLP API 补全作者机构信息
- 筛选维度：
  - 按研究方向关键词过滤
  - 按机构过滤（只看 Stanford / CMU / ...）
  - 按作者位次过滤（只看一作）
- 输出：与 Lab Sourcer 同格式，可编辑 → Import → Pool

**技术方案**：
- Semantic Scholar API：`GET /paper/search?query=...&venue=NeurIPS&year=2025`
- DBLP API：按会议 + 年份检索
- 一作判定：author 列表 position=0，或标注 `*` 的通讯作者
- 机构补全：S2 author → affiliations，或抓取个人主页

**预计工作量**：1 周

---

### v0.7 — 外联效果追踪

**目标**：闭环追踪 Outreach 效果，回答"哪种话术有效"。

**功能设计**：
- Pipeline 状态机完善：New → Contacted → Replied → Interview → Offer → Rejected
- 每条消息记录：发送时间、渠道（LinkedIn / Email）、候选人反应
- 回复率统计：按模式（社区/学术/直推）、按语言、按候选人类型
- Insights 面板新增：
  - 外联转化率趋势图
  - 最佳触达时间分析
  - A/B 话术对比（同类候选人不同话术的回复率）

---

### v0.8 — Enrichment 体系化

**目标**：从多源自动补全候选人档案，减少手动查资料时间。

**当前问题**：
- GitHub enrichment 已有信号分层，但不缓存
- LinkedIn 依赖手动粘贴
- Google Scholar 数据未接入
- 每次生成话术都重新调 API，浪费且慢

**功能设计**：
- Enrichment Cache（30 天 TTL）：
  - GitHub profile + 活跃项目摘要
  - Google Scholar：h-index、近 3 年论文数、高引论文
  - Semantic Scholar：合作网络、研究方向演变
  - 个人主页：抓取一次，提取结构化信息
- 自动触发：候选人入库时触发一轮 enrich
- 手动触发：详情页一键 "Re-enrich"
- 缓存指示器：详情页显示各源最后更新时间

---

### v0.9 — 批量操作 + 邮件集成

**目标**：支持批量外联和真实发送。

**功能设计**：
- Pool 页面批量选择 → 批量生成话术 → 批量标记状态
- Email 集成（Gmail API）：
  - 直接从平台发送邮件
  - 自动追踪打开 / 回复
  - 模板化：相同话术结构，自动填充个性化部分
- LinkedIn 消息（手动辅助）：
  - 生成后一键复制
  - Chrome 插件辅助：在 LinkedIn 页面自动填充

---

### v1.0 — JD 智能匹配

**目标**：给定一个 JD，自动从 Pool 中找出最匹配的候选人。

**功能设计**：
- 输入 JD 文本或 URL
- 自动解析：硬约束（地点、学历、年限）+ 软约束（技术栈、研究方向）
- 三层匹配：
  1. 结构化筛选缩小范围（SQL）
  2. 语义匹配排序（LanceDB）
  3. LLM 精排 + 匹配理由
- 输出：Top 20 候选人 + 每人匹配理由 + 一键生成话术

---

## 五、技术债 & 基础设施

| 优先级 | 事项 | 说明 |
|--------|------|------|
| P0 | Enrichment Cache | GitHub API 限流 60次/hr（无 token），每次外联都重新调用 |
| P0 | AI Provider 统一 | app.py 里直接 `import anthropic`，应走 `ai/provider.py` |
| P1 | 错误处理 | 抓取失败 / LLM 超时 / API 限流，用户只看到空结果 |
| P1 | 测试 | 零测试覆盖，db.py 和 scraper 应有基础测试 |
| P2 | 代码拆分 | app.py 已 2000+ 行，应按模块拆分路由 |
| P2 | 日志 | print → logging，生产环境可控级别 |
| P3 | 部署 | 目前只能 `python app.py`，应支持 Docker 一键部署 |

---

## 六、设计原则

1. **宁可模糊正确，不可精确犯错** — GitHub 信号弱就说"相关方向"，不点名项目
2. **本地优先** — 数据不出本机，LLM 可切全离线
3. **一步到位 < 快速迭代** — 先跑通再优化，不做过度设计
4. **自动化 ≠ 全自动** — 人在回路：AI 提取 → 人工校验 → 确认发送
5. **成本敏感** — 两阶段抓取（HTTP 免费 + 1 次 LLM）优于 10 轮 agent 对话

---

## 七、关键指标（待追踪）

| 指标 | 定义 | 目标 |
|------|------|------|
| 发现效率 | 每个教授提取成员的耗时和准确率 | < 30s/教授，> 80% 准确 |
| 话术个性化度 | 候选人能否感知消息是专门写给他的 | 回复中提及"你提到的 X 项目" |
| 回复率 | Contacted → Replied 转化率 | > 15%（行业均值 5-10%） |
| 误触率 | 话术引用了错误/无关的项目/论文 | < 5% |
| 单次外联成本 | LLM API 费用 / 外联人数 | < $0.05/人 |

---

## 八、版本历史

| 日期 | 版本 | 里程碑 |
|------|------|--------|
| 2025-03 | v0.1 | 话术生成器上线 |
| 2025-04 | v0.2 | 人才库 + CSV 导入 + Insights |
| 2025-05 | v0.3 | Lab Sourcer + CSRankings + 两阶段抓取 |
| 2026-05-24 | v0.4 | 学术人才库 + 可编辑结果表 + 毕业年份提取 |
| 2026-05-25 | v0.5 | 导航重构 + GitHub 信号分层 |
| TBD | v0.6 | Conference Papers 候选人发现 |
| TBD | v0.7 | 外联效果追踪 |
| TBD | v0.8 | Enrichment 体系化 |
| TBD | v0.9 | 批量操作 + 邮件集成 |
| TBD | v1.0 | JD 智能匹配 |
