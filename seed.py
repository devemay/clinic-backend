from sqlmodel import Session

from auth import hash_password
from database import engine, init_db
from models import Doctor

# can_create=True: được tạo mã lưu trữ mới (bệnh án mới / lần tái khám mới)
# can_export=True: được xuất dữ liệu tổng hợp nghiên cứu
# Mọi tài khoản đăng nhập được đều điền/sửa được dữ liệu trong hồ sơ đã có, không cần cờ riêng

DOCTORS = [
    ("havinh", "BS Hà Vinh"),
    ("tuy", "BS Tuỳ"),
    ("xuan", "BS Xuân"),
    ("trang_nam", "BS Tráng"),
    ("trang_nu", "BS Trang"),
    ("ngocanh", "BS Ngọc Anh"),
]  # can_create=True, can_export=False

RESIDENTS = [
    ("hocvien1", "Học viên NT50 - 1"),
    ("hocvien2", "Học viên NT50 - 2"),
]  # can_create=False, can_export=False — thêm bao nhiêu tuỳ ý theo mẫu này

RESEARCH_MANAGERS = [
    ("nghiencuu", "Tài khoản tổng hợp nghiên cứu"),
]  # can_create=True, can_export=True, is_admin=True — quyền đầy đủ + quản lý tài khoản

DEFAULT_PASSWORD = "123456"

if __name__ == "__main__":
    init_db()
    with Session(engine) as session:
        for username, display_name in DOCTORS:
            if session.get(Doctor, username):
                continue
            session.add(Doctor(
                username=username, display_name=display_name,
                hashed_password=hash_password(DEFAULT_PASSWORD),
                role="bac_si", can_create=True, can_export=False,
            ))
        for username, display_name in RESIDENTS:
            if session.get(Doctor, username):
                continue
            session.add(Doctor(
                username=username, display_name=display_name,
                hashed_password=hash_password(DEFAULT_PASSWORD),
                role="hoc_vien", can_create=False, can_export=False,
            ))
        for username, display_name in RESEARCH_MANAGERS:
            existing = session.get(Doctor, username)
            if existing:
                # Tài khoản đã tồn tại (VD tạo trước khi có quyền admin) — cập nhật lại quyền cho đúng,
                # không bỏ qua như 2 nhóm trên, để chạy lại seed.py là tự khắc phục được.
                existing.can_create = True
                existing.can_export = True
                existing.is_admin = True
                session.add(existing)
                continue
            session.add(Doctor(
                username=username, display_name=display_name,
                hashed_password=hash_password(DEFAULT_PASSWORD),
                role="hoc_vien", can_create=True, can_export=True, is_admin=True,
            ))
        session.commit()

    print(f"Đã tạo tài khoản, mật khẩu mặc định: {DEFAULT_PASSWORD}")
    print("-- Bác sĩ (tạo mã + điền dữ liệu, KHÔNG xuất được) --")
    for u, d in DOCTORS:
        print(f"  {u}  ({d})")
    print("-- Học viên (chỉ điền dữ liệu) --")
    for u, d in RESIDENTS:
        print(f"  {u}  ({d})")
    print("-- Tài khoản quyền đầy đủ (tạo mã + điền + xuất) --")
    for u, d in RESEARCH_MANAGERS:
        print(f"  {u}  ({d})")
