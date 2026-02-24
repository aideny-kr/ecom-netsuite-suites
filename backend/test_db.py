from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

engine = create_engine("postgresql://postgres:postgres@localhost:5432/ecom_netsuite")
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def check_logs():
    db = SessionLocal()
    try:
        logs = db.execute(text("SELECT method, url, response_status, error_message, source FROM netsuite_api_logs ORDER BY created_at DESC LIMIT 5")).fetchall()
        print("Latest API Logs:")
        for lg in logs:
            print(f"[{lg.source}] {lg.method} {lg.url} -> {lg.response_status} | Error: {lg.error_message}")
    finally:
        db.close()

if __name__ == "__main__":
    check_logs()
