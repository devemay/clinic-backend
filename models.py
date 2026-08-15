from datetime import datetime, date
from typing import Optional
from sqlalchemy import Column, Text
from sqlmodel import SQLModel, Field


class Doctor(SQLModel, table=True):
    username: str = Field(primary_key=True, max_length=64)
    display_name: str = Field(max_length=128)
    hashed_password: str = Field(max_length=128)
    # role chỉ để hiển thị (VD "Bác sĩ" / "Học viên"), KHÔNG dùng để phân quyền trực tiếp
    role: str = Field(default="hoc_vien", max_length=16)
    can_create: bool = Field(default=False)  # được tạo mã lưu trữ mới (bệnh án mới / tái khám mới)
    can_export: bool = Field(default=False)  # được xuất dữ liệu tổng hợp nghiên cứu
    is_admin: bool = Field(default=False)  # được cấp/sửa/xoá tài khoản người khác
    # Mọi tài khoản đăng nhập được đều mặc định điền/sửa được dữ liệu trong hồ sơ đã có — không cần cờ riêng


class Patient(SQLModel, table=True):
    ma_bn: str = Field(primary_key=True, max_length=64)
    ho_ten: Optional[str] = Field(default=None, max_length=128)
    gioi_tinh: Optional[str] = Field(default=None, max_length=16)
    nam_sinh: Optional[int] = None
    dia_chi: Optional[str] = Field(default=None, max_length=255)
    sdt: Optional[str] = Field(default=None, max_length=32)


class AACase(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ma_luu_tru: str = Field(index=True, unique=True, max_length=32)  # VD: AA-2026-0001
    ma_bn: str = Field(foreign_key="patient.ma_bn", index=True, max_length=64, unique=True)
    ngay_tao: date = Field(default_factory=date.today, index=True)
    bac_si_tao: Optional[str] = Field(default=None, max_length=64)
    da_dien_du_lieu: bool = Field(default=False)  # False = bác sĩ mới tạo khung, chờ điền
    benh_an_moi: str = Field(default="{}", sa_column=Column(Text))
    # cột trích riêng để tra cứu/lọc nhanh, không phải mở JSON mỗi lần
    muc_do_nang: Optional[str] = Field(default=None, max_length=32, index=True)
    the_lam_sang: Optional[str] = Field(default=None, max_length=64)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AAFollowUp(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    case_id: int = Field(foreign_key="aacase.id", index=True)
    ngay_kham: Optional[date] = Field(default=None, index=True)
    bac_si_tao: Optional[str] = Field(default=None, max_length=64)
    da_dien_du_lieu: bool = Field(default=False)
    data: str = Field(default="{}", sa_column=Column(Text))
    muc_do_nang: Optional[str] = Field(default=None, max_length=32, index=True)
    dieu_tri: Optional[str] = Field(default=None, max_length=255, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AGACase(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ma_luu_tru: str = Field(index=True, unique=True, max_length=32)  # VD: AGA260815001
    ma_bn: str = Field(foreign_key="patient.ma_bn", index=True, max_length=64, unique=True)
    ngay_tao: date = Field(default_factory=date.today, index=True)
    bac_si_tao: Optional[str] = Field(default=None, max_length=64)
    da_dien_du_lieu: bool = Field(default=False)
    benh_an_moi: str = Field(default="{}", sa_column=Column(Text))
    muc_do_nang: Optional[str] = Field(default=None, max_length=32, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AGAFollowUp(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    case_id: int = Field(foreign_key="agacase.id", index=True)
    ngay_kham: Optional[date] = Field(default=None, index=True)
    bac_si_tao: Optional[str] = Field(default=None, max_length=64)
    da_dien_du_lieu: bool = Field(default=False)
    data: str = Field(default="{}", sa_column=Column(Text))
    muc_do_nang: Optional[str] = Field(default=None, max_length=32, index=True)
    dieu_tri: Optional[str] = Field(default=None, max_length=255, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
