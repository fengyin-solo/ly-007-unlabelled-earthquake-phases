# 山地台网三次到时复核

10 月 23 日凌晨，值班地震员收到一批混在一起的 P/S 到时，台站钟差也已超过巡检阈值。
入口是 `/app/phase_association.py`，按 `python3 /app/phase_association.py CATALOGUE_JSON RESULT_JSON LEDGER_JSONL` 调用。
成功收口时，标准输出仅回显运行号（`OK <run_id>\n`），标准错误保持空白。
拒收时 stdout 为空，stderr 只写 `REFUSED BAD_INPUT\n`、`REFUSED RESIDUAL_LIMIT\n`、`REFUSED AMBIGUOUS_ASSOCIATION\n` 或 `REFUSED LEDGER_CONFLICT\n`。

## 台网导出

现场文件是 UTF-8 JSON 对象，顶层字段集合恰好是 `{arrivals,campaign_id,clock_bound_s,event_count,run_id,stations,velocity_km_s}`。
`campaign_id` 与 `run_id` 是非空字符串，`event_count` 是整数 3，`clock_bound_s` 是大于 0 且不超过 0.25 的有限数。
`velocity_km_s` 只有 `{p,s}`；`stations` 恰有 8 项，`arrivals` 恰有 48 项。
站对象固定由 `station_id`、`x_km`、`y_km` 三项组成。每条到时对象可写成 `{arrival_id,station_id,time_s}`。
站号和到时号都必须非空且各自不重复。
每条到时都应落在 0 到 1000 秒之间，且不能是无穷或 NaN。
每站恰有 6 条到时；现场不提供事件号、相位名、源参数、钟差或到时归属，输入顺序也不代表事件或相位。
所有事件共用同一组 `p`、`s` 速度；从震源到台站的直线距离除以对应速度就是走时。

## 关联与定位

一条到时满足“发震时刻 + 台站钟差 + 该相位走时”；同一事件同一台站的 P、S 共用一个钟差。
每站先由 P/S 同源对得到可检验的 S-P 残差，跨站关联不得依赖输入先后或未给出的标签。
三个事件在每站各出现一次 P 和一次 S；48 条到时必须全部使用一次，不能跨事件借用相位。
按每站把 6 条到时配成 3 组 P/S 计，单是台内配对就有 `15^8 = 2562890625` 种；完整六组相位关联下界为 `7.34e28` 量级。
对每个可行关联，用同一波速模型、三个发震时刻和八个钟差共同最小化 48 条到时的全局均方根残差。
全局残差不得超过 `0.030000` 秒；残差最低的两个不同关联相差不足 `0.001500` 秒时按 `AMBIGUOUS_ASSOCIATION` 拒收。
钟差取八站均值为零的规范解。
任一钟差绝对值不超过 `clock_bound_s`。
较早发生的事件先编号：发震时刻由早到晚依次写为 `EVT-01`、`EVT-02`、`EVT-03`。

## 交付与恢复

结果顶层字段集合恰好是 `{campaign_id,events,global_residual_rms,run_id,schema_version,stations}`，`schema_version` 是整数 6。
每个 `events` 成员固定包含 `event_id`、`hypocenter`、`origin_time_s`、`p_arrival_ids`、`residual_rms` 与 `s_arrival_ids`；`hypocenter` 的三个分量是 `depth_km`、`x_km`、`y_km`，两个到时号列表均按升序写出。
钟差表以 `station_id` 升序交出，每行仅有 `clock_offset_s` 与 `station_id`；数值按十进制 half-up 保留 6 位，结果用排序键紧凑 JSON 并以一个换行结束。
台账每行只有 `{campaign_id,entry_hash,input_digest,output_digest,previous_hash,run_id}`；首行前序哈希为 64 个零，后续行接上一行哈希，去掉 `entry_hash` 后按排序键紧凑编码的 SHA-256 即本行哈希。
相同输入重放时结果与台账字节不变；同号不同输入或结果摘要按 `LEDGER_CONFLICT` 拒收；新运行号只追加，缺失或损坏台账直接初始化。
任何拒收都不得改动输入、旧结果或旧台账；整次运行须在 12 秒内结束。
