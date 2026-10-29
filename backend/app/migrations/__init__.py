"""轻量数据库迁移机制（M2 引入）。

为什么不用 Alembic：
- M1 只有 `Base.metadata.create_all()`，对已有表加列这件事它做不了；
- M2 需要给 M1 已存在的表加列，且**不能破坏 M1 数据**；
- 引入 Alembic 意味着新增依赖 + 维护 versions/ 目录，而本项目的迁移需求很线性
  （只增表、只加列，不改列类型、不删列），一个带版本表的幂等迁移器就够。

机制：
- `schema_migrations` 表记录已执行的版本；
- 每个迁移是一个幂等函数，自己判断「表和列是不是已经存在」；
- SQLite 与 PostgreSQL 通用（列类型按方言取，DDL 都是标准 ADD COLUMN）；
- 版本已执行则跳过，因此重复运行安全。

命令行：
    python -m backend.app.migrations          # 执行迁移并打印结果
    python -m backend.app.migrations --status  # 只看状态
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger("testflow.migrations")

# ------------------------------------------------------------------ 工具函数


def _table_exists(conn: Connection, table: str) -> bool:
    rows = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name")
        if conn.dialect.name == "sqlite"
        else text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename=:name"),
        {"name": table},
    ).fetchall()
    return bool(rows)


def _column_exists(conn: Connection, table: str, column: str) -> bool:
    if not _table_exists(conn, table):
        return False
    if conn.dialect.name == "sqlite":
        rows = conn.execute(text(f'PRAGMA table_info("{table}")')).fetchall()
        return any(str(row[1]) == column for row in rows)
    rows = conn.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name=:t AND column_name=:c"),
        {"t": table, "c": column},
    ).fetchall()
    return bool(rows)


def _type_for(conn: Connection, kind: str, length: int | None = None) -> str:
    sqlite = conn.dialect.name == "sqlite"
    if kind == "bool":
        return "BOOLEAN"
    if kind == "int":
        return "INTEGER"
    if kind == "text":
        return "TEXT"
    if kind == "json":
        return "TEXT" if sqlite else "JSON"
    if kind == "datetime":
        return "DATETIME" if sqlite else "TIMESTAMP WITH TIME ZONE"
    if kind == "string":
        return f"VARCHAR({length or 255})"
    raise ValueError(f"未知列类型：{kind}")


def add_column(
    conn: Connection,
    table: str,
    column: str,
    kind: str,
    *,
    length: int | None = None,
    default: Any = None,
    not_null: bool = False,
) -> bool:
    """幂等加列。返回 True 表示真的执行了 DDL。"""
    if not _table_exists(conn, table):
        logger.warning("表 %s 不存在，跳过加列 %s", table, column)
        return False
    if _column_exists(conn, table, column):
        return False
    ddl = f'ALTER TABLE "{table}" ADD COLUMN "{column}" {_type_for(conn, kind, length)}'
    if default is not None:
        literal = "0" if default is False else ("1" if default is True else f"'{default}'")
        ddl += f" DEFAULT {literal}"
    if not_null:
        ddl += " NOT NULL"
    conn.execute(text(ddl))
    logger.info("已加列：%s.%s", table, column)
    return True


def _index_exists(conn: Connection, name: str) -> bool:
    sqlite = conn.dialect.name == "sqlite"
    if sqlite:
        row = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'index' AND name = :name"), {"name": name}
        ).first()
        return row is not None
    row = conn.execute(
        text("SELECT indexname FROM pg_indexes WHERE indexname = :name"), {"name": name}
    ).first()
    return row is not None


def _create_index(
    conn: Connection,
    name: str,
    table: str,
    columns: tuple[str, ...],
    *,
    unique: bool = False,
    where: str | None = None,
) -> bool:
    """幂等建索引（支持唯一索引与部分索引）。

    先查再建，既能区分日志里的「已存在」和「刚创建」，
    也让「create_all 已建过、迁移再跑一次」这种情况安全通过。
    """
    if not _table_exists(conn, table):
        logger.warning("表 %s 不存在，跳过建索引 %s", table, name)
        return False
    if _index_exists(conn, name):
        return False
    joined = ", ".join(f'"{column}"' for column in columns)
    prefix = "CREATE UNIQUE INDEX" if unique else "CREATE INDEX"
    ddl = f'{prefix} "{name}" ON "{table}" ({joined})'
    if where:
        ddl += f" WHERE {where}"
    conn.execute(text(ddl))
    logger.info("已建索引：%s(%s)%s", name, joined, f" WHERE {where}" if where else "")
    return True


# ------------------------------------------------------------------ 迁移定义


def migration_001_baseline(conn: Connection) -> None:
    """基线：M1 的 12 张表由 SQLAlchemy create_all 负责，这里只做版本登记。"""
    return None


def migration_002_execution_center(conn: Connection) -> None:
    """M2 执行中心：

    - 新表 test_suites / test_suite_cases / case_result_attempts 由 create_all 建；
    - 给 M1 已有表补列。全部幂等。
    """
    # 套件：计划优先引用 suite_id，旧 case_ids 保留
    add_column(conn, "regression_plans", "suite_id", "string", length=32)

    # Run：Flaky/取消计数、失败重跑来源、停止信号
    add_column(conn, "execution_runs", "flaky_cases", "int", default=0, not_null=True)
    add_column(conn, "execution_runs", "cancelled_cases", "int", default=0, not_null=True)
    add_column(conn, "execution_runs", "source_run_id", "string", length=32)
    add_column(conn, "execution_runs", "case_ids", "json")
    add_column(conn, "execution_runs", "rerun_mode", "string", length=16, default="")
    add_column(conn, "execution_runs", "cancel_requested", "bool", default=False, not_null=True)
    add_column(conn, "execution_runs", "cancelled_at", "datetime")

    # CaseResult：attempt 次数与 Flaky 标记
    add_column(conn, "case_results", "attempts_count", "int", default=1, not_null=True)
    add_column(conn, "case_results", "flaky", "bool", default=False, not_null=True)

    # 步骤与证据：归属到具体 attempt
    add_column(conn, "step_results", "attempt_id", "string", length=32)
    add_column(conn, "artifacts", "attempt_id", "string", length=32)


def migration_003_backfill_null_defaults(conn: Connection) -> None:
    """修补 002 遗留的空值。

    002 加 `rerun_mode` 时没有给 DDL 默认值，导致 M1 时期的历史行该列是 NULL，
    接口序列化时报 "Input should be a valid string"。这里统一回填成空串。
    以后新增可空字符串列时，记得在 add_column 里带上 default。
    """
    if _column_exists(conn, "execution_runs", "rerun_mode"):
        conn.execute(text("UPDATE execution_runs SET rerun_mode = '' WHERE rerun_mode IS NULL"))


def migration_004_failure_center(conn: Connection) -> None:
    """M3 失败中心：

    - 新表 failure_fingerprints / failure_groups / failure_occurrences 由 create_all 建；
    - 给 step_results 补失败归因需要的现场信号列。
      这些信号本来就在执行器里采集到了（console / network），只是 M2 时没落到步骤上，
      导致 Fingerprint 只能靠 error 文本猜。补上之后归因有据可依。
    """
    add_column(conn, "step_results", "console_summary", "text", default="")
    add_column(conn, "step_results", "network_summary", "text", default="")
    add_column(conn, "step_results", "request_method", "string", length=16, default="")
    add_column(conn, "step_results", "request_url", "string", length=1024, default="")
    add_column(conn, "step_results", "http_status", "int")
    add_column(conn, "step_results", "js_error", "text", default="")


def migration_005_failure_indexes(conn: Connection) -> None:
    """M3 查询索引与防御性回填。

    索引只加在两个真实存在的查询路径上（方案 §55：以实际查询计划为准，不盲目加）：

    - 失败中心列表：`WHERE project_id = ? ORDER BY last_seen_at DESC`
    - 分组详情历史：`WHERE failure_group_id = ? ORDER BY created_at DESC`

    另外回填 004 新增列的 NULL 值。004 的 ALTER TABLE 带了 DEFAULT，
    历史行本该拿到空串；但 `rerun_mode` 那次教训说明「本该」不等于「一定」，
    多花一条 UPDATE 换接口永远不会因为 NULL 报序列化错误。
    """
    for name, table, columns in (
        ("ix_failure_groups_project_last_seen", "failure_groups", ("project_id", "last_seen_at")),
        ("ix_failure_occurrences_group_created", "failure_occurrences", ("failure_group_id", "created_at")),
    ):
        _create_index(conn, name, table, columns)

    for column in ("console_summary", "network_summary", "request_method", "request_url", "js_error"):
        if _column_exists(conn, "step_results", column):
            conn.execute(text(f'UPDATE step_results SET "{column}" = \'\' WHERE "{column}" IS NULL'))


def migration_006_occurrence_category(conn: Connection) -> None:
    """给 failure_occurrences 补上自身分类。

    存量的 occurrence（M3 开发期产生的）按 is_flaky 回填：
    Flaky 的回填 FLAKY，其余回填 UNKNOWN（当时的分类没有落下来，
    宁可标 UNKNOWN 也不能猜成 PRODUCT_BUG —— 方案 §13 的原则）。
    """
    if not add_column(conn, "failure_occurrences", "category", "string", length=32, default="UNKNOWN"):
        return
    conn.execute(text("UPDATE failure_occurrences SET category = 'FLAKY' WHERE is_flaky = 1"))
    conn.execute(text("UPDATE failure_occurrences SET category = 'UNKNOWN' WHERE category IS NULL OR category = ''"))


def migration_007_ci_idempotency(conn: Connection) -> None:
    """M4：CI 触发幂等的部分唯一索引。

    为什么必须是「部分」索引（带 WHERE）：手动执行的 pipeline_id 是空串、
    手动重跑会继承原 Run 的 pipeline_id 但触发来源是 MANUAL —— 如果做成全表唯一，
    这两种正常操作都会被自己的幂等约束挡死。

    这里用 SQL 直建而不是靠 create_all：create_all 只建缺失的表，
    对已存在的表不会补索引，而这套库已经有 M1/M2/M3 的数据了。
    """
    _create_index(
        conn,
        "uq_execution_runs_ci_idempotency",
        "execution_runs",
        ("project_id", "plan_id", "pipeline_id"),
        unique=True,
        where="pipeline_id <> '' AND trigger_source IN ('CI', 'CLI', 'GITLAB')",
    )


def migration_008_base_url_override(conn: Connection) -> None:
    """M4：CLI 的 --base-url 覆盖。

    为什么不改环境配置而是存到 Run 上：改环境会影响别人、也会污染配置本身；
    而一次 CI 执行指向另一个地址是「这一次的事」。覆盖值同样过安全门禁，
    所以它绕不过域名白名单。
    """
    add_column(conn, "execution_runs", "base_url_override", "string", length=255, default="")


def migration_009_ai_alert_notification(conn: Connection) -> None:
    """M5：AI 分析 / 告警 / 通知三张表。

    表本身由 create_all 建（模型已声明）；这里补的是 create_all 对**已存在的表**
   做不到的事，以及部分唯一索引 —— 幂等，可反复执行。

    两个部分唯一索引是 M5 幂等的基石：
    - `uq_alerts_active_group`：同一个失败分组同时只能有一条活动告警，
      解决/忽略后允许再开新的；
    - `uq_notification_alert_content`：同一告警 + 同一内容只保留一行，
      重试是更新这一行而不是插新行，否则重试次数数不清。
    """
    _create_index(conn, "ix_failure_analyses_group_prompt", "failure_analyses", ("failure_group_id", "prompt_version"))
    _create_index(conn, "ix_alerts_group_status", "alerts", ("failure_group_id", "status"))
    _create_index(conn, "ix_notification_logs_alert_status", "notification_logs", ("alert_id", "status"))
    _create_index(
        conn,
        "uq_alerts_active_group",
        "alerts",
        ("failure_group_id",),
        unique=True,
        where="status IN ('OPEN', 'ACKNOWLEDGED', 'FIXING', 'REVERIFY')",
    )


def migration_010_artifact_retention(conn: Connection) -> None:
    """M6-B：Retention 审计表与查询索引。

    `retention_jobs` 表本身由 create_all 建（模型已声明）；这里补的是
    create_all 对**已存在的表**做不到的事，以及按执行时间查询审计记录的索引。
    """
    _create_index(conn, "ix_retention_jobs_executed_at", "retention_jobs", ("executed_at",))


MIGRATIONS: list[tuple[int, str, Callable[[Connection], None]]] = [
    (1, "001_baseline", migration_001_baseline),
    (2, "002_execution_center", migration_002_execution_center),
    (3, "003_backfill_null_defaults", migration_003_backfill_null_defaults),
    (4, "004_failure_center", migration_004_failure_center),
    (5, "005_failure_indexes", migration_005_failure_indexes),
    (6, "006_occurrence_category", migration_006_occurrence_category),
    (7, "007_ci_idempotency", migration_007_ci_idempotency),
    (8, "008_base_url_override", migration_008_base_url_override),
    (9, "009_ai_alert_notification", migration_009_ai_alert_notification),
    (10, "010_artifact_retention", migration_010_artifact_retention),
]

CURRENT_VERSION = max(version for version, _name, _fn in MIGRATIONS)


# ------------------------------------------------------------------ 执行器


def _ensure_version_table(conn: Connection) -> None:
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name VARCHAR(64) NOT NULL,
                applied_at VARCHAR(40) NOT NULL
            )
            """
        )
    )


