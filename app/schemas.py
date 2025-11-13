from pydantic import BaseModel, Field
from typing import Optional, Any, List
from datetime import date, datetime

# ---------- Users ----------
class UserCreate(BaseModel):
    phone: str
    name: str
    display_name: str

class UserOut(BaseModel):
    user_id: int
    phone: str
    name: str
    display_name: str
    created_at: datetime
    class Config: from_attributes = True

# ---------- Today ----------
class TodayCreate(BaseModel):
    entry_date: date
    mood_code: Optional[str] = None
    content: str

class TodayOut(BaseModel):
    entry_id: int
    user_id: int
    entry_date: date
    mood_code: Optional[str]
    content: str
    created_at: datetime
    class Config: from_attributes = True

# ---------- Questions ----------
class QuestionCreate(BaseModel):
    text: str
    category: Optional[str] = None
    answer: str

class QuestionOut(BaseModel):
    question_id: int
    text: str
    category: Optional[str]
    class Config: from_attributes = True

# ---------- Sessions / Answers ----------
class StartSessionOut(BaseModel):
    session_id: int

class AnswerIn(BaseModel):
    question_no: int
    question_id: int | None = None
    stt_text: Optional[str] = None
    typed_text: Optional[str] = None
    score: Optional[float] = None
    latency_ms: Optional[int] = None
    voice_vector: Optional[List[float]] = None  # 23ai VECTOR면 서버에서 변환 없이 그대로 바인딩됨(드라이버 지원 필요)

class SubmitAnswersIn(BaseModel):
    answers: List[AnswerIn]

class FinishOut(BaseModel):
    session_id: int
    total_score: Optional[float] = None
    status: str

class SessionOut(BaseModel):
    session_id: int
    user_id: int
    started_at: datetime
    finished_at: Optional[datetime]
    total_score: Optional[float]
    status: str
    class Config: from_attributes = True
