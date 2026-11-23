"""Text enums of the data model (docs/data-model.md §1).

The database stores each one as text with a `CHECK` constraint built from these classes
(`hub/db/models.py`), so the code and the schema cannot list different values.
"""

from enum import StrEnum


class TicketStatus(StrEnum):
    PROCESSING = "processing"
    PENDING_APPROVAL = "pending_approval"
    HANDED_OFF = "handed_off"
    ANSWERED = "answered"
    CLOSED = "closed"


class Channel(StrEnum):
    CHAT = "chat"


class Intent(StrEnum):
    """The 12 intents of triage (docs/ai-pipeline.md §5)."""

    PAYMENT_PENDING = "payment_pending"
    WITHDRAWAL_ISSUE = "withdrawal_issue"
    CARD_DECLINED = "card_declined"
    CARD_LOST_STOLEN = "card_lost_stolen"
    KYC_VERIFICATION = "kyc_verification"
    ACCOUNT_RESTRICTED = "account_restricted"
    FEES_FX = "fees_fx"
    LIMITS = "limits"
    DISPUTE_CHARGEBACK = "dispute_chargeback"
    LOGIN_SECURITY = "login_security"
    ACCOUNT_MANAGEMENT = "account_management"
    OTHER = "other"


class Urgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class Route(StrEnum):
    AUTO_REPLY = "auto_reply"
    APPROVAL = "approval"
    HANDOFF = "handoff"


class Resolution(StrEnum):
    AI = "ai"
    AI_APPROVED = "ai_approved"
    AI_EDITED = "ai_edited"
    HUMAN = "human"


class CustomerTier(StrEnum):
    STARTER = "starter"
    VERIFIED = "verified"
    BUSINESS = "business"


class OperatorRole(StrEnum):
    OPERATOR = "operator"
    LEAD = "lead"
    COMPLIANCE = "compliance"
    ADMIN = "admin"


class MessageRole(StrEnum):
    CUSTOMER = "customer"
    ASSISTANT = "assistant"
    OPERATOR = "operator"
    SYSTEM = "system"


class FeedbackRating(StrEnum):
    UP = "up"
    DOWN = "down"


class SettingKey(StrEnum):
    """Runtime settings changed from the console (data model §1, `settings`)."""

    AUTO_REPLY_ENABLED = "auto_reply_enabled"
    CONFIDENCE_THRESHOLD = "confidence_threshold"
    DAILY_BUDGET_USD = "daily_budget_usd"
    INTENT_ALLOWLIST = "intent_allowlist"


class ActorType(StrEnum):
    AI = "ai"
    OPERATOR = "operator"
    CUSTOMER = "customer"
    SYSTEM = "system"


class MemoryKind(StrEnum):
    """`customer_memory.kind` (data model §1, ai-pipeline §9)."""

    TICKET_SUMMARY = "ticket_summary"


class LlmCallContext(StrEnum):
    """Why a model call was made (data model §1, `llm_calls.context`)."""

    ONLINE = "online"
    EVAL = "eval"
    MEMORY = "memory"
    RECORD = "record"  # `make record-llm`: refreshing the fake-mode recordings


class LlmCallMode(StrEnum):
    """Which provider answered (data model §1, `llm_calls.mode`; P3-06). Only `real` rows were
    paid and count against the daily budget (G8): a `fake` row repeats the usage and cost of the
    recording it answered from."""

    REAL = "real"
    FAKE = "fake"


class LlmNode(StrEnum):
    """The step that made a model call (data model §1, `llm_calls.node`)."""

    TRIAGE = "triage"
    RESEARCH = "research"
    WRITE = "write"
    VERIFY = "verify"
    HANDOFF_PREP = "handoff_prep"
    MEMORY_SUMMARY = "memory_summary"
    JUDGE = "judge"
    EMBEDDING = "embedding"


class LlmCallStatus(StrEnum):
    """`retry`: this attempt failed and the gateway tries again; `error`: it failed for good
    (the gateway may still go on to the fallback model); `invalid_output`: the answer did not
    match the schema (P3-02)."""

    OK = "ok"
    RETRY = "retry"
    ERROR = "error"
    INVALID_OUTPUT = "invalid_output"


class RunStatus(StrEnum):
    """One execution of the workflow for one customer message (data model §1, `runs`)."""

    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"


class DraftStatus(StrEnum):
    """`superseded`: the operator answered with a free reply or closed the ticket without using
    the draft (data model §1, `drafts`)."""

    PENDING = "pending"
    APPROVED = "approved"
    EDITED = "edited"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class RejectReason(StrEnum):
    WRONG_FACTS = "wrong_facts"
    WRONG_TONE = "wrong_tone"
    MISSING_INFO = "missing_info"
    POLICY = "policy"
    OTHER = "other"


class RouteReason(StrEnum):
    """Reason codes of a route (docs/ai-pipeline.md §6, decision table; P6-02).

    Row 1 (hard rules, `policy_gate`) lists its codes in the order the rules are checked: when
    several match, every code goes into `route_reasons` in this order and the first one decides.
    """

    # row 1: hard rules, handoff
    POLICY_INTENT = "policy_intent"
    FRAUD_REPORT = "fraud_report"
    INJECTION = "injection"
    COMPLAINT = "complaint"
    CUSTOMER_REQUEST = "customer_request"
    ACTION_REQUEST = "action_request"
    RESTRICTED_ACCOUNT = "restricted_account"
    LANGUAGE = "language"
    # row 2: system state, handoff
    BUDGET_EXCEEDED = "budget_exceeded"
    SYSTEM_ERROR = "system_error"
    MASKING_FAILED = "masking_failed"
    # row 3, handoff
    NO_KB_ANSWER = "no_kb_answer"
    # rows 4-8, approval
    POLICY_VIOLATION = "policy_violation"
    UNSUPPORTED_CLAIMS = "unsupported_claims"
    DEGRADED = "degraded"
    POLICY_REVIEW = "policy_review"
    AUTO_REPLY_OFF = "auto_reply_off"
    LOW_CONFIDENCE = "low_confidence"
    # outside the graph: an operator claimed a ticket that was still `processing`
    OPERATOR_TAKEOVER = "operator_takeover"


class ToolCallStatus(StrEnum):
    """`tool_calls.status` (data model §1, P7-03): `denied` by the permissions (G6)."""

    OK = "ok"
    ERROR = "error"
    DENIED = "denied"


class ToolNode(StrEnum):
    """The graph nodes that call tools (`tool_calls.node`, security G6, P7-04)."""

    INTAKE = "intake"
    RESEARCH = "research"
    HANDOFF_PREP = "handoff_prep"
    ACT = "act"
    # Not a graph node: the console's account view reads the core tools for an operator
    # (P10-03 review, `GET /console/tickets/{id}/account`), recorded under the latest run.
    CONSOLE = "console"
