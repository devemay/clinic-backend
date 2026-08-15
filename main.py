import csv
import io
import json
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlmodel import Session, select

from auth import authenticate_doctor, create_access_token, get_current_doctor, require_export_permission, require_create_permission, require_admin, hash_password, verify_password
from database import get_session, init_db
from models import AACase, AAFollowUp, AGACase, AGAFollowUp, Doctor, Patient
from storage import get_storage, refresh_url

app = FastAPI(title="Bệnh án nghiên cứu — API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()


# ---------- schemas ----------
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    display_name: str
    role: str
    can_create: bool
    can_export: bool
    is_admin: bool


class PatientIn(BaseModel):
    ma_bn: str
    ho_ten: Optional[str] = None
    gioi_tinh: Optional[str] = None
    nam_sinh: Optional[int] = None
    dia_chi: Optional[str] = None
    sdt: Optional[str] = None


class CreateCaseIn(BaseModel):
    ngay_kham: Optional[str] = None


class CreateFollowUpIn(BaseModel):
    ngay_kham: Optional[str] = None


class DataIn(BaseModel):
    data: Dict[str, Any]


SALT_VUNG = [("dinh", 40), ("cham", 24), ("tdPhai", 18), ("tdTrai", 18)]


def calc_salt(vung: Dict[str, Any]) -> float:
    total = 0.0
    for key, weight in SALT_VUNG:
        b = float((vung or {}).get(key, {}).get("b", 0) or 0)
        total += weight * b / 100
    return round(total, 1)


def mucdo_from_salt(score: float) -> str:
    if score <= 0:
        return "Không rụng tóc"
    if score <= 20:
        return "Nhẹ/giới hạn"
    if score <= 49:
        return "Trung bình"
    if score <= 94:
        return "Nặng"
    return "Rất nặng"


def mucdo_sau_dieu_chinh(score: float, yeu_to_nang_bac: Optional[list]) -> str:
    levels = ["Không rụng tóc", "Nhẹ/giới hạn", "Trung bình", "Nặng", "Rất nặng"]
    base = mucdo_from_salt(score)
    idx = levels.index(base)
    if yeu_to_nang_bac and idx < len(levels) - 1:
        return levels[idx + 1]
    return base


def compute_gpb_status(data: dict):
    """Khớp đúng logic gpbStatus() bên frontend: None | {'type':'waiting','days':N} | {'type':'done'}"""
    if not data or data.get("gpbCo") != "Có":
        return None
    if data.get("gpbKetQua") and str(data["gpbKetQua"]).strip():
        return {"type": "done"}
    if data.get("gpbNgayThucHien"):
        try:
            ngay = date.fromisoformat(str(data["gpbNgayThucHien"])[:10])
            days = max(0, (date.today() - ngay).days)
            return {"type": "waiting", "days": days}
        except ValueError:
            return None
    return None


# ---------- kiểm tra "đã điền": mỗi mục phải có ít nhất 1 trường có giá trị (khớp logic frontend) ----------
def is_filled(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, bool):
        return True
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, list):
        return len(v) > 0
    if isinstance(v, dict):
        return any(is_filled(x) for x in v.values())
    return bool(v)


def section_filled(data: dict, keys: list) -> bool:
    return any(is_filled(data.get(k)) for k in keys)


def all_sections_filled(data: dict, section_map: dict) -> bool:
    return all(section_filled(data, keys) for keys in section_map.values())


def refresh_images(data: dict) -> dict:
    if not isinstance(data, dict):
        return data
    d = dict(data)
    if isinstance(d.get("anh"), list):
        d["anh"] = [refresh_url(u) for u in d["anh"]]
    return d


NEW_CASE_SECTIONS = {
    "Hành chính": ["ngayKham", "bacSiKham", "ngheNghiep", "trinhDo", "chieuCao", "canNang"],
    "Bệnh sử - Tiền sử": ["tuoiKhoiPhat", "thoiGianMacBenh", "soDotTaiPhat", "benhSuTruoc", "yeuToKhoiPhat", "dieuTriTruocDoStatus", "thuocDangDung", "tienSuBanThan", "tienSuGiaDinh"],
    "Khám thực thể": ["sotStatus", "mach", "ha", "viTriRungToc", "pullTest", "tocToMoc", "viTriTonThuong", "tonThuongMong", "trieuChungCoNang", "theLamSang"],
    "Mức độ nặng (SALT)": ["soLuongMang", "dienTichThucTe", "vung", "mangDai", "mangRong", "mangViTri", "yeuToNangBac"],
    "Dermoscopy": ["dermoscopy"],
    "Cận lâm sàng": ["labs", "treponema", "viNam", "sieuAmTuyenGiap", "moBenhHoc", "il15", "il13", "ifnG", "ifnGMo", "il13Mo", "ngayLayMau"],
    "Giải phẫu bệnh": ["gpbCo"],
    "Điều trị & thủ thuật": ["dieuTri", "vas", "tdkm", "henKham"],
    "Hình ảnh": ["anh"],
}
FOLLOWUP_SECTIONS = {
    "Lâm sàng": ["ngayKham", "bacSiKham", "lamSang", "pullTest", "tocToMoc", "mucDoSoVoiTruoc", "tacDungPhuStatus"],
    "Mức độ nặng (SALT)": ["soLuongMang", "vung", "mucDoDapUng", "mangDai", "mangRong", "mangViTri", "yeuToNangBac"],
    "Dermoscopy": ["dermoscopy", "vas", "tdkm"],
    "Cận lâm sàng & điều trị": ["xnStatus", "xnKetQua", "dieuTri"],
    "Giải phẫu bệnh": ["gpbCo"],
    "Hình ảnh": ["anh"],
}

