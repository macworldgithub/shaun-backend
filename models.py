from datetime import datetime, timezone, timedelta
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, EmailStr, ConfigDict, model_validator
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
    team: Optional[str] = 'All Teams'
    active_site: Optional[str] = 'Fairfield'
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
    team: Optional[str] = 'All Teams'
    active_site: Optional[str] = 'Fairfield'
    password: Optional[str] = None  # if None, server generates


class UserUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[UserRole] = None
    team: Optional[str] = None
    active_site: Optional[str] = None
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
RegistrationStatus = Literal['Awaiting registration documents', 'Awaiting VIN', 'Ready to register', 'Partial', 'Complete']
DocumentType = Literal['ATR signed', 'ATR incomplete', 'Licence front', 'Licence back', 'EFT form', 'Bank statement', 'Handover checklist', 'Medicare card', 'Transfer form', 'Acquisition police strip', 'Customer trade-in checklist', 'Other']
HandoverChecklistStatus = Literal['Not issued', 'Issued in VY', 'Signed copy on file', 'Exception']
OfferStatus = Literal['Eligible', 'At risk', 'Ineligible']
DocumentCompleteness = Literal['Requested', 'Partial', 'Complete']
TradeInStatus = Literal['Pending', 'Quoted', 'Accepted', 'Vehicle received', 'Settled', 'Valid', 'Expiring soon', 'Expiring', 'Expired', 'Cancelled']
SaleType = Literal['Retail', 'Lease', 'Novated', 'Novated lease', 'Fleet', 'Government', 'Rental', 'Cash', 'Demo', 'Other']
RegisteredOperatorType = Literal['Individual', 'Company']
ActivationStatus = Literal['Blocked', 'Ready', 'Submitted to BYD', 'Active']


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
    po_number: Optional[str] = None
    payment_method: Optional[str] = None  # Cash / Finance / Novated lease / Other
    order_date: Optional[str] = None
    sale_type: SaleType = 'Retail'
    fleet_company: Optional[str] = None
    fleet_reference: Optional[str] = None
    lease_consultant: Optional[str] = None
    registered_operator_type: RegisteredOperatorType = 'Individual'
    trade_in_flag: bool = False
    trade_in_attached: bool = False
    trade_in_sale_date: Optional[str] = None
    trade_in_valid_until: Optional[str] = None
    trade_in_status: TradeInStatus = 'Pending'
    trade_in_manager_reason: Optional[str] = None
    trade_in_override_by: Optional[str] = None
    trade_in_override_at: Optional[datetime] = None
    linked_offer_ids: List[str] = Field(default_factory=list)
    your_way_selection: Optional[Literal['Cashback', 'Accessories', 'Car care', 'Merchandise', 'Charging', 'Other']] = None
    your_way_note: Optional[str] = None
    registration_status: RegistrationStatus = 'Awaiting registration documents'
    registration_docs_complete: bool = False
    handover_checklist_status: HandoverChecklistStatus = 'Not issued'
    activation_ready: bool = False
    activation_status: ActivationStatus = 'Blocked'
    activation_override_reason: Optional[str] = None
    activation_override_by: Optional[str] = None
    activation_override_at: Optional[datetime] = None
    offer_status: OfferStatus = 'Eligible'
    offer_reason: Optional[str] = None
    documents: List[dict] = Field(default_factory=list)
    document_completeness: DocumentCompleteness = 'Requested'
    deal_type: Optional[str] = None
    delivery_date: Optional[str] = None  # ISO yyyy-mm-dd
    stage: DeliveryStage = 'Scheduled'
    salesperson: Optional[str] = None
    secondary_salesperson: Optional[str] = None
    delivery_consultant: Optional[str] = None
    handover_specialist: Optional[str] = None
    notes: Optional[str] = None
    address: Optional[str] = None
    location: Optional[str] = None  # suburb / state for at-a-glance
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
    trade_in_valuation: Optional[float] = 0.0
    site_location: Optional[str] = 'Fairfield'
    business_client_id: Optional[str] = None
    imported_from: Optional[str] = None  # 'paste' | 'email' | 'manual'
    imported_at: Optional[datetime] = None

    @model_validator(mode='after')
    def sync_activation_state(self):
        if self.activation_status in ('Ready', 'Submitted to BYD', 'Active'):
            self.activation_ready = True
        elif self.activation_status == 'Blocked':
            self.activation_ready = False
        if self.trade_in_attached and self.trade_in_sale_date and not self.trade_in_valid_until:
            try:
                sale_date = datetime.strptime(self.trade_in_sale_date, '%Y-%m-%d').date()
                # Group policy rules: Non-Fairfield sites hold >$30k for 7 days, <=$30k for 14 days; Fairfield holds 30 days
                site = (self.site_location or 'Fairfield').strip().lower()
                val = self.trade_in_valuation or 0.0
                if 'fairfield' not in site:
                    days = 7 if val > 30000 else 14
                else:
                    days = 30
                self.trade_in_valid_until = (sale_date + timedelta(days=days)).isoformat()
            except ValueError:
                pass
        if self.trade_in_valid_until and self.trade_in_valid_until < datetime.now(timezone.utc).date().isoformat() and self.trade_in_status in ('Pending', 'Valid', 'Expiring soon', 'Expiring'):
            self.trade_in_status = 'Expired'
        return self


