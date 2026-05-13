from datetime import datetime, timezone
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, EmailStr, ConfigDict
import uuid


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return str(uuid.uuid4())


# ===== USERS =====
UserRole = Literal['super_admin', 'admin', 'agent']


class User(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(default_factory=_id)
    email: EmailStr
    name: str
    role: UserRole = 'agent'
    active: bool = True
    created_at: datetime = Field(default_factory=_now)
    last_login_at: Optional[datetime] = None
    must_change_password: bool = False


class UserInDB(User):
    password_hash: str


class UserCreate(BaseModel):
    email: EmailStr
    name: str
    role: UserRole = 'agent'
    password: Optional[str] = None  # if None, server generates


class UserUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[UserRole] = None
    active: Optional[bool] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = 'bearer'
    user: User


# ===== CLIENTS =====
DeliveryStage = Literal['Scheduled', 'Pre-Delivery Inspection', 'In Transit', 'Ready for Pickup', 'Delivered']
ContactStatus = Literal['Not Contacted', 'Contacted', 'Booked', 'Awaiting Reply']


class Comment(BaseModel):
    id: str = Field(default_factory=_id)
    author_id: Optional[str] = None
    author_name: str
    body: str
    created_at: datetime = Field(default_factory=_now)


class Accessory(BaseModel):
    id: str = Field(default_factory=_id)
    name: str
    status: Literal['Pending Order', 'Ordered', 'Fitted', 'On Hand'] = 'Pending Order'
    note: Optional[str] = None
    updated_at: datetime = Field(default_factory=_now)


class ClientBase(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None
    vehicle: str
    rego: Optional[str] = None
    vin: Optional[str] = None
    delivery_date: Optional[str] = None  # ISO yyyy-mm-dd
    stage: DeliveryStage = 'Scheduled'
    salesperson: Optional[str] = None
    notes: Optional[str] = None
    address: Optional[str] = None
    location: Optional[str] = None  # suburb / state for at-a-glance
    deal_type: Optional[str] = None
    # VY ingestion
    vy_order_id: Optional[str] = None
    vy_stock_id: Optional[str] = None
    stripe_customer_id: Optional[str] = None
    # Vehicle arrival tracking
    arrived: bool = False
    arrived_at: Optional[datetime] = None
    # Contact tracking
    contact_status: ContactStatus = 'Not Contacted'
    last_contacted_at: Optional[datetime] = None
    # Assignment
    assigned_agent_id: Optional[str] = None  # delivery agent user id
    # Accessories / aftermarket
    accessories: List[Accessory] = Field(default_factory=list)
    aftermarket_notes: Optional[str] = None
    addons: List[str] = Field(default_factory=list)  # legacy short list
    imported_from: Optional[str] = None  # 'paste' | 'email' | 'manual'
    imported_at: Optional[datetime] = None


class Client(ClientBase):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(default_factory=_id)
    comments: List[Comment] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ClientUpdate(BaseModel):
    model_config = ConfigDict(extra='ignore')
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    vehicle: Optional[str] = None
    rego: Optional[str] = None
    vin: Optional[str] = None
    delivery_date: Optional[str] = None
    stage: Optional[DeliveryStage] = None
    salesperson: Optional[str] = None
    notes: Optional[str] = None
    address: Optional[str] = None
    location: Optional[str] = None
    deal_type: Optional[str] = None
    arrived: Optional[bool] = None
    contact_status: Optional[ContactStatus] = None
    assigned_agent_id: Optional[str] = None
    aftermarket_notes: Optional[str] = None
    addons: Optional[List[str]] = None


class CommentCreate(BaseModel):
    body: str


class AccessoryCreate(BaseModel):
    name: str
    status: Optional[Literal['Pending Order', 'Ordered', 'Fitted', 'On Hand']] = 'Pending Order'
    note: Optional[str] = None


class AccessoryUpdate(BaseModel):
    name: Optional[str] = None
    status: Optional[Literal['Pending Order', 'Ordered', 'Fitted', 'On Hand']] = None
    note: Optional[str] = None


# ===== MESSAGES (SMS) =====
class Message(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(default_factory=_id)
    client_id: Optional[str] = None
    client_name: Optional[str] = None
    phone: str
    body: str
    direction: Literal['outbound', 'inbound'] = 'outbound'
    status: str = 'queued'
    provider: str = 'mobilemessage'
    provider_message_id: Optional[str] = None
    provider_response: Optional[dict] = None
    sent_by_id: Optional[str] = None
    sent_at: datetime = Field(default_factory=_now)


class SendSmsRequest(BaseModel):
    client_id: Optional[str] = None
    phone: Optional[str] = None  # required if no client_id
    body: str


class BulkSmsRequest(BaseModel):
    client_ids: List[str]
    body: str  # may contain template vars


# ===== TEMPLATES =====
class Template(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(default_factory=_id)
    name: str
    body: str
    category: str = 'General'
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class TemplateCreate(BaseModel):
    name: str
    body: str
    category: str = 'General'


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    body: Optional[str] = None
    category: Optional[str] = None


# ===== AUDIT =====
class AuditEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(default_factory=_id)
    actor_id: Optional[str] = None
    actor_email: Optional[str] = None
    action: str
    entity: Optional[str] = None
    entity_id: Optional[str] = None
    meta: Optional[dict] = None
    ip: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)


# ===== SHARE LINKS =====
class ShareLink(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(default_factory=_id)
    token: str
    label: str
    scope: Literal['dashboard'] = 'dashboard'
    allowed_emails: List[EmailStr] = Field(default_factory=list)  # empty = anyone with token+email
    created_by_id: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)
    expires_at: Optional[datetime] = None
    active: bool = True
    view_count: int = 0
    last_viewed_at: Optional[datetime] = None
    last_viewer_email: Optional[str] = None


class ShareLinkCreate(BaseModel):
    label: str
    allowed_emails: List[EmailStr] = Field(default_factory=list)
    expires_in_hours: Optional[int] = None


class ShareAccessRequest(BaseModel):
    email: EmailStr


class ShareAccessResponse(BaseModel):
    access_token: str
    label: str
    expires_at: Optional[datetime] = None
