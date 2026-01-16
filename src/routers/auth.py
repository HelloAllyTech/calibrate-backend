import os
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx

from db import get_or_create_user
from auth_utils import create_access_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class GoogleLoginRequest(BaseModel):
    """Request body for Google login."""

    id_token: str


class UserResponse(BaseModel):
    """User response model."""

    uuid: str
    first_name: str
    last_name: str
    email: str
    created_at: str
    updated_at: str


class LoginResponse(BaseModel):
    """Response for successful login."""

    access_token: str
    token_type: str = "bearer"
    user: UserResponse
    message: str


async def verify_google_token(id_token: str) -> dict:
    """
    Verify Google ID token using Google's tokeninfo endpoint.

    Args:
        id_token: The Google ID token from the frontend

    Returns:
        Dict containing user info (email, given_name, family_name, etc.)

    Raises:
        HTTPException: If token verification fails
    """
    google_client_id = os.getenv("GOOGLE_CLIENT_ID")
    if not google_client_id:
        logger.warning("GOOGLE_CLIENT_ID not set, skipping client_id validation")

    # Use Google's tokeninfo endpoint to verify the token
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"https://oauth2.googleapis.com/tokeninfo?id_token={id_token}"
            )

            if response.status_code != 200:
                logger.error(f"Google token verification failed: {response.text}")
                raise HTTPException(status_code=401, detail="Invalid Google token")

            token_info = response.json()

            return token_info

        except httpx.RequestError as e:
            logger.error(f"Error verifying Google token: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to verify Google token")


@router.post("/google", response_model=LoginResponse)
async def google_login(request: GoogleLoginRequest):
    """
    Authenticate a user with Google OAuth.

    This endpoint:
    1. Verifies the Google ID token with Google's servers
    2. Extracts user info (email, name) from the token
    3. Creates a new user or retrieves existing user by email
    4. Returns the user info

    Args:
        request: Contains the Google ID token from the frontend

    Returns:
        LoginResponse with user info and success message
    """
    # Verify the Google token
    token_info = await verify_google_token(request.id_token)

    # Extract user info from token
    email = token_info.get("email")
    given_name = token_info.get("given_name", "")
    family_name = token_info.get("family_name", "")

    if not email:
        raise HTTPException(
            status_code=400, detail="Email not provided in Google token"
        )

    # Get or create user
    user = get_or_create_user(
        email=email,
        first_name=given_name,
        last_name=family_name,
    )

    # Generate JWT access token
    access_token = create_access_token(user["uuid"], user["email"])

    logger.info(f"User logged in: {email} (UUID: {user['uuid']})")

    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
        user=UserResponse(
            uuid=user["uuid"],
            first_name=user["first_name"],
            last_name=user["last_name"],
            email=user["email"],
            created_at=user["created_at"],
            updated_at=user["updated_at"],
        ),
        message="Login successful",
    )
