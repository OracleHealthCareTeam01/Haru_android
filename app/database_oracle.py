# app/database_oracle.py
import os
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker
from .config import settings

# TNS/Thick 관련 환경변수 제거(이 프로세스 한정)
for var in ("TNS_ADMIN", "ORACLE_HOME"):
    os.environ.pop(var, None)

def _oracle_url() -> URL:
    if settings.oracle_service:
        query = {"service_name": settings.oracle_service}
    elif settings.oracle_sid:
        query = {"sid": settings.oracle_sid}
    else:
        raise RuntimeError("Set ORACLE_SERVICE or ORACLE_SID in .env")

    return URL.create(
        drivername="oracle+oracledb",  # Thin 모드 (init_oracle_client 호출 금지)
        username=settings.oracle_user,
        password=settings.oracle_password,
        host=settings.oracle_host,
        port=settings.oracle_port,
        query=query,
    )

engine = create_engine(_oracle_url(), pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def ping_db() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM DUAL"))
        return True
    except Exception:
        return False
