import logging
import random
import sys
from typing import List, Dict, Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from ..models import CognitiveSession, CognitiveQuestion, CognitiveAnswer
from sqlalchemy.orm import Session
from sqlalchemy import func
from ..database_oracle import get_db
from ..schemas_cognitive import (
    Question, StartResponse, AnswerItem, SubmitRequest, Result
)
from datetime import datetime

# ----------------------------------------------------------------------
# 로깅 설정하기
# ----------------------------------------------------------------------
LOG_FORMAT = "%(levelname)s: [%(asctime)s] - [%(module)s] => %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)
# (이름은 파일 이름(예: routers_cognitive)으로 정해집니다)
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

@router.get("/start", response_model=StartResponse)
def start_cognitive(
        user_id: int = Query(1, ge=1, description="임시 유저 ID (로그인 연동 전)"),
        count: int = Query(10, description="총 문제 수"),
        category: Optional[str] = Query(None, description="특정 카테고리만 지정 (보통 None)"),
        db: Session = Depends(get_db)
):
    """
    인지 능력 검사 시작
    :param user_id:
    :param count:
    :param category:
    :param db:
    :return:
    """
    # --- 로깅 ---
    logger.info(
        f"REQ => /start: API 시작. [user_id={user_id}, count={count}, category={category}]"
    )
    session_id: int = 0  # 세션 ID를 담을 변수

    # --- 1단계: 세션 생성 ---
    try:
        logger.info(f"[세션생성] user_id={user_id}의 새 CognitiveSession 생성 시도...")
        new_session = CognitiveSession(
            user_id=user_id,
            status="IN_PROGRESS",
            started_at=datetime.utcnow()  # DB 시간은 표준시(UTC)로 저장
        )
        db.add(new_session)  # DB에 추가 (아직 임시)
        db.flush()  # DB에 임시 저장 (session_id를 미리 받기 위해)

        session_id = int(new_session.session_id)  # 할당된 세션 ID 확보

        db.commit()  # DB에 최종 확정!
        logger.info(f"[세션 생성] user_id={user_id} 생성 성공! [session_id={session_id}]")

    except Exception as e:
        logger.error(f"[세션 생성] user_id={user_id} 생성 실패! [Error: {e}]", exc_info=True)
        db.rollback()  # 문제 생겼으니 되돌리기
        raise HTTPException(status_code=500, detail=f"세션 생성 오류: {e}")

    # --- 질문 조회 ---
    try:
        logger.info(f"[질문 조회] session_id={session_id}의 질문 {count}개 뽑기 시작...")

        # 최종 10문제를 담을 리스트
        question_objects: List[CognitiveQuestion] = []

        if category:
            # (예외 처리) 만약 특정 카테고리만 콕 집어 요청했다면
            logger.info(f"특정 카테고리 '{category}'만 {count}개 무작위로 뽑습니다.")
            question_objects = (
                db.query(CognitiveQuestion)
                .filter(CognitiveQuestion.category == category)
                .order_by(func.dbms_random.value())  # 무작위 정렬
                .limit(count)  # 개수 제한
                .all()
            )
        else:
            # --- 질문 생성 로직 ---
            # TODO 현재는 임시 방편으로 Orcale Cloud DB 를 받으면 바로 진행
            logger.info(f"[질문 조회] user_id={user_id} 검사 생성 START!")

            # 1. '기억력-회상력' mapping
            #    (Q6-Q21, Q7-Q22, Q8-Q23, Q9-Q24, Q10-Q25)
            #    데이터셋의 ID 구조(6~10, 21~25)를 활용합니다.

            # 5개 세트(1~5) 중 하나를 무작위
            set_id = random.randint(1, 5)
            logger.info(f"[질문 조회] user_id={user_id} 지남력 '{set_id}번'")

            # (예: set_id=1이면, mem_id=6, rec_id=21)
            mem_q_id = 5 + set_id  # 기억력(Memory) 질문 ID
            rec_q_id = 20 + set_id  # 회상력(Recall) 질문 ID

            # DB에서 '기억력' 짝과 '회상력' 짝을 조회
            pair_questions = (
                db.query(CognitiveQuestion)
                .filter(CognitiveQuestion.question_id.in_([mem_q_id, rec_q_id]))
                .all()
            )
            question_objects.extend(pair_questions)  # 최종 리스트에 2개 추가
            logger.info(f"[질문 조회] user_id={user_id} 기억력, 회상력 생성 (Q_ID: {mem_q_id}, {rec_q_id})")

            # 2. 나머지 문제 (총 10 - 2 = 8개) 뽑기
            remaining_count = count - 2
            if remaining_count > 0:
                # 8개를 뽑아야 할 남은 카테고리
                other_cats = ['지남력', '주의력', '언어능력']

                # 8개를 3개 카테고리로 나누기 (3, 3, 2)
                n_per_cat = remaining_count // len(other_cats)  # 8 // 3 = 2
                remainder = remaining_count % len(other_cats)  # 8 % 3 = 2

                # {카테고리: 뽑을 개수} hashMap 구현
                needs_map = {cat: n_per_cat for cat in other_cats}  # {'지남력': 2, '주의력': 2, '언어능력': 2}

                # 나머지(2개)를 앞 카테고리부터 1개씩 더하기
                for i in range(remainder):
                    needs_map[other_cats[i]] += 1
                # 최종: {'지남력': 3, '주의력': 3, '언어능력': 2}

                logger.info(f"[질문 조회] user_id={user_id} 나머지 {remaining_count}개 배분: {needs_map}")

                # 카테고리별로 필요한 개수만큼 DB에서 무작위로 뽑습니다.
                for cat_name, needed_count in needs_map.items():
                    cat_questions = (
                        db.query(CognitiveQuestion)
                        .filter(CognitiveQuestion.category == cat_name)
                        .order_by(func.dbms_random.value())
                        .limit(needed_count)
                        .all()
                    )
                    question_objects.extend(cat_questions)  # 최종 리스트에 추가
                    logger.info(f"'{cat_name}' 카테고리 {len(cat_questions)}개 확보.")

            # 질문 섞기, 카테고리 별 순서
            random.shuffle(question_objects)  # 10문제 순서를 섞음
            logger.info(f"[질문 조회] user_id={user_id}총 {len(question_objects)}개 문제 확보 및 순서 섞기 완료.")



        # (응답용) Pydantic 모델 리스트 만들기
        questions_for_response: List[Question] = []

        logger.info(f"[Step 3] session_id={session_id}에 대한 '빈 답안지' {len(question_objects)}개 생성 시작...")

        # 10개의 질문을 순회하면서
        for i, q_obj in enumerate(question_objects, start=1):
            # (응답용) 사용자에게 보낼 JSON 질문 목록에 추가
            questions_for_response.append(
                Question(
                    questionNo=i,  # 1번, 2번... (섞인 순서대로)
                    questionId=int(q_obj.question_id),
                    text=str(q_obj.text or ""),
                    category=str(q_obj.category) if q_obj.category is not None else None
                )
            )

            # (DB 저장용) COGNITIVE_ANSWER에 insert될 객체 생성
            new_blank_answer = CognitiveAnswer(
                session_id=session_id,  # 이 시험지의
                question_no=i,  # 이 번호(1~10)에
                question_id=int(q_obj.question_id),  # 이 '진짜 문제' ID를 연결
                created_at=datetime.utcnow(),
                voice_vector=None
                # (score, stt_text 등은 모두 비어있음 - NULL)
            )
            db.add(new_blank_answer)  # DB에 추가 (아직 임시)

        # 질문이 하나도 없으면 더미 질문 (이 경우엔 빈 답안지 저장 안 함)
        if not question_objects:
            logger.warning(f"[질문 백업] user_id={user_id} DB에 질문이 없음! [session_id={session_id}] 더미 질문 반환.")
            # (이 부분은 기존 로직 유지 - 데모용)
            demo = [("올해는 몇 년인가요?", "지남력"), ("오늘은 무슨 요일인가요?", "지남력")]
            for i, (q_text, cat) in enumerate(demo, start=1):
                questions_for_response.append(
                    Question(questionNo=i, questionId=0, text=q_text, category=cat)
                )
        else:
            # 10개를 DB에 최종 확정
            db.commit()
            logger.info(f"[질문 백업] user_id={user_id} {len(question_objects)}개 DB 저장 완료.")

        # --- 최종 응답 ---
        logger.info(f"RES => user_id={user_id} /start: 성공! [session_id={session_id}] 질문 {len(questions_for_response)}개 반환.")
        logger.info(f"RES => user_id={user_id} {StartResponse(sessionId=session_id, questions=questions_for_response)}")
        return StartResponse(sessionId=session_id, questions=questions_for_response)

    except Exception as e:
        logger.error(f"[질문 백업] user_id={user_id} 질문 조회/백업 생성 실패! [Error: {e}]", exc_info=True)
        db.rollback()  # 초기 데이터 추가하던 것 되돌리기
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
            # ORM으로 IN 쿼리 실행
            rows = (
                db.query(CognitiveQuestion.question_id, CognitiveQuestion.answer)
                  .filter(CognitiveQuestion.question_id.in_(qids))
                  .all()
            )
            # 딕셔너리로 변환
            answer_map = {int(r[0]): str(r[1] or "") for r in rows}
        except Exception:
            # 정답 조회 실패 시 빈 맵으로 처리 (해당 문제는 0점)
            answer_map = {}

    # 2) 각 문항 점수 계산 및 CognitiveAnswer에 저장
    per_q_score: Dict[int, float] = {}
    total = 0.0

    try:
        # payload.answers 순회
        for item in payload.answers:
            expected = answer_map.get(item.questionId, "")
            # 정규화한 문자열이 같으면 정답(1.0), 아니면 오답(0.0)
            user_text = item.sttText or item.typedText or ""
            hit = 1.0 if _norm(user_text) == _norm(expected) and expected != "" else 0.0

            # 새로운 CognitiveAnswer 인스턴스 생성
            ans = CognitiveAnswer(
                session_id=payload.sessionId,
                question_no=item.questionNo,
                question_id=item.questionId if item.questionId else None,
                stt_text=item.sttText,
                typed_text=item.typedText,
                score=hit * 100.0,    # 0 또는 100
                latency_ms=item.latencyMs,
                created_at=datetime.utcnow()
            )

            # DB에 추가
            db.add(ans)

            # 점수 맵과 합계 갱신
            per_q_score[item.questionNo] = hit * 100.0
            total += hit * 100.0

        # 모든 답안을 추가한 뒤 커밋
        db.commit()

    except Exception as e:
        # 문제 발생 시 롤백하고 에러 반환
        db.rollback()
        raise HTTPException(status_code=500, detail=f"답안 저장 오류: {e}")

    # 3) 세션 종료 및 총점 업데이트
    try:
        n = len(payload.answers)
        avg = total / n if n else 0.0

        # 세션을 찾아서 필드 업데이트
        session_obj = db.query(CognitiveSession).filter(CognitiveSession.session_id == payload.sessionId).one_or_none()
        if not session_obj:
            # 만약 세션이 없으면(잘못된 sessionId) 에러
            raise HTTPException(status_code=400, detail="유효하지 않은 sessionId 입니다.")

        # 값 갱신
        session_obj.finished_at = datetime.utcnow()
        session_obj.total_score = avg
        session_obj.status = "COMPLETED"

        # 변경사항 커밋
        db.commit()

    except HTTPException:
        # 이미 HTTPException을 던진 경우 그대로 재전파
        raise
    except Exception as e:
        db.rollback()
        # 답안이 이미 저장된 상태라면 결과는 내려주되 세션 업데이트 실패 알림
        raise HTTPException(status_code=500, detail=f"세션 업데이트 오류: {e}")

    # 결과 생성
    grade = _grade_from(avg)
    summary = f"총 {n}문항 평균 {avg:.1f}점 → 등급 {grade}"

    return Result(
        totalScore=round(avg, 1),
        perQuestion=per_q_score,
        summary=summary,
        grade=grade
    )