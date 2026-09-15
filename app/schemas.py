from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr = Field(max_length=254)
    password: SecretStr = Field(min_length=1, max_length=128)


class RecoveryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr = Field(max_length=254)


class ResetPasswordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_token: SecretStr = Field(min_length=1, max_length=8192)
    password: SecretStr = Field(min_length=12, max_length=128)


class AdminIdentity(BaseModel):
    id: UUID
    email: EmailStr
    name: str
    role: Literal["super_admin", "admin"]


class SessionResponse(BaseModel):
    user: AdminIdentity
    expires_in: int
