from sqlalchemy import (
    Column, Integer, String, Date, DateTime, Text, ForeignKey, Identity,
    Numeric, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

Base = declarative_base()

# ---------- (선택) Oracle 23ai VECTOR 타입 ----------
from sqlalchemy.types import UserDefinedType
class Vector768(UserDefinedType):
    def get_col_spec(self, **kw):
        return "VECTOR(768)"  # Oracle 23ai에서만 동작

# ========== APP_USER ==========
class AppUser(Base):
    __tablename__ = "APP_USER"
    user_id     = Column(Integer, Identity(start=1, increment=1), primary_key=True)
    phone       = Column(String(50), nullable=False, unique=True)
    name        = Column(String(200), nullable=False)
    display_name= Column(String(200), nullable=False)
    created_at  = Column(DateTime, nullable=False, default=datetime.utcnow)

    todays      = relationship("Today", back_populates="user", cascade="all, delete-orphan")
    sessions    = relationship("CognitiveSession", back_populates="user", cascade="all, delete-orphan")

# ========== TODAY ==========
class Today(Base):
    __tablename__ = "TODAY"
    entry_id    = Column(Integer, Identity(start=1, increment=1), primary_key=True)
    user_id     = Column(Integer, ForeignKey("APP_USER.USER_ID"), nullable=False)
    entry_date  = Column(Date, nullable=False)
    mood_code   = Column(String(50))
    content     = Column(Text, nullable=False)  # CLOB
    created_at  = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "entry_date", name="UQ_TODAY_USER_DATE"),
    )

    user        = relationship("AppUser", back_populates="todays")

# ========== COGNITIVE_SESSION ==========
class CognitiveSession(Base):
    __tablename__ = "COGNITIVE_SESSION"
    session_id  = Column(Integer, Identity(start=1, increment=1), primary_key=True)
    user_id     = Column(Integer, ForeignKey("APP_USER.USER_ID"), nullable=False)
    started_at  = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime)
    total_score = Column(Numeric(5, 2))
    status      = Column(String(30), nullable=False, default="IN_PROGRESS")

    user        = relationship("AppUser", back_populates="sessions")
    answers     = relationship("CognitiveAnswer", back_populates="session", cascade="all, delete-orphan")

# ========== COGNITIVE_QUESTION ==========
class CognitiveQuestion(Base):
    __tablename__ = "COGNITIVE_QUESTION"
    question_id = Column(Integer, Identity(start=1, increment=1), primary_key=True)
    text        = Column(Text, nullable=False)   # CLOB
    category    = Column(String(100))
    answer      = Column(Text, nullable=False)   # CLOB (정답)

    answers     = relationship("CognitiveAnswer", back_populates="question")

# ========== COGNITIVE_ANSWER ==========
class CognitiveAnswer(Base):
    __tablename__ = "COGNITIVE_ANSWER"
    answer_id   = Column(Integer, Identity(start=1, increment=1), primary_key=True)
    session_id  = Column(Integer, ForeignKey("COGNITIVE_SESSION.SESSION_ID"), nullable=False)
    question_no = Column(Integer, nullable=False)
    question_id = Column(Integer, ForeignKey("COGNITIVE_QUESTION.QUESTION_ID"))
    stt_text    = Column(Text)    # CLOB
    typed_text  = Column(Text)    # CLOB
    score       = Column(Numeric(5, 2))
    latency_ms  = Column(Integer)

    # 23ai라면:
    voice_vector = Column(Vector768)
    # 23ai가 아니면, 예) JSON으로:
    # from sqlalchemy import JSON
    # voice_vector = Column(JSON)   # [float, ...] 리스트를 그대로 저장

    created_at  = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("session_id", "question_no", name="UQ_ANSWER_SESSION_QNO"),
    )


    session     = relationship("CognitiveSession", back_populates="answers")
    question    = relationship("CognitiveQuestion", back_populates="answers")
