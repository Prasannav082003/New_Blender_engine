from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from typing_extensions import Annotated

from ..schemas import schemas
from ..database import models, session
from ..services import auth_service
import secrets

router = APIRouter()

# Dependency for getting the database session
DBSession = Annotated[Session, Depends(session.get_db)]
# Dependency for getting the current user from a token
CurrentUser = Annotated[models.User, Depends(auth_service.get_user_from_api_key)]

@router.post("/register", response_model=schemas.User, status_code=status.HTTP_201_CREATED, summary="Register a new user")
def register_user(user: schemas.UserCreate, db: DBSession):
    """
    Create a new user in the system.
    - **email**: Must be a valid and unique email address.
    - **password**: Will be securely hashed.
    """
    db_user = auth_service.get_user_by_email(db, email=user.email)
    if db_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    return auth_service.create_user(db=db, user=user)

@router.post("/token", response_model=schemas.Token, summary="Login and get an access token")
def login_for_access_token(form_data: Annotated[OAuth2PasswordRequestForm, Depends()], db: DBSession):
    """
    Authenticate a user and return a JWT access token.
    - **username**: The user's email address.
    - **password**: The user's plain text password.
    """
    user = auth_service.authenticate_user(db, email=form_data.username, password=form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = auth_service.create_access_token(data={"sub": user.email})
    return {"access_token": access_token, "token_type": "bearer"}

@router.post("/api-key", response_model=schemas.ApiKey, summary="Generate or retrieve an API Key")
def generate_api_key(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: DBSession
):
    """
    Authenticate with email and password to generate a new API key.
    If a key already exists, it will be replaced.
    THE KEY IS ONLY SHOWN ONCE. Save it securely.
    """
    user = auth_service.authenticate_user(db, email=form_data.username, password=form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
   
    if user.hashed_api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An API key already exists for this user. To generate a new key, the existing one must be revoked first."
        )

    # Generate a new, secure API key
    new_api_key = secrets.token_hex(32)
   
    # Hash the new key and save it to the user's record
    user.hashed_api_key = models.User.get_api_key_hash(new_api_key)
    db.commit()

    return {"api_key": new_api_key}

@router.get("/me", response_model=schemas.User, summary="Get current user's details via API Key")
def read_users_me(current_user: CurrentUser):
    """
    Fetch details for the user authenticated via an API Key.
    """
    return current_user

