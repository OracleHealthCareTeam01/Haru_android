# app/services/diary_coach.py
from __future__ import annotations

from typing import Optional
from datetime import date

from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from ..schemas import DiaryAIResponse


class DiaryCoachResult(BaseModel):
    """
    LLM이 직접 채워주는 내부용 스키마.
    -> 이걸 DiaryAIResponse로 변환해서 클라이언트에 보낸다.
    """
    summary: str = Field(..., description="일기 요약 (2~3문장)")
    emotion: str = Field(..., description="감정 설명 + 공감 멘트")
    advice: str = Field(..., description="구체적인 조언")


# gpt-4.1-mini 기준 (quiz_scoring.py와 스타일 맞추기)
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

[입력 정보]
- 닉네임(있다면): {display_name}
- 날짜(있다면): {entry_date}
- 사용자가 선택한 기분(mood_code, 있으면 참고): {mood_code}

[일기 본문]
{content}

[내부 사고(생각) 단계 - 출력하지 말 것]
1) 문장 정리: 일기의 내용을 시간 흐름 또는 주제별로 머릿속에서 정리한다.
2) 감정 분석: 사용자의 주요 감정(예: 불안, 외로움, 안도감 등)과 그 강도를 추론한다.
3) 핵심 사건 추출: 오늘 하루 중 감정에 영향을 준 중요한 사건을 1~3개 뽑는다.
4) 감정 트렌드 해석: 사건과 감정이 어떻게 연결되는지, 사용자의 패턴은 무엇인지 이해한다.
5) 맞춤형 코칭 메시지 생성: 사용자의 감정 상태를 공감해주고, 짧고 구체적인 행동 조언을 만든다.

[중요 지침]
- 위 1~5단계의 생각 과정은 "절대" 사용자에게 그대로 보여주지 말 것.
- 사용자에게는 오직 아래의 JSON 스키마(DiaryCoachResult)에 맞는 최종 결과만 보여줘라.
- 전문 의사 진단이나 응급 상황 대응은 할 수 없으며,
  그런 상황으로 보이면 "전문가 상담이나 응급센터에 도움을 요청하라"는 취지로 부드럽게 권유한다.
- 말투는 부드럽고 존중하는 반말/존댓말 혼합 느낌으로, 부담스럽지 않게.

[최종 출력 형식]
다음 Pydantic 스키마에 맞는 JSON만 출력해라:

DiaryCoachResult:
- summary: str  # 사용자의 하루를 2~3문장으로 정리
- emotion: str  # 사용자의 감정을 공감하며 설명 (2~5문장)
- advice: str   # 공감 한 문장 + 지금 당장 실천 가능한 구체적 행동 제안 1~3개

JSON 이외의 다른 텍스트는 출력하지 마라.
"""
)


_chain = _PROMPT | _llm.with_structured_output(DiaryCoachResult)


def analyze_diary_with_coach(
    content: str,
    mood_code: Optional[str] = None,
    entry_date: Optional[date] = None,
    display_name: Optional[str] = None,
) -> DiaryAIResponse:
    """
    일기 본문을 받아 정신건강 코치의 요약/감정/조언을 생성한다.
    - DB에는 아무것도 저장하지 않는다.
    - DiaryAIResponse 형태로 반환한다.
    """
    if not content or not content.strip():
        raise ValueError("content must not be empty")

    variables = {
        "content": content,
        "mood_code": mood_code or "",
        "entry_date": entry_date.isoformat() if entry_date else "",
        "display_name": display_name or "",
    }

    result: DiaryCoachResult = _chain.invoke(variables)

    return DiaryAIResponse(
        summary=result.summary,
        emotion=result.emotion,
        advice=result.advice,
    )
