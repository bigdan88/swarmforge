"""Public response contracts. Keep database rows and internal configuration private."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PublicResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class WorkItem(PublicResponse):
    id: str
    title: str
    repo: str | None
    status: str
    stage: str | None
    run_id: str | None
    created_at: float | None
    updated_at: float | None
    finished_at: float | None
    reason: str | None
    pr_url: str | None
    archived: bool = False
    attempts: int = Field(default=1, ge=1)


class WorkPage(PublicResponse):
    items: list[WorkItem] = Field(max_length=100)
    total: int = Field(ge=0)
    counts: dict[str, int]  # Global counts, independent of the current filter.
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0, le=100000)
    attempts_total: int | None = Field(default=None, ge=0)  # matching attempts before folding


class RunCost(PublicResponse):
    calls: int
    reported_usd: float
    estimated_usd: float
    unavailable_calls: int


class Allowance(PublicResponse):
    attempt: int
    budget_usd: float
    spent_usd: float
    reserved_usd: float
    available_usd: float
    deadline: float
    last_progress_at: float
    current_action: str | None
    last_result: str | None
    next_action: str | None
    stop_reason: str | None


class RecordedEvent(PublicResponse):
    id: int
    ts: float
    level: str
    kind: str
    message: str | None
    truncated: bool


class AcceptanceCheck(PublicResponse):
    id: int
    cycle: int | None
    criterion_id: str | None
    text: str | None
    status: str | None
    evidence: str | None
    truncated: bool


class RecordedTest(PublicResponse):
    id: int
    ts: float
    cycle: int | None
    label: str | None
    exit_code: int | None
    passed: int | None
    failed: int | None
    errors: int | None
    skipped: int | None
    timed_out: bool


class ExecutionTask(PublicResponse):
    task_key: str
    cycle: int
    kind: str
    title: str | None
    role: str | None
    model: str | None
    state: str
    depends_on: list[str] = Field(max_length=50)
    dependencies_complete: bool
    expected_files: list[str] = Field(max_length=20)
    started_at: float | None
    ended_at: float | None
    attempts: int
    summary: str | None
    error: str | None
    truncated: bool


class ExecutionBot(PublicResponse):
    role: str | None
    provider: str | None
    model: str | None
    calls: int
    tokens: int
    known_cost_usd: float
    unavailable_calls: int
    errors: int
    first_call_at: float | None
    last_call_at: float | None


class ExecutionPhase(PublicResponse):
    id: int
    ts: float
    state: str


class WorkExecution(PublicResponse):
    mode: str
    repair_cycle: int
    counts: dict[str, int]
    tasks: list[ExecutionTask] = Field(max_length=200)
    tasks_has_more: bool
    bots: list[ExecutionBot] = Field(max_length=40)
    bots_has_more: bool
    phases: list[ExecutionPhase] = Field(max_length=100)
    phases_has_more: bool


class WorkEvidence(PublicResponse):
    item_id: str
    run_id: str | None
    costs: RunCost | None
    allowance: Allowance | None
    checks: list[AcceptanceCheck] = Field(max_length=100)
    checks_has_more: bool
    tests: list[RecordedTest] = Field(max_length=25)
    tests_has_more: bool
    events: list[RecordedEvent] = Field(max_length=100)
    next_event_id: int
    events_has_more: bool
    execution: WorkExecution | None = None


class Screenshot(PublicResponse):
    name: str
    size_bytes: int


class WorkArtifacts(PublicResponse):
    item_id: str
    run_id: str | None
    report_status: Literal["missing", "available", "unavailable"]
    report_bytes: int | None
    report_truncated: bool
    screenshots: list[Screenshot] = Field(max_length=24)
    screenshots_truncated: bool
    screenshots_unavailable: bool


class RetainedReport(PublicResponse):
    item_id: str
    run_id: str
    text: str
    size_bytes: int
    truncated: bool


class ActionCommand(PublicResponse):
    operation: Literal["cancel_work", "retry_work", "decide_approval", "queue_task", "start_queue", "pause_queue",
                       "start_project", "create_project", "save_project", "amend_project", "retry_project_lead", "extend_project_task", "launch_project", "pause_project", "cancel_project", "resume_project", "request_project_changes", "launch_project_changes", "launch_chat_task",
                       "create_conversation", "rename_conversation", "send_message", "project_message",
                       "create_note", "save_note", "delete_note", "diagnose_failure", "merge_pr", "update_pr",
                       "mark_features_read", "set_archived", "save_settings", "test_models"]
    target: str = Field(default="", max_length=128)
    args: dict = Field(default_factory=dict)


class UploadCommand(PublicResponse):
    name: str = Field(min_length=1, max_length=200)
    data: str = Field(min_length=1, max_length=7_000_000)


class ActionResult(PublicResponse):
    ok: bool
    message: str
    item_id: str | None = None
    project_id: str | None = None
    conversation_id: str | None = None
    draft_ready: bool | None = None
    note_id: str | None = None
    details: list[dict[str, str | bool | float | None]] | None = Field(default=None, max_length=50)


class ProjectSummary(PublicResponse):
    id: str
    name: str
    repo: str
    status: str
    current_milestone: int
    budget_usd: float
    spent_usd: float
    created_at: float
    approved_at: float | None
    last_error: str | None
    conversation_id: str


class ProjectPage(PublicResponse):
    items: list[ProjectSummary] = Field(max_length=100)
    total: int
    limit: int
    offset: int


class Attachment(PublicResponse):
    id: str
    name: str
    kind: Literal["image", "text"]
    size: int


class Message(PublicResponse):
    id: int
    role: str
    content: str
    created_at: float
    truncated: bool
    attachments: list[Attachment] = Field(default_factory=list, max_length=6)
    actions: dict = Field(default_factory=dict)
    launched_tasks: dict[str, str] = Field(default_factory=dict)
    files: list[dict] = Field(default_factory=list, max_length=8)


class ChatStep(PublicResponse):
    tool: str
    ok: bool
    at: float


class ChatRequest(PublicResponse):
    id: str
    status: Literal["thinking", "done", "failed", "interrupted"]
    question: str
    steps: list[ChatStep] = Field(max_length=40)
    error: str | None
    started_at: float
    finished_at: float | None
    attachments: list[Attachment] = Field(max_length=6)
    partial_text: str = ""
    assistant_message_id: int | None = None


class ProjectMilestone(PublicResponse):
    idx: int
    name: str
    status: str
    estimate_usd: float
    spent_usd: float
    started_at: float | None
    finished_at: float | None
    report: str | None


class ProjectEvent(PublicResponse):
    id: int
    ts: float
    kind: str
    detail: str


class ProjectDetail(PublicResponse):
    project: ProjectSummary
    spec: str
    spec_sha: str
    definition: dict
    truncated: bool
    milestones: list[ProjectMilestone] = Field(max_length=6)
    tasks: list[WorkItem] = Field(max_length=100)
    events: list[ProjectEvent] = Field(max_length=50)
    messages: list[Message] = Field(max_length=100)
    requests: list[ChatRequest] = Field(default_factory=list, max_length=5)
    recovery: dict = Field(default_factory=dict)


class ConversationSummary(PublicResponse):
    id: str
    title: str | None
    created_at: float
    updated_at: float | None


class ConversationPage(PublicResponse):
    items: list[ConversationSummary] = Field(max_length=100)
    total: int
    limit: int
    offset: int


class ConversationDetail(PublicResponse):
    conversation: ConversationSummary
    messages: list[Message] = Field(max_length=100)
    costs: dict[str, float | int | None]
    requests: list[ChatRequest] = Field(default_factory=list, max_length=5)


class ApprovalSummary(PublicResponse):
    id: int
    question: str
    truncated: bool


class WorkControls(PublicResponse):
    can_cancel: bool
    can_retry: bool
    project_id: str | None
    approvals: list[ApprovalSummary] = Field(max_length=20)


class Lane(PublicResponse):
    name: str
    status: str
    count: int


class ProjectBlocker(PublicResponse):
    id: str
    name: str
    status: str
    reason: str | None = None


class QueueSnapshot(PublicResponse):
    paused: bool
    worker_running: bool
    lanes: list[Lane] = Field(max_length=100)
    queued_count: int = 0
    blocked_projects: list[ProjectBlocker] = Field(default_factory=list, max_length=20)


class NoteSummary(PublicResponse):
    id: str
    title: str
    excerpt: str
    type: str = "user-note"
    repo: str | None = None
    folder: str | None = None


class NotesPage(PublicResponse):
    items: list[NoteSummary] = Field(max_length=25)
    total: int
    limit: int
    offset: int
    configured: bool
    limited: bool
    types: list[str] = Field(default_factory=list, max_length=50)
    repos: list[str] = Field(default_factory=list, max_length=200)


class NoteDetail(PublicResponse):
    id: str
    title: str
    text: str
    size_bytes: int
    truncated: bool
    type: str = "user-note"
    repo: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=30)
    folder: str | None = None
    source: str | None = None
    created: str | None = None
    sha: str | None = None
    editable: bool = False


class FailureSummary(PublicResponse):
    run_id: str
    created_at: float
    title: str | None
    repo: str | None
    state: str | None
    category: str | None
    category_label: str
    root_cause: str | None
    outcome: str | None
    cost_usd: float | None
    heal_run_id: str | None
    work_item: str


class FailurePage(PublicResponse):
    items: list[FailureSummary] = Field(max_length=25)
    total: int
    limit: int
    offset: int
    counts: dict[str, int]
    categories: list[dict[str, str | int]] = Field(max_length=50)
    undiagnosed: int


class FailureDetail(FailureSummary):
    reason: str | None
    fix: str | None
    lesson: str | None
    evidence: str | None
    evidence_truncated: bool
    diagnosed_by: str | None
    heal_work_item: str | None
