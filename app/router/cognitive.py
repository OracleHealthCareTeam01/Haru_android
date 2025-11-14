import logging
import random
import sys
import json
from typing import List, Dict, Optional
from datetime import datetime

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, text

from ..models import CognitiveSession, CognitiveQuestion, CognitiveAnswer
from ..database_oracle import get_db
from ..schemas_cognitive import (
    Question, StartResponse, AnswerItem, SubmitRequest, Result
)

# ----------------------------------------------------------------------
# 로깅 설정하기
# ----------------------------------------------------------------------
LOG_FORMAT = "%(levelname)s: [%(asctime)s] - [%(module)s] => %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)
logger = logging.getLogger(__name__)

# Router 선언 (이 파일의 API 주소는 모두 /cognitive 로 시작)
router = APIRouter(prefix="/cognitive", tags=["cognitive"])


def _norm(s: Optional[str]) -> str:
    """문자열을 비교하기 전에 앞뒤 공백 제거하고 모두 소문자로 바꿔요"""
    return (s or "").strip().casefold()


def _grade_from(score_pct: float) -> str:
    """평균 점수로 등급을 간단히 판단해요"""
    if score_pct >= 80:
        return "정상"
    if score_pct >= 60:
        return "주의"
    return "위험"


def _select_question_ids_with_select_ai(
    db: Session,
    count: int,
    category: Optional[str]
) -> List[int]:
    """
    Oracle 23ai SELECT AI를 사용해서
    cognitive_question 테이블에 이미 존재하는 문제들 중에서
    questionId 들만 골라오는 함수.

    반환: [questionId1, questionId2, ...] (최대 count개)
    """

    # 1) 이 세션에서 사용할 AI 프로필 설정 (환경에 맞게 프로필 이름 조정 가능)
    try:
        db.execute(text("BEGIN DBMS_CLOUD_AI.SET_PROFILE('COGNITIVE_AI'); END;"))
    except Exception as e:
        logger.error(f"[SELECT AI] SET_PROFILE 실패: {e}", exc_info=True)
        raise

    # 2) 후보 문제 목록 조회 (기존 cognitive_question에서만!)
    query = db.query(
        CognitiveQuestion.question_id,
        CognitiveQuestion.category,
        CognitiveQuestion.text
    )

    if category:
        query = query.filter(CognitiveQuestion.category == category)

    candidates = query.all()

    if not candidates:
        raise ValueError("cognitive_question에 사용 가능한 문제가 없습니다.")

    # 후보가 count보다 적으면, 그냥 전부 쓰도록 AI에게 안내하긴 하지만
    # 최종적으로는 Python에서도 한번 더 방어적으로 처리할 예정
    logger.info(f"[SELECT AI] 후보 문제 개수: {len(candidates)}")

    candidates_payload = [
        {
            "questionId": int(row.question_id),
            "category": str(row.category or ""),
            "text": str(row.text or ""),
        }
        for row in candidates
    ]

    candidates_json = json.dumps(candidates_payload, ensure_ascii=False)

    # 3) 프롬프트 구성
    prompt = f"""
당신은 병원에서 사용하는 한국어 인지기능 검사를 설계하는 전문가입니다.

다음은 이미 데이터베이스에 저장되어 있는 인지검사 문제 후보 목록입니다:

candidates = {candidates_json}

당신의 역할:
- 이 후보들 중에서 총 {count}개의 서로 다른 문항을 선택하세요.
- 가능한 한 다양한 카테고리(지남력, 주의력, 언어능력, 기억력, 회상력)를 고르게 포함하도록 노력하세요.
- 난이도는 전체적으로 중간 수준이 되도록 선택하세요.
- 같은 문제를 중복해서 선택하지 마세요.
- 후보에 없는 questionId는 절대 사용하지 마세요.

반환 형식:
- 선택한 문제의 questionId만 담긴 JSON 배열을 반환합니다.
- 예시: [6, 21, 3, 10]
- 설명 문장이나 다른 텍스트는 절대 포함하지 마세요.
"""

    # 4) SELECT AI 호출
    try:
        sql = text("SELECT AI NARRATE :prompt")
        raw_response = db.execute(sql, {"prompt": prompt}).scalar()
        logger.info(f"[SELECT AI] raw_response (앞 200자): {str(raw_response)[:200]}...")
    except Exception as e:
        logger.error(f"[SELECT AI] 호출 실패: {e}", exc_info=True)
        raise

    # 5) JSON 파싱
    try:
        data = json.loads(raw_response)
    except json.JSONDecodeError as e:
        logger.error(f"[SELECT AI] JSON 파싱 실패: {e} / raw={raw_response}", exc_info=True)
        raise

    if not isinstance(data, list):
        raise ValueError("SELECT AI 응답이 JSON 배열이 아닙니다. 응답: " + str(data))

    # 6) 후보 id 검증 및 클리닝
    candidate_ids = {int(c["questionId"]) for c in candidates_payload}
    selected_ids: List[int] = []

    for idx, item in enumerate(data, start=1):
        # 리스트 요소가 그냥 숫자인 경우, {"questionId": ...}인 경우 둘 다 허용
        if isinstance(item, dict):
            qid = item.get("questionId")
        else:
            qid = item

        try:
            qid_int = int(qid)
        except (TypeError, ValueError):
            logger.warning(f"[SELECT AI] {idx}번째 항목에서 questionId 추출 실패: {item}")
            continue

        if qid_int not in candidate_ids:
            logger.warning(f"[SELECT AI] 후보에 없는 questionId 선택됨: {qid_int}")
            continue

        if qid_int in selected_ids:
            logger.warning(f"[SELECT AI] 중복 questionId 선택됨: {qid_int}")
            continue

        selected_ids.append(qid_int)
        if len(selected_ids) >= count:
            break

    # 후보 수가 적거나, AI가 적게 골랐을 경우 보정
    if len(selected_ids) < min(count, len(candidate_ids)):
        logger.warning(
            f"[SELECT AI] 선택된 questionId가 부족합니다. "
            f"선택={len(selected_ids)}, 목표={count}, 후보={len(candidate_ids)}"
        )
        # 부족한 만큼 남은 후보에서 랜덤으로 채우기
        remaining_ids = list(candidate_ids - set(selected_ids))
        random.shuffle(remaining_ids)
        need = min(count - len(selected_ids), len(remaining_ids))
        selected_ids.extend(remaining_ids[:need])

    if not selected_ids:
        raise ValueError("SELECT AI에서 유효한 questionId를 하나도 선택하지 못했습니다.")

    # 최종적으로 count개까지만 자르기
    return selected_ids[:count]