def applied_versions(engine: Engine) -> dict[int, str]:
    with engine.begin() as conn:
        _ensure_version_table(conn)
        rows = conn.execute(text("SELECT version, name FROM schema_migrations ORDER BY version")).fetchall()
    return {int(row[0]): str(row[1]) for row in rows}


def run_migrations(engine: Engine) -> dict[str, Any]:
    """按版本顺序执行未应用的迁移。可重复调用。"""
    from datetime import datetime, timezone  # noqa: PLC0415

    applied = applied_versions(engine)
    executed: list[str] = []
    skipped: list[str] = []

    for version, name, func in MIGRATIONS:
        if version in applied:
            skipped.append(name)
            continue
        with engine.begin() as conn:
            func(conn)
            conn.execute(
                text("INSERT INTO schema_migrations (version, name, applied_at) VALUES (:v, :n, :t)"),
                {"v": version, "n": name, "t": datetime.now(timezone.utc).isoformat(timespec="seconds")},
            )
        executed.append(name)
        logger.info("迁移已应用：%s", name)

    return {
        "current_version": CURRENT_VERSION,
        "applied_now": executed,
        "already_applied": skipped,
        "applied_total": sorted(set(applied) | {v for v, _n, _f in MIGRATIONS if _n in executed}),
    }


def _main() -> int:
    import argparse  # noqa: PLC0415
    import json  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="TestFlow CI 数据库迁移")
    parser.add_argument("--status", action="store_true", help="只查看状态，不执行")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from ..db import engine  # noqa: PLC0415

    if args.status:
        print(json.dumps({"applied": applied_versions(engine), "current_version": CURRENT_VERSION}, ensure_ascii=False, indent=2))
        return 0

    result = run_migrations(engine)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
