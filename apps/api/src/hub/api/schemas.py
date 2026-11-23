"""Request and response models of the customer endpoints (docs/api.md §2).

Customer responses carry only what the chat shows. Internal fields (chunk ids, scores,
confidence, route reasons, intent) never appear here.
"""

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from hub.api.limits import MAX_MESSAGE_CHARS
from hub.domain.enums import CustomerTier, FeedbackRating, MessageRole, TicketStatus

# Security §7 (EU AI Act Article 50): shown at the start of every conversation.
AI_DISCLOSURE = (
    "You are chatting with Tallybridge's AI assistant. It can make mistakes. "
    "You can ask for a human at any time."
)

MAX_FEEDBACK_COMMENT_CHARS = 1_000

# Length is counted in characters (code points), as the chat counter does (docs/ui.md §2).
# `strip_whitespace` is not used: the stored text stays what the customer wrote (masked).
# NUL is refused here (422): Postgres text cannot store it, so it would otherwise fail at the
# INSERT with a 500.
MessageText = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=MAX_MESSAGE_CHARS, pattern=r"^[^\x00]*[^\x00\s][^\x00]*$"
    ),
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DemoSessionIn(_Strict):
    customer_ref: Annotated[str, StringConstraints(pattern=r"^CUS-\d{4}$")]
    access_code: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class CustomerProfileOut(BaseModel):
    customer_ref: str
    display_name: str
    tier: CustomerTier


class DemoCustomerOut(CustomerProfileOut):
    """A card of the chat's customer picker (UI §2)."""

    hint: str
    suggestions: list[str]


class MessageIn(_Strict):
    message: MessageText


class FeedbackIn(_Strict):
    message_id: uuid.UUID
    rating: FeedbackRating
    comment: (
        Annotated[
            str,
            StringConstraints(max_length=MAX_FEEDBACK_COMMENT_CHARS, pattern=r"^[^\x00]*$"),
        ]
        | None
    ) = None


class CitationOut(BaseModel):
    marker: int
    title: str
    url: str


class MessageOut(BaseModel):
    id: uuid.UUID
    role: MessageRole
    author_label: str
    is_ai_generated: bool
    body: str
    citations: list[CitationOut]
    # True when the server hid card data in this message; the chat shows a note (docs/ui.md §2).
    masked: bool
    created_at: datetime


class TicketOut(BaseModel):
    id: uuid.UUID
    status: TicketStatus
    ai_disclosure: str = AI_DISCLOSURE
    ai_disclosed_at: datetime
    created_at: datetime


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


class FeedbackOut(BaseModel):
    id: uuid.UUID


class TicketCreatedEvent(BaseModel):
    ticket_id: uuid.UUID
    message: MessageOut


# A help-centre link the server builds: `/help/<doc_id>` or `/help/<doc_id>#<anchor>` (data
# model §2; anchors from `hub/kb/sections.py`). Nothing else is ever a link for the chat (T13).
_HELP_URL = re.compile(r"/help/[a-z0-9][a-z0-9-]*(#[\w-]+)?")


def safe_citation_url(url: object) -> str:
    """The url when it is a relative help-centre link, else "" (the chat shows no link). The
    urls are built by the server from search hits, so this is a second line (P8-04): a stored
    absolute, `javascript:` or protocol-relative url never reaches the customer."""
    return url if isinstance(url, str) and _HELP_URL.fullmatch(url) else ""


def citations_for_customer(stored: list[Any]) -> list[CitationOut]:
    """Only marker, title and url: chunk and document ids are internal."""
    return [
        CitationOut(marker=item["marker"], title=item["title"], url=safe_citation_url(item["url"]))
        for item in stored
        if isinstance(item, dict)
    ]


SseEventName = Literal["ticket.created", "message", "done"]
