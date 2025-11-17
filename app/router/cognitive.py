import logging
import random
import sys
import json
from typing import List, Dict, Optional
from datetime import datetime
import re

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, text

from ..models import CognitiveSession, CognitiveQuestion, CognitiveAnswer
from ..database_oracle import get_db
from ..schemas_cognitive import (
    Question, StartResponse, AnswerItem, SubmitRequest, Result
)
from ..services.quiz_scoring import choose_scoring

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
    Oracle 23ai DBMS_CLOUD_AI.GENERATE 를 사용해서
    cognitive_question 테이블에 이미 존재하는 문제들 중에서
    questionId 들만 골라오는 함수.

    1차 시도: LLM이 JSON 배열([6,21,3,...])을 직접 반환 → json.loads
    2차 시도: JSON이 아니면, 설명 텍스트/SQL 안에서 숫자를 regex로 뽑아서 사용
    """

    # 1) 프로필 설정
    try:
        db.execute(text("BEGIN DBMS_CLOUD_AI.SET_PROFILE('QUIZ_AI'); END;"))
    except Exception as e:
        logger.error(f"[SELECT AI] SET_PROFILE 실패: {e}", exc_info=True)
        raise

    # 2) 후보 문제 목록 조회
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

    # 여기서 미리 candidate_ids 만들어 둔다 (텍스트 파싱 때 필요)
    candidate_ids = {int(c["questionId"]) for c in candidates_payload}

    # 3) 프롬프트
    prompt = f"""
당신은 병원에서 사용하는 한국어 인지기능 검사를 설계하는 전문가입니다.

다음은 이미 데이터베이스에 저장되어 있는 인지검사 문제 후보 목록입니다 (JSON 배열):

candidates = {candidates_json}

각 객체는 다음 필드를 가집니다:
- questionId: 정수형 문제 ID
- category: 문자열 (지남력, 주의력, 언어능력, 기억력, 회상력 중 하나)
- text: 문제 내용 (질문 문장)

당신의 역할:
- 이 후보들 중에서 총 {count}개의 서로 다른 문항을 선택하세요.
- 가능한 한 다양한 카테고리(지남력, 주의력, 언어능력, 기억력, 회상력)를 고르게 포함하도록 노력하세요.
- 난이도는 전체적으로 중간 수준이 되도록 선택하세요.
- 같은 문제를 중복해서 선택하지 마세요.
- 후보에 없는 questionId는 절대 사용하지 마세요.
- 반드시 정확히 {count}개의 questionId를 선택하세요.

출력 형식 (매우 중요):

지금부터 당신의 전체 출력은 오직 하나의 JSON 배열만 포함해야 합니다.

규칙:
1. 출력은 JSON 배열 하나뿐이어야 합니다.
2. 배열의 각 원소는 선택한 문제의 questionId 정수입니다.
3. 배열에 포함되는 값은 모두 candidates 목록 안에 있는 questionId여야 합니다.
4. 같은 questionId를 중복해서 넣지 마세요.
5. 배열 길이는 반드시 {count}여야 합니다.
6. JSON 배열 이외의 어떤 문자, 설명, 줄바꿈 텍스트, 마크다운 코드블록을 절대 출력하지 마세요.

