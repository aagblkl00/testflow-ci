"""初始化数据。

M1 只做一件事：保证有一个可登录的管理员。
业务数据（项目/环境/用例/流程/计划）一律由使用者在页面上创建——
这是 M1 验收里「创建项目 → … → 执行」那条链路的起点，不能提前塞进去。
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from .auth.security import hash_password
from .config import settings
from .db import SessionLocal
from .models import User

logger = logging.getLogger("testflow.seed")


def bootstrap_admin() -> None:
    db = SessionLocal()
    try:
        exists = db.execute(select(User).where(User.username == settings.bootstrap_admin_username)).scalars().first()
        if exists is not None:
            return
        db.add(
            User(
                username=settings.bootstrap_admin_username,
                display_name="系统管理员",
                password_hash=hash_password(settings.bootstrap_admin_password),
                role="ADMIN",
            )
        )
        db.commit()
        logger.info("已创建初始管理员账号：%s", settings.bootstrap_admin_username)
    finally:
        db.close()
