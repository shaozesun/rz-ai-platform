# DCIM 幽灵列卡死园区消费者（2026-10-26）

## 结论

`00000000-00WG-0000-0000-000000000000` 在 PostgreSQL 里登记了 **6,775** 个测点列（全部在 g0），
而 TDengine 的 `st_00000000_00wg_0000_0000_000000000000` 实际只有 **4,043** 个业务列 ——
**2,732 个映射是「幽灵」**：PG 里有、TDengine 里从来没有过。

`ensure_stable()` 每次都发 `CREATE STABLE IF NOT EXISTS <表> (6,778 列) TAGS (...)`，
而 **TDengine 即使表已存在也校验列数** → `Too many columns` → `RealtimeError`
→ `[廊坊A/real]` 消费者每 2 秒重连一次，**该园区实时数据停更**（offset 未提交，数据不丢，只停滞）。

## 根因

旧代码的提交顺序是「**先提交 PG 映射 → 再 ALTER TDengine**」。历史上那次根盘写满导致
ALTER 失败时，PG 里那批映射已经提交 —— 这些列从未建出来，但一直在映射里。
只要某组 PG 列数超过 4,096，建表语句就会被拒；而 `IF NOT EXISTS` 并不能让它跳过列数校验。

换组之后这个问题消失了：`allocate_columns_locked(..., commit=False)` 现在**先建表成功再提交**，
失败就 rollback，重放会分到同样的列名与分组。

## 证据（生产只读核实）

| 项 | 值 |
|---|---|
| PG `dcim_td_column`（00WG） | 6,775，全部 `point_group='g0'` |
| TDengine `DESCRIBE st_…_00wg…` 业务列 | 4,043（+3 系统列 = 4,046） |
| 幽灵（PG 有 / TD 无） | **2,732** |
| TDengine 独有（TD 有 / PG 无） | 0 |
| 其余 95 组 | 全部一一对应，无幽灵 |

## 处置

`deploy/scripts/dcim_group_reconcile.py`：默认 dry-run，只搬「PG 有、TDengine 没有」的列；
目标组按**TDengine 真实列数**算空位（容量线 `GROUP_MAX_COLUMNS`），装不下顺延 g1/g2…
有数据的列一律不动。**只改 PG，不建表、不删行** —— TDengine 侧的表由写侧下一次
`ensure_stable` 自动补。

```bash
rsync -av -e "ssh -i ~/.ssh/id_rsa_mage -p 18116" \
  deploy/scripts/dcim_group_reconcile.py <host>:/tmp/
ssh <host> 'docker cp /tmp/dcim_group_reconcile.py rz-dcim-ingest:/tmp/ && \
  docker exec -w /app -e PYTHONPATH=/app rz-dcim-ingest \
    python /tmp/dcim_group_reconcile.py --device-type 00000000-00WG-0000-0000-000000000000'
# 数字核对无误后加 --apply
```

⚠️ 容器 `PYTHONPATH` 为空、镜像里不含 `deploy/`：必须 `docker cp` 且显式 `-e PYTHONPATH=/app`
（脚本本身也会按「哪个目录下有 `service/`」自行补 `sys.path`）。

实测结果：

| 检查 | 修前 | 修后 |
|---|---|---|
| PG 分组 | g0 = 6,775 | g0 = 4,043 / g1 = 2,732 |
| 超级表 | 仅 g0（4,046 数据列） | g0（4,046）+ g1（2,735） |
| `[廊坊A/real]` | 每 2 秒 `Too many columns` | 0.5~0.9s 落库 ~1,400 行/批 |

搬迁后 g0 仍有 4,043 个**有数据**的列（故意超过 4,000 容量线，存量不搬），
建表 4,043 + 3 系统 + 6 tag = 4,052 < 4,096，仍能通过；新测点会进 g1。

## 边界与后续

- 幽灵列没有历史数据，**换组安全**；判定只用「PG 映射 vs `DESCRIBE` 实列」，不猜。
- 回滚：`UPDATE dcim_td_column SET point_group='g0' WHERE device_type='…' AND point_group='g1'`
  （搬完那一刻 g1 里恰是全部被搬走的行）。已建出来的空 g1 表留着无害；删表属删数据，需先问。
- 脚本与预警线（`GROUP_WARN_COLUMNS`）随下一次镜像重建上线，**解卡不需要重建**。
- 消费恢复后 Kafka 在重放积压（实测写入的 `ts` 是三天前的），所以「近 5 分钟无数据」不代表没写 ——
  看 `LAST(ts)` 与累计行数，别只看 `WHERE ts > NOW - 5m`。
