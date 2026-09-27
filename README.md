# 临床试验随机分配与盲法服务

仅使用 Python 3.11+ 标准库的独立随机化服务。支持入组前筛选(知情同意、最低年龄、关键化验有效期)、分层区组随机、试验方案锁定、隐藏分组、外部编号并发幂等、中心隔离、双人揭盲和审计。

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

## 主要接口

- `POST /api/trials`：创建草稿试验，指定分组、分层因素、区组长度、随机种子，以及筛选标准 `min_age`(最低年龄)和 `lab_validity_days`(关键化验有效期天数)。
- `POST /api/trials/{id}/protocol`：入组前修改方案和筛选标准；一旦入组即锁定。
- `POST /api/trials/{id}/start`：开始入组。
- `POST /api/trials/{id}/screenings`：中心登记筛选(知情同意及日期、年龄、关键化验日期和结果 `normal/abnormal/pending`)。系统判定 `eligible`(合格)、`ineligible`(不合格)或 `pending_review`(待复核)并返回原因；同一受试者重复提交即重筛，历史次数全部保留，已入组者筛选记录锁定。
- `GET /api/trials/{id}/screenings`：查看本中心当前筛选状态；加 `?external_id=` 查看该受试者含重筛的完整历史。协调员和监查员可见全部中心。
- `POST /api/trials/{id}/enroll`：仅当筛选合格且化验未过期时才分配随机号，否则返回 409 及被挡原因(`screening_required`/`screening_ineligible`/`screening_pending_review`/`screening_lab_expired`，附 `detail.reasons`)；响应只返回分配编号，不返回分组。
- `GET /api/trials/{id}/participants`：分中心返回数据，中心用户看不到其他中心。
- `POST /api/participants/{id}/unblinding-requests`：发起揭盲。
- `POST /api/unblinding-requests/{id}/approve`：两人独立审批；同一人不能审批两次。
- `GET /api/trials/{id}/summary`：中心级汇总、筛选状态统计和审计记录。

随机表按“试验种子 + 中心 + 分层因素”确定性生成，每个区组为分组数的整数倍并打乱；分配在 SQLite `BEGIN IMMEDIATE` 事务中原子占用，筛选卡口在占用分配之前执行，未通过筛选不会消耗随机号。实现适合作为流程原型，不替代经认证的临床试验随机化系统。