@router.get("/start", response_model=StartResponse)
def start_cognitive(
    user_id: int = Query(1, ge=1, description="임시 유저 ID (로그인 연동 전)"),
    count: int = Query(10, description="총 문제 수"),
    category: Optional[str] = Query(None, description="특정 카테고리만 지정 (보통 None)"),
    db: Session = Depends(get_db),
):
    """
    인지 능력 검사 시작
    :param user_id:
    :param count:
    :param category:
    :param db:
    :return:
    """
    logger.info(
        f"REQ => /start: API 시작. [user_id={user_id}, count={count}, category={category}]"
    )
    session_id: int = 0

    # --- 1단계: 세션 생성 ---
    try:
        logger.info(f"[세션생성] user_id={user_id}의 새 CognitiveSession 생성 시도...")
        new_session = CognitiveSession(
            user_id=user_id,
            status="IN_PROGRESS",
            started_at=datetime.utcnow(),  # DB 시간은 표준시(UTC)로 저장
        )
        db.add(new_session)
        db.flush()  # session_id 확보용

        session_id = int(new_session.session_id)

        db.commit()
        logger.info(
            f"[세션 생성] user_id={user_id} 생성 성공! [session_id={session_id}]"
        )

    except Exception as e:
        logger.error(
            f"[세션 생성] user_id={user_id} 생성 실패! [Error: {e}]",
            exc_info=True,
        )
        db.rollback()
        raise HTTPException(status_code=500, detail=f"세션 생성 오류: {e}")

    # --- 2단계: 질문 조회 / 선택 ---
    try:
        logger.info(f"[질문 조회] session_id={session_id}의 질문 {count}개 뽑기 시작...")

        # 최종 문제들을 담을 리스트
        question_objects: List[CognitiveQuestion] = []

        # 2-1. 먼저 SELECT AI로 cognitive_question에서 questionId를 고르게 시도
        selected_ids: List[int] = []
        try:
            logger.info(
                f"[SELECT AI] user_id={user_id}, count={count}, category={category} 질문 선택 시도..."
            )
            selected_ids = _select_question_ids_with_select_ai(db, count, category)
            logger.info(f"[SELECT AI] 선택된 questionId 목록: {selected_ids}")

            # 선택된 ID들로 cognitive_question에서 실제 문제 객체들을 가져옴
            if selected_ids:
                rows = (
                    db.query(CognitiveQuestion)
                    .filter(CognitiveQuestion.question_id.in_(selected_ids))
                    .all()
                )
                # id -> 객체 매핑
                row_map = {int(r.question_id): r for r in rows}
                # AI가 준 순서를 유지하면서 리스트 만들기
                for qid in selected_ids:
                    if qid in row_map:
                        question_objects.append(row_map[qid])

            logger.info(
                f"[SELECT AI] cognitive_question에서 실제 문제 {len(question_objects)}개 조회 완료."
            )

        except Exception as e:
            logger.error(
                f"[SELECT AI] 질문 선택 실패, 기존 로직으로 fallback 합니다. Error: {e}",
                exc_info=True,
            )
            question_objects = []

        # 2-2. SELECT AI가 실패했거나 0개만 만든 경우: 기존 랜덤 로직 사용
        if not question_objects:
            if category:
                logger.info(
                    f"[Fallback] 특정 카테고리 '{category}'만 {count}개 무작위로 뽑습니다."
                )
                question_objects = (
                    db.query(CognitiveQuestion)
                    .filter(CognitiveQuestion.category == category)
                    .order_by(func.dbms_random.value())
                    .limit(count)
                    .all()
                )
            else:
                logger.info(f"[Fallback] 기존 검사 생성 로직 사용 START!")

                # 1. '기억력-회상력' mapping
                #    (Q6-Q21, Q7-Q22, Q8-Q23, Q9-Q24, Q10-Q25)
                set_id = random.randint(1, 5)
                logger.info(
                    f"[Fallback] user_id={user_id} 기억력/회상력 세트 '{set_id}번' 선택"
                )

                mem_q_id = 5 + set_id
                rec_q_id = 20 + set_id

                pair_questions = (
                    db.query(CognitiveQuestion)
                    .filter(
                        CognitiveQuestion.question_id.in_([mem_q_id, rec_q_id])
                    )
                    .all()
                )
                question_objects.extend(pair_questions)
                logger.info(
                    f"[Fallback] 기억력, 회상력 생성 (Q_ID: {mem_q_id}, {rec_q_id})"
                )

                # 2. 나머지 문제 (총 count - 2개)
                remaining_count = count - 2
                if remaining_count > 0:
                    other_cats = ["지남력", "주의력", "언어능력"]

                    n_per_cat = remaining_count // len(other_cats)
                    remainder = remaining_count % len(other_cats)

                    needs_map = {cat: n_per_cat for cat in other_cats}
                    for i in range(remainder):
                        needs_map[other_cats[i]] += 1

                    logger.info(
                        f"[Fallback] 나머지 {remaining_count}개 배분: {needs_map}"
                    )

                    for cat_name, needed_count in needs_map.items():
                        cat_questions = (
                            db.query(CognitiveQuestion)
                            .filter(CognitiveQuestion.category == cat_name)
                            .order_by(func.dbms_random.value())
                            .limit(needed_count)
                            .all()
                        )
                        question_objects.extend(cat_questions)
                        logger.info(
                            f"[Fallback] '{cat_name}' 카테고리 {len(cat_questions)}개 확보."
                        )

                random.shuffle(question_objects)
                logger.info(
                    f"[Fallback] 총 {len(question_objects)}개 문제 확보 및 순서 섞기 완료."
                )

        # --- 3단계: 응답용 Pydantic 리스트 + 빈 답안지 생성 ---
        questions_for_response: List[Question] = []

        logger.info(
            f"[Step 3] session_id={session_id}에 대한 '빈 답안지' {len(question_objects)}개 생성 시작..."
        )

        for i, q_obj in enumerate(question_objects, start=1):
            # 응답용 질문 리스트
            questions_for_response.append(
                Question(
                    questionNo=i,
                    questionId=int(q_obj.question_id),
                    text=str(q_obj.text or ""),
                    category=str(q_obj.category)
                    if q_obj.category is not None
                    else None,
                )
            )

            # DB 저장용 빈 CognitiveAnswer 생성
            new_blank_answer = CognitiveAnswer(
                session_id=session_id,
                question_no=i,
                question_id=int(q_obj.question_id),
                created_at=datetime.utcnow(),
                voice_vector=None,
            )
            db.add(new_blank_answer)

        # 질문이 하나도 없으면 더미 질문 (이 경우엔 빈 답안지 저장 안 함)
        if not question_objects:
            logger.warning(
                f"[질문 백업] user_id={user_id} DB에 질문이 없음! [session_id={session_id}] 더미 질문 반환."
            )
            demo = [
                ("올해는 몇 년인가요?", "지남력"),
                ("오늘은 무슨 요일인가요?", "지남력"),
            ]
            for i, (q_text, cat) in enumerate(demo, start=1):
                questions_for_response.append(
                    Question(questionNo=i, questionId=0, text=q_text, category=cat)
                )
        else:
            db.commit()
            logger.info(
                f"[질문 백업] user_id={user_id} {len(question_objects)}개 DB 저장 완료."
            )

        # --- 최종 응답 ---
        logger.info(
            f"RES => user_id={user_id} /start: 성공! [session_id={session_id}] "
            f"질문 {len(questions_for_response)}개 반환."
        )
        logger.info(
            f"RES => user_id={user_id} {StartResponse(sessionId=session_id, questions=questions_for_response)}"
        )
        return StartResponse(sessionId=session_id, questions=questions_for_response)

    except Exception as e:
        logger.error(
            f"[질문 백업] user_id={user_id} 질문 조회/백업 생성 실패! [Error: {e}]",
            exc_info=True,
        )
        db.rollback()
        raise HTTPException(status_code=500, detail=f"질문 조회/빈 답안 생성 오류: {e}")