NEW_AGA_CASE_SECTIONS = {
    "Hành chính": ["ngayKham", "bacSiKham", "luuHuyetTuong", "luuHuyetThanh"],
    "Bệnh sử - Tiền sử": ["thoiGianKhoiPhat", "benhSuTruoc", "tienSuBanThan", "tienSuGiaDinh"],
    "Khám thực thể": ["canNang", "chieuCao", "vongBung", "mach", "ha", "dauHieuCuongAndrogen", "phanBoRungToc", "matDoToc", "duongKinhSoiToc", "pullTest"],
    "Thang điểm": ["hamiltonNorwood", "sinclairScale", "ludwig", "pcos"],
    "Cận lâm sàng": ["labs", "sieuAmOBung", "sieuAmTuyenGiap", "moBenhHoc", "dermoscopy", "vungTran", "vungDinh", "vungCham", "xnKhac"],
    "Giải phẫu bệnh": ["gpbCo"],
    "Điều trị & thủ thuật": ["dieuTri", "henKham"],
    "Hình ảnh": ["anh"],
}
FOLLOWUP_AGA_SECTIONS = {
    "Lâm sàng": ["ngayKham", "bacSiKham", "lamSang", "pullTest", "mucDoSoVoiTruoc"],
    "Thang điểm": ["hamiltonNorwood", "sinclairScale"],
    "Tác dụng phụ & Xét nghiệm": ["tacDungPhuStatus", "xnStatus"],
    "Giải phẫu bệnh": ["gpbCo"],
    "Điều trị": ["dieuTri"],
    "Hình ảnh": ["anh"],
}


def parse_date(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def next_ma_luu_tru(session: Session, disease: str, model=AACase) -> str:
    today = date.today()
    prefix = f"{disease}{today.strftime('%y%m%d')}"
    existing = session.exec(
        select(model.ma_luu_tru).where(model.ma_luu_tru.like(f"{prefix}%"))
    ).all()
    max_seq = 0
    for m in existing:
        try:
            max_seq = max(max_seq, int(m[len(prefix):]))
        except ValueError:
            continue
    return f"{prefix}{max_seq + 1:03d}"


# ---------- auth ----------
@app.post("/auth/login", response_model=Token)
def login(form: OAuth2PasswordRequestForm = Depends(), session: Session = Depends(get_session)):
    doctor = authenticate_doctor(session, form.username, form.password)
    if not doctor:
        raise HTTPException(status_code=401, detail="Sai tên đăng nhập hoặc mật khẩu")
    token = create_access_token(doctor.username)
    return Token(access_token=token, display_name=doctor.display_name, role=doctor.role, can_create=doctor.can_create, can_export=doctor.can_export, is_admin=doctor.is_admin)


@app.get("/auth/me")
def me(doctor: Doctor = Depends(get_current_doctor)):
    return {"username": doctor.username, "display_name": doctor.display_name, "role": doctor.role, "can_create": doctor.can_create, "can_export": doctor.can_export, "is_admin": doctor.is_admin}


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


@app.post("/auth/change-password")
def change_password(
    payload: ChangePasswordIn,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(get_current_doctor),
):
    if not verify_password(payload.old_password, doctor.hashed_password):
        raise HTTPException(status_code=400, detail="Mật khẩu hiện tại không đúng")
    if len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="Mật khẩu mới phải có ít nhất 6 ký tự")
    doctor.hashed_password = hash_password(payload.new_password)
    session.add(doctor)
    session.commit()
    return {"ok": True}


# ---------- quản lý tài khoản (chỉ admin) ----------
class DoctorCreateIn(BaseModel):
    username: str
    display_name: str
    password: str
    role: str = "hoc_vien"  # chỉ để hiển thị, không quyết định quyền
    can_create: bool = False
    can_export: bool = False
    is_admin: bool = False


class DoctorPermissionsIn(BaseModel):
    display_name: Optional[str] = None
    role: Optional[str] = None
    can_create: Optional[bool] = None
    can_export: Optional[bool] = None
    is_admin: Optional[bool] = None


class ResetPasswordIn(BaseModel):
    new_password: str


def doctor_public(d: Doctor) -> dict:
    return {
        "username": d.username, "display_name": d.display_name, "role": d.role,
        "can_create": d.can_create, "can_export": d.can_export, "is_admin": d.is_admin,
    }


