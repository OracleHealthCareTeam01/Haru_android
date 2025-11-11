# test/app/services/quiz_scoring.py
from __future__ import annotations
from typing import List, Tuple
from datetime import datetime
from rapidfuzz import fuzz
import re

# 동적 정답 토큰
def resolve_answer_tokens(raw: str) -> str:
    if not raw: return raw
    now = datetime.now()
    mapping = {
        "{current.year}": str(now.year),
        "{today.kor_date}": f"{now.year}년 {now.month}월 {now.day}일",
        "{today.month}": str(now.month),
        "{today.day}": str(now.day),
        "{today.weekday_kor}": ["월요일","화요일","수요일","목요일","금요일","토요일","일요일"][now.weekday()],
        "{xmas.kor}": "12월 25일",
        "{this.month.kor}": f"{now.month}월",
        "{this.year.kor}": f"{now.year}년",
    }
    out = raw
    for k, v in mapping.items():
        out = out.replace(k, v)
    return out

def normalize_kor(s: str) -> str:
    s = (s or "")
    return (s.lower().replace(" ", "").replace(",", "").replace("·", "")
            .replace("，", "").replace(".", "")
            .replace("년", "").replace("월", "").replace("일", "").strip())

def split_items(s: str) -> List[str]:
    parts = re.split(r"[,\\s/·\\-]+", (s or "").strip())
    return [p for p in parts if p]

def score_exact_or_fuzzy(user: str, correct: str) -> float:
    u = normalize_kor(user); c = normalize_kor(correct)
    if not u and not c: return 1.0
    if u == c: return 1.0
    return fuzz.ratio(u, c) / 100.0

def score_list_ordered(user: str, correct: str) -> float:
    u_items = split_items(user); c_items = split_items(correct)
    if not c_items: return 0.0
    per = []
    for i in range(min(len(u_items), len(c_items))):
        per.append(score_exact_or_fuzzy(u_items[i], c_items[i]))
    return sum(per) / len(c_items)

def score_list_unordered(user: str, correct: str) -> float:
    u_set = set(split_items(user)); c_set = set(split_items(correct))
    if not c_set: return 0.0
    hit = 0
    for c in c_set:
        best = max((fuzz.ratio(normalize_kor(c), normalize_kor(u)) for u in u_set), default=0) / 100.0
        if best >= 0.8: hit += 1
    return hit / len(c_set)

def choose_scoring(category: str, text: str, correct_answer_raw: str, user_answer: str) -> Tuple[float, str, str]:
    resolved_correct = resolve_answer_tokens(correct_answer_raw or "")
    cat = (category or "").strip()

    if cat == "기억력":
        score = score_list_unordered(user_answer, resolved_correct)
        fb = "기억 단어 일치율 기반 채점(순서 무시)"
    elif cat == "주의력":
        if "역순" in (text or "") or "거꾸로" in (text or ""):
            score = score_list_ordered(user_answer, resolved_correct)
            fb = "숫자 순서 일치율 기반 채점"
        else:
            score = score_exact_or_fuzzy(user_answer, resolved_correct)
            fb = "단답 퍼지 일치 기반 채점"
    else:
        score = score_exact_or_fuzzy(user_answer, resolved_correct)
        fb = "단답 퍼지 일치 기반 채점"

    score = max(0.0, min(1.0, score))
    return score, fb, resolved_correct
