"""Who is calling: a Clerk session token in production, a fixed "dev" user locally."""

from __future__ import annotations

from functools import lru_cache

import jwt
from fastapi import Header, HTTPException

from . import config
from .db import User, session


@lru_cache(maxsize=1)
def _jwks() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"{config.CLERK_ISSUER}/.well-known/jwks.json", cache_keys=True)


def _verify(token: str) -> dict:
    key = _jwks().get_signing_key_from_jwt(token)
    claims = jwt.decode(token, key.key, algorithms=["RS256"], issuer=config.CLERK_ISSUER,
                        options={"require": ["exp", "sub"]}, leeway=10)
    azp = claims.get("azp")  # the origin the token was minted for
    if azp and azp not in config.WEB_ORIGINS:
        raise jwt.InvalidTokenError(f"token minted for {azp}")
    return claims


def current_user(authorization: str | None = Header(None)) -> User:
    if config.CLERK_ISSUER and authorization and authorization.startswith("Bearer "):
        try:
            claims = _verify(authorization.removeprefix("Bearer ").strip())
        except jwt.PyJWTError:
            raise HTTPException(401, "Your session has expired. Sign in again.")
        user_id = claims["sub"]
    elif config.DEV_AUTH:
        user_id = "dev"
    else:
        raise HTTPException(401, "Sign in to continue.")

    with session() as s:
        user = s.get(User, user_id)
        if not user:
            user = User(id=user_id)
            s.add(user)
            s.commit()
        return user
