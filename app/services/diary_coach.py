# app/services/diary_coach.py
from __future__ import annotations

import random
from typing import Optional
from datetime import date

from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from ..schemas import DiaryAIResponse, EmotionReport, YoutubeRecommendation


class DiaryCoachResult(BaseModel):
    """
    LLM이 직접 채워주는 내부용 스키마.
    -> 이걸 DiaryAIResponse로 변환해서 클라이언트에 보낸다.
    """
    # 1) 요약
    summary: str = Field(..., description="일기 요약 (2~3문장)")

    # 2) 감정 리포트
    emotion: str = Field(
        ...,
        description="짧은 감정 + 이모지 1개 (예: '기쁨😊')",
    )
    empathy: str = Field(
        ...,
        description="사용자의 일기와 감정에 공감하는 설명 (2~4문장)",
    )
    life_tip: str = Field(
        ...,
        description="한 줄짜리 짧은 인생 조언",
    )

    # 3) 유튜브 추천 (카테고리 + 이유 + 검색어만 LLM이 생성)
    youtube_category: str = Field(
        ...,
        description="추천 타입 (예: '운동', '스트레칭', '힐링', '동물', '명상' 등)",
    )
    youtube_reason: str = Field(
        ...,
        description="이 카테고리를 추천하는 이유",
    )
    youtube_search_query: str = Field(
        ...,
        description="유튜브 검색에 사용할 키워드 (예: '10분 수면 명상')",
    )

_llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0.4,
)


_PROMPT = ChatPromptTemplate.from_template(
    """
[시스템 역할]
너는 따뜻하고 차분한 한국어 정신건강 코치 AI야.
사용자의 하루 일기를 듣고,
- 내용을 이해하기 쉽게 정리하고,
- 사용자의 감정을 섬세하게 짚어주고,
- 부담스럽지 않은 현실적인 조언을 해준다.
- 그리고 사용자의 감정 상태에 도움이 될 만한 유튜브 영상 "카테고리"를 추천한다.

[입력 정보]
- 닉네임(있다면): {display_name}
- 날짜(있다면): {entry_date}

[일기 본문]
{content}

[내부 사고(생각) 단계 - 출력하지 말 것]
1) 문장 정리: 일기의 내용을 시간 흐름 또는 주제별로 머릿속에서 정리한다.
2) 감정 분석: 사용자의 주요 감정(예: 불안, 외로움, 안도감 등)과 그 강도를 추론한다.
3) 핵심 사건 추출: 오늘 하루 중 감정에 영향을 준 중요한 사건을 1~3개 뽑는다.
4) 감정 트렌드 해석: 사건과 감정이 어떻게 연결되는지, 사용자의 패턴은 무엇인지 이해한다.
5) 맞춤형 코칭 메시지 생성: 사용자의 감정 상태를 공감해주고, 짧고 구체적인 행동 조언을 만든다.
6) 유튜브 카테고리 추천:
   - 아래 예시 중에서 사용자의 상태에 가장 도움이 될 만한 타입을 하나 고른다.
   - 예시: "운동", "스트레칭", "힐링", "동물", "명상"
   - 그리고 해당 타입에 맞는 검색 키워드를 만든다. (예: "10분 수면 명상", "집에서 하는 가벼운 스트레칭")

[중요 지침]
- 위 1~6단계의 생각 과정은 "절대" 사용자에게 그대로 보여주지 말 것.
- 사용자에게는 오직 아래의 JSON 스키마(DiaryCoachResult)에 맞는 최종 결과만 보여줘라.
- 전문 의사 진단이나 응급 상황 대응은 할 수 없으며,
  그런 상황으로 보이면 "전문가 상담이나 응급센터에 도움을 요청하라"는 취지로 부드럽게 권유한다.
- 말투는 부드럽고 존중하는 반말/존댓말 혼합 느낌으로, 부담스럽지 않게.
- 유튜브 링크(URL)는 생성하지 마라.
  대신 youtube_category, youtube_reason, youtube_search_query 세 가지만 채운다.

[최종 출력 형식]
다음 Pydantic 스키마에 맞는 JSON만 출력해라:

DiaryCoachResult:
- summary: str             # 사용자의 하루를 2~3문장으로 정리
- emotion: str             # 짧은 감정 + 이모지 1개 (예: "기쁨😊")
- empathy: str             # 사용자의 감정을 공감하며 설명 (2~4문장)
- life_tip: str            # 한 줄짜리 짧은 인생 조언
- youtube_category: str    # 추천 타입 (예: "운동", "스트레칭", "힐링", "동물", "명상" 등)
- youtube_reason: str      # 이 카테고리를 추천하는 이유
- youtube_search_query: str# 유튜브 검색에 사용할 키워드 (예: "10분 수면 명상")

JSON 이외의 다른 텍스트는 출력하지 마라.
"""
)


