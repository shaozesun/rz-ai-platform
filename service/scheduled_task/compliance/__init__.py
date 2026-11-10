"""运营监控中心告警不合规检查（班次班产出）。

确定性流水线：纯 SQL + openpyxl，不调模型、不进 agent 会话。
检查项注册表在 `checks.CHECKS`（前端与 xlsx 都由它驱动）。
"""
