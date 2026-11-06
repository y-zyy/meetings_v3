from datetime import date, datetime

from pydantic import BaseModel


class DecisionOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    content: str
    order: int


class ActionItemOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    content: str
    assignee: str | None
    due_date: date | None
    status: str
    order: int


class ActionItemUpdate(BaseModel):
    content: str | None = None
    assignee: str | None = None
    due_date: date | None = None
    status: str | None = None


class TranscriptSegment(BaseModel):
    start: float
    end: float
    speaker: str | None = None
    text: str


class MeetingCreate(BaseModel):
    title: str
    meeting_date: date | None = None
    location: str = ""
    attendees: str = ""
    agenda: str = ""
    notes: str = ""
    language: str = "ko"


class MeetingUpdate(BaseModel):
    title: str | None = None
    meeting_date: date | None = None
    location: str | None = None
    attendees: str | None = None
    agenda: str | None = None
    notes: str | None = None
    summary: str | None = None
    speaker_names: dict[str, str] | None = None


class MeetingListItem(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    title: str
    meeting_date: date | None
    location: str
    status: str
    duration_seconds: int | None
    created_at: datetime
    owner_username: str = ""


class MeetingDetail(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    title: str
    meeting_date: date | None
    location: str
    attendees: str
    agenda: str
    notes: str
    language: str
    status: str
    error_message: str | None
    file_name: str | None
    file_size: int | None
    duration_seconds: int | None
    transcript: str | None
    segments: list[TranscriptSegment] | None = None
    speaker_names: dict[str, str] | None = None
    summary: str | None
    created_at: datetime
    updated_at: datetime
    decisions: list[DecisionOut]
    action_items: list[ActionItemOut]


class MeetingStatus(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    status: str
    error_message: str | None
