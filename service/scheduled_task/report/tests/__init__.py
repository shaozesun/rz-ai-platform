"""报告口径自检（**不依赖测试框架**）。

仓库 `requirements.txt` 里没有 pytest，所以这里用纯 Python `assert` 写成：
既可直接跑 —— `python -m service.scheduled_task.report.tests.test_event_compute`，
将来接入 pytest 也能用（函数名都是 `test_*`）。

**为什么要单独放一份**：报告改成"确定性工作流"的核心卖点就是**口径固定、可断言**。
2026-10-15 出过一次真实事故 —— KPI 卡片用位置下标去取"最多的那个"，同一份报告里
卡片写"专业集中度 供配电(32 起)"、正文写"制冷 98 起"，自相矛盾且连看几版都没发现。
这类"取错下标/算错聚合"的错，只有断言能拦住。以后给其余 5 类工单加 spec 时，
照 `test_event_compute.py` 的结构补一份即可。
"""
