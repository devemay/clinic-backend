import os
from sqlmodel import SQLModel, Session, create_engine

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


def init_db():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session

