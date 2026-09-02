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
_COT_BENH_AN = [("dong_mac", "VARCHAR(64)"), ("gpb_trang_thai", "VARCHAR(8)"), ("gpb_cho_tu", "DATE")]
_COT_TAI_KHAM = [("gpb_trang_thai", "VARCHAR(8)"), ("gpb_cho_tu", "DATE")]

NEW_COLUMNS = {
    "doctor": [("is_admin", "BOOLEAN DEFAULT 0")],
    "patient": [("dan_toc", "VARCHAR(64)"), ("ngay_sinh", "DATE")],
    # Cột trích sẵn từ JSON, thêm sau khi hệ thống đã có dữ liệu thật.
    # Để mặc định NULL (không đặt DEFAULT) để phân biệt "chưa nạp giá trị" với "đã nạp, giá trị rỗng"
    # — hàm backfill_cot_phu() trong main.py dựa vào đó để chỉ chạy 1 lần.
    "aacase": _COT_BENH_AN, "agacase": _COT_BENH_AN, "nonscarcase": _COT_BENH_AN,
    "sacase": _COT_BENH_AN, "ttmcase": _COT_BENH_AN,
    "aafollowup": _COT_TAI_KHAM, "agafollowup": _COT_TAI_KHAM, "nonscarfollowup": _COT_TAI_KHAM,
    "safollowup": _COT_TAI_KHAM, "ttmfollowup": _COT_TAI_KHAM,
}

# Chỉ mục cho các cột mới — create_all() không tự thêm index vào bảng đã tồn tại
NEW_INDEXES = [(t, c) for t in ("aacase", "agacase", "nonscarcase", "sacase", "ttmcase")
               for c in ("dong_mac", "gpb_cho_tu")] + \
              [(t, "gpb_cho_tu") for t in ("aafollowup", "agafollowup", "nonscarfollowup",
                                           "safollowup", "ttmfollowup")]


def ensure_new_indexes():
    inspector = inspect(engine)
    ten_bang = set(inspector.get_table_names())
    with engine.connect() as conn:
        for bang, cot in NEW_INDEXES:
            if bang not in ten_bang:
                continue
            ten_idx = f"ix_{bang}_{cot}"
            if any(i["name"] == ten_idx for i in inspector.get_indexes(bang)):
                continue
            try:
                conn.execute(text(f"CREATE INDEX {ten_idx} ON {bang} ({cot})"))
                conn.commit()
            except Exception:
                pass  # thiếu index chỉ làm chậm, không được để nó chặn khởi động


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
    ensure_new_indexes()


def get_session():
    with Session(engine) as session:
        yield session

