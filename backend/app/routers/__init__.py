"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import ingest
from app.routers import resource
from app.routers import chains

# ingest 含 /api/ingest/* 固定路径，先注册，避免被 /api/{module} 动态路径抢先匹配。
ROUTERS = [ingest, resource, chains]
