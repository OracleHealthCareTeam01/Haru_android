# test/app/main.py
from fastapi import FastAPI
from .database_oracle import ping_db
from .router import users, today, questions, sessions
from .router import auth_android, quiz

app = FastAPI(title="Cognitive API on Oracle")

try:
    ping_db()  # 실패하면 경고만 찍고 계속 진행
except Exception as e:
    print("[WARN] DB ping failed:", e)

app.include_router(users.router)
app.include_router(today.router)
app.include_router(questions.router)
app.include_router(sessions.router)
app.include_router(auth_android.router)
app.include_router(quiz.router)

@app.get("/")
def root():
    return {"ok": True}

# 추가: DB 헬스체크
@app.get("/health")
def health():
    try:
        ping_db()  # SELECT 1 FROM DUAL 실행
        return {"db": "ok"}
    except Exception as e:
        return {"db": "fail", "error": str(e)}
