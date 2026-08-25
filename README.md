# HotSpot Pulse — 热点流量预测与情绪分析系统（设计架构 v1）

> 本文档是**设计逻辑架构**，不含实现。目的：让主模型（DeepSeek V4 Pro 总指挥）与
> 5 个 GLM 子代理在后续「项目预演」中能**理解各自职责、并行分工、调度依赖**。
> 每个 stage 是「委派单元」：一个 stage = 一个子代理的独立任务（或主模型亲自处理）。

---

## 快速开始

```bash
cd dsh-hotspot-predictor
python main.py "AI芯片 24小时 中国"      # 直接跑（默认 MOCK + LINEAR，零依赖）
# 或安装为命令：
pip install . && hotspot "AI芯片"
```

- 输出：终端打印汇总报告（关联话题 / 增长预测 / 情绪分布 / 区域热度），并在 `out/` 生成 `hotspot.svg` 热力网图。
- 纯标准库，无网络、无第三方依赖即可端到端跑通。

---

## 1. 目标（一句话）

输入**一个新 tag（话题）**，输出四样东西：

1. **关联话题网络**：该 tag 及关联话题，边权 = 关联度/共现度。
2. **短期增长预测**：未来 N 小时内的量/热度预测 + 增长信号（速度、加速度、破点）。
3. **情绪分析**：按话题、按区域聚合的正负/情绪类别分布。
4. **热度区域分布网图**：节点 = 区域（或话题），边 = 跨区传播 / 话题关联，
   节点大小/颜色编码热度 → 一张「热力网图」。

---

## 2. 总体流水线（阶段 = 委派单元）

```
[用户输入: 新 tag]
   │
   ▼
┌─ 阶段 0 · 输入解析与安全过滤 ─────────────┐   ←  V4 Pro 亲自
│ 解析 tag / 时间窗 / 地域范围 / 情绪维度；   │       · 委派前安全过滤（防注入/越权）
│ 产出标准化 Query。                        │       · 主模型自主，绝不委派
└──────────────────────────────────────────┘
   │
   ▼
┌─ 阶段 1 · 采集 DataIngestor ─────────────┐   ←  senior-coder
│ 多源采集（社交API/搜索/爬虫），统一 Post； │       · 适配器模式，可插拔数据源
│ 增量轮询 + 幂等去重。                     │
└──────────────────────────────────────────┘
   │
   ▼
┌─ 阶段 2 · 关联话题 TopicGraphBuilder ────┐   ←  general-reasoner（语义层）
│ 检测关联话题，构建 tag→related 图；        │       + senior-coder（聚合实现）
│ 边权 = 共现/语义相似。                    │
└──────────────────────────────────────────┘
   │
   ▼
┌─ 阶段 3 · 情绪 SentimentAnalyzer ────────┐   ←  general-reasoner（文本）
│ 文本情绪分类；可选图像表情多模态；         │   + multimodal-operator（图文混合时）
│ 按 topic/region 聚合。                   │
└──────────────────────────────────────────┘
   │
   ▼
┌─ 阶段 4 · 短期增长 GrowthPredictor ──────┐   ←  senior-coder（建模实现）
│ 时序预测（短窗口）；增长信号提取；         │   + V4 Pro（**核心算法亲自把关**）
│ 输出每话题未来量 + 置信度 + 破点。         │
└──────────────────────────────────────────┘
   │
   ▼
┌─ 阶段 5 · 区域热度 RegionHeatBuilder ────┐   ←  general-reasoner（地域归类）
│ 地理推断 → 按区域聚合热度；               │   + senior-coder（聚合实现）
│ 建区域节点 + 跨区传播边。                 │
└──────────────────────────────────────────┘
   │
   ▼
┌─ 阶段 6 · 热力网图 NetworkGraphRenderer ─┐   ←  visual-analyst
│ 渲染网图：节点=区域/话题，边=关联/传播；   │       · 图表/可视化专长
│ 节点大小/颜色=热度。                      │
└──────────────────────────────────────────┘
   │
   ▼
┌─ 阶段 7 · 报告合成与最终审核 ─────────────┐   ←  V4 Pro 亲自
│ 汇总预测+情绪+网图；一致性校验；拍板发布。  │       · 最终拍板权 / 安全审核
└──────────────────────────────────────────┘
```

---

## 3. 分工调度表（主模型 + 子代理）

