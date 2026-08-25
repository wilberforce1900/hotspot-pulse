"""
stages/__init__.py — HotSpot Pulse 阶段包。

阶段（委派单元）一览：
  input_parser.py  —— 阶段0 输入解析+安全过滤   [V4 Pro 亲自]
  collector.py     —— 阶段1 数据采集             [senior-coder]
  topic_graph.py   —— 阶段2 关联话题发现         [general-reasoner + senior-coder]
  sentiment.py     —— 阶段3 情绪分析             [general-reasoner / multimodal-operator]
  growth.py        —— 阶段4 短期增长预测         [senior-coder + V4 Pro 把关]
  region_heat.py   —— 阶段5 区域热度分布         [general-reasoner + senior-coder]
  renderer.py      —— 阶段6 热力网图渲染         [visual-analyst]
  orchestrator.py  —— 阶段0+7 调度与审核         [V4 Pro 亲自]
"""
