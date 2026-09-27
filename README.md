# 教材试用观察期

本地课堂观察服务：观察先存草稿、证据可分次补充、审核完成后才能签发；每次状态变化都记录原因，检索默认不混入过期记录。纯 Python，SQLite 持久化，JSON API 边界。

## 状态机

```
draft → reviewing → reviewed → issued
reviewing → draft            （审核退回，补充证据后可再次提交）
draft/reviewing/reviewed → expired（观察期结束，要求 as_of 晚于 valid_until）
issued 为终态，不会过期
```

## API（service_09261_005/api.py 的 dispatch）

- `POST /observations` `{id, school, observed_on, valid_until, actor, idempotency_key?}` → 201，创建草稿
- `POST /observations/{id}/evidence` `{id, content, actor, idempotency_key?}` → 201，仅草稿状态可补充证据
- `POST /observations/{id}/transition` `{to_state, actor, reason, idempotency_key?, as_of?}` → 200，reason 必填并写入历史
- `POST /observations/expire-due` `{as_of, actor, reason}` → 200，批量过期所有 valid_until 早于 as_of 的未完成记录
- `GET /observations?school=&date_from=&date_to=&state=&include_expired=` → 200，默认排除 expired；显式 `state=expired` 或 `include_expired=1` 才返回过期记录
- `GET /observations/{id}` → 200，含证据列表与状态变更历史（from/to/reason/actor/时间）

## 一致性约定

- 状态只通过命令变更，查询不读取系统时钟：同一批观察跨日期边界重复查询，签发状态与证据关系保持一致。
- 变更类命令支持 `idempotency_key`，重试不会产生重复证据或重复变更。
- 每次成功变更后整体快照写入 SQLite（store.py），重启后从最新快照恢复。

测试命令：python3 -m unittest discover -s tests -v

编译命令：python3 -m compileall -q service_09261_005 tests
