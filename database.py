import os
from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy import text, inspect

import config

if config.USE_MYSQL:
    DATABASE_URL = (
        f"mysql+pymysql://{config.MYSQL_USER}:{config.MYSQL_PASSWORD}"
        f"@{config.MYSQL_HOST}:{config.MYSQL_PORT}/{config.MYSQL_DB}?charset=utf8mb4"
    )
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=280)
else:
    # Chưa cấu hình MySQL -> dùng SQLite tại chỗ để chạy thử, không mất dữ liệu code khi chuyển sang MySQL thật
    DB_PATH = os.path.join(os.path.dirname(__file__), "clinic.db")
    DATABASE_URL = f"sqlite:///{DB_PATH}"
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


# Các cột được thêm vào SAU KHI hệ thống đã có dữ liệu thật — create_all() không tự thêm cột
# vào bảng đã tồn tại, nên cần tự kiểm tra & ALTER TABLE thủ công tại đây (không đụng dữ liệu cũ).
NEW_COLUMNS = {
    "doctor": [("is_admin", "BOOLEAN DEFAULT 0")],
}


def ensure_new_columns():
    inspector = inspect(engine)
    with engine.connect() as conn:
        for table, columns in NEW_COLUMNS.items():
            if table not in inspector.get_table_names():
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for col_name, col_def in columns:
                if col_name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}"))
                    conn.commit()


def init_db():
    SQLModel.metadata.create_all(engine)
    ensure_new_columns()


def get_session():
    with Session(engine) as session:
        yield session

