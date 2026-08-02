import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
import bcrypt
from jose import JWTError, jwt
from sqlmodel import Session, select

from database import get_session
from models import Doctor

SECRET_KEY = os.environ.get("CLINIC_SECRET_KEY", "change-this-secret-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 12

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def create_access_token(username: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": username, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def authenticate_doctor(session: Session, username: str, password: str) -> Optional[Doctor]:
    doctor = session.get(Doctor, username)
    if not doctor or not verify_password(password, doctor.hashed_password):
        return None
    return doctor


def get_current_doctor(
    token: str = Depends(oauth2_scheme), session: Session = Depends(get_session)
) -> Doctor:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Không xác thực được — vui lòng đăng nhập lại",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    doctor = session.get(Doctor, username)
    if doctor is None:
        raise credentials_exception
    return doctor


def require_role(*roles):
    def checker(doctor: Doctor = Depends(get_current_doctor)) -> Doctor:
        if doctor.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Chỉ tài khoản {'/'.join(roles)} mới được thực hiện thao tác này",
            )
        return doctor
    return checker


def require_create_permission(doctor: Doctor = Depends(get_current_doctor)) -> Doctor:
    if not doctor.can_create:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản này chưa được cấp quyền tạo mã lưu trữ mới",
        )
    return doctor


def require_export_permission(doctor: Doctor = Depends(get_current_doctor)) -> Doctor:
    if not doctor.can_export:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản này chưa được cấp quyền xuất dữ liệu",
        )
    return doctor


def require_admin(doctor: Doctor = Depends(get_current_doctor)) -> Doctor:
    if not doctor.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Chỉ tài khoản admin mới được quản lý tài khoản người dùng",
        )
    return doctor