_chain = _PROMPT | _llm.with_structured_output(DiaryCoachResult)

YOUTUBE_POOL = {
    "명상": [
        {
            "title": "잠들기 전 10분 명상",
            "url": "https://www.youtube.com/watch?v=VIDEO_ID_MEDITATION_1",
        },
        {
            "title": "불안을 가라앉히는 호흡 명상",
            "url": "https://www.youtube.com/watch?v=VIDEO_ID_MEDITATION_2",
        },
    ],
    "스트레칭": [
        {
            "title": "하루 피로를 풀어주는 5분 스트레칭",
            "url": "https://www.youtube.com/watch?v=VIDEO_ID_STRETCH_1",
        },
        {
            "title": "의자에 앉아서 하는 간단 스트레칭",
            "url": "https://www.youtube.com/watch?v=VIDEO_ID_STRETCH_2",
        },
    ],
    "운동": [
        {
            "title": "집에서 하는 가벼운 홈트 10분",
            "url": "https://www.youtube.com/watch?v=VIDEO_ID_WORKOUT_1",
        },
    ],
    "힐링": [
        {
            "title": "잔잔한 피아노 힐링 음악",
            "url": "https://www.youtube.com/watch?v=VIDEO_ID_HEALING_1",
        },
    ],
    "동물": [
        {
            "title": "귀여운 강아지 힐링 모음",
            "url": "https://www.youtube.com/watch?v=VIDEO_ID_ANIMAL_1",
        },
    ],
}


def _pick_youtube(category: str) -> tuple[str, str]:
    """
    LLM이 고른 카테고리 기준으로, 우리가 준비해 둔 유튜브 목록에서 하나 선택.
    - category가 없거나 목록이 비어 있으면 기본값 사용.
    """
    default_title = "편안한 음악 모음"
    default_url = "https://www.youtube.com/results?search_query=relax+music"

    pool = YOUTUBE_POOL.get(category)
    if not pool:
        return default_title, default_url

    choice = random.choice(pool)
    return choice["title"], choice["url"]

def analyze_diary_with_coach(
    content: str,
    entry_date: Optional[date] = None,
    display_name: Optional[str] = None,
) -> DiaryAIResponse:
    """
    일기 본문을 받아 정신건강 코치의
    1) 요약
    2) 감정 리포트
    3) 유튜브 추천
    을 생성한다.
    - DB에는 아무것도 저장하지 않는다.
    """
    if not content or not content.strip():
        raise ValueError("content must not be empty")

    variables = {
        "content": content,
        "entry_date": entry_date.isoformat() if entry_date else "",
        "display_name": display_name or "",
    }

    result: DiaryCoachResult = _chain.invoke(variables)

    # 감정 리포트 구성
    emotion_report = EmotionReport(
        emotion=result.emotion,
        empathy=result.empathy,
        life_tip=result.life_tip,
    )

    yt_title, yt_url = _pick_youtube(result.youtube_category)

    youtube = YoutubeRecommendation(
        title=yt_title,
        url=yt_url,
        reason=result.youtube_reason,
        category=result.youtube_category,
    )

    return DiaryAIResponse(
        summary=result.summary,
        emotion_report=emotion_report,
        youtube=youtube,
    )
