# test/app/router/quiz.py
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text, func, select
from datetime import datetime

from ..database_oracle import get_db
from ..services.quiz_scoring import choose_scoring, resolve_answer_tokens

router = APIRouter(prefix="/quiz", tags=["Cognitive Quiz"])

# ---------- Schemas ----------
class StartReq(BaseModel):
    user_id: int
    count: int = 10
    categories: Optional[List[str]] = None

class QuestionOut(BaseModel):
    question_no: int
    question_id: int
    text: str
    category: str

class StartRes(BaseModel):
    session_id: int
    questions: List[QuestionOut]

class AnswerIn(BaseModel):
    question_no: int
    question_id: int
    stt_text: Optional[str] = None
    typed_text: Optional[str] = None
    latency_ms: Optional[int] = None

class SubmitReq(BaseModel):
    user_id: int
    session_id: int
    answers: List[AnswerIn]

class ItemScore(BaseModel):
    question_no: int
    question_id: int
    score: float
    correct_answer: str
    feedback: str

class SubmitRes(BaseModel):
    session_id: int
    total_score: float
    max_score: float
    items: List[ItemScore]

class SessionResult(BaseModel):
    session_id: int
    user_id: Optional[int] = None
    total_score: Optional[float] = None
    status: Optional[str] = None
    finished_at: Optional[str] = None
    items: Optional[List[ItemScore]] = None

# ---------- Helpers ----------
def _create_session(db: Session, user_id: int) -> int:
    sid_out = db.execute(text("""
        INSERT INTO COGNITIVE_SESSION (USER_ID, STATUS)
        VALUES (:uid, 'IN_PROGRESS')
        RETURNING SESSION_ID INTO :sid
    """), {"uid": user_id, "sid": None})
    # SQLAlchemy + Oracle RETURNING 얻기: ORM 세팅에 따라 다름 → 안전하게 다시 조회
    row = db.execute(text("SELECT MAX(SESSION_ID) FROM COGNITIVE_SESSION WHERE USER_ID=:uid"),
                     {"uid": user_id}).scalar_one()
    db.commit()
    return int(row)

def _finish_session(db: Session, session_id: int, total_score: float):
    db.execute(text("""
        UPDATE COGNITIVE_SESSION
           SET FINISHED_AT = SYSTIMESTAMP,
               TOTAL_SCORE = :ts,
               STATUS = 'COMPLETED'
         WHERE SESSION_ID = :sid
    """), {"ts": float(total_score), "sid": int(session_id)})
    db.commit()

def _fetch_random_questions(db: Session, count: int, categories: Optional[List[str]]) -> List[dict]:
    if categories:
        # 바인딩 자리수 생성
        binds = ",".join([f":c{i}" for i in range(len(categories))])
        sql = text(f"""
            SELECT question_id, text, category, answer
              FROM (
                SELECT question_id, text, category, answer
                  FROM COGNITIVE_QUESTION
                 WHERE category IN ({binds})
                 ORDER BY dbms_random.value
              )
             WHERE ROWNUM <= :cnt
        """)
        params = {f"c{i}": cat for i, cat in enumerate(categories)}
        params["cnt"] = count
    else:
        sql = text("""
            SELECT question_id, text, category, answer
              FROM (
                SELECT question_id, text, category, answer
                  FROM COGNITIVE_QUESTION
                 ORDER BY dbms_random.value
              )
             WHERE ROWNUM <= :cnt
        """)
        params = {"cnt": count}

    rows = db.execute(sql, params).mappings().all()
    return [dict(r) for r in rows]

def _get_questions_map(db: Session, qids: List[int]) -> dict[int, dict]:
    if not qids: return {}
    binds = ",".join([f":q{i}" for i in range(len(qids))])
    sql = text(f"""
        SELECT question_id, text, category, answer
          FROM COGNITIVE_QUESTION
         WHERE question_id IN ({binds})
    """)
    params = {f"q{i}": q for i, q in enumerate(qids)}
    rows = db.execute(sql, params).mappings().all()
    return {int(r["question_id"]): dict(r) for r in rows}

