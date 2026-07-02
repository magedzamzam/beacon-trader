from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from beacon_core.config import get_settings
from beacon_core.db.models import User
from beacon_core.security import hash_password, make_token, verify_password, verify_token
from ..deps import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    username: str
    password: str


async def _user_count(db: AsyncSession) -> int:
    return int((await db.execute(select(func.count(User.id)))).scalar() or 0)


@router.get("/status")
async def status(db: AsyncSession = Depends(get_db)):
    """Whether any user exists yet — the frontend uses this to show the first
    'create admin' screen vs the normal login."""
    return {"users_exist": (await _user_count(db)) > 0}


@router.post("/register")
async def register(body: Credentials, authorization: str = Header(default=""),
                   db: AsyncSession = Depends(get_db)):
    """Create a user. Allowed when NO users exist yet (first-run bootstrap), or
    when the caller presents the master API_TOKEN."""
    existing = await _user_count(db)
    api_token = get_settings().api_token
    is_admin_caller = authorization == f"Bearer {api_token}" and bool(api_token)
    if existing > 0 and not is_admin_caller:
        raise HTTPException(403, "registration is closed; ask an admin or use the API token")

    if (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none():
        raise HTTPException(409, "username already exists")
    if len(body.password) < 8:
        raise HTTPException(422, "password must be at least 8 characters")

    u = User(username=body.username, password_hash=hash_password(body.password), is_admin=True)
    db.add(u)
    await db.commit()
    return {"token": make_token(u.username), "username": u.username}


@router.post("/login")
async def login(body: Credentials, db: AsyncSession = Depends(get_db)):
    u = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if not u or not verify_password(body.password, u.password_hash):
        raise HTTPException(401, "invalid username or password")
    return {"token": make_token(u.username), "username": u.username}


@router.get("/me")
async def me(authorization: str = Header(default="")):
    presented = authorization[len("Bearer "):] if authorization.startswith("Bearer ") else ""
    if presented and presented == get_settings().api_token:
        return {"username": "api-token", "via": "api_token"}
    sub = verify_token(presented)
    if not sub:
        raise HTTPException(401, "not authenticated")
    return {"username": sub, "via": "session"}
