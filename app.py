"""临床试验分层区组随机分配与盲法服务（含入组前筛选门禁）。"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "randomization.db"
MAX_ARM_LENGTH = 40
LAB_RESULTS = ("normal", "abnormal")
SCREENING_STATUSES = ("pending", "eligible", "failed", "enrolled")
UNSET = object()


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request", details=None):
        super().__init__(message)
        self.message, self.status, self.code, self.details = message, status, code, details


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today():
    return datetime.now(timezone.utc).date()


def _parse_iso_date(value, field):
    if not isinstance(value, str):
        raise BusinessError(f"{field}必须是 YYYY-MM-DD 格式的日期", 422, "invalid_date")
    text = value.strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise BusinessError(f"{field}日期格式应为 YYYY-MM-DD", 422, "invalid_date")


# (原因码, 中文说明)；failed 类原因优先于 pending 类
def _judge_screening(trial, consent_date, age, lab_date, lab_result, as_of):
    fail, pending = [], []
    if not consent_date:
        fail.append(("consent_missing", "缺少知情同意书签署日期，未签署知情同意不能入组"))
    if age is not None and age < trial["min_age"]:
        fail.append(("age_below_minimum", f"年龄 {age} 岁低于方案规定的最低入组年龄 {trial['min_age']} 岁"))
    if lab_result == "abnormal":
        fail.append(("lab_abnormal", "关键化验结果异常，不符合入组标准"))
    if age is None:
        pending.append(("age_missing", "年龄尚未登记，暂无法判定"))
    if not lab_date:
        pending.append(("lab_pending", "关键化验尚未完成或化验日期未登记"))
    elif lab_result is None:
        pending.append(("lab_result_pending", "关键化验结果尚未回报，暂不能入组"))
    elif lab_result == "normal":
        days = (as_of - _parse_iso_date(lab_date, "化验日期")).days
        if days > trial["lab_validity_days"]:
            pending.append(
                ("lab_expired", f"化验日期 {lab_date} 距今天 {days} 天，已超过 {trial['lab_validity_days']} 天有效期，需重新化验")
            )
    reasons = fail + pending
    if fail:
        status = "failed"
    elif pending:
        status = "pending"
    else:
        status = "eligible"
    return status, [{"code": c, "message": m} for c, m in reasons]


class RandomizationStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def init_schema(self):
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('site','coordinator','monitor')),
                    site_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS trials(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
                    protocol_version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft','running','stopped')),
                    arms_json TEXT NOT NULL, strata_factors_json TEXT NOT NULL,
                    block_size INTEGER NOT NULL CHECK(block_size >= 2),
                    seed TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, started_at TEXT,
                    min_age INTEGER NOT NULL DEFAULT 0,
                    lab_validity_days INTEGER NOT NULL DEFAULT 30
                );
                CREATE TABLE IF NOT EXISTS strata(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id INTEGER NOT NULL REFERENCES trials(id),
                    stratum_key TEXT NOT NULL, factors_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(trial_id,stratum_key)
                );
                CREATE TABLE IF NOT EXISTS allocations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id INTEGER NOT NULL REFERENCES trials(id),
                    stratum_id INTEGER NOT NULL REFERENCES strata(id),
                    sequence INTEGER NOT NULL, block_no INTEGER NOT NULL,
                    arm TEXT NOT NULL, used_by INTEGER, used_at TEXT,
                    UNIQUE(stratum_id,sequence)
                );
                CREATE TABLE IF NOT EXISTS participants(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id INTEGER NOT NULL REFERENCES trials(id),
                    site_id TEXT NOT NULL, external_id TEXT NOT NULL,
                    stratum_id INTEGER NOT NULL REFERENCES strata(id),
                    allocation_id INTEGER NOT NULL UNIQUE REFERENCES allocations(id),
                    allocation_code TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'enrolled'
                        CHECK(status IN ('enrolled','withdrawn','completed')),
                    enrolled_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(trial_id,external_id)
                );
                CREATE TABLE IF NOT EXISTS screening_records(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id INTEGER NOT NULL REFERENCES trials(id),
                    site_id TEXT NOT NULL, external_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                    consent_date TEXT, age INTEGER, lab_date TEXT,
                    lab_result TEXT CHECK(lab_result IS NULL OR lab_result IN ('normal','abnormal')),
                    status TEXT NOT NULL
                        CHECK(status IN ('pending','eligible','failed','enrolled')),
                    reasons_json TEXT NOT NULL DEFAULT '[]', judged_at TEXT,
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    participant_id INTEGER UNIQUE REFERENCES participants(id),
                    UNIQUE(trial_id,site_id,external_id,attempt)
                );
                CREATE TABLE IF NOT EXISTS unblinding_requests(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    participant_id INTEGER NOT NULL REFERENCES participants(id),
                    requester_id TEXT NOT NULL REFERENCES users(id), reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
                    first_approver TEXT REFERENCES users(id), second_approver TEXT REFERENCES users(id),
                    decided_at TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, trial_id INTEGER REFERENCES trials(id),
                    actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                """
            )
            # 兼容旧库：补充筛选相关列
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(trials)")}
            if "min_age" not in cols:
                conn.execute("ALTER TABLE trials ADD COLUMN min_age INTEGER NOT NULL DEFAULT 0")
            if "lab_validity_days" not in cols:
                conn.execute("ALTER TABLE trials ADD COLUMN lab_validity_days INTEGER NOT NULL DEFAULT 30")

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role,site_id) VALUES(?,?,?,?)",
                [
                    ("site1", "中心一协调员", "site", "S001"),
                    ("site2", "中心二协调员", "site", "S002"),
                    ("coord", "项目协调员", "coordinator", "CENTER"),
                    ("monitor1", "独立监查员甲", "monitor", "CENTER"),
                    ("monitor2", "独立监查员乙", "monitor", "CENTER"),
                ],
            )

    def _user(self, conn, user_id, roles=None):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在或已停用", 401, "unknown_user")
        if roles and user["role"] not in roles:
            raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
        return user

    def _trial(self, conn, trial_id):
        row = conn.execute("SELECT * FROM trials WHERE id=?", (trial_id,)).fetchone()
        if not row:
            raise BusinessError("试验不存在", 404, "not_found")
        return row

    def _audit(self, conn, trial_id, actor, action, detail):
        conn.execute(
            "INSERT INTO audit_log(trial_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (trial_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    @staticmethod
    def _validate_screening_config(min_age, lab_validity_days):
        if isinstance(min_age, bool) or not isinstance(min_age, int) or not 0 <= min_age <= 150:
            raise BusinessError("最低入组年龄必须是 0~150 之间的整数", 422, "invalid_min_age")
        if isinstance(lab_validity_days, bool) or not isinstance(lab_validity_days, int) or not 1 <= lab_validity_days <= 3650:
            raise BusinessError("化验有效期必须是 1~3650 之间的整数天数", 422, "invalid_lab_validity")

    @staticmethod
    def _validate_screening_fields(consent_date=UNSET, age=UNSET, lab_date=UNSET, lab_result=UNSET):
        """归一化筛选登记字段；None 表示该资料缺失，UNSET 表示本次未提供。"""
        out = {}
        if consent_date is not UNSET:
            if consent_date is None:
                out["consent_date"] = None
            else:
                d = _parse_iso_date(consent_date, "知情同意日期")
                if d > today():
                    raise BusinessError("知情同意日期不能晚于今天", 422, "consent_in_future")
                out["consent_date"] = d.isoformat()
        if age is not UNSET:
            if age is None:
                out["age"] = None
            else:
                if isinstance(age, bool) or not isinstance(age, int) or not 0 <= age <= 150:
                    raise BusinessError("年龄必须是 0~150 之间的整数", 422, "invalid_age")
                out["age"] = age
        if lab_date is not UNSET:
            if lab_date is None:
                out["lab_date"] = None
            else:
                d = _parse_iso_date(lab_date, "化验日期")
                if d > today():
                    raise BusinessError("化验日期不能晚于今天", 422, "lab_in_future")
                out["lab_date"] = d.isoformat()
        if lab_result is not UNSET:
            if lab_result is not None and lab_result not in LAB_RESULTS:
                raise BusinessError("化验结果只能是 normal（正常）或 abnormal（异常）", 422, "invalid_lab_result")
            out["lab_result"] = lab_result
        return out

    def create_trial(self, user_id, name, protocol_version, arms, strata_factors, block_size, seed,
                     min_age, lab_validity_days):
        name = name.strip()
        if len(name) < 3 or not protocol_version.strip() or len(seed.strip()) < 8:
            raise BusinessError("试验名称、方案版本和至少 8 位随机种子不能为空", 422, "invalid_trial")
        if not isinstance(arms, list) or len(arms) < 2:
            raise BusinessError("至少需要两个试验组", 422, "invalid_arms")
        arms = [str(a).strip() for a in arms]
        if any(not a or len(a) > MAX_ARM_LENGTH for a in arms) or len(set(arms)) != len(arms):
            raise BusinessError("试验组名称必须非空、唯一且不过长", 422, "invalid_arms")
        if not isinstance(strata_factors, list) or any(not str(x).strip() for x in strata_factors) or len(set(strata_factors)) != len(strata_factors):
            raise BusinessError("分层因素必须是非空且不重复的数组", 422, "invalid_strata")
        if isinstance(block_size, bool) or not isinstance(block_size, int) or block_size < len(arms) or block_size % len(arms) != 0:
            raise BusinessError("区组长度必须为试验组数的正整数倍", 422, "invalid_block_size")
        self._validate_screening_config(min_age, lab_validity_days)
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"coordinator"})
            try:
                cur = conn.execute(
                    """INSERT INTO trials(name,protocol_version,arms_json,strata_factors_json,block_size,seed,created_by,created_at,min_age,lab_validity_days)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (name, protocol_version.strip(), json.dumps(arms), json.dumps([str(x).strip() for x in strata_factors]),
                     block_size, seed.strip(), user_id, now(), min_age, lab_validity_days),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("试验名称已存在", 409, "trial_exists")
            trial_id = cur.lastrowid
            self._audit(conn, trial_id, user_id, "trial.create",
                        {"protocol_version": protocol_version, "arms": len(arms), "block_size": block_size,
                         "min_age": min_age, "lab_validity_days": lab_validity_days})
            return {"id": trial_id, "name": name, "status": "draft", "arms": arms,
                    "strata_factors": strata_factors, "block_size": block_size,
                    "min_age": min_age, "lab_validity_days": lab_validity_days}

    def update_protocol(self, user_id, trial_id, protocol_version, arms=None, strata_factors=None,
                        block_size=None, seed=None, min_age=UNSET, lab_validity_days=UNSET):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"coordinator"})
            trial = self._trial(conn, trial_id)
            enrolled = conn.execute("SELECT COUNT(*) FROM participants WHERE trial_id=?", (trial_id,)).fetchone()[0]
            if enrolled or trial["status"] != "draft":
                raise BusinessError("入组开始后不能修改随机方案", 409, "protocol_locked")
            new_arms = arms if arms is not None else json.loads(trial["arms_json"])
            new_strata = strata_factors if strata_factors is not None else json.loads(trial["strata_factors_json"])
            new_block = block_size if block_size is not None else trial["block_size"]
            new_seed = str(seed) if seed is not None else trial["seed"]
            new_min_age = trial["min_age"] if min_age is UNSET else min_age
            new_lab_validity = trial["lab_validity_days"] if lab_validity_days is UNSET else lab_validity_days
            self.create_trial_validation_only(new_arms, new_strata, new_block, new_seed)
            self._validate_screening_config(new_min_age, new_lab_validity)
            conn.execute(
                """UPDATE trials SET protocol_version=?,arms_json=?,strata_factors_json=?,block_size=?,seed=?,min_age=?,lab_validity_days=? WHERE id=?""",
                (protocol_version.strip(), json.dumps(new_arms), json.dumps(new_strata), new_block, new_seed,
                 new_min_age, new_lab_validity, trial_id),
            )
            self._audit(conn, trial_id, user_id, "protocol.update",
                        {"protocol_version": protocol_version, "min_age": new_min_age,
                         "lab_validity_days": new_lab_validity})
            return {"id": trial_id, "protocol_version": protocol_version, "arms": new_arms,
                    "block_size": new_block, "min_age": new_min_age, "lab_validity_days": new_lab_validity}

    @staticmethod
    def create_trial_validation_only(arms, strata_factors, block_size, seed):
        if not isinstance(arms, list) or len(arms) < 2 or len(set(arms)) != len(arms):
            raise BusinessError("试验组配置无效", 422, "invalid_arms")
        if not isinstance(strata_factors, list) or not strata_factors or len(set(strata_factors)) != len(strata_factors):
            raise BusinessError("分层因素配置无效", 422, "invalid_strata")
        if isinstance(block_size, bool) or not isinstance(block_size, int) or block_size < len(arms) or block_size % len(arms):
            raise BusinessError("区组长度无效", 422, "invalid_block_size")
        if len(str(seed)) < 8:
            raise BusinessError("随机种子至少 8 位", 422, "invalid_seed")

    def start_trial(self, user_id, trial_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"coordinator"})
            trial = self._trial(conn, trial_id)
            if trial["status"] != "draft":
                raise BusinessError("只有草稿试验可以开始", 409, "invalid_status")
            conn.execute("UPDATE trials SET status='running',started_at=? WHERE id=?", (now(), trial_id))
            self._audit(conn, trial_id, user_id, "trial.start", {})
            return {"id": trial_id, "status": "running"}

    def get_trial(self, user_id, trial_id):
        with self.connect() as conn:
            self._user(conn, user_id)
            trial = self._trial(conn, trial_id)
            return {
                "id": trial["id"], "name": trial["name"], "protocol_version": trial["protocol_version"],
                "status": trial["status"], "arms": json.loads(trial["arms_json"]),
                "strata_factors": json.loads(trial["strata_factors_json"]),
                "block_size": trial["block_size"], "min_age": trial["min_age"],
                "lab_validity_days": trial["lab_validity_days"],
            }

    def _stratum(self, conn, trial, factors, site_id):
        expected = json.loads(trial["strata_factors_json"])
        if set(factors) != set(expected):
            raise BusinessError(f"必须提供分层因素: {', '.join(expected)}", 422, "invalid_factors")
        normalized = {k: str(factors[k]).strip() for k in sorted(expected)}
        if any(not v for v in normalized.values()):
            raise BusinessError("分层因素值不能为空", 422, "invalid_factors")
        key = f"{site_id}|" + json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        row = conn.execute("SELECT * FROM strata WHERE trial_id=? AND stratum_key=?", (trial["id"], key)).fetchone()
        if row:
            return row
        cur = conn.execute(
            "INSERT INTO strata(trial_id,stratum_key,factors_json,created_at) VALUES(?,?,?,?)",
            (trial["id"], key, json.dumps({"site_id": site_id, **normalized}, ensure_ascii=False, sort_keys=True), now()),
        )
        return conn.execute("SELECT * FROM strata WHERE id=?", (cur.lastrowid,)).fetchone()

    def _next_allocation(self, conn, trial, stratum):
        for block_no in range(1, 101):
            count = conn.execute(
                "SELECT COUNT(*) FROM allocations WHERE stratum_id=? AND block_no=?", (stratum["id"], block_no)
            ).fetchone()[0]
            if count == 0:
                rng = random.Random(f"{trial['seed']}:{stratum['stratum_key']}:{block_no}")
                arms = json.loads(trial["arms_json"])
                plan = []
                blocks = len(arms) if trial["block_size"] > len(arms) else 1
                for _ in range(blocks * (trial["block_size"] // len(arms))):
                    plan.extend(arms)
                rng.shuffle(plan)
                start = conn.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM allocations WHERE stratum_id=?", (stratum["id"],)
                ).fetchone()[0]
                for offset, arm in enumerate(plan, 1):
                    conn.execute(
                        "INSERT INTO allocations(trial_id,stratum_id,sequence,block_no,arm) VALUES(?,?,?,?,?)",
                        (trial["id"], stratum["id"], start + offset, block_no, arm),
                    )
            free = conn.execute(
                "SELECT * FROM allocations WHERE stratum_id=? AND used_by IS NULL ORDER BY sequence LIMIT 1", (stratum["id"],)
            ).fetchone()
            if free:
                return free
        raise BusinessError("随机分配表已耗尽，请由统计人员扩展方案", 409, "allocation_exhausted")

    # ---------- 入组前筛选 ----------

    def _latest_screening(self, conn, trial_id, site_id, external_id):
        return conn.execute(
            "SELECT * FROM screening_records WHERE trial_id=? AND site_id=? AND external_id=? ORDER BY attempt DESC LIMIT 1",
            (trial_id, site_id, external_id),
        ).fetchone()

    def _load_screening(self, conn, screening_id, actor):
        row = conn.execute("SELECT * FROM screening_records WHERE id=?", (screening_id,)).fetchone()
        if not row:
            raise BusinessError("筛选记录不存在", 404, "not_found")
        if actor["role"] == "site" and row["site_id"] != actor["site_id"]:
            raise BusinessError("筛选记录按中心隔离，不能操作其他中心的受试者", 403, "site_isolation")
        return row

    def _compute_judgment(self, conn, trial, row):
        """按当前资料和当天日期重新判定，返回 (status, reasons)；不落库。已入组记录冻结。"""
        if row["status"] == "enrolled":
            return row["status"], json.loads(row["reasons_json"])
        return _judge_screening(
            trial, row["consent_date"], row["age"], row["lab_date"], row["lab_result"], today()
        )

    def _refresh_judgment(self, conn, trial, row, actor_id=None):
        """写路径专用：状态/原因变化时落库并以操作者身份留痕。已入组记录冻结。"""
        status, reasons = self._compute_judgment(conn, trial, row)
        old_reasons = json.loads(row["reasons_json"])
        if status != row["status"] or reasons != old_reasons:
            conn.execute(
                "UPDATE screening_records SET status=?,reasons_json=?,judged_at=?,updated_at=? WHERE id=?",
                (status, json.dumps(reasons, ensure_ascii=False), now(), now(), row["id"]),
            )
            self._audit(conn, trial["id"], actor_id or row["created_by"], "screening.judge",
                        {"screening_id": row["id"], "external_id": row["external_id"], "attempt": row["attempt"],
                         "from": row["status"], "to": status, "reasons": [r["code"] for r in reasons]})
            row = conn.execute("SELECT * FROM screening_records WHERE id=?", (row["id"],)).fetchone()
        return row

    def _screening_dict(self, conn, row, trial=None):
        # 读路径：用最新日期瞬时重算（如化验刚好过期），但不写库、不留痕
        if trial is None:
            trial = self._trial(conn, row["trial_id"])
        status, reasons = self._compute_judgment(conn, trial, row)
        return {
            "id": row["id"], "trial_id": row["trial_id"], "site_id": row["site_id"],
            "external_id": row["external_id"], "attempt": row["attempt"],
            "consent_date": row["consent_date"], "age": row["age"],
            "lab_date": row["lab_date"], "lab_result": row["lab_result"],
            "status": status, "reasons": reasons,
            "can_enroll": status == "eligible",
            "participant_id": row["participant_id"],
            "created_at": row["created_at"], "updated_at": row["updated_at"], "judged_at": row["judged_at"],
        }

    def create_screening(self, user_id, trial_id, external_id, fields):
        external_id = str(external_id or "").strip()
        if not external_id:
            raise BusinessError("受试者筛选编号不能为空", 422, "invalid_external_id")
        clean = self._validate_screening_fields(
            consent_date=fields.get("consent_date", UNSET),
            age=fields.get("age", UNSET),
            lab_date=fields.get("lab_date", UNSET),
            lab_result=fields.get("lab_result", UNSET),
        )
        consent_date = clean.get("consent_date")
        age = clean.get("age")
        lab_date = clean.get("lab_date")
        lab_result = clean.get("lab_result")
        if lab_result is not None and not lab_date:
            raise BusinessError("登记化验结果时必须同时提供化验日期", 422, "lab_date_required")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site"})
            trial = self._trial(conn, trial_id)
            if trial["status"] != "running":
                raise BusinessError("试验尚未开始或已经停止，不能登记筛选", 409, "trial_not_running")
            latest = self._latest_screening(conn, trial_id, actor["site_id"], external_id)
            if latest is not None:
                if latest["status"] == "enrolled":
                    raise BusinessError("该受试者已入组，不能重复筛选", 409, "already_enrolled")
                raise BusinessError(
                    "该受试者已有筛选记录，筛败后请使用重筛发起新一轮筛选",
                    409, "screening_exists",
                    details={"latest_screening_id": latest["id"], "status": latest["status"], "attempt": latest["attempt"]},
                )
            status, reasons = _judge_screening(trial, consent_date, age, lab_date, lab_result, today())
            ts = now()
            cur = conn.execute(
                """INSERT INTO screening_records(trial_id,site_id,external_id,attempt,consent_date,age,lab_date,lab_result,status,reasons_json,judged_at,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (trial_id, actor["site_id"], external_id, 1, consent_date, age, lab_date, lab_result,
                 status, json.dumps(reasons, ensure_ascii=False), ts, user_id, ts, ts),
            )
            self._audit(conn, trial_id, user_id, "screening.create",
                        {"screening_id": cur.lastrowid, "external_id": external_id, "attempt": 1,
                         "status": status, "reasons": [r["code"] for r in reasons]})
            row = conn.execute("SELECT * FROM screening_records WHERE id=?", (cur.lastrowid,)).fetchone()
            return self._screening_dict(conn, row, trial)

    def update_screening(self, user_id, screening_id, fields):
        clean = self._validate_screening_fields(**fields)
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site"})
            row = self._load_screening(conn, screening_id, actor)
            trial = self._trial(conn, row["trial_id"])
            latest = self._latest_screening(conn, row["trial_id"], row["site_id"], row["external_id"])
            if latest["id"] != row["id"]:
                raise BusinessError("该轮筛选已结束，历史记录不可修改，请在最新一轮筛选上登记", 409, "screening_locked")
            if row["status"] == "enrolled":
                raise BusinessError("受试者已入组，筛选记录冻结", 409, "already_enrolled")
            new_values = {
                "consent_date": clean["consent_date"] if "consent_date" in clean else row["consent_date"],
                "age": clean["age"] if "age" in clean else row["age"],
                "lab_date": clean["lab_date"] if "lab_date" in clean else row["lab_date"],
                "lab_result": clean["lab_result"] if "lab_result" in clean else row["lab_result"],
            }
            if new_values["lab_result"] is not None and not new_values["lab_date"]:
                raise BusinessError("登记化验结果时必须同时提供化验日期", 422, "lab_date_required")
            status, reasons = _judge_screening(
                trial, new_values["consent_date"], new_values["age"],
                new_values["lab_date"], new_values["lab_result"], today(),
            )
            conn.execute(
                "UPDATE screening_records SET consent_date=?,age=?,lab_date=?,lab_result=?,status=?,reasons_json=?,judged_at=?,updated_at=? WHERE id=?",
                (new_values["consent_date"], new_values["age"], new_values["lab_date"], new_values["lab_result"],
                 status, json.dumps(reasons, ensure_ascii=False), now(), now(), screening_id),
            )
            self._audit(conn, row["trial_id"], user_id, "screening.update",
                        {"screening_id": screening_id, "external_id": row["external_id"], "attempt": row["attempt"],
                         "from": row["status"], "to": status, "reasons": [r["code"] for r in reasons],
                         "changed": sorted(clean.keys())})
            row = conn.execute("SELECT * FROM screening_records WHERE id=?", (screening_id,)).fetchone()
            return self._screening_dict(conn, row, trial)

    def rescreen_screening(self, user_id, screening_id):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site"})
            row = self._load_screening(conn, screening_id, actor)
            latest = self._latest_screening(conn, row["trial_id"], row["site_id"], row["external_id"])
            if latest["id"] != row["id"]:
                raise BusinessError("只能基于最新一轮筛选发起重筛", 409, "screening_locked")
            if row["status"] == "enrolled":
                raise BusinessError("受试者已入组，不能重筛", 409, "already_enrolled")
            row = self._refresh_judgment(conn, self._trial(conn, row["trial_id"]), row, user_id)
            if row["status"] != "failed":
                raise BusinessError("只有筛败（不合格）的受试者可以发起重筛", 409, "rescreen_not_allowed",
                                    details={"status": row["status"]})
            trial = self._trial(conn, row["trial_id"])
            attempt = row["attempt"] + 1
            # 重筛：保留受试者身份与既往同意/年龄信息，关键化验必须重新完成
            consent_date, age, lab_date, lab_result = row["consent_date"], row["age"], None, None
            status, reasons = _judge_screening(trial, consent_date, age, lab_date, lab_result, today())
            ts = now()
            cur = conn.execute(
                """INSERT INTO screening_records(trial_id,site_id,external_id,attempt,consent_date,age,lab_date,lab_result,status,reasons_json,judged_at,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["trial_id"], row["site_id"], row["external_id"], attempt, consent_date, age,
                 lab_date, lab_result, status, json.dumps(reasons, ensure_ascii=False), ts, user_id, ts, ts),
            )
            self._audit(conn, row["trial_id"], user_id, "screening.rescreen",
                        {"screening_id": cur.lastrowid, "external_id": row["external_id"],
                         "attempt": attempt, "previous_screening_id": row["id"],
                         "status": status, "reasons": [r["code"] for r in reasons]})
            return self._screening_dict(
                conn, conn.execute("SELECT * FROM screening_records WHERE id=?", (cur.lastrowid,)).fetchone(), trial
            )

    def list_screenings(self, user_id, trial_id):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            trial = self._trial(conn, trial_id)
            if actor["role"] == "site":
                rows = conn.execute(
                    "SELECT * FROM screening_records WHERE trial_id=? AND site_id=? ORDER BY external_id,attempt",
                    (trial_id, actor["site_id"]),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM screening_records WHERE trial_id=? ORDER BY site_id,external_id,attempt",
                    (trial_id,),
                ).fetchall()
            return [self._screening_dict(conn, r, trial) for r in rows]

    def get_screening(self, user_id, screening_id):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            row = self._load_screening(conn, screening_id, actor)
            return self._screening_dict(conn, row)

    def enroll(self, user_id, trial_id, screening_id, factors):
        """凭本中心当前合格（且化验未过期）的筛选记录入组，不合格/待复核一律不发随机号。"""
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                trial = self._trial(conn, trial_id)
                if trial["status"] != "running":
                    raise BusinessError("试验尚未开始或已经停止", 409, "trial_not_running")
                screening = conn.execute("SELECT * FROM screening_records WHERE id=?", (screening_id,)).fetchone()
                if not screening:
                    raise BusinessError("筛选记录不存在", 404, "screening_not_found")
                if screening["trial_id"] != trial_id or screening["site_id"] != actor["site_id"]:
                    raise BusinessError("不能凭其他中心或其他试验的筛选记录入组", 403, "site_isolation")
                if screening["participant_id"] is not None:
                    participant = conn.execute(
                        "SELECT * FROM participants WHERE id=?", (screening["participant_id"],)
                    ).fetchone()
                    conn.commit()
                    return self._blinded_participant(conn, participant, actor, allow_arm=False, idempotent=True)
                screening = self._refresh_judgment(conn, trial, screening, user_id)
                if screening["status"] != "eligible":
                    reasons = json.loads(screening["reasons_json"])
                    raise BusinessError(
                        "筛选未通过，系统不发放随机号：" + "；".join(r["message"] for r in reasons),
                        409, "screening_not_eligible",
                        details={"screening_id": screening["id"], "status": screening["status"], "reasons": reasons},
                    )
                external_id = screening["external_id"]
                stratum = self._stratum(conn, trial, factors, actor["site_id"])
                allocation = self._next_allocation(conn, trial, stratum)
                allocation_code = hashlib.sha256(f"{trial_id}:{external_id}".encode()).hexdigest()[:12].upper()
                cur = conn.execute(
                    """INSERT INTO participants(trial_id,site_id,external_id,stratum_id,allocation_id,allocation_code,enrolled_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (trial_id, actor["site_id"], external_id, stratum["id"], allocation["id"], allocation_code, user_id, now()),
                )
                participant_id = cur.lastrowid
                conn.execute("UPDATE allocations SET used_by=?,used_at=? WHERE id=?", (participant_id, now(), allocation["id"]))
                conn.execute(
                    "UPDATE screening_records SET status='enrolled',participant_id=?,judged_at=?,updated_at=? WHERE id=?",
                    (participant_id, now(), now(), screening["id"]),
                )
                self._audit(conn, trial_id, user_id, "participant.enroll",
                            {"participant_id": participant_id, "external_id": external_id,
                             "allocation_id": allocation["id"], "site_id": actor["site_id"],
                             "screening_id": screening["id"]})
                participant = conn.execute("SELECT * FROM participants WHERE id=?", (participant_id,)).fetchone()
                return self._blinded_participant(conn, participant, actor, allow_arm=False, idempotent=False)
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise BusinessError(f"并发入组冲突，请重新提交（{exc}）", 409, "enrollment_conflict")
            except Exception:
                conn.rollback()
                raise

    def _blinded_participant(self, conn, participant, viewer, allow_arm=False, idempotent=False):
        result = {
            "id": participant["id"], "trial_id": participant["trial_id"],
            "external_id": participant["external_id"], "site_id": participant["site_id"],
            "allocation_code": participant["allocation_code"], "status": participant["status"],
            "created_at": participant["created_at"], "idempotent": idempotent,
        }
        if allow_arm:
            result["arm"] = conn.execute("SELECT arm FROM allocations WHERE id=?", (participant["allocation_id"],)).fetchone()["arm"]
        return result

    def list_participants(self, user_id, trial_id):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            self._trial(conn, trial_id)
            if actor["role"] == "site":
                rows = conn.execute("SELECT * FROM participants WHERE trial_id=? AND site_id=? ORDER BY id", (trial_id, actor["site_id"])).fetchall()
            else:
                rows = conn.execute("SELECT * FROM participants WHERE trial_id=? ORDER BY id", (trial_id,)).fetchall()
            return [self._blinded_participant(conn, row, actor) for row in rows]

    def get_participant(self, user_id, participant_id):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            row = conn.execute("SELECT * FROM participants WHERE id=?", (participant_id,)).fetchone()
            if not row:
                raise BusinessError("受试者不存在", 404, "not_found")
            if actor["role"] == "site" and row["site_id"] != actor["site_id"]:
                raise BusinessError("只能查看本中心受试者", 403, "site_isolation")
            approved = conn.execute(
                "SELECT 1 FROM unblinding_requests WHERE participant_id=? AND status='approved'", (participant_id,)
            ).fetchone() is not None
            return self._blinded_participant(conn, row, actor, allow_arm=approved)

    def request_unblinding(self, user_id, participant_id, reason):
        if len(reason.strip()) < 8:
            raise BusinessError("揭盲原因至少 8 字", 422, "reason_required")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator"})
            participant = conn.execute("SELECT * FROM participants WHERE id=?", (participant_id,)).fetchone()
            if not participant:
                raise BusinessError("受试者不存在", 404, "not_found")
            if actor["role"] == "site" and participant["site_id"] != actor["site_id"]:
                raise BusinessError("不能申请其他中心的揭盲", 403, "site_isolation")
            open_request = conn.execute(
                "SELECT id FROM unblinding_requests WHERE participant_id=? AND status='pending'", (participant_id,)
            ).fetchone()
            if open_request:
                raise BusinessError("该受试者已有待审批的揭盲申请", 409, "request_exists")
            cur = conn.execute(
                "INSERT INTO unblinding_requests(participant_id,requester_id,reason,created_at) VALUES(?,?,?,?)",
                (participant_id, user_id, reason.strip(), now()),
            )
            self._audit(conn, participant["trial_id"], user_id, "unblinding.request", {"request_id": cur.lastrowid, "participant_id": participant_id})
            return {"id": cur.lastrowid, "status": "pending"}

    def approve_unblinding(self, user_id, request_id):
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                approver = self._user(conn, user_id, {"monitor", "coordinator"})
                request = conn.execute("SELECT * FROM unblinding_requests WHERE id=?", (request_id,)).fetchone()
                if not request:
                    raise BusinessError("揭盲申请不存在", 404, "not_found")
                if request["status"] != "pending":
                    raise BusinessError("揭盲申请已经完成", 409, "already_decided")
                if request["first_approver"] is None:
                    conn.execute("UPDATE unblinding_requests SET first_approver=? WHERE id=?", (user_id, request_id))
                    participant = conn.execute("SELECT * FROM participants WHERE id=?", (request["participant_id"],)).fetchone()
                    self._audit(conn, participant["trial_id"], user_id, "unblinding.approve.first", {"request_id": request_id})
                    return {"id": request_id, "status": "pending", "first_approver": user_id, "second_approval_required": True}
                if request["first_approver"] == user_id:
                    raise BusinessError("两次揭盲审批必须由不同人员完成", 409, "distinct_approver_required")
                conn.execute(
                    "UPDATE unblinding_requests SET second_approver=?,status='approved',decided_at=? WHERE id=?",
                    (user_id, now(), request_id),
                )
                participant = conn.execute("SELECT * FROM participants WHERE id=?", (request["participant_id"],)).fetchone()
                arm = conn.execute("SELECT arm FROM allocations WHERE id=?", (participant["allocation_id"],)).fetchone()["arm"]
                self._audit(conn, participant["trial_id"], user_id, "unblinding.approve.second", {"request_id": request_id, "participant_id": participant["id"]})
                return {"id": request_id, "status": "approved", "first_approver": request["first_approver"], "second_approver": user_id, "arm": arm}
            except Exception:
                conn.rollback()
                raise

    def trial_summary(self, user_id, trial_id):
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"site", "coordinator", "monitor"})
            trial = self._trial(conn, trial_id)
            where, params = "", [trial_id]
            if actor["role"] == "site":
                where, params = " AND site_id=?", [trial_id, actor["site_id"]]
            total = conn.execute(f"SELECT COUNT(*) FROM participants WHERE trial_id=?" + where, params).fetchone()[0]
            by_site = conn.execute(
                f"SELECT site_id,COUNT(*) AS count FROM participants WHERE trial_id=?" + where + " GROUP BY site_id", params
            ).fetchall()
            audit = conn.execute("SELECT * FROM audit_log WHERE trial_id=? ORDER BY id", (trial_id,)).fetchall()
            return {
                "trial": {"id": trial["id"], "name": trial["name"], "protocol_version": trial["protocol_version"], "status": trial["status"]},
                "participants_visible": total, "by_site": [dict(x) for x in by_site],
                "audit": [dict(x) | {"detail": json.loads(x["detail"])} for x in audit],
            }