| 阶段 | 模块文件 | 负责 | 输入 | 输出 | 是否核心逻辑 |
|---|---|---|---|---|---|
| 0 | `input_parser.py` | **V4 Pro 亲自** | 原始用户输入 | `Query` | 是（含安全过滤） |
| 1 | `collector.py` | senior-coder | `Query` | `Post[]` | 否（重但机械） |
| 2 | `topic_graph.py` | general-reasoner + senior-coder | `Post[]` | `TopicGraph` | 否（语义聚类可委派） |
| 3 | `sentiment.py` | general-reasoner / multimodal-operator | `Post[]` | `SentimentAgg` | 否（可委派） |
| 4 | `growth.py` | senior-coder(建模) + **V4 Pro 把关** | 时序数据 | `Prediction[]` | **是（V4 Pro 审定算法）** |
| 5 | `region_heat.py` | general-reasoner + senior-coder | `Post[]`+地理 | `RegionGraph` | 否 |
| 6 | `renderer.py` | visual-analyst | 各图 | `HotspotGraph`(PNG/SVG) | 否（图表专长） |
| 7 | `report.py`/orchestrator | **V4 Pro 亲自** | 所有中间结果 | 最终报告 | 是（拍板/审核） |

**委派原则（对齐 V4 Pro 总指挥 persona）：**
- 阶段 0、4 的**算法决策与安全边界**、阶段 7 的**最终审核** → V4 Pro 亲自，省 Token 且保安全。
- 阶段 1/2/3/5/6 的**机械、批量、图表、语义**任务 → 委派给对应子代理（便宜模型），V4 Pro 只做调度 + 审校。
- 每个子代理结果 = **草稿**，回传后 V4 Pro 做最终审核（无毒/合规/自洽）再合并发布。

---

## 4. 统一数据契约（见 `models.py`）

阶段之间**只通过数据契约交互**（dataclass），不共享内部状态。这是让子代理**并行开发互不阻塞**的关键：

- `Query`（输入）
- `Post`（原始消息）— 采集的输出，一切分析的输入
- `Topic` / `TopicGraph` / `RelatedEdge`（关联话题）
- `Sentiment` / `SentimentAgg`（情绪）
- `HeatRecord` / `Prediction` / `TrendSignal`（增长）
- `RegionNode` / `RegionEdge` / `RegionGraph`（区域）
- `HotspotGraph` / `LayoutHint`（网图）
- `PipelineResult`（最终汇总）

> 所有 dataclass 建议用 `@dataclass(slots=True)` + 可选 `pydantic` 校验，字段带类型与注释。

---

## 5. V4 Pro 总指挥调度流（见 `orchestrator.py`）

```
1. 收用户输入 → 阶段0：解析 + 安全过滤（防注入）→ Query
2. 阶段1（可先并行）：派 senior-coder 采集
3. 阶段2/3/5（相互可并行）：
     - 等阶段1出 Post[] 后，派 general-reasoner 做话题图 + 情绪 + 区域归类
     - 若含图片/多模态 → 追加 multimodal-operator
4. 阶段4：V4 Pro 亲自设计/审定增长模型；senior-coder 出实现草稿 → V4 Pro 审核
5. 阶段6：派 visual-analyst 渲染热力网图
6. 阶段7：V4 Pro 汇总 + 一致性校验 + 最终安全审核 → 拍板输出
```

**Token 策略：** 尽量并行派发（一次 assistant 消息多发），V4 Pro 只做解析/审核/整合；
图表、长文本、批量语义全部下放便宜 GLM，核心算法与最终答案留在 V4 Pro。

---

## 6. 配置（见 `config.py`）

- 数据源适配器（`data_source: social_api | search | mock`）
- 时间窗（`window_hours`、`prediction_horizon_hours`）
- 地域映射（`region_mapper: geoip | field | mock`）
- 预测模型（`predictor: linear | arima | prophet | lstm`，策略模式可插拔）
- 渲染参数（图大小、颜色映射、布局算法 `networkx`/`graphviz`）

---

## 7. 可扩展性与非功能

- **适配器模式**：数据源、地域映射、预测器均可替换，不改主流程。
- **策略模式**：`GrowthPredictor` 由 `predictor` 配置选实现。
- **幂等去重**：采集按 `(source, external_id)` 去重，可重跑。
- **错误隔离**：任一阶段失败不拖垮整体，失败 → 降级（mock/空图）+ 报告里标注。
- **离线 mock 模式**：无网络也可端到端跑通（供预演/测试）。

---

## 8. 预演分工建议（启动顺序）

1. **V4 Pro** 先定 `models.py`（契约是地基，谁也不许先改）。← 地基
2. `config.py` + `input_parser.py`（V4 Pro / lightweight-assistant）。
3. `collector.py`（senior-coder）→ 出 `Post[]`。
4. `topic_graph.py` / `sentiment.py` / `region_heat.py`（general-reasoner + senior-coder，三者并行）。
5. `growth.py`（senior-coder 草稿 → V4 Pro 审定算法）。
6. `renderer.py`（visual-analyst）。
7. `orchestrator.py` + `report`（V4 Pro）串联 + 端到端验证。