예시 (형식 예시일 뿐, 값과 개수는 실제와 다를 수 있습니다):
[6, 21, 3, 10]
"""

    # 4) DBMS_CLOUD_AI.GENERATE 호출
    try:
        sql = text("""
            SELECT DBMS_CLOUD_AI.GENERATE(
                :prompt,
                'QUIZ_AI',
                'NARRATE',
                NULL
            ) AS response
            FROM DUAL
        """)
        raw_response = db.execute(sql, {"prompt": prompt}).scalar()
        logger.info(f"[SELECT AI] raw_response (앞 200자): {str(raw_response)[:200]}...")
    except Exception as e:
        logger.error(f"[SELECT AI] GENERATE 호출 실패: {e}", exc_info=True)
        raise

    if raw_response is None:
        raise ValueError("SELECT AI(GENERATE)에서 빈 응답이 반환되었습니다.")

    # 5) 1차: JSON 파싱 시도
    try:
        data = json.loads(raw_response)
        if not isinstance(data, list):
            raise ValueError("JSON이 배열이 아닙니다.")
        logger.info("[SELECT AI] JSON 배열 파싱 성공")
    except Exception as e:
        # 텍스트 안에서 숫자 뽑기
        logger.error(f"[SELECT AI] JSON 파싱 실패: {e} / raw={raw_response}", exc_info=True)
        logger.info("[SELECT AI] JSON이 아니므로 텍스트/SQL에서 questionId 추출 시도")

        selected_ids_text: List[int] = []

        # 1) IN ( ... ) 패턴 안의 숫자 먼저 노린다.
        m = re.search(r"IN\s*\(([^)]*)\)", raw_response, re.IGNORECASE | re.DOTALL)
        sources = []
        if m:
            sources.append(m.group(1))
        else:
            # 그래도 못 찾으면 전체 텍스트에서 숫자 검색
            sources.append(raw_response)

        for src in sources:
            for num_str in re.findall(r"\b\d+\b", src):
                try:
                    qid = int(num_str)
                except ValueError:
                    continue

                if qid in candidate_ids and qid not in selected_ids_text:
                    selected_ids_text.append(qid)
                    logger.info(f"[SELECT AI] 텍스트에서 추출된 questionId: {qid}")
                    if len(selected_ids_text) >= count:
                        break
            if len(selected_ids_text) >= count:
                break

        if not selected_ids_text:
            # 텍스트에서도 못 뽑았으면 진짜 실패
            raise ValueError("SELECT AI(GENERATE) 응답이 JSON 형식도 아니고, 텍스트에서도 questionId를 추출하지 못했습니다.")

        logger.info(f"[SELECT AI] 텍스트에서 추출된 questionId 목록: {selected_ids_text}")
        data = selected_ids_text  # 아래 공통 로직 타게 함

    # 6) 후보 id 검증 및 정리
    selected_ids: List[int] = []

    for idx, item in enumerate(data, start=1):
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
        remaining_ids = list(candidate_ids - set(selected_ids))
        random.shuffle(remaining_ids)
        need = min(count - len(selected_ids), len(remaining_ids))
        selected_ids.extend(remaining_ids[:need])

    if not selected_ids:
        raise ValueError("SELECT AI(GENERATE)에서 유효한 questionId를 하나도 선택하지 못했습니다.")

    logger.info(f"[SELECT AI] 최종 선택된 questionId 목록: {selected_ids[:count]}")
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

        question_objects: List[CognitiveQuestion] = []

        # 2-1. 먼저 LLM(GENERATE)로 questionId를 고르게 시도
        selected_ids: List[int] = []
        try:
            logger.info(
                f"[SELECT AI] user_id={user_id}, count={count}, category={category} 질문 선택 시도..."
            )
            selected_ids = _select_question_ids_with_select_ai(db, count, category)
            logger.info(f"[SELECT AI] 선택된 questionId 목록: {selected_ids}")

            if selected_ids:
                rows = (
                    db.query(CognitiveQuestion)
                    .filter(CognitiveQuestion.question_id.in_(selected_ids))
                    .all()
                )
                row_map = {int(r.question_id): r for r in rows}
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

        # 2-2. LLM이 실패했거나 0개만 만든 경우: 기존 랜덤 로직 사용
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

            new_blank_answer = CognitiveAnswer(
                session_id=session_id,
                question_no=i,
                question_id=int(q_obj.question_id),
                created_at=datetime.utcnow(),
                voice_vector=None,
            )
            db.add(new_blank_answer)

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


@router.post("/submit", response_model=Result)
def submit_cognitive(payload: SubmitRequest, db: Session = Depends(get_db)):
    """
    클라이언트에서 보낸 답안을 채점하고 저장합니다.
    """

    if not payload.answers:
        raise HTTPException(status_code=400, detail="answers가 비어 있습니다.")

    logger.info(f"[제출 시작] 세션 ID: {payload.sessionId}, 답변 수: {len(payload.answers)}")

    # 1) 세션 확인
    try:
        session_obj = (
            db.query(CognitiveSession)
            .filter(CognitiveSession.session_id == payload.sessionId)
            .one_or_none()
        )
        if not session_obj:
            raise HTTPException(status_code=400, detail="유효하지 않은 sessionId입니다.")

        logger.info(f"[세션 확인] 세션 {payload.sessionId} 존재 확인 완료")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[세션 조회 오류] {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"세션 조회 오류: {e}")

    # 2) 문항 정보 로드
    qids = [a.questionId for a in payload.answers if a.questionId]
    question_map: Dict[int, CognitiveQuestion] = {}

    if qids:
        try:
            rows: List[CognitiveQuestion] = (
                db.query(CognitiveQuestion)
                .filter(CognitiveQuestion.question_id.in_(qids))
                .all()
            )
            question_map = {int(r.question_id): r for r in rows}
            logger.info(f"[문항 조회] {len(question_map)}개 문항 정보 로드 완료")

        except Exception as e:
            logger.error(f"[문항 조회 오류] {e}", exc_info=True)
            question_map = {}

    # 3) 채점 + 업데이트
    per_q_score: Dict[int, float] = {}
    total_score = 0.0
    vector_success_count = 0
    vector_fail_count = 0

    category_sum: Dict[str, float] = {}
    category_count: Dict[str, int] = {}

    try:
        for idx, item in enumerate(payload.answers, 1):
            logger.info(f"[처리 {idx}/{len(payload.answers)}] Q{item.questionNo} 시작")

            user_text = item.sttText or item.typedText or ""

            qinfo = question_map.get(item.questionId) if item.questionId else None

            if qinfo:
                try:
                    score_0_1, feedback, resolved_correct = choose_scoring(
                        category=qinfo.category or "",
                        text=qinfo.text or "",
                        correct_answer_raw=qinfo.answer or "",
                        user_answer=user_text,
                    )
                    logger.info(
                        f"[AI 채점] Q{item.questionNo}: 점수={score_0_1:.2f}"
                    )
                except Exception as e:
                    logger.error(f"[AI 채점 오류] Q{item.questionNo}: {e}", exc_info=True)
                    score_0_1 = 0.0
                    feedback = f"AI 채점 오류: {str(e)}"
                    resolved_correct = ""
            else:
                score_0_1 = 0.0
                feedback = "문항 정보 없음"
                resolved_correct = ""
                logger.warning(f"[문항 누락] Q{item.questionNo}")

            score_pct = score_0_1 * 100.0

            qinfo = question_map.get(item.questionId) if item.questionId else None
            category_name = (qinfo.category.strip() if qinfo and qinfo.category else "UNKNOWN")

            if category_name not in category_sum:
                category_sum[category_name] = 0.0
                category_count[category_name] = 0

            category_sum[category_name] += score_pct
            category_count[category_name] += 1

            existing = (
                db.query(CognitiveAnswer)
                .filter(
                    CognitiveAnswer.session_id == payload.sessionId,
                    CognitiveAnswer.question_no == item.questionNo,
                )
                .one_or_none()
            )

            if existing:
                existing.question_id = item.questionId if item.questionId else existing.question_id
                existing.stt_text = item.sttText
                existing.typed_text = item.typedText
                existing.score = round(score_pct, 2)
                existing.latency_ms = item.latencyMs
                existing.created_at = existing.created_at or datetime.utcnow()

                logger.info(f"[UPDATE 완료] Q{item.questionNo}")
            else:
                logger.warning(f"[INSERT] Q{item.questionNo} placeholder 없음 - 새로 생성")

                ans = CognitiveAnswer(
                    session_id=payload.sessionId,
                    question_no=item.questionNo,
                    question_id=item.questionId if item.questionId else None,
                    stt_text=item.sttText,
                    typed_text=item.typedText,
                    score=round(score_pct, 2),
                    latency_ms=item.latencyMs,
                    created_at=datetime.utcnow(),
                )
                db.add(ans)

            per_q_score[item.questionNo] = round(score_pct, 2)
            total_score += score_pct

        db.commit()

        logger.info(
            f"[답변 저장 완료] 세션 {payload.sessionId}: "
            f"{len(payload.answers)}개 답변 저장 완료 "
            f"(벡터 성공: {vector_success_count}, 실패: {vector_fail_count})"
        )

    except Exception as e:
        db.rollback()
        logger.error(f"[답변 저장 오류] {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"답안 저장 오류: {e}")

    # 4) 세션 종료 및 총점 업데이트
    try:
        n = len(payload.answers)
        avg_score = total_score / n if n > 0 else 0.0

        session_obj.finished_at = datetime.utcnow()
        session_obj.total_score = round(avg_score, 2)
        session_obj.status = "COMPLETED"

        db.commit()

        logger.info(
            f"[세션 완료] 세션 {payload.sessionId}: "
            f"평균 {avg_score:.2f}점, 상태 COMPLETED"
        )

    except Exception as e:
        db.rollback()
        logger.error(f"[세션 업데이트 오류] {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"세션 업데이트 오류: {e}")

    # 5) 결과 반환
    grade = _grade_from(avg_score)
    summary = f"총 {len(payload.answers)}문항 평균 {avg_score:.1f}점 → 등급 {grade}"

    category_average: Dict[str, float] = {}
    for cat, s in category_sum.items():
        cnt = category_count.get(cat, 0)
        category_average[cat] = round((s / cnt) if cnt > 0 else 0.0, 2)

    recent_sessions_list = []
    try:
        if getattr(session_obj, "user_id", None) is not None:
            rows = (
                db.query(CognitiveSession)
                .filter(
                    CognitiveSession.user_id == session_obj.user_id,
                    CognitiveSession.status == "COMPLETED",
                )
                .order_by(CognitiveSession.finished_at.desc())
                .limit(3)
                .all()
            )
        else:
            rows = (
                db.query(CognitiveSession)
                .filter(CognitiveSession.status == "COMPLETED")
                .order_by(CognitiveSession.finished_at.desc())
                .limit(3)
                .all()
            )

        for r in rows:
            recent_sessions_list.append({
                "sessionId": int(r.session_id),
                "finishedAt": r.finished_at.isoformat() if r.finished_at else None,
                "totalScore": float(r.total_score) if getattr(r, "total_score", None) is not None else None
            })

    except Exception as e:
        logger.warning(f"[최근 세션 조회 실패] {e}", exc_info=True)
        recent_sessions_list = []

    logger.info(f"[제출 완료] {summary}")

    return Result(
        totalScore=round(avg_score, 1),
        categoryAverage=category_average,
        recentSessions=recent_sessions_list,
        summary=summary,
        grade=grade,
    )