class Handler(BaseHTTPRequestHandler):
    server_version = "Randomization/1.0"
    def _store(self): return self.server.store  # type: ignore[attr-defined]
    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try: data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError): raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict): raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data
    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _screening_fields(self, d):
        """从请求体挑出筛选字段，缺省视为未提供（PATCH 语义）。"""
        return {k: d[k] for k in ("consent_date", "age", "lab_date", "lab_result") if k in d}
    def _dispatch(self, method):
        path = urlparse(self.path).path.rstrip("/") or "/"; parts = [p for p in path.split("/") if p]
        user = self.headers.get("X-User-Id", ""); store = self._store()
        if method == "GET" and path == "/":
            body = (BASE_DIR / "web" / "index.html").read_bytes(); self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if method == "GET" and path == "/health": return self._send(200, {"ok": True})
        if parts == ["api", "trials"] and method == "POST":
            d=self._body()
            return self._send(201, store.create_trial(user,d.get("name",""),d.get("protocol_version",""),d.get("arms"),
                d.get("strata_factors"),d.get("block_size"),d.get("seed",""),d.get("min_age"),d.get("lab_validity_days")))
        if len(parts) >= 3 and parts[:2] == ["api", "trials"]:
            trial_id=int(parts[2])
            if len(parts)==3 and method=="GET": return self._send(200, store.get_trial(user,trial_id))
            if len(parts)==4 and parts[3]=="protocol" and method=="POST":
                d=self._body()
                return self._send(200, store.update_protocol(user,trial_id,d.get("protocol_version",""),d.get("arms"),
                    d.get("strata_factors"),d.get("block_size"),d.get("seed"),
                    min_age=d.get("min_age", UNSET), lab_validity_days=d.get("lab_validity_days", UNSET)))
            if len(parts)==4 and parts[3]=="start" and method=="POST": return self._send(200, store.start_trial(user,trial_id))
            if len(parts)==4 and parts[3]=="participants" and method=="GET": return self._send(200, {"items": store.list_participants(user,trial_id)})
            if len(parts)==4 and parts[3]=="screenings" and method=="GET":
                return self._send(200, {"items": store.list_screenings(user,trial_id)})
            if len(parts)==4 and parts[3]=="screenings" and method=="POST":
                d=self._body()
                return self._send(201, store.create_screening(user,trial_id,d.get("external_id",""),self._screening_fields(d)))
            if len(parts)==4 and parts[3]=="enroll" and method=="POST":
                d=self._body()
                return self._send(201, store.enroll(user,trial_id,d.get("screening_id"),d.get("factors",{})))
            if len(parts)==4 and parts[3]=="summary" and method=="GET": return self._send(200, store.trial_summary(user,trial_id))
        if len(parts)>=3 and parts[:2]==["api","screenings"]:
            screening_id=int(parts[2])
            if len(parts)==3 and method=="GET": return self._send(200, store.get_screening(user,screening_id))
            if len(parts)==3 and method=="PATCH":
                d=self._body(); return self._send(200, store.update_screening(user,screening_id,self._screening_fields(d)))
            if len(parts)==4 and parts[3]=="rescreen" and method=="POST":
                return self._send(201, store.rescreen_screening(user,screening_id))
        if len(parts)==3 and parts[:2]==["api","participants"] and method=="GET": return self._send(200, store.get_participant(user,int(parts[2])))
        if len(parts)==4 and parts[:2]==["api","participants"] and parts[3]=="unblinding-requests" and method=="POST":
            d=self._body(); return self._send(201, store.request_unblinding(user,int(parts[2]),d.get("reason","")))
        if len(parts)==4 and parts[:2]==["api","unblinding-requests"] and parts[3]=="approve" and method=="POST":
            return self._send(200, store.approve_unblinding(user,int(parts[2])))
        raise BusinessError("接口不存在",404,"not_found")
    def _handle(self, method):
        try: self._dispatch(method)
        except BusinessError as exc:
            error={"code":exc.code,"message":exc.message}
            if exc.details: error.update(exc.details)
            self._send(exc.status,{"error":error})
        except (ValueError,TypeError): self._send(400,{"error":{"code":"invalid_path","message":"路径参数格式错误"}})
        except Exception as exc: self._send(500,{"error":{"code":"internal_error","message":str(exc)}})
    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PATCH(self): self._handle("PATCH")
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class RandomizationServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store): self.store=store; super().__init__(address,Handler)


def main():
    parser=argparse.ArgumentParser(description="临床试验随机分配与盲法服务")
    parser.add_argument("--db",default=str(DEFAULT_DB)); parser.add_argument("--port",type=int,default=8104)
    parser.add_argument("--init",action="store_true"); parser.add_argument("--seed",action="store_true")
    args=parser.parse_args(); store=RandomizationStore(args.db); store.init_schema()
    if args.seed: store.seed()
    if args.init or args.seed: print(f"数据库已初始化: {args.db}"); return
    server=RandomizationServer(("127.0.0.1",args.port),store); print(f"随机化服务运行于 http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__=="__main__": main()
