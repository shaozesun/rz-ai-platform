# 红外质量检测「全是检测失败」复盘（2026-11-09）

> 现象：本机跑了一次检测，任务 `2EsTvRH1SPC` 的结果几乎全是「检测失败」。
> 结论：**两个独立原因叠加** —— 我们把未巡检的台账设备也出了行（代码问题，已修）；
> 这 36 张真实巡检图模型一张都没给「清晰」（模型/训练域问题，待模型方确认）。

## 一、517/553 行是「未巡检的台账设备」

任务 `2EsTvRH1SPC`（廊坊 · A1，巡检人刘健）：

| 数据 | 值 |
|---|---|
| 列表接口 `deviceCount` | 36 |
| 详情 `inspectionDeviceTypes[].inspectionDevices[]` 展平 | 553 台 |
| 其中有 `picture` / `detectionTime` | 36 台 |
| 落库计数 | `{合格:0, 待复核:8, 不合格:19, 检测失败:526}` |

`526 = 517 无图片 + 9 模型输出「无法确定」`。详情里的设备是**设备台账**，没图片的那些
根本没参与本次巡检，却被 `_check_task` 逐台出行并判「检测失败/无图片」。

对照其他任务（都是「详情设备数 = `deviceCount` = 有图数」）：

| 任务 | 详情设备 | 有图片 | 计数 |
|---|---|---|---|
| `2CkNeGlZl4O` | 170 | 170 | 不合格 170 |
| `2CkNdoO79J3` | 190 | 190 | 不合格 189 / 失败 1 |
| `2Cjy96OURVB` | 170 | 170 | — |
| `2ExtnK6bASV` | 116 | **0** | 老任务，没有图 |

### 修法

- 只对**有 `picture`** 的设备出行（`workflow.photo_devices`），序号从 1 重排。
- `device_count` 取详情 `deviceCount`（权威值），另存 `photo_count`。
- 整任务无图片 → `result_status='no_image'`、明细为空、计数全 0、说明「该任务无巡检图片」，
  照常写 `checked_at`（不重复查）。
- **历史文档不删库、读时过滤**：`records` 丢弃「说明=无图片」的行；`list_checked` 用 Mongo
  聚合（`$filter`/`$size`）按有图片的行重算计数，`photo_count` 对旧文档同法兜底推出；
  `latest_run.counts` 按重算后的任务计数求和。旧任务不用重跑，页面立刻只剩 36 行。

## 二、真实图片模型从不输出「清晰」

36 张有图设备的模型输出：`19 不合格 / 8 待复核 / 9 无法确定 / 0 合格`。

只读复测（2026-11-09，`INFRARED_QUALITY_API_URL=http://10.100.17.152:18080/`）：

| 输入 | 模型输出 |
|---|---|
| 真实巡检图（`2AA12低压柜`） | 模糊或失焦 88.1 |
| 真实巡检图（`2AA11低压柜`，肉眼很锐） | 无法确定 51.4 |
| 真实巡检图（`2AA15*1低压柜`） | 临界状态 66.4 |
| 1993×1080 最高对比合成网格图 | 无法确定 47.5 |
| 纯灰底图 | 无法确定 39.8 |
| 肉眼清晰的可见光参考照 | 模糊或失焦 84.1 |

### 已排除的链路问题

- 服务端取的是**居中热区裁剪**（Gallery `热区裁剪` 预览 = 809×1080），与巡检 App 截图
  （1993×1080，两侧黑边 + 右侧色标）的成像区域对齐；裁剪结果肉眼可见是完整热像。
- 上传 → FileData（Gradio 6 必须带 `meta._type`）→ `/predict_images` → SSE `complete` →
  结果表，全链路走通，返回分数与库内逐字一致。
- 模型 `/describe_selected_model`：`mobilenet_v3_large`，输入 224，训练于 2026-11-06，
  工作区「特殊数据集」，checkpoint `.../特殊数据集/runs/best.pt`。

### 判定

热区裁剪图本身是低分辨率热成像上采样、确实偏软，但连肉眼很锐的样本也不给「清晰」，
说明是**模型训练域/阈值问题**，不是判定口径能解决的。fail-closed 映射保持不变
（认不出的标签一律「检测失败」，绝不默认合格）。

## 三、待模型方确认

1. 用一张**模型方认定「清晰」**的巡检图复测，看是否也落到「模糊或失焦 / 无法确定」；
2. 若训练集里「清晰」样本极少或阈值偏严，需要重训/校准；确认前不要改
   `INFRARED_QUALITY_PASS_LABELS` 或放宽口径；
3. 二分类/三分类的实际 `predict` 阈值与 `无法确定` 的判据（当前分数是未校准的 softmax×100）。

复测命令（只读，不写库、不读 mgmt）：

```bash
python -m service.infrared_quality.cli --predict /path/to/known_clear.jpg
```

## 四、落地清单

- `service/infrared_quality/workflow.py`：只对有图片设备出行；`photo_count`；`no_image`；
  `records_for` 过滤历史「无图片」行。
- `service/infrared_quality/store.py`：`list_checked` 聚合重算 `counts`/`photo_count`；`sum_counts`。
- `api/v1/scheduled_tasks.py`：任务载荷增 `photo_count`；`latest_run.counts` 按任务重算求和。
- 前端：`result_status='no_image'` 徽标、明细头「有图片 N」。
- `cli.py`：新增 `--predict`；dry-run 打印有图片设备。

## 五、2026-11-10 后续（原结论作废）

推理接口当天改版并实测（本机 3 张真实巡检图）：

```
headers: ["文件名", "Prediction", "sharp", "borderline", "defocused", "错误"]
row:     ["298ROzNAi5U.jpg", "sharp", "97.9%", "2.1%", "0.1%", ""]
row:     ["2ESjTIgvnRW.jpg", "borderline", "0.9%", "87.4%", "11.7%", ""]
```

- 标签改为英文四类 `sharp / borderline / defocused / uncertain`，概率列是带 `%` 的字符串；
- **真实巡检图已能给出 `sharp`**，上文「从不输出清晰」的结论作废（`--predict` 实测
  `sharp 91.8%`）；
- 判定口径随之改为 `sharp=合格 / borderline=待复核 / defocused=不合格 / uncertain 等=检测失败`，
  并且**只认新英文标签**：旧中文缓存命中会判「检测失败」，勾「重新推理」重跑即可覆盖。
