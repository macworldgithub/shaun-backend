from datetime import datetime, timezone
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator
import uuid


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return str(uuid.uuid4())


JobStatus = Literal[
    'requested',
    'collected',
    'in_progress',
    'returned',
    'completed',
    'invoiced'
]

JobPriority = Literal['low', 'normal', 'high', 'urgent']
EvidenceStage = Literal['before', 'during', 'after', 'issue']
MediaType = Literal['image', 'video']
ActivityType = Literal[
    'comment',
    'status_change',
    'issue_flag',
    'issue_resolved',
    'assignment',
    'time_log',
    'location_update',
    'checklist_update',
    'integration_sync'
]


# ===== JOB CHECKLIST TASK =====
class JobTask(BaseModel):
    id: str = Field(default_factory=_id)
    title: str
    completed: bool = False
    completed_at: Optional[datetime] = None
    completed_by_id: Optional[str] = None
    completed_by_name: Optional[str] = None
    notes: Optional[str] = None


class JobTaskCreate(BaseModel):
    title: str
    notes: Optional[str] = None


class JobTaskUpdate(BaseModel):
    title: Optional[str] = None
    completed: Optional[bool] = None
    notes: Optional[str] = None


# ===== JOB CARD =====
class JobBase(BaseModel):
    client_id: Optional[str] = None
    model_name: str
    full_vin: str
    last_6_vin: Optional[str] = None
    rego: Optional[str] = None
    delivery_date_time: Optional[str] = None  # ISO format string or yyyy-mm-dd
    due_date: Optional[str] = None  # ISO format string or yyyy-mm-dd
    description: Optional[str] = None
    priority: JobPriority = 'normal'
    is_urgent: bool = False
    status: JobStatus = 'requested'
    assigned_contractor_id: Optional[str] = None
    assigned_contractor_name: Optional[str] = None
    site_location: Optional[str] = 'Fairfield'
    bay_location: Optional[str] = None
    checklist: List[JobTask] = Field(default_factory=list)
    template_id: Optional[str] = None
    issue_flag: bool = False
    issue_description: Optional[str] = None
    issue_reported_by_id: Optional[str] = None
    issue_reported_by_name: Optional[str] = None
    issue_reported_at: Optional[datetime] = None
    total_time_seconds: int = 0

    @model_validator(mode='after')
    def compute_last_6_vin(self):
        if self.full_vin and not self.last_6_vin:
            clean = self.full_vin.strip().upper()
            if len(clean) >= 6:
                self.last_6_vin = clean[-6:]
            else:
                self.last_6_vin = clean
        if self.priority == 'urgent':
            self.is_urgent = True
        elif self.is_urgent:
            self.priority = 'urgent'
        return self


class Job(JobBase):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(default_factory=_id)
    created_by_id: Optional[str] = None
    created_by_name: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    collected_at: Optional[datetime] = None
    in_progress_at: Optional[datetime] = None
    returned_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    invoiced_at: Optional[datetime] = None


class JobCreate(BaseModel):
    client_id: Optional[str] = None
    model_name: str
    full_vin: str
    last_6_vin: Optional[str] = None
    rego: Optional[str] = None
    delivery_date_time: Optional[str] = None
    due_date: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[JobPriority] = 'normal'
    is_urgent: Optional[bool] = False
    status: Optional[JobStatus] = 'requested'
    assigned_contractor_id: Optional[str] = None
    site_location: Optional[str] = 'Fairfield'
    bay_location: Optional[str] = None
    checklist: Optional[List[JobTaskCreate]] = None
    template_id: Optional[str] = None


class JobUpdate(BaseModel):
    model_config = ConfigDict(extra='ignore')
    model_name: Optional[str] = None
    full_vin: Optional[str] = None
    last_6_vin: Optional[str] = None
    rego: Optional[str] = None
    delivery_date_time: Optional[str] = None
    due_date: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[JobPriority] = None
    is_urgent: Optional[bool] = None
    status: Optional[JobStatus] = None
    assigned_contractor_id: Optional[str] = None
    assigned_contractor_name: Optional[str] = None
    site_location: Optional[str] = None
    bay_location: Optional[str] = None


class JobStatusUpdate(BaseModel):
    status: JobStatus
    notes: Optional[str] = None


class JobFlagIssue(BaseModel):
    description: str
    is_urgent: bool = True
    photos: Optional[List[str]] = Field(default_factory=list)


# ===== JOB TEMPLATES =====
class JobTemplate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(default_factory=_id)
    name: str
    description: Optional[str] = None
    tasks: List[str] = Field(default_factory=list)
    estimated_duration_minutes: Optional[int] = None
    category: Optional[str] = 'General'
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class JobTemplateCreate(BaseModel):
    name: str
    description: Optional[str] = None
    tasks: List[str] = Field(default_factory=list)
    estimated_duration_minutes: Optional[int] = None
    category: Optional[str] = 'General'


class JobTemplateUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    tasks: Optional[List[str]] = None
    estimated_duration_minutes: Optional[int] = None
    category: Optional[str] = None


class JobFromTemplateRequest(BaseModel):
    template_id: str
    model_name: str
    full_vin: str
    client_id: Optional[str] = None
    rego: Optional[str] = None
    delivery_date_time: Optional[str] = None
    due_date: Optional[str] = None
    assigned_contractor_id: Optional[str] = None
    priority: Optional[JobPriority] = 'normal'
    is_urgent: Optional[bool] = False
    site_location: Optional[str] = 'Fairfield'
    bay_location: Optional[str] = None


# ===== TIME TRACKING =====
class JobTimeEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(default_factory=_id)
    job_id: str
    task_id: Optional[str] = None
    contractor_id: str
    contractor_name: str
    clock_in: datetime = Field(default_factory=_now)
    clock_out: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    notes: Optional[str] = None


class ClockInRequest(BaseModel):
    task_id: Optional[str] = None
    notes: Optional[str] = None


class ClockOutRequest(BaseModel):
    notes: Optional[str] = None


# ===== EVIDENCE CAPTURE =====
class JobEvidence(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(default_factory=_id)
    job_id: str
    stage: EvidenceStage = 'before'
    media_type: MediaType = 'image'
    file_name: str
    storage_path: str
    content_type: str = 'image/jpeg'
    size_bytes: int = 0
    caption: Optional[str] = None
    captured_at: datetime = Field(default_factory=_now)
    uploaded_by_id: str
    uploaded_by_name: str


# ===== LOCATION REGISTRATION =====
class JobLocationEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(default_factory=_id)
    job_id: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy: Optional[float] = None
    location_name: Optional[str] = None  # e.g., "BYD Fairfield Bay 4", "Workshop A"
    notes: Optional[str] = None
    recorded_at: datetime = Field(default_factory=_now)
    recorded_by_id: str
    recorded_by_name: str


class LocationCheckInRequest(BaseModel):
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy: Optional[float] = None
    location_name: Optional[str] = None
    notes: Optional[str] = None


# ===== ACTIVITY & COMMENTS =====
class JobActivity(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(default_factory=_id)
    job_id: str
    activity_type: ActivityType = 'comment'
    author_id: str
    author_name: str
    message: str
    meta: Optional[dict] = None
    created_at: datetime = Field(default_factory=_now)


class JobCommentCreate(BaseModel):
    message: str


# ===== INTEGRATION & SYNC =====
class IntegrationSyncLog(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: str = Field(default_factory=_id)
    job_id: Optional[str] = None
    client_id: Optional[str] = None
    direction: Literal['inbound_to_contractor', 'outbound_to_delivery']
    event: str
    status: Literal['success', 'failed', 'pending'] = 'success'
    details: Optional[dict] = None
    error_message: Optional[str] = None
    synced_at: datetime = Field(default_factory=_now)


class ConvertClientToJobRequest(BaseModel):
    tasks: Optional[List[str]] = None
    assigned_contractor_id: Optional[str] = None
    due_date: Optional[str] = None
    priority: Optional[JobPriority] = 'normal'
    is_urgent: Optional[bool] = False
    notes: Optional[str] = None


# ===== WEB PUSH NOTIFICATIONS =====
class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionPayload(BaseModel):
    endpoint: str
    keys: PushSubscriptionKeys
    device_info: Optional[str] = None


class SendPushTestRequest(BaseModel):
    title: str
    body: str
    data: Optional[dict] = None


# ===== BULK JOB IMPORT =====
class BulkJobImportRequest(BaseModel):
    jobs: List[JobCreate]
    apply_templates: bool = True


class BulkJobImportResultItem(BaseModel):
    index: int
    full_vin: Optional[str] = None
    success: bool
    job_id: Optional[str] = None
    error: Optional[str] = None


class BulkJobImportResponse(BaseModel):
    total_submitted: int
    imported_count: int
    failed_count: int
    created_job_ids: List[str] = Field(default_factory=list)
    results: List[BulkJobImportResultItem] = Field(default_factory=list)


# ===== OFFLINE BATCH SYNC =====
OfflineActionType = Literal[
    'clock_in',
    'clock_out',
    'location_checkin',
    'add_comment',
    'flag_issue',
    'update_task'
]


class OfflineActionItem(BaseModel):
    client_action_id: str
    action_type: OfflineActionType
    job_id: str
    payload: dict = Field(default_factory=dict)
    queued_at: Optional[datetime] = None


class OfflineActionResult(BaseModel):
    client_action_id: str
    success: bool
    message: Optional[str] = None
    data: Optional[dict] = None


class OfflineBatchSyncRequest(BaseModel):
    actions: List[OfflineActionItem]


class OfflineBatchSyncResponse(BaseModel):
    processed: int
    succeeded: int
    failed: int
    results: List[OfflineActionResult] = Field(default_factory=list)

