"""Founder identity only: fixed Access issuer/JWKS, never a token-supplied URL."""
import os
import re
import time

import httpx
import jwt

_cache = {}  # ponytail: one team per process; bounded TTL, no stale keys on fetch failure.


async def verify_founder(token: str, *, client=None) -> bool:
    return await verify_founder_identity(token, client=client) is not None


async def verify_founder_identity(token: str, *, client=None) -> str | None:
    """The verified founder's lowercase email, or None. The email is the identity api_auth records."""
    team = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "")
    audience = os.environ.get("CF_ACCESS_DESK_AUD", "")
    emails = {e.strip().lower() for e in os.environ.get("CF_ACCESS_DESK_EMAILS", "").split(",") if e.strip()}
    if not (re.fullmatch(r"[a-zA-Z0-9-]+\.cloudflareaccess\.com", team)
            and audience and emails and isinstance(token, str) and len(token) <= 16384):
        return None
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            return None
        url = f"https://{team}/cdn-cgi/access/certs"
        cached_url, expires, keys = (_cache.get("keys") or (None, 0, []))
        if client is not None or cached_url != url or time.monotonic() >= expires:
            if client is None:
                async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as owned:
                    response = await owned.get(url)
            else:
                response = await client.get(url)
            response.raise_for_status()
            keys = response.json()["keys"]
            if not isinstance(keys, list):
                return None
            if client is None:
                _cache["keys"] = (url, time.monotonic() + 600, keys)
        key = next(k for k in keys if k.get("kid") == header["kid"] and k.get("kty") == "RSA")
        claims = jwt.decode(token, jwt.PyJWK.from_dict(key, algorithm="RS256").key,
                            algorithms=["RS256"], audience=audience, issuer=f"https://{team}",
                            options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        ok = (claims.get("type") == "app" and bool(claims["sub"])
              and isinstance(claims.get("email"), str) and claims["email"].lower() in emails)
        return claims["email"].lower() if ok else None
    except (jwt.PyJWTError, httpx.HTTPError, ValueError, TypeError, KeyError, StopIteration, AttributeError):
        return None