@app.get("/admin/doctors")
def list_doctors(session: Session = Depends(get_session), admin: Doctor = Depends(require_admin)):
    doctors = session.exec(select(Doctor).order_by(Doctor.username)).all()
    return [doctor_public(d) for d in doctors]


@app.post("/admin/doctors")
def create_doctor(payload: DoctorCreateIn, session: Session = Depends(get_session), admin: Doctor = Depends(require_admin)):
    if session.get(Doctor, payload.username):
        raise HTTPException(status_code=400, detail="Tên đăng nhập đã tồn tại")
    if len(payload.password) < 6:
        raise HTTPException(status_code=400, detail="Mật khẩu phải có ít nhất 6 ký tự")
    d = Doctor(
        username=payload.username, display_name=payload.display_name,
        hashed_password=hash_password(payload.password), role=payload.role,
        can_create=payload.can_create, can_export=payload.can_export, is_admin=payload.is_admin,
    )
    session.add(d)
    session.commit()
    return doctor_public(d)


@app.put("/admin/doctors/{username}")
def update_doctor_permissions(username: str, payload: DoctorPermissionsIn, session: Session = Depends(get_session), admin: Doctor = Depends(require_admin)):
    d = session.get(Doctor, username)
    if not d:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản")
    if username == admin.username and payload.is_admin is False:
        raise HTTPException(status_code=400, detail="Không thể tự bỏ quyền admin của chính mình")
    for field in ["display_name", "role", "can_create", "can_export", "is_admin"]:
        value = getattr(payload, field)
        if value is not None:
            setattr(d, field, value)
    session.add(d)
    session.commit()
    return doctor_public(d)


@app.post("/admin/doctors/{username}/reset-password")
def admin_reset_password(username: str, payload: ResetPasswordIn, session: Session = Depends(get_session), admin: Doctor = Depends(require_admin)):
    d = session.get(Doctor, username)
    if not d:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản")
    if len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="Mật khẩu mới phải có ít nhất 6 ký tự")
    d.hashed_password = hash_password(payload.new_password)
    session.add(d)
    session.commit()
    return {"ok": True}


@app.delete("/admin/doctors/{username}")
def delete_doctor(username: str, session: Session = Depends(get_session), admin: Doctor = Depends(require_admin)):
    if username == admin.username:
        raise HTTPException(status_code=400, detail="Không thể tự xoá tài khoản của chính mình")
    d = session.get(Doctor, username)
    if not d:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản")
    session.delete(d)
    session.commit()
    return {"ok": True}


# ---------- patients ----------
@app.get("/patients/{ma_bn}")
def get_patient(ma_bn: str, session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)):
    p = session.get(Patient, ma_bn)
    if not p:
        # Mã BN thật thường có số 0 ở đầu (VD 0030053708) nhưng người dùng hay gõ tắt bỏ số 0
        # (VD 30053708). Nếu không khớp chính xác, thử so khớp với mã đã bỏ số 0 ở đầu của từng
        # bệnh nhân trong hệ thống.
        query_stripped = ma_bn.lstrip("0")
        if query_stripped:
            for candidate in session.exec(select(Patient)).all():
                if candidate.ma_bn.lstrip("0") == query_stripped:
                    p = candidate
                    break
    if not p:
        raise HTTPException(status_code=404, detail="Không tìm thấy bệnh nhân")
    return p


@app.post("/patients")
def upsert_patient(payload: PatientIn, session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)):
    p = session.get(Patient, payload.ma_bn)
    if p:
        for k, v in payload.dict().items():
            setattr(p, k, v)
    else:
        p = Patient(**payload.dict())
        session.add(p)
    session.commit()
    session.refresh(p)
    return p


@app.delete("/patients/{ma_bn}")
def delete_patient(ma_bn: str, session: Session = Depends(get_session), doctor: Doctor = Depends(require_export_permission)):
    """Xoá toàn bộ hồ sơ của 1 bệnh nhân (bệnh án + mọi lần tái khám + thông tin bệnh nhân) —
    dùng để dọn dữ liệu demo/test, không thể hoàn tác. Chỉ tài khoản quyền đầy đủ mới xoá được."""
    p = session.get(Patient, ma_bn)
    if not p:
        raise HTTPException(status_code=404, detail="Không tìm thấy bệnh nhân")
    case = session.exec(select(AACase).where(AACase.ma_bn == ma_bn)).first()
    if case:
        for f in session.exec(select(AAFollowUp).where(AAFollowUp.case_id == case.id)).all():
            session.delete(f)
        session.delete(case)
    session.delete(p)
    session.commit()
    return {"ok": True}


