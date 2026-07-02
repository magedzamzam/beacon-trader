from fastapi import Header, HTTPException
from beacon_core.config import get_settings
from beacon_core.security import verify_token


async def require_token(authorization: str = Header(default="")) -> None:
    """Authorize a request. Accepts either the master API_TOKEN (bootstrap /
    machine access) or a valid user session token issued by /auth/login."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid or missing token")
    presented = authorization[len("Bearer "):]

    token = get_settings().api_token
    if token and presented == token:
        return
    if verify_token(presented):
        return
    raise HTTPException(status_code=401, detail="invalid or missing token")
