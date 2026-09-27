# 临床试验随机分配与盲法服务

仅使用 Python 3.11+ 标准库的独立随机化服务。支持入组前筛选门禁、分层区组随机、试验方案锁定、隐藏分组、外部编号并发幂等、中心隔离、双人揭盲和审计。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8104>，默认数据库 `randomization.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`site1`、`site2`（研究中心），`coord`（协调员），`monitor1`、`monitor2`（监查员）。

## 入组前筛选门禁

受试者必须先在中心完成筛选登记，系统按方案自动判定为**合格（eligible）/ 不合格（failed）/ 待复核（pending）**，只有合格且关键化验未过期的记录才能入组；未通过时一律不占用、不发放随机号。

- 试验级配置（建试验时登记，草稿期可在方案接口修改）：
  - `min_age`：最低入组年龄（周岁）；登记年龄低于该值判不合格。
  - `lab_validity_days`：关键化验有效期（天）；正常化验距当天超过有效期转待复核，需重新化验。
- 中心登记字段：`consent_date`（知情同意签署日期，缺失即不合格）、`age`、`lab_date`、`lab_result`（`normal`/`abnormal`/未回报）。
- 判定原因码随接口返回，例如 `consent_missing`、`age_below_minimum`、`lab_pending`、`lab_result_pending`、`lab_abnormal`、`lab_expired`。
- 化验结果回报或资料更正（PATCH）后系统**重新判定**；待复核可转合格，合格也可能因化验过期转待复核（入组瞬间会再次校验）。
- 筛败记录不可修改；对筛败受试者**重筛**会生成新一轮（attempt+1）筛选并完整保留历史，旧轮次冻结。已入组记录同样冻结。
- 筛选按中心隔离：中心用户只能登记/查看/重筛/入组本中心受试者，跨中心使用筛选记录返回 `site_isolation`；协调员和监查员可跨中心只读查看。

## 主要接口

- `POST /api/trials`：创建草稿试验，指定分组、分层因素、区组长度、随机种子、`min_age`、`lab_validity_days`。
- `GET /api/trials/{id}`：查看试验配置（含筛选标准）。
- `POST /api/trials/{id}/protocol`：入组前修改方案；一旦入组即锁定。
- `POST /api/trials/{id}/start`：开始入组。
- `POST /api/trials/{id}/screenings`：本中心登记筛选，响应含 `status`、`reasons`、`can_enroll`。
- `GET /api/trials/{id}/screenings`：查看筛选列表（中心用户只看本中心；自动按当天日期给出最新判定）。
- `GET /api/screenings/{id}` / `PATCH /api/screenings/{id}`：查看单条筛选 / 补录或更正资料并重新判定。
- `POST /api/screenings/{id}/rescreen`：筛败受试者重筛，历史轮次保留。
- `POST /api/trials/{id}/enroll`：请求体 `{"screening_id": 12, "factors": {...}}`；筛选不合格/待复核/化验过期时返回 409 `screening_not_eligible` 并在 `reasons` 中说明被挡原因；成功只返回分配编号，不返回分组。重复提交同一筛选记录为幂等返回。
- `GET /api/trials/{id}/participants`：分中心返回数据，中心用户看不到其他中心。
- `POST /api/participants/{id}/unblinding-requests`：发起揭盲。
- `POST /api/unblinding-requests/{id}/approve`：两人独立审批；同一人不能审批两次。
- `GET /api/trials/{id}/summary`：中心级汇总和审计记录。

随机表按“试验种子 + 中心 + 分层因素”确定性生成，每个区组为分组数的整数倍并打乱；分配在 SQLite `BEGIN IMMEDIATE` 事务中原子占用。实现适合作为流程原型，不替代经认证的临床试验随机化系统。
