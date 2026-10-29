from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_env_file(path: Path) -> None:
    """Minimal .env reader. Existing environment variables always win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_env_file(PROJECT_ROOT / ".env")


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    database_url: str
    executor_mode: str
    celery_broker_url: str
    celery_result_backend: str
    storage_dir: Path
    playwright_browsers_path: str
    playwright_headless: bool
    playwright_record_video: bool
    playwright_record_har: bool
    secret: str
    token_ttl_minutes: int
    bootstrap_admin_username: str
    bootstrap_admin_password: str
    ai_base_url: str
    ai_api_key: str
    ai_model: str
    # M5：AI Provider 选择与调用约束。全部有默认值 —— 不配就等于「AI 功能关闭」，
    # 而不是报错，因为绝大多数执行根本用不到 AI。
    ai_provider: str
    ai_timeout: float
    ai_max_tokens: int
    ai_max_retries: int
    # 需要 AI 介入的规则白名单（逗号分隔的 rule_id）。
    # 默认空 = 只有 UNKNOWN 会进 AI —— 方案 §6 的核心要求。
    ai_required_rules: tuple[str, ...]
    # 是否在 Run 结束后自动分析（AI 未配置时该开关无效，自动跳过）
    ai_auto_analyze: bool
    wecom_webhook_url: str
    # 群机器人的 key 在 URL 里；这个额外 secret 用于自建应用形态，且必须从日志里脱敏
    wecom_webhook_secret: str
    # 是否在告警产生时自动发通知
    notification_enabled: bool
    notification_max_attempts: int
    # M6-A：Artifact 签名 URL 的有效期。浏览器 <img>/<video>/<a> 不带 Bearer，
    # 只能用短时 HMAC 签名替代；到期后必须重新向 API 取 URL。
    artifact_url_ttl_seconds: int
    # M6-B：全局 Artifact 保留天数。Retention 第一版只做人工 Preview / Apply，
    # 不做自动调度；默认保守 30 天，宁可多保留，也不能误删有效证据。
    artifact_retention_days: int
    public_base_url: str
    # M4：CI 触发的共享密钥。只从环境变量读，不写代码、不落库、不进 git。
    # 留空时 CI 接口直接禁用（返回 503），避免「忘了配 Token」变成没有认证的开放入口。
    ci_token: str

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def ai_enabled(self) -> bool:
        """AI 分析是否可用。

        判据是「有 key 且有模型」而不是有 base_url —— 少了任何一个，调用都必然失败。
        返回 False 时上层一律走「跳过」路径，不抛异常：没配 AI 不是错误，
        只是这个功能没开。
        """
        return bool(self.ai_api_key and self.ai_model)

    @property
    def wecom_enabled(self) -> bool:
        return bool(self.wecom_webhook_url and self.notification_enabled)

    @property
    def runs_dir(self) -> Path:
        return self.storage_dir / "projects"

    def ensure_directories(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)


def _default_database_url() -> str:
    return f"sqlite:///{(PROJECT_ROOT / 'testflow.db').as_posix()}"


def _resolve_storage_dir(raw: str) -> Path:
    path = Path(raw or "./storage")
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def load_settings() -> Settings:
    settings = Settings(
        project_root=PROJECT_ROOT,
        database_url=os.getenv("DATABASE_URL", "").strip() or _default_database_url(),
        executor_mode=(os.getenv("EXECUTOR_MODE", "inline").strip().lower() or "inline"),
        celery_broker_url=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0").strip(),
        celery_result_backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1").strip(),
        storage_dir=_resolve_storage_dir(os.getenv("STORAGE_DIR", "./storage")),
        playwright_browsers_path=os.getenv("PLAYWRIGHT_BROWSERS_PATH", "").strip(),
        playwright_headless=_as_bool(os.getenv("PLAYWRIGHT_HEADLESS"), True),
        playwright_record_video=_as_bool(os.getenv("PLAYWRIGHT_RECORD_VIDEO"), True),
        playwright_record_har=_as_bool(os.getenv("PLAYWRIGHT_RECORD_HAR"), True),
        secret=os.getenv("TESTFLOW_SECRET", "dev-only-secret-change-me").strip(),
        token_ttl_minutes=_as_int(os.getenv("ACCESS_TOKEN_TTL_MINUTES"), 720),
        bootstrap_admin_username=os.getenv("BOOTSTRAP_ADMIN_USERNAME", "admin").strip(),
        bootstrap_admin_password=os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "admin123").strip(),
        ai_base_url=os.getenv("AI_BASE_URL", "").strip(),
        ai_api_key=os.getenv("AI_API_KEY", "").strip(),
        ai_model=os.getenv("AI_MODEL", "").strip(),
        ai_provider=os.getenv("AI_PROVIDER", "openai_compatible").strip() or "openai_compatible",
        ai_timeout=float(os.getenv("AI_TIMEOUT", "60") or 60),
        ai_max_tokens=int(os.getenv("AI_MAX_TOKENS", "1200") or 1200),
        ai_max_retries=int(os.getenv("AI_MAX_RETRIES", "1") or 1),
        ai_required_rules=tuple(
            item.strip().upper() for item in os.getenv("AI_ANALYSIS_REQUIRED_RULES", "").split(",") if item.strip()
        ),
        ai_auto_analyze=_as_bool(os.getenv("AI_AUTO_ANALYZE"), True),
        wecom_webhook_url=os.getenv("WECOM_WEBHOOK_URL", "").strip(),
        wecom_webhook_secret=os.getenv("WECOM_WEBHOOK_SECRET", "").strip(),
        notification_enabled=_as_bool(os.getenv("NOTIFICATION_ENABLED"), True),
        notification_max_attempts=int(os.getenv("NOTIFICATION_MAX_ATTEMPTS", "3") or 3),
        artifact_url_ttl_seconds=int(os.getenv("ARTIFACT_URL_TTL_SECONDS", "300") or 300),
        artifact_retention_days=_as_int(os.getenv("ARTIFACT_RETENTION_DAYS"), 30),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").strip(),
        ci_token=os.getenv("TESTFLOW_CI_TOKEN", "").strip(),
    )
    if settings.playwright_browsers_path:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = settings.playwright_browsers_path
    return settings


settings = load_settings()
settings.ensure_directories()