class ClientDocument(BaseModel):
    id: str = Field(default_factory=_id)
    client_id: Optional[str] = None
    document_type: DocumentType = 'Other'
    status: Literal['requested', 'partial', 'complete', 're-requested'] = 'requested'
    file_name: Optional[str] = None
    source: Literal['email', 'staff-upload', 'vy', 'manual'] = 'manual'
    uploaded_by: Optional[str] = None
    storage_path: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    sent_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_now)
    notes: Optional[str] = None
    signature_present: bool = False
    date_left_blank: Optional[bool] = None
    customer_present: Optional[bool] = None
    bank_name: Optional[str] = None
    bsb: Optional[str] = None
    account_number: Optional[str] = None
    amount: Optional[float] = None
    reference: Optional[str] = None
    bank_statement_received: bool = False


class ClientDocumentUpdate(BaseModel):
    document_type: Optional[DocumentType] = None
    status: Optional[Literal['requested', 'partial', 'complete', 're-requested']] = None
    file_name: Optional[str] = None
    source: Optional[Literal['email', 'staff-upload', 'vy', 'manual']] = None
    notes: Optional[str] = None
    signature_present: Optional[bool] = None
    date_left_blank: Optional[bool] = None
    customer_present: Optional[bool] = None
    bank_name: Optional[str] = None
    bsb: Optional[str] = None
    account_number: Optional[str] = None
    amount: Optional[float] = None
    reference: Optional[str] = None
    bank_statement_received: Optional[bool] = None


class OfferRecord(BaseModel):
    id: str = Field(default_factory=_id)
    name: str
    source_key: Optional[str] = None
    model_variant: Optional[str] = None
    body_style: Optional[str] = None
    powertrain: Optional[str] = None
    display_value: Optional[str] = None
    display_unit: Optional[str] = None
    image_url: Optional[str] = None
    configurator_url: Optional[str] = None
    source_url: Optional[str] = None
    eligible_models: List[str] = Field(default_factory=list)
    order_from: Optional[str] = None
    order_to: Optional[str] = None
    deliver_by: Optional[str] = None
    honour_if_delayed: bool = False
    sale_type_exclusions: List[str] = Field(default_factory=list)
    combinable: bool = True
    claim_doc_template: Optional[str] = None
    claim_doc_templates: List[str] = Field(default_factory=list)
    cash_or_product: Literal['cash', 'product', 'both'] = 'cash'
    public_url: Optional[str] = None
    internal_notes: Optional[str] = None
    active: bool = True
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    last_refreshed_at: Optional[datetime] = None


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
    po_number: Optional[str] = None
    payment_method: Optional[str] = None
    order_date: Optional[str] = None
    sale_type: Optional[SaleType] = None
    fleet_company: Optional[str] = None
    fleet_reference: Optional[str] = None
    lease_consultant: Optional[str] = None
    registered_operator_type: Optional[RegisteredOperatorType] = None
    trade_in_flag: Optional[bool] = None
    trade_in_attached: Optional[bool] = None
    trade_in_sale_date: Optional[str] = None
    trade_in_valid_until: Optional[str] = None
    trade_in_status: Optional[TradeInStatus] = None
    trade_in_manager_reason: Optional[str] = None
    linked_offer_ids: Optional[List[str]] = None
    your_way_selection: Optional[Literal['Cashback', 'Accessories', 'Car care', 'Merchandise', 'Charging', 'Other']] = None
    your_way_note: Optional[str] = None
    registration_status: Optional[RegistrationStatus] = None
    registration_docs_complete: Optional[bool] = None
    handover_checklist_status: Optional[HandoverChecklistStatus] = None
    activation_ready: Optional[bool] = None
    activation_status: Optional[ActivationStatus] = None
    offer_status: Optional[OfferStatus] = None
    offer_reason: Optional[str] = None
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
    trade_in_valuation: Optional[float] = None
    site_location: Optional[str] = None
    business_client_id: Optional[str] = None
    secondary_salesperson: Optional[str] = None
    delivery_consultant: Optional[str] = None
    handover_specialist: Optional[str] = None


class OfferCreate(BaseModel):
    name: str
    source_key: Optional[str] = None
    model_variant: Optional[str] = None
    body_style: Optional[str] = None
    powertrain: Optional[str] = None
    display_value: Optional[str] = None
    display_unit: Optional[str] = None
    image_url: Optional[str] = None
    configurator_url: Optional[str] = None
    source_url: Optional[str] = None
    eligible_models: List[str] = Field(default_factory=list)
    order_from: Optional[str] = None
    order_to: Optional[str] = None
    deliver_by: Optional[str] = None
    honour_if_delayed: bool = False
    sale_type_exclusions: List[str] = Field(default_factory=list)
    combinable: bool = True
    claim_doc_template: Optional[str] = None
    claim_doc_templates: List[str] = Field(default_factory=list)
    cash_or_product: Literal['cash', 'product', 'both'] = 'cash'
    public_url: Optional[str] = None
    internal_notes: Optional[str] = None
    active: bool = True


class OfferUpdate(BaseModel):
    name: Optional[str] = None
    source_key: Optional[str] = None
    model_variant: Optional[str] = None
    body_style: Optional[str] = None
    powertrain: Optional[str] = None
    display_value: Optional[str] = None
    display_unit: Optional[str] = None
    image_url: Optional[str] = None
    configurator_url: Optional[str] = None
    source_url: Optional[str] = None
    eligible_models: Optional[List[str]] = None
    order_from: Optional[str] = None
    order_to: Optional[str] = None
    deliver_by: Optional[str] = None
    honour_if_delayed: Optional[bool] = None
    sale_type_exclusions: Optional[List[str]] = None
    combinable: Optional[bool] = None
    claim_doc_template: Optional[str] = None
    claim_doc_templates: Optional[List[str]] = None
    cash_or_product: Optional[Literal['cash', 'product', 'both']] = None
    public_url: Optional[str] = None
    internal_notes: Optional[str] = None
    active: Optional[bool] = None


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


