from typing import List, Dict, Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from ..database_oracle import get_db
from ..schemas_cognitive import (
    Question, StartResponse, AnswerItem, SubmitRequest, Result
)
from datetime import datetime

router = APIRouter(prefix="/cognitive", tags=["cognitive"])


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().casefold()

def _grade_from(score_pct: float) -> str:
    if score_pct >= 80:
        return "정상"
    if score_pct >= 60:
        return "주의"
    return "위험"

# - 세션 생성 + 질문 N개 조회

@router.get("/start", response_model=StartResponse)
def start_cognitive(
    user_id: int = Query(1, ge=1, description="임시 유저 ID (로그인 연동 전)"),
    count: int = Query(10, ge=1, le=50),
    category: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    # 1) 세션 생성
    try:
        ins = text("""
            INSERT INTO COGNITIVE_SESSION (USER_ID, STATUS)
            VALUES (:user_id, 'IN_PROGRESS')
            RETURNING SESSION_ID
        """)
        session_id_row = db.execute(ins, {"user_id": user_id}).fetchone()
        if not session_id_row:
            raise HTTPException(status_code=500, detail="세션 생성 실패")
        session_id = int(session_id_row[0])
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"세션 생성 오류: {e}")

    # 2) 질문 조회
    try:
        if category:
            sel = text("""
                SELECT QUESTION_ID, TEXT, CATEGORY
                  FROM COGNITIVE_QUESTION
                 WHERE CATEGORY = :category
                 FETCH FIRST :limit ROWS ONLY
            """)
            rows = db.execute(sel, {"category": category, "limit": count}).all()
        else:
            sel = text("""
                SELECT QUESTION_ID, TEXT, CATEGORY
                  FROM COGNITIVE_QUESTION
                 FETCH FIRST :limit ROWS ONLY
            """)
            rows = db.execute(sel, {"limit": count}).all()

        questions: List[Question] = []
        for i, (qid, txt, cat) in enumerate(rows, start=1):
            questions.append(
                Question(
                    questionNo=i,
                    questionId=int(qid),
                    text=str(txt or ""),
                    category=str(cat) if cat is not None else None
                )
            )

        # 질문이 하나도 없으면 더미로라도 내려줌 (프론트 개발 막힘 방지)
        if not questions:
            demo = [
                ("올해는 몇 년인가요?", "지남력"),
                ("오늘은 무슨 요일인가요?", "지남력"),
                ("사과와 배는 무엇의 종류인가요?", "언어"),
            ]
            for i, (q, cat) in enumerate(demo, start=1):
                questions.append(
                    Question(
                        questionNo=i,
                        questionId=0,  # 더미
                        text=q,
                        category=cat
                    )
                )

        return StartResponse(sessionId=session_id, questions=questions)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"질문 조회 오류: {e}")

# 답안 저장 + 점수 계산 + 세션 종료 업데이트

@router.post("/submit", response_model=Result)
def submit_cognitive(payload: SubmitRequest, db: Session = Depends(get_db)):
    # 0) 유효성
    if not payload.answers:
        raise HTTPException(status_code=400, detail="answers가 비어 있습니다.")

    # 1) 정답 사전 조회: {questionId: answer_text}
    qids = [a.questionId for a in payload.answers if a.questionId]
    answer_map: Dict[int, str] = {}
    if qids:
        try:
            sel = text("""
                SELECT QUESTION_ID, ANSWER
                  FROM COGNITIVE_QUESTION
                 WHERE QUESTION_ID IN :qids
            """)
            rows = db.execute(sel, {"qids": tuple(qids)}).all()
            answer_map = {int(r[0]): str(r[1] or "") for r in rows}
        except Exception as e:
            # 정답 조회 실패해도 진행은 가능(더미 0점 처리)
            answer_map = {}

    # 2) 각 문항에 대해 점수 계산 & COGNITIVE_ANSWER 저장
    per_q_score: Dict[int, float] = {}
    total = 0.0

    try:
        for item in payload.answers:
            expected = answer_map.get(item.questionId, "")
            # 간단 문자열 매칭 (운영 시 고도화)
            hit = 1.0 if _norm(item.sttText or item.typedText) == _norm(expected) and expected != "" else 0.0

            ins = text("""
                INSERT INTO COGNITIVE_ANSWER
                    (SESSION_ID, QUESTION_NO, QUESTION_ID, STT_TEXT, TYPED_TEXT, SCORE, LATENCY_MS)
                VALUES (:session_id, :qno, :qid, :stt, :typed, :score, :latency)
            """)
            db.execute(ins, {
                "session_id": payload.sessionId,
                "qno": item.questionNo,
                "qid": item.questionId if item.questionId else None,
                "stt": item.sttText,
                "typed": item.typedText,
                "score": hit * 100.0,   # 0 or 100 스케일
                "latency": item.latencyMs
            })
            per_q_score[item.questionNo] = hit * 100.0
            total += hit * 100.0

        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"답안 저장 오류: {e}")

    # 3) 세션 종료/총점 업데이트
    try:
        n = len(payload.answers)
        avg = total / n if n else 0.0
        upd = text("""
            UPDATE COGNITIVE_SESSION
               SET FINISHED_AT = SYSTIMESTAMP,
                   TOTAL_SCORE = :total_score,
                   STATUS = 'COMPLETED'
             WHERE SESSION_ID = :sid
        """)
        db.execute(upd, {"total_score": avg, "sid": payload.sessionId})
        db.commit()
    except Exception as e:
        db.rollback()
        # 답안은 저장됐으므로 결과는 내려주되 세션 업데이트 실패 알림
        raise HTTPException(status_code=500, detail=f"세션 업데이트 오류: {e}")

    grade = _grade_from(avg)
    summary = f"총 {n}문항 평균 {avg:.1f}점 → 등급 {grade}"

    return Result(
        totalScore=round(avg, 1),
        perQuestion=per_q_score,
        summary=summary,
        grade=grade
    )
