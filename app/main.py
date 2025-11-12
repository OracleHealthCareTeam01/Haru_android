from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .database_oracle import ping_db

try:
    from .router import cognitive
    HAS_COG = True
except Exception:
    HAS_COG = False

app = FastAPI(title="HaruCoach API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if HAS_COG:
    app.include_router(cognitive.router)

@app.get("/health")
def health():
    return {"status": "up", **ping_db()}