# ---------------- SUBMIT 엔드포인트 (ORM 방식) ----------------
@router.post("/submit", response_model=Result)
def submit_cognitive(payload: SubmitRequest, db: Session = Depends(get_db)):
    """
    클라이언트에서 보낸 답안을 저장하고 채점한 뒤 결과를 반환합니다.
    models.py의 ORM 클래스를 사용합니다.
    """

    # 0) 유효성 검사: answers가 비어있으면 오류
    if not payload.answers:
        raise HTTPException(status_code=400, detail="answers가 비어 있습니다.")

    # 1) 정답 사전 조회: {questionId: answer_text}
    qids = [a.questionId for a in payload.answers if a.questionId]
    answer_map: Dict[int, str] = {}
    if qids:
        try:
            rows = (
                db.query(CognitiveQuestion.question_id, CognitiveQuestion.answer)
                .filter(CognitiveQuestion.question_id.in_(qids))
                .all()
            )
            answer_map = {int(r[0]): str(r[1] or "") for r in rows}
        except Exception:
            # 정답 조회 실패 시 빈 맵으로 처리 (해당 문제는 0점)
            answer_map = {}

    # 2) 각 문항 점수 계산 및 CognitiveAnswer에 저장
    per_q_score: Dict[int, float] = {}
    total = 0.0

    try:
        for item in payload.answers:
            expected = answer_map.get(item.questionId, "")
            user_text = item.sttText or item.typedText or ""
            hit = 1.0 if _norm(user_text) == _norm(expected) and expected != "" else 0.0

            ans = CognitiveAnswer(
                session_id=payload.sessionId,
                question_no=item.questionNo,
                question_id=item.questionId if item.questionId else None,
                stt_text=item.sttText,
                typed_text=item.typedText,
                score=hit * 100.0,  # 0 또는 100
                latency_ms=item.latencyMs,
                created_at=datetime.utcnow(),
            )

            db.add(ans)

            per_q_score[item.questionNo] = hit * 100.0
            total += hit * 100.0

        db.commit()

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"답안 저장 오류: {e}")

    # 3) 세션 종료 및 총점 업데이트
    try:
        n = len(payload.answers)
        avg = total / n if n else 0.0

        session_obj = (
            db.query(CognitiveSession)
            .filter(CognitiveSession.session_id == payload.sessionId)
            .one_or_none()
        )
        if not session_obj:
            raise HTTPException(
                status_code=400, detail="유효하지 않은 sessionId 입니다."
            )

        session_obj.finished_at = datetime.utcnow()
        session_obj.total_score = avg
        session_obj.status = "COMPLETED"

        db.commit()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"세션 업데이트 오류: {e}")

    grade = _grade_from(avg)
    summary = f"총 {len(payload.answers)}문항 평균 {avg:.1f}점 → 등급 {grade}"

    return Result(
        totalScore=round(avg, 1),
        perQuestion=per_q_score,
        summary=summary,
        grade=grade,
    )
