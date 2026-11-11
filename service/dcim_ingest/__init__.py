"""DCIM Kafka 接入消费端（阶段一：空间/设备/点位配置数据）。

设计与字段口径见 docs/dcim/dcim-kafka-ingest-design.md。

独立进程运行：`python -m service.dcim_ingest`（与 web 同镜像、不同 command；
不与 web 共享内存，只通过 PostgreSQL 交互）。
"""
