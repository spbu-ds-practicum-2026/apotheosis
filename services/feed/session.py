import re
from typing import Optional

from fastapi import Header, HTTPException
from pydantic import BaseModel

from schemas import LOGIN_PATTERN

_LOGIN_RE = re.compile(LOGIN_PATTERN)


class CurrentUser(BaseModel):
    user_id: str


async def get_current_user(authorization: Optional[str] = Header(None)) -> CurrentUser:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    scheme, _, token = authorization.partition(" ")
    if scheme != "Bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid Authorization format. Use: Bearer <token>")

    user_id, _, random_part = token.partition(".")
    if not _LOGIN_RE.fullmatch(user_id) or not random_part:
        raise HTTPException(status_code=401, detail="Invalid token format. Expected: <userId>.<random_string>")

    return CurrentUser(user_id=user_id)
