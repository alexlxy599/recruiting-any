# 模型视角功能 · 进度与决策记录

> 会话上下文会被摘要压缩,这份文件是断点续传的依据。
> 最后更新:2026-08-31

## 已定的口径(与用户确认过)

1. **清单来源 = C 方案**:Artificial Analysis 榜单打底 + 从库内画像反向抽取补充。
   榜单保证行业完整性,反向抽取保证「库里已有的人不会没有归属」。
2. **不做能力排行,做覆盖度**。排名看 Artificial Analysis 就行,我们做不出更好的;
   「哪个模型库里有几个人 / 哪条线是空的」才是本库独有的信息。
3. **粒度 = 模型族,不到版本**。榜单原始 250 条里绝大多数是推理档位变体
   (Claude Opus 5 的 max/xhigh/high/medium/low),对招聘无意义。去重后 45 个族。
4. **技术栈词表**(用户认可):
   预训练 / 后训练·RLHF / RL / 多模态 / 推理加速·量化 / 数据 / 评测 /
   Infra·训练系统 / Agent / 安全对齐 / 3D·世界模型 / 视频生成 / 图像生成

## 已完成

- `data/raw/model_registry.json` — 71 个模型族 / 44 机构
  (45 来自榜单 + 26 从库内反向补:Cosmos、SAM、MetaCLIP、Uni-1、Pika 等)
- `data/raw/model_coverage.json` — 首版交叉结果(**含噪声,见下**)

## 已知问题(必须先修,否则界面在展示错数据)

1. **匹配是子串,没有词边界** → `Vidu` 命中 30 人,几乎全来自 "indi**vidu**al"。
2. **不区分关系类型** → `Gemini` 50 人里混了「做过 Gemini」和「用过 Gemini」。
   `GPT` 50 人大量来自 "openai" 泛匹配。
3. **榜单元数据会错** → 抓取把 `MAI-Image` 标成 Mistral AI,实为 **Microsoft AI**
   (MAI = Microsoft AI)。是靠库内 #4450 Haoyu Ma 的主页对账才发现的。
   **教训:榜单信息要和库内已知事实交叉验证,不能直接采信。**

## 可靠的结论(不受匹配噪声影响,0 就是 0)

库内 0 人的 11 个模型族:
```
GPT Image (OpenAI)      Seedance / Seedream (字节)
GLM (智谱)               LongCat (美团)
PixVerse (爱诗)          SkyReels (昆仑万维)
Mistral                 Command (Cohere)
LTX (Lightricks)        Hermes (Nous Research)
```
最值得注意:**字节 Doubao/Seed 有 22 人,但 Seedance(视频)与 Seedream(图像)是 0** —— 
同一家公司,LLM 线覆盖好,视觉生成线全空。智谱 GLM 在 LLM 榜前十,库内也是 0。

## 下一步(按顺序)

- [ ] **第一步:把匹配做准**(用户尚未拍板是否先做这步)
      - 词边界匹配 + 停用词表(挡 individual/vidu 这类)
      - 关系分级:核心贡献者 / 参与 / 使用 / 评测过 —— 只有前两类算覆盖
      - 取 20 个已知案例人工验准确率
- [ ] 第二步:落库 `models` + `person_models`(person_id, model_family, role,
      tech_stack, evidence, observed_at) —— 是 person_facts 思路的具体实例
- [ ] 第三步:界面。`/pool` 加「模型」透镜 或 独立 `/models` 覆盖矩阵页

## 平行进行中的任务

**主页画像提取**:已完成 253 人,剩 806 人。
- 通道:`python3.12 ingest/manual_extract.py dump --n 22 --cohort <xlsx>` → 读 → 写 JSON →
  `python3.12 ingest/manual_extract.py load <json>`
- 批次存档在 `data/raw/extract_batches/b0*.json`
- 进度查询:`python3.12 ingest/manual_extract.py status`
- 提取由人(Opus)读原文判断,不走本地模型 —— 用户明确要求

## 反复出现的数据问题(每批都会遇到)

- **库内机构大面积过期**:已修 39 人(migrations/002),但新批次仍在持续发现。
  典型:库内写 NVIDIA 实为 UCSB 教职 / 库内写 Ai2 实为 Thinking Machines。
- **主页会留过期文本**:不少人主页还写着「正在找工作」但早已入职。
  处理原则:以库内现职为准 + 标注「主页过期」,不当求职信号。
- **姓名对不上**:已发现 2 例(#4331、#4743)疑似主页挂错人,已标 ⚠ 未覆盖库内姓名。
