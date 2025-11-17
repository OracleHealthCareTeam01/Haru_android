import sys
import logging
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import select
import datetime

from ..models import Today
from ..schemas import  TodayCreate, TodayOut
from ..database_oracle import get_db

from ..schemas import TodayCreate, TodayOut, DiaryAIRequest, DiaryAIResponse
from ..services.diary_coach import analyze_diary_with_coach

LOG_FORMAT = "%(levelname)s: [%(asctime)s] - [%(module)s] => %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/diary", tags=["diary"])


# ============================================
# 1. 일기 목록 조회 API
# ============================================
@router.get("/list", response_model=List[TodayOut])
async def get_diary_list(
        user_id: int = Query(..., description="사용자 ID"),
        db: Session = Depends(get_db)
):
    """
    특정 사용자의 모든 일기 목록을 조회합니다.

    Parameters:
    - user_id: 조회할 사용자의 ID (필수)

    Returns:
    - 일기 목록 (최신순으로 정렬)
    """
    try:
        # --- Step 1: 조회 시작 로그 ---
        logger.info(f"[일기 목록 조회] user_id={user_id}의 일기 조회 시도...")

        # --- Step 2: DB 조회 ---
        # Oracle의 경우 select 문으로 조회
        stmt = (
            select(Today)
            .where(Today.user_id == user_id)
            .order_by(Today.entry_date.desc())  # 날짜 내림차순 정렬
        )

        result = db.execute(stmt)
        diaries = result.scalars().all()

        # --- Step 3: 조회 결과 확인 ---
        if not diaries:
            logger.warning(f"[일기 목록 조회] user_id={user_id} 일기 없음! 빈 배열 반환.")
            return []

        logger.info(f"[일기 목록 조회] user_id={user_id} 조회 성공! 총 {len(diaries)}개의 일기 발견.")

        # --- Step 4: 응답 생성 ---
        # Pydantic 모델로 자동 변환됨 (response_model 덕분)
        logger.info(f"RES => user_id={user_id} /diary/list: 성공! {len(diaries)}개 반환.")
        return diaries

    except Exception as e:
        # --- 에러 처리 ---
        logger.error(
            f"[일기 목록 조회] user_id={user_id} 조회 실패! [Error: {e}]",
            exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail=f"일기 목록 조회 오류: {e}"
        )


# ============================================
# 2. 일기 등록 API
# ============================================
@router.post("/create", response_model=TodayOut, status_code=201)
async def create_or_update_diary(
        diary_data: TodayCreate,
        user_id: int = Query(..., description="사용자 ID"),
        db: Session = Depends(get_db)
):
    """
    새로운 일기를 등록하거나 기존 일기를 수정합니다.

    Parameters:
    - user_id: 작성자의 사용자 ID (필수)
    - diary_data: 일기 내용 (entry_date, mood_code, content)

    Returns:
    - 생성/수정된 일기 정보 (is_updated 플래그 포함)
    """
    existing_diary, new_diary = None, None
    try:
        logger.info(f"일기 등록/수정 요청 - user_id: {user_id}, date: {diary_data.entry_date}")

        # 같은 날짜에 이미 일기가 있는지 확인
        stmt = select(Today).where(
            Today.user_id == user_id,
            Today.entry_date == diary_data.entry_date
        )
        existing_diary = db.execute(stmt).scalar_one_or_none()

        if existing_diary:
            # 기존 일기 수정
            logger.info(f"기존 일기 수정 - entry_id: {existing_diary.entry_id}")
            existing_diary.mood_code = diary_data.mood_code
            existing_diary.content = diary_data.content
            existing_diary.modified_date = datetime.utcnow()  # 수정 시간 기록

            db.commit()
            db.refresh(existing_diary)

            logger.info(f"일기 수정 완료 - entry_id: {existing_diary.entry_id}")

            return existing_diary
        else:
            # 새로운 일기 생성
            logger.info(f"새로운 일기 생성 - user_id: {user_id}, date: {diary_data.entry_date}")
            new_diary = Today(
                user_id=user_id,
                entry_date=diary_data.entry_date,
                mood_code=diary_data.mood_code,
                content=diary_data.content,
                modified_date=None  # 최초 생성 시에는 None
            )

            db.add(new_diary)
            db.commit()
            db.refresh(new_diary)

            logger.info(f"일기 등록 완료 - entry_id: {new_diary.entry_id}")

            return new_diary

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"일기 등록/수정 중 오류 발생: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail="일기 등록/수정 실패")

@router.post("/ai", response_model=DiaryAIResponse)
async def diary_ai_coach(payload: DiaryAIRequest):
    """
    오늘의 일기 본문을 받아 AI 정신건강 코치의
    1) 요약
    2) 감정 리포트 (감정/공감/인생 조언)
    3) 유튜브 영상 추천
    을 반환하는 엔드포인트.

    참골 DB에는 아무 것도 저장하지 않는다.
       (저장은 /diary/create 로 따로 처리)
    """
    if not payload.content or not payload.content.strip():
        raise HTTPException(status_code=400, detail="content는 비어 있을 수 없습니다.")

    try:
        return analyze_diary_with_coach(
            content=payload.content,
            entry_date=payload.entry_date,
            display_name=payload.display_name,
        )
    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail="AI 분석 중 오류가 발생했습니다.")