def _insert_answer(db: Session, session_id: int, question_no: int, question_id: int,
                   stt_text: Optional[str], typed_text: Optional[str],
                   score: float, latency_ms: Optional[int]):
    # (SESSION_ID, QUESTION_NO) 중복 확인
    exists = db.execute(text("""
        SELECT 1 FROM COGNITIVE_ANSWER WHERE SESSION_ID=:sid AND QUESTION_NO=:qno
    """), {"sid": int(session_id), "qno": int(question_no)}).first()
    if exists:
        raise ValueError("duplicate")

    db.execute(text("""
        INSERT INTO COGNITIVE_ANSWER
          (SESSION_ID, QUESTION_NO, QUESTION_ID, STT_TEXT, TYPED_TEXT, SCORE, LATENCY_MS)
        VALUES
          (:sid, :qno, :qid, :stt, :typed, :score, :latency)
    """), {
        "sid": int(session_id),
        "qno": int(question_no),
        "qid": int(question_id),
        "stt": stt_text,
        "typed": typed_text,
        "score": float(score),
        "latency": int(latency_ms) if latency_ms is not None else None
    })
    db.commit()

# ---------- Endpoints ----------
@router.post("/start", response_model=StartRes)
def start_quiz(payload: StartReq, db: Session = Depends(get_db)):
    if payload.count <= 0 or payload.count > 50:
        raise HTTPException(400, "count는 1~50 사이여야 합니다.")

    sid = _create_session(db, payload.user_id)
    items = _fetch_random_questions(db, payload.count, payload.categories)

    out: List[QuestionOut] = []
    for idx, it in enumerate(items, start=1):
        out.append(QuestionOut(
            question_no=idx,
            question_id=int(it["question_id"]),
            text=it["text"],
            category=it["category"],
        ))

    return StartRes(session_id=sid, questions=out)

@router.post("/submit", response_model=SubmitRes)
def submit_quiz(payload: SubmitReq, db: Session = Depends(get_db)):
    if not payload.answers:
        raise HTTPException(400, "answers 비어있음")

    qids = list({a.question_id for a in payload.answers})
    qmap = _get_questions_map(db, qids)
    if len(qmap) != len(qids):
        raise HTTPException(400, "유효하지 않은 question_id 포함")

    total = 0.0
    max_score = float(len(payload.answers))
    items_out: List[ItemScore] = []

    for ans in payload.answers:
        meta = qmap.get(ans.question_id)
        if not meta:
            raise HTTPException(400, f"문항 {ans.question_id} 메타 정보 없음")

        user_text = ans.typed_text if ans.typed_text else ans.stt_text
        score, fb, correct_resolved = choose_scoring(
            meta["category"], meta["text"], meta["answer"], user_text or ""
        )

        try:
            _insert_answer(
                db, payload.session_id, ans.question_no, ans.question_id,
                ans.stt_text, ans.typed_text, float(score), ans.latency_ms
            )
        except ValueError:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail=f"이미 제출된 question_no={ans.question_no}")

        total += score
        items_out.append(ItemScore(
            question_no=ans.question_no,
            question_id=ans.question_id,
            score=round(float(score), 2),
            correct_answer=correct_resolved,
            feedback=fb
        ))

    _finish_session(db, payload.session_id, round(total, 2))
    return SubmitRes(
        session_id=payload.session_id,
        total_score=round(total, 2),
        max_score=round(max_score, 2),
        items=items_out
    )

@router.get("/result/{session_id}", response_model=SessionResult)
def get_result(session_id: int, db: Session = Depends(get_db)):
    s = db.execute(text("""
        SELECT SESSION_ID, USER_ID, TOTAL_SCORE, STATUS, FINISHED_AT
          FROM COGNITIVE_SESSION WHERE SESSION_ID=:sid
    """), {"sid": int(session_id)}).first()
    if not s:
        raise HTTPException(404, "세션 없음")

    # 답안 + 정답 복원
    rows = db.execute(text("""
        SELECT a.QUESTION_NO, a.QUESTION_ID, a.SCORE, q.ANSWER, q.TEXT, q.CATEGORY
          FROM COGNITIVE_ANSWER a
          JOIN COGNITIVE_QUESTION q ON q.QUESTION_ID = a.QUESTION_ID
         WHERE a.SESSION_ID = :sid
         ORDER BY a.QUESTION_NO
    """), {"sid": int(session_id)}).mappings().all()

    items: List[ItemScore] = []
    for r in rows:
        correct = resolve_answer_tokens(r["ANSWER"] or "")
        items.append(ItemScore(
            question_no=int(r["QUESTION_NO"]),
            question_id=int(r["QUESTION_ID"]),
            score=round(float(r["SCORE"]), 2),
            correct_answer=correct,
            feedback="기존 제출 점수"
        ))

    return SessionResult(
        session_id=int(s[0]),
        user_id=int(s[1]) if s[1] is not None else None,
        total_score=float(s[2]) if s[2] is not None else None,
        status=s[3],
        finished_at=s[4].isoformat() if s[4] else None,
        items=items
    )
