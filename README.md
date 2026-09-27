# 教材试用观察期

本地服务，用于管理教材试用学校的课堂观察记录：先草稿保存、证据分次补充、
完成审核后才能签发；每次状态变化留痕，支持按学校 / 日期 / 当前状态检索。

## 状态模型

```
draft ──提交审核──▶ reviewing ──审核通过──▶ issued   （终态，证据与状态冻结）
  ▲                    │
  └──── 退回补充 ──────┘
draft / reviewing ──取消──▶ cancelled（终态）
```

- **draft 草稿**：观察先存草稿，编辑可分多次补充证据；
- **reviewing 审核中**：草稿送审后进入；可通过签发、退回草稿、取消；
- **issued 已签发**：只有 reviewing + 至少一条有效证据才能进入；进入时把当时
  状态与有效证据关系存入 `frozen_snapshot`，之后不可再补/撤证据，状态终态；
- **cancelled 已取消**：误录等情况的终态。

## 关键设计

- 每次状态迁移都写一条 `transitions`（from/to、**原因 reason**、操作人、
  生效时间），草稿创建本身也记一条，任何时候都能看出"为什么变成现在这样"。
- 证据只追加、只软撤回（`superseded_at` 标记），不物理删除；签发后冻结。
- 检索只匹配 cases 当前行：记录曾处于 draft/reviewing 不影响当前过滤，
  **过期状态不会混进结果**。
- 乐观并发（`expected_version`）：状态被他人推进后，旧客户端的提交返回 409。
- 业务时间由可注入时钟产生并显式落库；SQLite 单文件持久化，**跨日期边界
  （节假日、跨学期）重开服务或重新查询，签发状态与证据关系保持一致**。
- 创建接口支持幂等键（`idempotency_key`），同键同请求重放返回同一结果。

## 运行

```bash
python3 -m service_09261_005.server --db ./observations.db --port 8000
# 可选 --now 2026-09-27T10:00:00+08:00 固定业务时钟，便于跨日期演示/排查
```

仅依赖 Python 3.9+ 标准库（`argparse`、`http.server`、`sqlite3`）。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/cases` | 创建草稿（需 id/actor/school/observed_on，可带 reason、idempotency_key） |
| POST | `/cases/{id}/move` | 状态迁移，必须带 `reason`；签发建议带 `expected_version` |
| POST | `/cases/{id}/evidence` | 补充一条证据（kind/ref，可带 note） |
| POST | `/cases/{id}/evidence/{eid}/withdraw` | 软撤回证据（可带 note） |
| GET  | `/cases/{id}` | 详情：当前状态、版本、证据（含撤回标记）、状态历史 |
| GET  | `/cases` | 检索：`school`、`observed_on`（精确日）、`from`/`to`（闭区间）、`state`（当前状态） |

错误以 HTTP 状态码区分：400 校验错误、404 记录不存在、409 状态/版本冲突。

### 示例

```bash
curl -X POST localhost:8000/cases -H 'Content-Type: application/json' -d '{
  "id":"obs-7","actor":"张编辑","school":"育才中学",
  "observed_on":"2026-09-26","reason":"首次听课"}'

curl -X POST localhost:8000/cases/obs-7/evidence -H 'Content-Type: application/json' -d '{
  "actor":"张编辑","kind":"照片","ref":"p/001.jpg","note":"课堂全景"}'

curl -X POST localhost:8000/cases/obs-7/move -H 'Content-Type: application/json' -d '{
  "actor":"张编辑","state":"reviewing","reason":"证据齐，送审"}'

curl -X POST localhost:8000/cases/obs-7/move -H 'Content-Type: application/json' -d '{
  "actor":"王审核","state":"issued","reason":"材料完整，同意签发","expected_version":2}'

curl "localhost:8000/cases?school=育才中学&state=issued&from=2026-09-01&to=2026-09-30"
```

## 测试与编译

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q service_09261_005 tests
```
