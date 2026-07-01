from fastapi import Header, HTTPException
from beacon_core.config import get_settings


async def require_token(authorization: str = Header(default="")) -> None:
    token = get_settings().api_token
    expected = f"Bearer {token}"
    if not token or authorization != expected:
        raise HTTPException(status_code=401, detail="invalid or missing token")
