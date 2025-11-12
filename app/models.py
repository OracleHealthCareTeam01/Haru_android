from sqlalchemy import (
    Column, Integer, String, Date, DateTime, Text, ForeignKey, Identity,
    Numeric, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

Base = declarative_base()

# ---------- (선택) Oracle 23ai VECTOR 타입 ----------
# 23ai가 아니면 사용하지 않거나 주석 처리하세요.
from sqlalchemy.types import UserDefinedType
class Vector768(UserDefinedType):
    def get_col_spec(self, **kw):
        return "VECTOR(768)"  # Oracle 23ai에서만 동작


# ========== APP_USER ==========
class AppUser(Base):
    __tablename__ = "APP_USER"
    user_id      = Column(Integer, Identity(start=1, increment=1), primary_key=True)
    phone        = Column(String(50), nullable=False, unique=True)
    name         = Column(String(200), nullable=False)
    display_name = Column(String(200), nullable=False)
    created_at   = Column(DateTime, nullable=False, default=datetime.utcnow)

    todays   = relationship("Today", back_populates="user", cascade="all, delete-orphan")
    sessions = relationship("CognitiveSession", back_populates="user", cascade="all, delete-orphan")


# ========== TODAY ==========
class Today(Base):
    __tablename__ = "TODAY"
    entry_id   = Column(Integer, Identity(start=1, increment=1), primary_key=True)
    user_id    = Column(Integer, ForeignKey("APP_USER.USER_ID"), nullable=False)
    entry_date = Column(Date, nullable=False)
    mood_code  = Column(String(50))
    content    = Column(Text, nullable=False)  # CLOB
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "entry_date", name="UQ_TODAY_USER_DATE"),
    )

    user = relationship("AppUser", back_populates="todays")


# ========== COGNITIVE_SESSION ==========
class CognitiveSession(Base):
    __tablename__ = "COGNITIVE_SESSION"
    session_id  = Column(Integer, Identity(start=1, increment=1), primary_key=True)
    user_id     = Column(Integer, ForeignKey("APP_USER.USER_ID"), nullable=False)
    started_at  = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime)
    total_score = Column(Numeric(5, 2))
    status      = Column(String(30), nullable=False, default="IN_PROGRESS")

    user    = relationship("AppUser", back_populates="sessions")
    answers = relationship("CognitiveAnswer", back_populates="session", cascade="all, delete-orphan")


# ========== COGNITIVE_QUESTION  → 기존 QUESTIONS 테이블에 매핑 ==========
class CognitiveQuestion(Base):
    """
    기존 문제은행 테이블 구조에 맞춰 매핑:
      - __tablename__ = "QUESTIONS"
      - QUESTION_ID NUMBER (PK)
      - TEXT        CLOB   (NOT NULL)
      - CATEGORY    VARCHAR2(30)
      - ANSWER      CLOB
    주의: 이미 존재하는 테이블이므로 Identity/Sequence를 새로 걸지 않습니다.
    """
    __tablename__ = "QUESTIONS"

    # 이미 존재하는 PK라서 autoincrement는 DB가 관리합니다.
    question_id = Column("QUESTION_ID", Integer, primary_key=True)  # autoincrement=False 의미
    text        = Column("TEXT",      Text,    nullable=False)       # CLOB
    category    = Column("CATEGORY",  String(30))
    answer      = Column("ANSWER",    Text)                          # CLOB

    answers = relationship("CognitiveAnswer", back_populates="question")


# ========== COGNITIVE_ANSWER ==========
class CognitiveAnswer(Base):
    __tablename__ = "COGNITIVE_ANSWER"
    answer_id   = Column(Integer, Identity(start=1, increment=1), primary_key=True)
    session_id  = Column(Integer, ForeignKey("COGNITIVE_SESSION.SESSION_ID"), nullable=False)
    question_no = Column(Integer, nullable=False)
    # 기존 QUESTIONS 테이블을 참조하도록 FK 수정
    question_id = Column(Integer, ForeignKey("QUESTIONS.QUESTION_ID"))

    stt_text   = Column(Text)    # CLOB
    typed_text = Column(Text)    # CLOB
    score      = Column(Numeric(5, 2))
    latency_ms = Column(Integer)

    # 23ai라면:
    voice_vector = Column(Vector768)  # nullable 기본 True
    # 23ai가 아니면, 예: JSON 컬럼을 쓰거나(별도) 생략하세요.

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("session_id", "question_no", name="UQ_ANSWER_SESSION_QNO"),
    )

    session  = relationship("CognitiveSession", back_populates="answers")
    question = relationship("CognitiveQuestion", back_populates="answers")