@app.delete("/patients/{ma_bn}")
def delete_patient(ma_bn: str, session: Session = Depends(get_session), doctor: Doctor = Depends(require_export_permission)):
    """Xoá toàn bộ hồ sơ của 1 bệnh nhân (bệnh án + mọi lần tái khám + thông tin bệnh nhân) —
    dùng để dọn dữ liệu demo/test, không thể hoàn tác. Chỉ tài khoản quyền đầy đủ mới xoá được."""
    p = session.get(Patient, ma_bn)
    if not p:
        raise HTTPException(status_code=404, detail="Không tìm thấy bệnh nhân")
    case = session.exec(select(AACase).where(AACase.ma_bn == ma_bn)).first()
    if case:
        for f in session.exec(select(AAFollowUp).where(AAFollowUp.case_id == case.id)).all():
            session.delete(f)
        session.delete(case)
    session.delete(p)
    session.commit()
    return {"ok": True}


# ---------- AA case: tạo mã lưu trữ (chỉ bác sĩ) ----------
@app.post("/cases/{ma_bn}/aa/create")
def create_case(
    ma_bn: str,
    payload: CreateCaseIn,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(require_create_permission),
):
    if not session.get(Patient, ma_bn):
        raise HTTPException(status_code=404, detail="Bệnh nhân chưa tồn tại — tạo bệnh nhân trước")
    existing = session.exec(select(AACase).where(AACase.ma_bn == ma_bn)).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Bệnh nhân đã có mã lưu trữ AA: {existing.ma_luu_tru}")
    ma_luu_tru = next_ma_luu_tru(session, "AA")
    case = AACase(
        ma_luu_tru=ma_luu_tru,
        ma_bn=ma_bn,
        bac_si_tao=doctor.display_name,
        benh_an_moi=json.dumps({"ngayKham": payload.ngay_kham or date.today().isoformat()}, ensure_ascii=False),
        da_dien_du_lieu=False,
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    return {"ok": True, "case_id": case.id, "ma_luu_tru": case.ma_luu_tru}


@app.get("/cases/{ma_bn}/aa")
def get_aa_case(ma_bn: str, session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)):
    case = session.exec(select(AACase).where(AACase.ma_bn == ma_bn)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Bệnh nhân chưa có bệnh án AA")
    followups = session.exec(
        select(AAFollowUp).where(AAFollowUp.case_id == case.id).order_by(AAFollowUp.ngay_kham)
    ).all()
    return {
        "ma_luu_tru": case.ma_luu_tru,
        "da_dien_du_lieu": case.da_dien_du_lieu,
        "bac_si_tao": case.bac_si_tao,
        "benh_an_moi": refresh_images(json.loads(case.benh_an_moi)),
        "tai_khams": [
            {"id": f.id, "ngay_kham": f.ngay_kham, "da_dien_du_lieu": f.da_dien_du_lieu, "bac_si_tao": f.bac_si_tao, **refresh_images(json.loads(f.data))}
            for f in followups
        ],
        "updated_at": case.updated_at,
    }


# ---------- điền / sửa dữ liệu (bác sĩ hoặc học viên) ----------
@app.put("/cases/{ma_bn}/aa")
def save_case_data(
    ma_bn: str, payload: DataIn, session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)
):
    case = session.exec(select(AACase).where(AACase.ma_bn == ma_bn)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Chưa có mã lưu trữ — bác sĩ cần tạo bệnh án trước")
    case.benh_an_moi = json.dumps(payload.data, ensure_ascii=False)
    case.da_dien_du_lieu = all_sections_filled(payload.data, NEW_CASE_SECTIONS)
    salt = calc_salt(payload.data.get("vung", {}))
    case.muc_do_nang = mucdo_sau_dieu_chinh(salt, payload.data.get("yeuToNangBac"))
    case.the_lam_sang = payload.data.get("theLamSang")
    case.updated_at = datetime.utcnow()
    session.add(case)
    session.commit()
    return {"ok": True, "ma_luu_tru": case.ma_luu_tru}


# ---------- tái khám: tạo mã (chỉ bác sĩ) ----------
@app.post("/cases/{ma_bn}/aa/followups/create")
def create_followup(
    ma_bn: str,
    payload: CreateFollowUpIn,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(require_create_permission),
):
    case = session.exec(select(AACase).where(AACase.ma_bn == ma_bn)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Bệnh nhân chưa có mã lưu trữ AA")
    ngay = payload.ngay_kham or date.today().isoformat()
    fu = AAFollowUp(
        case_id=case.id,
        ngay_kham=parse_date(ngay),
        bac_si_tao=doctor.display_name,
        data=json.dumps({"ngayKham": ngay}, ensure_ascii=False),
        da_dien_du_lieu=False,
    )
    session.add(fu)
    session.commit()
    session.refresh(fu)
    return {"ok": True, "followup_id": fu.id, "ma_luu_tru": case.ma_luu_tru}


@app.put("/cases/{ma_bn}/aa/followups/{followup_id}")
def save_followup_data(
    ma_bn: str,
    followup_id: int,
    payload: DataIn,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(get_current_doctor),
):
    fu = session.get(AAFollowUp, followup_id)
    if not fu:
        raise HTTPException(status_code=404, detail="Không tìm thấy lần tái khám")
    fu.data = json.dumps(payload.data, ensure_ascii=False)
    fu.ngay_kham = parse_date(payload.data.get("ngayKham")) or fu.ngay_kham
    fu.da_dien_du_lieu = all_sections_filled(payload.data, FOLLOWUP_SECTIONS)
    case = session.get(AACase, fu.case_id)
    salt_now = calc_salt(payload.data.get("vung", {}))
    fu.muc_do_nang = mucdo_sau_dieu_chinh(salt_now, payload.data.get("yeuToNangBac"))
    fu.dieu_tri = (payload.data.get("dieuTri") or "")[:255]
    session.add(fu)
    session.commit()
    return {"ok": True}


# ---------- AGA case: tạo mã lưu trữ (chỉ bác sĩ) ----------
@app.post("/cases/{ma_bn}/aga/create")
def create_aga_case(
    ma_bn: str,
    payload: CreateCaseIn,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(require_create_permission),
):
    if not session.get(Patient, ma_bn):
        raise HTTPException(status_code=404, detail="Bệnh nhân chưa tồn tại — tạo bệnh nhân trước")
    existing = session.exec(select(AGACase).where(AGACase.ma_bn == ma_bn)).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Bệnh nhân đã có mã lưu trữ AGA: {existing.ma_luu_tru}")
    ma_luu_tru = next_ma_luu_tru(session, "AGA", AGACase)
    case = AGACase(
        ma_luu_tru=ma_luu_tru,
        ma_bn=ma_bn,
        bac_si_tao=doctor.display_name,
        benh_an_moi=json.dumps({"ngayKham": payload.ngay_kham or date.today().isoformat()}, ensure_ascii=False),
        da_dien_du_lieu=False,
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    return {"ok": True, "case_id": case.id, "ma_luu_tru": case.ma_luu_tru}


@app.get("/cases/{ma_bn}/aga")
def get_aga_case(ma_bn: str, session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)):
    case = session.exec(select(AGACase).where(AGACase.ma_bn == ma_bn)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Bệnh nhân chưa có bệnh án AGA")
    followups = session.exec(
        select(AGAFollowUp).where(AGAFollowUp.case_id == case.id).order_by(AGAFollowUp.ngay_kham)
    ).all()
    return {
        "ma_luu_tru": case.ma_luu_tru,
        "da_dien_du_lieu": case.da_dien_du_lieu,
        "bac_si_tao": case.bac_si_tao,
        "benh_an_moi": refresh_images(json.loads(case.benh_an_moi)),
        "tai_khams": [
            {"id": f.id, "ngay_kham": f.ngay_kham, "da_dien_du_lieu": f.da_dien_du_lieu, "bac_si_tao": f.bac_si_tao, **refresh_images(json.loads(f.data))}
            for f in followups
        ],
        "updated_at": case.updated_at,
    }


@app.put("/cases/{ma_bn}/aga")
def save_aga_case_data(
    ma_bn: str, payload: DataIn, session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)
):
    case = session.exec(select(AGACase).where(AGACase.ma_bn == ma_bn)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Chưa có mã lưu trữ — bác sĩ cần tạo bệnh án trước")
    case.benh_an_moi = json.dumps(payload.data, ensure_ascii=False)
    case.da_dien_du_lieu = all_sections_filled(payload.data, NEW_AGA_CASE_SECTIONS)
    case.updated_at = datetime.utcnow()
    session.add(case)
    session.commit()
    return {"ok": True, "ma_luu_tru": case.ma_luu_tru}


@app.post("/cases/{ma_bn}/aga/followups/create")
def create_aga_followup(
    ma_bn: str,
    payload: CreateFollowUpIn,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(require_create_permission),
):
    case = session.exec(select(AGACase).where(AGACase.ma_bn == ma_bn)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Bệnh nhân chưa có mã lưu trữ AGA")
    ngay = payload.ngay_kham or date.today().isoformat()
    fu = AGAFollowUp(
        case_id=case.id,
        ngay_kham=parse_date(ngay),
        bac_si_tao=doctor.display_name,
        data=json.dumps({"ngayKham": ngay}, ensure_ascii=False),
        da_dien_du_lieu=False,
    )
    session.add(fu)
    session.commit()
    session.refresh(fu)
    return {"ok": True, "followup_id": fu.id, "ma_luu_tru": case.ma_luu_tru}


@app.put("/cases/{ma_bn}/aga/followups/{followup_id}")
def save_aga_followup_data(
    ma_bn: str,
    followup_id: int,
    payload: DataIn,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(get_current_doctor),
):
    fu = session.get(AGAFollowUp, followup_id)
    if not fu:
        raise HTTPException(status_code=404, detail="Không tìm thấy lần tái khám")
    fu.data = json.dumps(payload.data, ensure_ascii=False)
    fu.ngay_kham = parse_date(payload.data.get("ngayKham")) or fu.ngay_kham
    fu.da_dien_du_lieu = all_sections_filled(payload.data, FOLLOWUP_AGA_SECTIONS)
    fu.dieu_tri = (payload.data.get("dieuTri") or "")[:255]
    session.add(fu)
    session.commit()
    return {"ok": True}


# ---------- dashboard ----------
@app.get("/dashboard/today")
def dashboard_today(session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)):
    today = date.today()
    new_cases = session.exec(select(AACase).where(AACase.ngay_tao == today)).all()
    followups = session.exec(select(AAFollowUp).where(AAFollowUp.ngay_kham == today)).all()

    out = []
    for c in new_cases:
        p = session.get(Patient, c.ma_bn)
        d = json.loads(c.benh_an_moi)
        out.append({
            "loai": "Bệnh án mới", "ma_luu_tru": c.ma_luu_tru, "ma_bn": c.ma_bn, "benh": "AA",
            "ho_ten": p.ho_ten if p else None, "da_dien_du_lieu": c.da_dien_du_lieu,
            "bac_si_tao": c.bac_si_tao, "followup_id": None, "dieu_tri": d.get("dieuTri", ""),
            "gpb_co": d.get("gpbCo"), "gpb_ngay_thuc_hien": d.get("gpbNgayThucHien"), "gpb_ket_qua": d.get("gpbKetQua"),
            "has_anh": bool(d.get("anh")),
        })
    for f in followups:
        c = session.get(AACase, f.case_id)
        p = session.get(Patient, c.ma_bn) if c else None
        fd = json.loads(f.data)
        out.append({
            "loai": "Tái khám", "ma_luu_tru": c.ma_luu_tru if c else None, "ma_bn": c.ma_bn if c else None, "benh": "AA",
            "ho_ten": p.ho_ten if p else None, "da_dien_du_lieu": f.da_dien_du_lieu,
            "bac_si_tao": f.bac_si_tao, "followup_id": f.id, "dieu_tri": f.dieu_tri or "",
            "gpb_co": fd.get("gpbCo"), "gpb_ngay_thuc_hien": fd.get("gpbNgayThucHien"), "gpb_ket_qua": fd.get("gpbKetQua"),
            "has_anh": bool(fd.get("anh")),
        })
    return {"ngay": today.isoformat(), "tong_so": len(out), "danh_sach": out}


@app.get("/gpb/waitlist")
def gpb_waitlist(session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)):
    """Danh sách chờ giải phẫu bệnh — quét toàn bộ bệnh nhân (không chỉ hôm nay), mở cho mọi tài khoản đăng nhập."""
    out = []
    for c in session.exec(select(AACase)).all():
        d = json.loads(c.benh_an_moi)
        st = compute_gpb_status(d)
        if st and st["type"] == "waiting":
            p = session.get(Patient, c.ma_bn)
            out.append({"loai": "Bệnh án mới", "ma_bn": c.ma_bn, "ho_ten": p.ho_ten if p else None, "ma_luu_tru": c.ma_luu_tru, "days": st["days"], "followup_id": None})
        followups = session.exec(select(AAFollowUp).where(AAFollowUp.case_id == c.id).order_by(AAFollowUp.ngay_kham)).all()
        for i, f in enumerate(followups):
            fd = json.loads(f.data)
            st = compute_gpb_status(fd)
            if st and st["type"] == "waiting":
                p = session.get(Patient, c.ma_bn)
                out.append({"loai": f"Tái khám {i + 1}", "ma_bn": c.ma_bn, "ho_ten": p.ho_ten if p else None, "ma_luu_tru": c.ma_luu_tru, "days": st["days"], "followup_id": f.id})
    out.sort(key=lambda r: -r["days"])
    return out


def get_json_path(json_str: str, path: str):
    try:
        obj = json.loads(json_str)
    except Exception:
        return None
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


@app.get("/cases/search")
def search_cases(
    ten_bn: Optional[str] = None,
    tu_ngay: Optional[str] = None,
    den_ngay: Optional[str] = None,
    muc_do: Optional[str] = None,
    dieu_tri_chua: Optional[str] = None,
    xet_nghiem_co: Optional[str] = None,
    chi_chua_dien: Optional[bool] = None,
    so_luot_tai_kham_it_nhat: Optional[int] = None,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(get_current_doctor),
):
    results = []

    # ---------- Chế độ thống kê theo số lượt tái khám tối thiểu ----------
    # Tìm bệnh nhân đủ số lượt tái khám (và đúng tên nếu có lọc thêm), trả về TOÀN BỘ
    # bản ghi của họ (T0 + mọi lần tái khám) — bỏ qua các bộ lọc ngày/mức độ/điều trị khác.
    if so_luot_tai_kham_it_nhat and so_luot_tai_kham_it_nhat > 0:
        for c in session.exec(select(AACase)).all():
            p = session.get(Patient, c.ma_bn)
            if ten_bn and ten_bn.lower() not in ((p.ho_ten if p else "") or "").lower():
                continue
            followups = session.exec(
                select(AAFollowUp).where(AAFollowUp.case_id == c.id).order_by(AAFollowUp.ngay_kham)
            ).all()
            so_luot_tk = len(followups)
            if so_luot_tk < so_luot_tai_kham_it_nhat:
                continue
            d0 = json.loads(c.benh_an_moi)
            results.append({
                "loai": "Bệnh án mới", "ma_luu_tru": c.ma_luu_tru, "ma_bn": c.ma_bn,
                "ho_ten": p.ho_ten if p else None, "ngay": c.ngay_tao.isoformat() if c.ngay_tao else None,
                "muc_do_nang": c.muc_do_nang, "da_dien_du_lieu": c.da_dien_du_lieu, "followup_id": None,
                "so_luot_tai_kham": so_luot_tk,
                "gpb_co": d0.get("gpbCo"), "gpb_ngay_thuc_hien": d0.get("gpbNgayThucHien"), "gpb_ket_qua": d0.get("gpbKetQua"),
                "has_anh": bool(d0.get("anh")),
            })
            for i, f in enumerate(followups):
                fd = json.loads(f.data)
                results.append({
                    "loai": f"Tái khám {i + 1}", "ma_luu_tru": c.ma_luu_tru, "ma_bn": c.ma_bn,
                    "ho_ten": p.ho_ten if p else None, "ngay": f.ngay_kham.isoformat() if f.ngay_kham else None,
                    "muc_do_nang": f.muc_do_nang, "da_dien_du_lieu": f.da_dien_du_lieu, "followup_id": f.id,
                    "so_luot_tai_kham": so_luot_tk,
                    "gpb_co": fd.get("gpbCo"), "gpb_ngay_thuc_hien": fd.get("gpbNgayThucHien"), "gpb_ket_qua": fd.get("gpbKetQua"),
            "has_anh": bool(fd.get("anh")),
                })
        results.sort(key=lambda r: (r["ma_bn"] or "", r["ngay"] or ""))
        return {"tong_so": len(results), "ket_qua": results}

    q = select(AACase)
    if muc_do:
        q = q.where(AACase.muc_do_nang == muc_do)
    if chi_chua_dien is not None:
        q = q.where(AACase.da_dien_du_lieu == (not chi_chua_dien))
    if tu_ngay:
        q = q.where(AACase.ngay_tao >= tu_ngay)
    if den_ngay:
        q = q.where(AACase.ngay_tao <= den_ngay)
    for c in session.exec(q).all():
        p = session.get(Patient, c.ma_bn)
        if ten_bn and ten_bn.lower() not in ((p.ho_ten if p else "") or "").lower():
            continue
        if dieu_tri_chua and dieu_tri_chua.lower() not in str(get_json_path(c.benh_an_moi, "dieuTri") or "").lower():
            continue
        if xet_nghiem_co and not get_json_path(c.benh_an_moi, xet_nghiem_co):
            continue
        d0 = json.loads(c.benh_an_moi)
        results.append({
            "loai": "Bệnh án mới", "ma_luu_tru": c.ma_luu_tru, "ma_bn": c.ma_bn,
            "ho_ten": p.ho_ten if p else None, "ngay": c.ngay_tao.isoformat() if c.ngay_tao else None,
            "muc_do_nang": c.muc_do_nang, "da_dien_du_lieu": c.da_dien_du_lieu, "followup_id": None,
            "gpb_co": d0.get("gpbCo"), "gpb_ngay_thuc_hien": d0.get("gpbNgayThucHien"), "gpb_ket_qua": d0.get("gpbKetQua"),
                "has_anh": bool(d0.get("anh")),
        })

    q2 = select(AAFollowUp)
    if muc_do:
        q2 = q2.where(AAFollowUp.muc_do_nang == muc_do)
    if dieu_tri_chua:
        q2 = q2.where(AAFollowUp.dieu_tri.like(f"%{dieu_tri_chua}%"))
    if chi_chua_dien is not None:
        q2 = q2.where(AAFollowUp.da_dien_du_lieu == (not chi_chua_dien))
    if tu_ngay:
        q2 = q2.where(AAFollowUp.ngay_kham >= tu_ngay)
    if den_ngay:
        q2 = q2.where(AAFollowUp.ngay_kham <= den_ngay)
    for f in session.exec(q2).all():
        if xet_nghiem_co and not get_json_path(f.data, xet_nghiem_co):
            continue
        c = session.get(AACase, f.case_id)
        p = session.get(Patient, c.ma_bn) if c else None
        if ten_bn and ten_bn.lower() not in ((p.ho_ten if p else "") or "").lower():
            continue
        fd = json.loads(f.data)
        results.append({
            "loai": "Tái khám", "ma_luu_tru": c.ma_luu_tru if c else None, "ma_bn": c.ma_bn if c else None,
            "ho_ten": p.ho_ten if p else None, "ngay": f.ngay_kham.isoformat() if f.ngay_kham else None,
            "muc_do_nang": f.muc_do_nang, "da_dien_du_lieu": f.da_dien_du_lieu, "followup_id": f.id,
            "gpb_co": fd.get("gpbCo"), "gpb_ngay_thuc_hien": fd.get("gpbNgayThucHien"), "gpb_ket_qua": fd.get("gpbKetQua"),
            "has_anh": bool(fd.get("anh")),
        })

    results.sort(key=lambda r: r["ngay"] or "", reverse=True)
    return {"tong_so": len(results), "ket_qua": results}


@app.get("/cases/recent")
def recent_cases(limit: int = 8, session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)):
    cases = session.exec(select(AACase).order_by(AACase.updated_at.desc()).limit(limit)).all()
    out = []
    for c in cases:
        p = session.get(Patient, c.ma_bn)
        fu_count = len(session.exec(select(AAFollowUp).where(AAFollowUp.case_id == c.id)).all())
        salt = calc_salt(json.loads(c.benh_an_moi).get("vung", {}))
        out.append({"ma_luu_tru": c.ma_luu_tru, "ma_bn": c.ma_bn, "ho_ten": p.ho_ten if p else None, "salt": salt, "so_lan_tk": fu_count})
    return out


# ---------- xuất dữ liệu nghiên cứu (chỉ tài khoản được cấp quyền) ----------
@app.get("/export/raw")
def export_raw(session: Session = Depends(get_session), doctor: Doctor = Depends(require_export_permission)):
    """Trả về toàn bộ dữ liệu AA (mọi bệnh nhân) dạng JSON đầy đủ — dùng để dựng file Excel phía trình duyệt."""
    out = []
    for c in session.exec(select(AACase)).all():
        p = session.get(Patient, c.ma_bn)
        followups = session.exec(
            select(AAFollowUp).where(AAFollowUp.case_id == c.id).order_by(AAFollowUp.ngay_kham)
        ).all()
        out.append({
            "maBN": c.ma_bn,
            "patient": {
                "hoTen": p.ho_ten if p else None, "gioiTinh": p.gioi_tinh if p else None,
                "namSinh": p.nam_sinh if p else None,
            },
            "case": {
                "maLuuTru": c.ma_luu_tru, "ngayTao": c.ngay_tao.isoformat() if c.ngay_tao else None,
                "daDienDuLieu": c.da_dien_du_lieu,
                "benhAnMoi": refresh_images(json.loads(c.benh_an_moi)),
                "taiKhams": [
                    {"id": f.id, "ngayKham": f.ngay_kham.isoformat() if f.ngay_kham else None,
                     "daDienDuLieu": f.da_dien_du_lieu, **refresh_images(json.loads(f.data))}
                    for f in followups
                ],
            },
        })
    return out


@app.get("/export/aa.csv")
def export_aa_csv(
    tu_ngay: Optional[str] = None,
    den_ngay: Optional[str] = None,
    muc_do: Optional[str] = None,
    dieu_tri_chua: Optional[str] = None,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(require_export_permission),
):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["maLuuTru", "maBN", "hoTen", "gioiTinh", "namSinh", "lanKham", "ngay", "saltScore", "mucDoNang", "dieuTri"])

    q = select(AACase)
    if tu_ngay:
        q = q.where(AACase.ngay_tao >= tu_ngay)
    if den_ngay:
        q = q.where(AACase.ngay_tao <= den_ngay)
    if muc_do:
        q = q.where(AACase.muc_do_nang == muc_do)
    for c in session.exec(q).all():
        p = session.get(Patient, c.ma_bn)
        d = json.loads(c.benh_an_moi)
        salt = calc_salt(d.get("vung", {}))
        writer.writerow([c.ma_luu_tru, c.ma_bn, p.ho_ten if p else "", p.gioi_tinh if p else "", p.nam_sinh if p else "",
                          "T0", c.ngay_tao, salt, c.muc_do_nang, d.get("dieuTri", "")])

        followups = session.exec(select(AAFollowUp).where(AAFollowUp.case_id == c.id).order_by(AAFollowUp.ngay_kham)).all()
        for i, f in enumerate(followups):
            if muc_do and f.muc_do_nang != muc_do:
                continue
            if dieu_tri_chua and (dieu_tri_chua.lower() not in (f.dieu_tri or "").lower()):
                continue
            if tu_ngay and f.ngay_kham and str(f.ngay_kham) < tu_ngay:
                continue
            if den_ngay and f.ngay_kham and str(f.ngay_kham) > den_ngay:
                continue
            fd = json.loads(f.data)
            salt_f = calc_salt(fd.get("vung", {}))
            writer.writerow([c.ma_luu_tru, c.ma_bn, p.ho_ten if p else "", p.gioi_tinh if p else "", p.nam_sinh if p else "",
                              f"Tái khám {i+1}", f.ngay_kham, salt_f, f.muc_do_nang, f.dieu_tri or ""])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=aa_export.csv"},
    )


# ---------- ảnh ----------
@app.post("/images/upload")
async def upload_image(ma_bn: str, file: UploadFile = File(...), doctor: Doctor = Depends(get_current_doctor)):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File tải lên phải là ảnh")
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Ảnh quá lớn (giới hạn 8MB sau khi nén WebP)")
    url = get_storage().save(data, ma_bn)
    return {"url": url}


@app.get("/uploads/{filename}")
def serve_local_upload(filename: str):
    path = os.path.join(os.path.dirname(__file__), "uploads", filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Không tìm thấy ảnh")
    return FileResponse(path, media_type="image/webp")
