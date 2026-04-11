
import time
from datetime import datetime, timedelta
from typing import Optional
import bcrypt
from jose import jwt, JWTError
from fastapi import HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer
from ..core.config import settings
from ..database.mongo import get_db
from bson import ObjectId
from ..utils.id import to_object_id
from fastapi import HTTPException, Depends, Request

# OAuth2 scheme points to /auth/token endpoint
# username field in the form will be treated as email
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_PREFIX}/auth/token")


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode(), salt).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


def create_access_token(subject: dict, expires_minutes: int | None = None) -> str:
    expire = datetime.utcnow() + timedelta(minutes=expires_minutes or settings.JWT_EXPIRE_MINUTES)
    payload = {**subject, "exp": expire}
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token


async def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
):
    from fastapi import Request  # already imported at top of file
    db = await get_db()

    # ── Path 1: Request came through API Gateway ──────────────
    # Gateway already validated the JWT and injected X-User-Id
    x_user_id   = request.headers.get("x-user-id")
    x_user_role = request.headers.get("x-user-role")
    x_internal  = request.headers.get("x-internal-token")

    # Reject requests that bypassed gateway AND have no valid token
    # (services are not directly reachable in k8s, but extra safety)

    if x_user_id and x_internal == settings.INTERNAL_SERVICE_TOKEN:
        # Fast path — trust the gateway
        user = None
        target_is_customer = x_user_role == "customer"
        primary  = db.users       if target_is_customer else db.staff_users
        fallback = db.staff_users if target_is_customer else db.users
        try:
            uid = int(x_user_id)
            user = await primary.find_one({"_id": uid})
            if not user:
                user = await fallback.find_one({"_id": uid})
        except Exception:
            oid = to_object_id(x_user_id)
            user = await primary.find_one({"_id": oid})
            if not user:
                user = await fallback.find_one({"_id": oid})

        if not user or not user.get("is_active", True):
            raise HTTPException(status_code=401, detail="User inactive or not found")
        return user

    # ── Path 2: Direct access (dev/curl) — decode JWT ─────────
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = payload.get("user_id")
    role    = payload.get("role")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    user = None
    target_is_customer = role == "customer"
    primary  = db.users       if target_is_customer else db.staff_users
    fallback = db.staff_users if target_is_customer else db.users

    try:
        uid = int(user_id)
        user = await primary.find_one({"_id": uid})
        if not user:
            user = await fallback.find_one({"_id": uid})
    except Exception:
        oid = to_object_id(user_id)
        user = await primary.find_one({"_id": oid})
        if not user:
            user = await fallback.find_one({"_id": oid})

    if not user or not user.get("is_active", True):
        raise HTTPException(status_code=401, detail="User inactive or not found")
    return user


def require_roles(*allowed_roles):
    async def dep(user = Depends(get_current_user)):
        user_role = str(user.get("role")).lower()

        normalized_roles = []
        for role in allowed_roles:
            if hasattr(role, "value"):  # Enum case
                normalized_roles.append(str(role.value).lower())
            else:
                normalized_roles.append(str(role).lower())

        if user_role not in normalized_roles:
            raise HTTPException(status_code=403, detail="Not authorized for this operation")

        return user
    return dep
