import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import oracledb  

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ------------------ 환경 변수 읽기 ------------------
ORACLE_USER = os.getenv("ORACLE_USER")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD")

# tnsnames.ora 안에 있는 alias (team003_medium)
ORACLE_DSN = os.getenv("ORACLE_DSN")

# Instant Client & Wallet 경로
ORACLE_INSTANT_CLIENT = os.getenv(
    "ORACLE_INSTANT_CLIENT"
)

ORACLE_WALLET_DIR = os.getenv(
    "ORACLE_WALLET_DIR"
)

# ------------------ Thick 모드 초기화 (한 번만!) ------------------
# ⚠ 같은 프로세스에서 init_oracle_client는 한 번만 호출 가능
oracledb.init_oracle_client(
    lib_dir=ORACLE_INSTANT_CLIENT,
    config_dir=ORACLE_WALLET_DIR,
)

# ------------------ SQLAlchemy 엔진 URL ------------------
# ✅ 여기서는 host/port/service_name 안 쓰고, test.py와 똑같이 TNS alias만 사용
DB_URL = f"oracle+oracledb://{ORACLE_USER}:{ORACLE_PASSWORD}@{ORACLE_DSN}"

_safe = DB_URL.replace(ORACLE_PASSWORD, "****") if ORACLE_PASSWORD else DB_URL
print(f"[DB URL(check)] { _safe }")

engine = create_engine(
    DB_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=5,
    max_overflow=5,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

def get_db():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def ping_db() -> dict:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM DUAL"))
        return {"db": "ok"}
    except Exception as e:
        return {"db": "fail", "error": str(e)}