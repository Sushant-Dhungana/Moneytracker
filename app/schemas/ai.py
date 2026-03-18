from pydantic import BaseModel, Field


class PersonalChatRequest(BaseModel):
    message: str = Field(min_length=1)
    thread_id: str | None = None
    stream: bool = False


class BusinessChatRequest(BaseModel):
    message: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    thread_id: str | None = None
    stream: bool = False


class ChatResponse(BaseModel):
    thread_id: str
    reply: str
    streamed: bool = False
    usage: dict | None = None
    warnings: list[str] | None = None


class ChatThreadItem(BaseModel):
    thread_id: str
    scope: str
    profile_id: str | None = None
    title: str | None = None
    message_count: int = 0
    created_at: str
    updated_at: str


class ChatThreadsResponse(BaseModel):
    items: list[ChatThreadItem]


class ChatMessageItem(BaseModel):
    id: int
    role: str
    content: str
    created_at: str


class ChatMessagesResponse(BaseModel):
    thread_id: str
    items: list[ChatMessageItem]
