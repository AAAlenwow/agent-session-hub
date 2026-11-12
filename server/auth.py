"""鉴权：Bearer token → 用户行（逐人归因的基础）。"""
import hashlib
from typing import Optional

from fastapi import Header, HTTPException, Request


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


async def current_user(request: Request, authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    row = await request.app.state.pool.fetchrow(
        "select id, name, is_admin from users where token_hash = $1",
        sha256(authorization[7:]),
    )
    if not row:
        raise HTTPException(401, "invalid token")
    return row
