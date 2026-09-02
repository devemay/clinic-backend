import csv
import io
import json
import os
import traceback
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlmodel import Session, select

from auth import authenticate_doctor, create_access_token, get_current_doctor, require_export_permission, require_create_permission, require_admin, hash_password, verify_password
from database import get_session, init_db
from models import (AACase, AAFollowUp, AGACase, AGAFollowUp, NonScarCase, NonScarFollowUp,
                    SACase, SAFollowUp, TTMCase, TTMFollowUp, Doctor, Patient)
from storage import get_storage, refresh_url
import survey

app = FastAPI(title="Bệnh án nghiên cứu — API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Nén dữ liệu trả về (gzip). Dữ liệu bệnh án là JSON tiếng Việt lặp nhiều tên trường
# nên nén rất tốt — đo thực tế giảm khoảng 8-12 lần. Đây là cách rẻ nhất để tăng tốc
# tra cứu và xuất Excel khi số bệnh nhân lớn, vì nút thắt là băng thông chứ không phải CPU.
# minimum_size=1000: phản hồi nhỏ (VD /health) không nén, tránh tốn CPU vô ích.
app.add_middleware(GZipMiddleware, minimum_size=1000)


# Endpoint đánh thức server — không cần đăng nhập, không đụng database, phản hồi cực nhẹ.
# Dùng URL này cho dịch vụ ping ngoài (cron-job.org, UptimeRobot...) thay vì /docs (quá nặng).
@app.get("/health")
def health():
    return "ok"


def backfill_cot_phu() -> None:
    """Nạp giá trị cho các cột trích sẵn (gpb_trang_thai, gpb_cho_tu, dong_mac) với dữ liệu
    đã có từ trước khi thêm cột. Chỉ đụng vào bản ghi còn NULL nên thực chất chạy đúng 1 lần;
    nếu bị ngắt giữa chừng thì lần khởi động sau làm nốt phần còn lại, không hỏng dữ liệu."""
    from database import engine as _engine
    LO = 500
    da_xu_ly = 0
    with Session(_engine) as session:
        for cfg in DISEASE_CONFIGS:
            for Model, la_benh_an in ((cfg["case_model"], True), (cfg["followup_model"], False)):
                while True:
                    rows = session.exec(select(Model).where(Model.gpb_trang_thai.is_(None)).limit(LO)).all()
                    if not rows:
                        break
                    for r in rows:
                        try:
                            d = json.loads(r.benh_an_moi if la_benh_an else r.data)
                        except Exception:
                            d = {}
                        cap_nhat_cot_gpb(r, d)
                        if la_benh_an:
                            cap_nhat_cot_dong_mac(r, d)
                        session.add(r)
                    session.commit()
                    da_xu_ly += len(rows)
            # phòng trường hợp lần trước dừng giữa chừng: bệnh án có gpb rồi nhưng chưa có đồng mắc
            CaseModel = cfg["case_model"]
            while True:
                rows = session.exec(select(CaseModel).where(CaseModel.dong_mac.is_(None)).limit(LO)).all()
                if not rows:
                    break
                for r in rows:
                    try:
                        d = json.loads(r.benh_an_moi)
                    except Exception:
                        d = {}
                    cap_nhat_cot_dong_mac(r, d)
                    session.add(r)
                session.commit()
                da_xu_ly += len(rows)
    if da_xu_ly:
        print(f"[khởi động] Đã nạp giá trị cột trích sẵn cho {da_xu_ly} bản ghi cũ.")


@app.on_event("startup")
def on_startup():
    init_db()
    backfill_cot_phu()


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
    ngay_sinh: Optional[date] = None
    dan_toc: Optional[str] = None
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


KHONG_CO_YEU_TO = "Không có yếu tố nào"


def mucdo_sau_dieu_chinh(score: float, yeu_to_nang_bac: Optional[list]) -> str:
    levels = ["Không rụng tóc", "Nhẹ/giới hạn", "Trung bình", "Nặng", "Rất nặng"]
    base = mucdo_from_salt(score)
    idx = levels.index(base)
    # "Không có yếu tố nào" là lựa chọn để bác sĩ khẳng định KHÔNG có — không được nâng bậc
    yeu_to_nang_bac = [v for v in (yeu_to_nang_bac or []) if v != KHONG_CO_YEU_TO]
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


def get_path(data, path: str):
    """Đọc giá trị theo đường dẫn 'a.b.c' — khớp hàm getPath() bên frontend."""
    cur = data
    for part in str(path).split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


# Các mục BẮT BUỘC ĐIỀN HẾT mọi trường (khám thực thể và các thang điểm phải đầy đủ mới
# dùng được cho nghiên cứu). Mọi mục khác chỉ cần >= 1 trường có giá trị.
# Chỉ liệt kê những trường LUÔN HIỂN THỊ trên form — ô phụ thuộc (chỉ hiện khi chọn "Có")
# cố ý không đưa vào, để hồ sơ không bao giờ rơi vào thế không thể hoàn thành.
STRICT_PREFIXES = ("Khám thực thể", "Thang điểm", "Mức độ nặng")


def is_strict_section(name: str) -> bool:
    return str(name).startswith(STRICT_PREFIXES)


def section_filled(data: dict, keys: list, name: str = "") -> bool:
    if is_strict_section(name):
        return all(is_filled(get_path(data, k)) for k in keys)
    return any(is_filled(get_path(data, k)) for k in keys)


def all_sections_filled(data: dict, section_map: dict) -> bool:
    return all(section_filled(data, keys, name) for name, keys in section_map.items())


def refresh_images(data: dict) -> dict:
    if not isinstance(data, dict):
        return data
    d = dict(data)
    if isinstance(d.get("anh"), list):
        d["anh"] = [refresh_url(u) for u in d["anh"]]
    return d


BENH_HOP_LE = ("AA", "AGA", "NSA", "SA", "TTM")


def cap_nhat_cot_gpb(ban_ghi, data: dict) -> None:
    """Trích trạng thái giải phẫu bệnh từ JSON ra cột riêng, để danh sách chờ GPB lọc và
    sắp xếp bằng SQL thay vì phải mở JSON của toàn bộ bản ghi."""
    st = compute_gpb_status(data)
    if st is None:
        ban_ghi.gpb_trang_thai, ban_ghi.gpb_cho_tu = "", None
    elif st["type"] == "done":
        ban_ghi.gpb_trang_thai, ban_ghi.gpb_cho_tu = "co", None
    else:
        ban_ghi.gpb_trang_thai = "cho"
        ban_ghi.gpb_cho_tu = parse_date(data.get("gpbNgayThucHien"))


def chuoi_dong_mac(data: dict) -> str:
    """Chuẩn hoá danh sách bệnh đồng mắc thành chuỗi ngắn để lưu vào cột, VD "AGA,SA"."""
    if (data or {}).get("dongMac") != "Có":
        return ""
    ds = [str(x).strip().upper() for x in (data.get("dongMacBenh") or [])]
    return ",".join([k for k in dict.fromkeys(ds) if k in BENH_HOP_LE])[:64]


def cap_nhat_cot_dong_mac(case, data: dict) -> None:
    case.dong_mac = chuoi_dong_mac(data)


def dong_bo_dong_mac_tu_tai_kham(session: Session, fu, CaseModel, data: dict) -> None:
    """Đồng mắc thuộc về BỆNH ÁN, không thuộc riêng lần khám nào. Bác sĩ sửa ở phiếu tái khám
    nào thì cũng ghi vào bệnh án, nên mọi lần khám đều hiển thị giống nhau."""
    if "dongMac" not in (data or {}):
        return
    case = session.get(CaseModel, fu.case_id)
    if case is not None:
        cap_nhat_cot_dong_mac(case, data)
        session.add(case)


def gan_dong_mac(data: dict, case) -> dict:
    """Khi trả dữ liệu về cho phiếu, gắn đồng mắc ở cấp bệnh án vào MỌI lần khám.
    Chưa từng khai báo thì mặc định là "Không" — giữ nguyên hiện trạng của dữ liệu cũ,
    tránh việc mọi bệnh án đang "Đã điền" bỗng chuyển thành "Chưa điền"."""
    d = dict(data or {})
    ds = [x for x in (getattr(case, "dong_mac", "") or "").split(",") if x]
    d["dongMac"] = "Có" if ds else "Không"
    d["dongMacBenh"] = ds
    return d


# ---------- nạp dữ liệu theo lô (tránh N+1 truy vấn) ----------
# Trước đây mỗi bản ghi hiển thị tốn ~2 truy vấn riêng (lấy bệnh nhân, lấy bệnh án cha).
# Với 1.500 bản ghi là hơn 1.300 lượt đi-về database — chậm nhất ở chỗ này, không phải ở SQL.
# Ba hàm dưới gom lại thành vài truy vấn duy nhất, kết quả trả về PHẢI giống hệt cách cũ.

_LO = 400  # số phần tử tối đa trong 1 câu IN (...) — tránh câu lệnh SQL quá dài


def _chia_lo(items):
    items = list(items)
    for i in range(0, len(items), _LO):
        yield items[i:i + _LO]


def nap_benh_nhan(session: Session, ma_bn_list) -> Dict[str, Patient]:
    """Trả về {ma_bn: Patient} cho danh sách mã bệnh nhân, bằng vài truy vấn thay vì mỗi mã 1 truy vấn."""
    ids = {m for m in ma_bn_list if m}
    out: Dict[str, Patient] = {}
    for lo in _chia_lo(ids):
        for p in session.exec(select(Patient).where(Patient.ma_bn.in_(lo))).all():
            out[p.ma_bn] = p
    return out


def nap_benh_an(session: Session, CaseModel, case_ids) -> Dict[int, Any]:
    """Trả về {case_id: Case} — dùng khi đi từ bản ghi tái khám ngược về bệnh án cha."""
    ids = {i for i in case_ids if i is not None}
    out: Dict[int, Any] = {}
    for lo in _chia_lo(ids):
        for c in session.exec(select(CaseModel).where(CaseModel.id.in_(lo))).all():
            out[c.id] = c
    return out


def _khoa_sap_xep_tai_kham(f):
    # Giữ ĐÚNG thứ tự của "ORDER BY ngay_kham" trong SQL: bản ghi thiếu ngày khám xếp trước
    # (MySQL và SQLite đều xếp NULL lên đầu khi sắp tăng dần). Thêm id làm khoá phụ để
    # thứ tự luôn ổn định, tránh việc số thứ tự "Tái khám 1/2/3" nhảy lung tung giữa các lần gọi.
    return (f.ngay_kham is not None, f.ngay_kham or date.min, f.id or 0)


def nap_tai_kham(session: Session, FUModel, case_ids) -> Dict[int, list]:
    """Trả về {case_id: [tái khám đã sắp xếp theo ngày khám]}."""
    ids = {i for i in case_ids if i is not None}
    out: Dict[int, list] = {i: [] for i in ids}
    for lo in _chia_lo(ids):
        for f in session.exec(select(FUModel).where(FUModel.case_id.in_(lo))).all():
            out.setdefault(f.case_id, []).append(f)
    for ds in out.values():
        ds.sort(key=_khoa_sap_xep_tai_kham)
    return out


NEW_CASE_SECTIONS = {
    "Hành chính": ["ngayKham", "bacSiKham", "ngheNghiep", "trinhDo", "chieuCao", "canNang"],
    "Bệnh sử - Tiền sử": ["tuoiKhoiPhat", "thoiGianMacBenh", "soDotTaiPhat", "benhSuTruoc", "yeuToKhoiPhat", "dieuTriTruocDoStatus", "thuocDangDung", "tienSuBanThan", "tienSuGiaDinh"],
    "Khám thực thể": ["sotStatus", "mach", "ha", "viTriRungToc", "pullTest", "tocToMoc", "viTriTonThuong", "tonThuongMong", "trieuChungCoNang", "theLamSang"],
    "Mức độ nặng (SALT)": ["soLuongMang", "dienTichThucTe", "vung", "mangDai", "mangRong", "mangViTri", "yeuToNangBac"],
    "Dermoscopy": ["dermoscopy"],
    "Cận lâm sàng": ["labs", "treponema", "viNam", "sieuAmTuyenGiap", "il15", "il13", "ifnG", "ifnGMo", "il13Mo", "ngayLayMau"],
    "Giải phẫu bệnh": ["gpbCo"],
    "Điều trị & thủ thuật": ["dieuTri", "vas", "tdkm", "henKham"],
    "Hình ảnh": ["anh"],
    "Tình trạng đồng mắc": ["dongMac"],
}
FOLLOWUP_SECTIONS = {
    "Lâm sàng": ["ngayKham", "bacSiKham", "lamSang", "pullTest", "tocToMoc", "mucDoSoVoiTruoc", "tacDungPhuStatus"],
    "Mức độ nặng (SALT)": ["soLuongMang", "vung", "mucDoDapUng", "mangDai", "mangRong", "mangViTri", "yeuToNangBac"],
    "Dermoscopy": ["dermoscopy", "vas", "tdkm"],
    "Cận lâm sàng & điều trị": ["xnStatus", "xnKetQua", "dieuTri"],
    "Giải phẫu bệnh": ["gpbCo"],
    "Hình ảnh": ["anh"],
    "Tình trạng đồng mắc": ["dongMac"],
}

NEW_AGA_CASE_SECTIONS = {
    "Hành chính": ["ngayKham", "bacSiKham", "luuHuyetTuong", "luuHuyetThanh"],
    "Bệnh sử - Tiền sử": ["thoiGianKhoiPhat", "benhSuTruoc", "tienSuBanThan", "tienSuGiaDinh"],
    "Khám thực thể": ["canNang", "chieuCao", "vongBung", "mach", "ha", "dauHieuCuongAndrogen", "phanBoRungToc", "matDoToc", "duongKinhSoiToc", "pullTest"],
    "Thang điểm": ["hamiltonNorwood", "sinclairScale", "ludwig", "pcos.chanDoan"],
    "Dermoscopy": ["dermoscopy", "vungTran", "vungDinh", "vungCham"],
    "Cận lâm sàng": ["labs", "sieuAmOBung", "sieuAmTuyenGiap", "xnKhac"],
    "Giải phẫu bệnh": ["gpbCo"],
    "Điều trị & thủ thuật": ["dieuTri", "henKham"],
    "Hình ảnh": ["anh"],
    "Tình trạng đồng mắc": ["dongMac"],
}
FOLLOWUP_AGA_SECTIONS = {
    "Lâm sàng": ["ngayKham", "bacSiKham", "lamSang", "pullTest", "mucDoSoVoiTruoc"],
    "Thang điểm": ["hamiltonNorwood", "sinclairScale", "ludwig"],
    "Tác dụng phụ & Xét nghiệm": ["tacDungPhuStatus", "xnStatus"],
    "Giải phẫu bệnh": ["gpbCo"],
    "Điều trị": ["dieuTri"],
    "Hình ảnh": ["anh"],
    "Tình trạng đồng mắc": ["dongMac"],
}

NEW_NONSCAR_CASE_SECTIONS = {
    "Hành chính": ["ngayKham", "bacSiKham", "luuHuyetTuong", "luuHuyetThanh"],
    "Bệnh sử - Tiền sử": ["thoiGianKhoiPhat", "benhSuTruoc", "rungTocTruocDay", "tienSuBanThan", "tienSuGiaDinh"],
    "Khám thực thể": ["coNangDaDau", "chieuCao", "canNang", "mach", "ha", "viTriRungToc", "pullTestStatus", "kieuRungToc", "dauHieuViemSeoTeoDa"],
    "Dermoscopy": ["dermoscopy"],
    "Cận lâm sàng": ["labs", "testNhanhGiangMai", "sieuAmTuyenGiap", "xnKhac"],
    "Giải phẫu bệnh": ["gpbCo"],
    "Điều trị & thủ thuật": ["dieuTri", "henKham"],
    "Hình ảnh": ["anh"],
    "Tình trạng đồng mắc": ["dongMac"],
}
FOLLOWUP_NONSCAR_SECTIONS = {
    "Lâm sàng": ["ngayKham", "bacSiKham", "lamSang", "pullTest", "mucDoSoVoiTruoc"],
    "Giải phẫu bệnh": ["gpbCo"],
    "Xét nghiệm & Điều trị": ["xnStatus", "dieuTri"],
    "Hình ảnh": ["anh"],
    "Tình trạng đồng mắc": ["dongMac"],
}


# ---------- Rụng tóc sẹo (SA) ----------
NEW_SA_CASE_SECTIONS = {
    "Hành chính": ["ngayKham", "bacSiKham", "icd10", "nahrs", "luuHuyetTuong", "luuHuyetThanh"],
    "Bệnh sử - Tiền sử": ["tuoiKhoiPhat", "thoiGianMacBenh", "yeuToKhoiPhat", "viTriKhoiPhat",
                          "tienTrienTonThuong", "benhSuTruoc", "dieuTriTruocDoStatus",
                          "tienSuBanThan", "thoiQuenChamSocToc", "tienSuGiaDinh"],
    "Khám thực thể": ["sotStatus", "mach", "ha", "ranhGioiTonThuong", "dienTichPhanTram",
                      "trieuChungCoNang", "pullTest", "luiChanToc", "tonThuongNgoaiDaDau"],
    "Mức độ nặng (LPPAI)": ["lppai.ngua", "lppai.dau", "lppai.cangDa",
                            "lppai.scaling", "lppai.erythema", "lppai.pullTest", "lppai.spreading"],
    "Dermoscopy": ["viTriKhaoSat", "dermoscopy"],
    "Cận lâm sàng": ["labs", "soiNam", "cayViKhuan", "pcrDemodex", "xnKhac"],
    "Giải phẫu bệnh": ["gpbCo"],
    "Điều trị & thủ thuật": ["dieuTri", "henKham"],
    "Hình ảnh": ["anh"],
    "Tình trạng đồng mắc": ["dongMac"],
}
FOLLOWUP_SA_SECTIONS = {
    "Lâm sàng": ["ngayKham", "bacSiKham", "lamSang", "pullTest", "dienTichPhanTram",
                 "thayDoiSoVoiTruoc", "trieuChungCoNang"],
    "Mức độ nặng (LPPAI)": ["lppai.ngua", "lppai.dau", "lppai.cangDa",
                            "lppai.scaling", "lppai.erythema", "lppai.pullTest", "lppai.spreading"],
    "Dermoscopy": ["viTriKhaoSat", "dermoscopyTK", "ketLuanDermoscopy"],
    "Xét nghiệm & Tác dụng phụ": ["xnStatus", "thuocDangDung", "tacDungPhu"],
    "Giải phẫu bệnh": ["gpbCo"],
    "Điều trị": ["dieuTri"],
    "Hình ảnh": ["anh"],
    "Tình trạng đồng mắc": ["dongMac"],
}

# ---------- Tật nhổ tóc (TTM) ----------
NEW_TTM_CASE_SECTIONS = {
    "Hành chính": ["ngayKham", "bacSiKham", "ngheNghiep", "trinhDo", "chieuCao", "canNang"],
    "Bệnh sử - Tiền sử": ["tuoiKhoiPhat", "thoiGianMacBenh", "hoanCanhKhoiPhat", "aiNhoToc",
                          "dieuTriTruoc", "dapUngDieuTriTruoc", "tienSuBanThan", "tienSuGiaDinh"],
    "Hành vi nhổ tóc": ["nhanThuc", "tinhHuongKichHoat", "tanSuatNhoToc", "thoiDiemTrongNgay",
                        "hanhViSauNho", "bfrb"],
    "Thang điểm (MGH-HPS)": ["mgh.q1", "mgh.q2", "mgh.q3", "mgh.q4", "mgh.q5", "mgh.q6", "mgh.q7"],
    "Khám thực thể": ["viTriDaDau", "viTriNgoaiDaDau", "dienTichPhanTram", "mangDai", "mangRong",
                      "pullTest", "hinhThaiTonThuong", "dauHieuKemTheo"],
    "Dermoscopy": ["dermoscopy"],
    "Cận lâm sàng": ["labs", "viNam", "sieuAmBung", "xnKhac"],
    "Chẩn đoán DSM-5": ["dsm5", "ketLuanChanDoan"],
    "Giải phẫu bệnh": ["gpbCo"],
    "Điều trị & thủ thuật": ["hrt", "dieuTri", "henKham"],
    "Hình ảnh": ["anh"],
    "Tình trạng đồng mắc": ["dongMac"],
}
FOLLOWUP_TTM_SECTIONS = {
    "Lâm sàng": ["ngayKham", "bacSiKham", "soVoiLanTruoc", "nhoTocGiua2Lan", "tuanThuDieuTri",
                 "dienTichPhanTram", "tocMocLai", "mangRungMoi", "dauHieuRTS"],
    "Thang điểm (MGH-HPS)": ["mgh.q1", "mgh.q2", "mgh.q3", "mgh.q4", "mgh.q5", "mgh.q6", "mgh.q7"],
    "Dermoscopy": ["dauHieuTraumatic", "loNang", "tocMocLaiDuoiKinh", "ketLuanDermoscopy"],
    "Tác dụng phụ & Điều trị": ["tacDungPhuStatus", "dieuTri"],
    "Giải phẫu bệnh": ["gpbCo"],
    "Hình ảnh": ["anh"],
    "Tình trạng đồng mắc": ["dongMac"],
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


# Danh sách các bệnh đang hỗ trợ — dùng chung cho dashboard/tra cứu/danh sách chờ GPB, để mỗi khi
# thêm bệnh mới chỉ cần thêm 1 dòng ở đây thay vì sửa lại từng endpoint riêng.
DISEASE_CONFIGS = [
    {"key": "aa", "label": "AA", "case_model": AACase, "followup_model": AAFollowUp},
    {"key": "aga", "label": "AGA", "case_model": AGACase, "followup_model": AGAFollowUp},
    {"key": "nonscar", "label": "NSA", "case_model": NonScarCase, "followup_model": NonScarFollowUp},
    {"key": "sa", "label": "SA", "case_model": SACase, "followup_model": SAFollowUp},
    {"key": "ttm", "label": "TTM", "case_model": TTMCase, "followup_model": TTMFollowUp},
]


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


# ---------- phiếu khảo sát bệnh nhân tự điền trên hệ thống bệnh viện ----------
# Khai báo TRƯỚC /survey/{ma_bn} — nếu đặt sau, "_ping" sẽ bị hiểu là một mã bệnh nhân.
@app.get("/survey/_ping")
def survey_ping():
    """Kiểm tra máy chủ này có gọi ra được hệ thống bệnh viện hay không.

    Không cần đăng nhập và KHÔNG trả về thông tin bệnh nhân nào (dùng mã không tồn tại),
    để bác sĩ chỉ cần mở địa chỉ này trên trình duyệt là biết nút Đồng bộ có chạy được không.
    """
    return survey.ping()


@app.get("/survey/{ma_bn}")
def get_survey(ma_bn: str, benh: str = "", doctor: Doctor = Depends(get_current_doctor)):
    """Đọc phiếu khảo sát của bệnh nhân từ hệ thống bệnh viện (api.dalieu.vn).

    Truyền benh=aa/aga/nonscar để nhận thêm "mapped" — các trường đã ánh xạ sẵn sang
    đúng tên trường của bệnh án tương ứng. Luôn trả mã 200 kèm cờ found/loi thay vì
    ném lỗi, để hệ thống bệnh viện chậm hoặc mất mạng không chặn màn nhập bệnh án.

    Bọc toàn bộ trong try/except là BẮT BUỘC: nếu để lỗi thoát ra ngoài, Starlette trả
    500 KHÔNG kèm header CORS, trình duyệt chặn luôn và chỉ báo "Không kết nối được tới
    backend" — che mất lỗi thật. Ở đây mọi lỗi đều thành thông báo đọc được trên màn hình.
    """
    try:
        return survey.build_response(ma_bn, benh)
    except Exception as e:
        return {
            "found": False, "co_khao_sat": False, "khao_sat": None, "mapped": {}, "dlqi_tong": None,
            "loi": f"Lỗi khi xử lý phiếu khảo sát: {type(e).__name__}: {e}",
            "chan_doan": traceback.format_exc()[-1500:],
        }


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
    """Xoá toàn bộ hồ sơ của 1 bệnh nhân (bệnh án + mọi lần tái khám của cả 3 bệnh + thông tin bệnh nhân) —
    dùng để dọn dữ liệu demo/test, không thể hoàn tác. Chỉ tài khoản quyền đầy đủ mới xoá được."""
    p = session.get(Patient, ma_bn)
    if not p:
        raise HTTPException(status_code=404, detail="Không tìm thấy bệnh nhân")
    for cfg in DISEASE_CONFIGS:
        CaseModel, FUModel = cfg["case_model"], cfg["followup_model"]
        case = session.exec(select(CaseModel).where(CaseModel.ma_bn == ma_bn)).first()
        if case:
            for f in session.exec(select(FUModel).where(FUModel.case_id == case.id)).all():
                session.delete(f)
            session.delete(case)
    session.delete(p)
    session.commit()
    return {"ok": True}


# Cùng một bệnh có thể được gọi bằng nhiều tên qua các phiên bản: khoá kỹ thuật ("nonscar"),
# nhãn cũ ("NONSCAR"), tiền tố mã lưu trữ ("NS") hay ký hiệu mới ("NSA"). Chuẩn hoá hết về
# nhãn hiện hành để bản frontend cũ còn trong bộ nhớ đệm trình duyệt vẫn dùng được bình thường.
_TEN_KHAC = {"NONSCAR": "NSA", "NS": "NSA", "NSA": "NSA"}


def chuan_hoa_nhan_benh(benh):
    if not benh:
        return benh
    t = str(benh).strip().upper()
    if t in _TEN_KHAC:
        return _TEN_KHAC[t]
    return next((c["label"] for c in DISEASE_CONFIGS if c["label"].upper() == t or c["key"].upper() == t), benh)


def _find_disease_config(benh: str):
    khoa = {"nsa": "nonscar", "ns": "nonscar", "nonscar": "nonscar"}.get(str(benh).lower(), str(benh).lower())
    cfg = next((c for c in DISEASE_CONFIGS if c["key"] == khoa), None)
    if not cfg:
        raise HTTPException(status_code=404, detail=f"Không rõ loại bệnh '{benh}'")
    return cfg


@app.delete("/cases/{ma_bn}/{benh}")
def delete_case(ma_bn: str, benh: str, session: Session = Depends(get_session), doctor: Doctor = Depends(require_export_permission)):
    """Xoá riêng 1 bệnh án (bệnh án mới + toàn bộ tái khám của đúng 1 bệnh) — GIỮ LẠI thông tin bệnh nhân,
    dùng khi cần làm lại từ đầu 1 bệnh án nhưng không muốn nhập lại hành chính bệnh nhân."""
    cfg = _find_disease_config(benh)
    CaseModel, FUModel = cfg["case_model"], cfg["followup_model"]
    case = session.exec(select(CaseModel).where(CaseModel.ma_bn == ma_bn)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Không tìm thấy bệnh án")
    for f in session.exec(select(FUModel).where(FUModel.case_id == case.id)).all():
        session.delete(f)
    session.delete(case)
    session.commit()
    return {"ok": True}


@app.delete("/cases/{ma_bn}/{benh}/followups/{followup_id}")
def delete_followup(ma_bn: str, benh: str, followup_id: int, session: Session = Depends(get_session), doctor: Doctor = Depends(require_export_permission)):
    """Xoá riêng 1 lần tái khám — giữ nguyên bệnh án mới và các lần tái khám khác."""
    cfg = _find_disease_config(benh)
    FUModel = cfg["followup_model"]
    fu = session.get(FUModel, followup_id)
    if not fu:
        raise HTTPException(status_code=404, detail="Không tìm thấy lần tái khám")
    session.delete(fu)
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
        "benh_an_moi": gan_dong_mac(refresh_images(json.loads(case.benh_an_moi)), case),
        "tai_khams": [
            {"id": f.id, "ngay_kham": f.ngay_kham, "da_dien_du_lieu": f.da_dien_du_lieu, "bac_si_tao": f.bac_si_tao, **gan_dong_mac(refresh_images(json.loads(f.data)), case)}
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
    cap_nhat_cot_gpb(case, payload.data)
    cap_nhat_cot_dong_mac(case, payload.data)
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
    cap_nhat_cot_gpb(fu, payload.data)
    dong_bo_dong_mac_tu_tai_kham(session, fu, AACase, payload.data)
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
        "benh_an_moi": gan_dong_mac(refresh_images(json.loads(case.benh_an_moi)), case),
        "tai_khams": [
            {"id": f.id, "ngay_kham": f.ngay_kham, "da_dien_du_lieu": f.da_dien_du_lieu, "bac_si_tao": f.bac_si_tao, **gan_dong_mac(refresh_images(json.loads(f.data)), case)}
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
    cap_nhat_cot_gpb(case, payload.data)
    cap_nhat_cot_dong_mac(case, payload.data)
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
    cap_nhat_cot_gpb(fu, payload.data)
    dong_bo_dong_mac_tu_tai_kham(session, fu, AGACase, payload.data)
    fu.dieu_tri = (payload.data.get("dieuTri") or "")[:255]
    session.add(fu)
    session.commit()
    return {"ok": True}


# ---------- NonScar case: tạo mã lưu trữ (chỉ bác sĩ) ----------
@app.post("/cases/{ma_bn}/nonscar/create")
def create_nonscar_case(
    ma_bn: str,
    payload: CreateCaseIn,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(require_create_permission),
):
    if not session.get(Patient, ma_bn):
        raise HTTPException(status_code=404, detail="Bệnh nhân chưa tồn tại — tạo bệnh nhân trước")
    existing = session.exec(select(NonScarCase).where(NonScarCase.ma_bn == ma_bn)).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Bệnh nhân đã có mã lưu trữ NSA: {existing.ma_luu_tru}")
    ma_luu_tru = next_ma_luu_tru(session, "NS", NonScarCase)
    case = NonScarCase(
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


@app.get("/cases/{ma_bn}/nonscar")
def get_nonscar_case(ma_bn: str, session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)):
    case = session.exec(select(NonScarCase).where(NonScarCase.ma_bn == ma_bn)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Bệnh nhân chưa có bệnh án rụng tóc không sẹo")
    followups = session.exec(
        select(NonScarFollowUp).where(NonScarFollowUp.case_id == case.id).order_by(NonScarFollowUp.ngay_kham)
    ).all()
    return {
        "ma_luu_tru": case.ma_luu_tru,
        "da_dien_du_lieu": case.da_dien_du_lieu,
        "bac_si_tao": case.bac_si_tao,
        "benh_an_moi": gan_dong_mac(refresh_images(json.loads(case.benh_an_moi)), case),
        "tai_khams": [
            {"id": f.id, "ngay_kham": f.ngay_kham, "da_dien_du_lieu": f.da_dien_du_lieu, "bac_si_tao": f.bac_si_tao, **gan_dong_mac(refresh_images(json.loads(f.data)), case)}
            for f in followups
        ],
        "updated_at": case.updated_at,
    }


@app.put("/cases/{ma_bn}/nonscar")
def save_nonscar_case_data(
    ma_bn: str, payload: DataIn, session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)
):
    case = session.exec(select(NonScarCase).where(NonScarCase.ma_bn == ma_bn)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Chưa có mã lưu trữ — bác sĩ cần tạo bệnh án trước")
    case.benh_an_moi = json.dumps(payload.data, ensure_ascii=False)
    case.da_dien_du_lieu = all_sections_filled(payload.data, NEW_NONSCAR_CASE_SECTIONS)
    cap_nhat_cot_gpb(case, payload.data)
    cap_nhat_cot_dong_mac(case, payload.data)
    case.updated_at = datetime.utcnow()
    session.add(case)
    session.commit()
    return {"ok": True, "ma_luu_tru": case.ma_luu_tru}


@app.post("/cases/{ma_bn}/nonscar/followups/create")
def create_nonscar_followup(
    ma_bn: str,
    payload: CreateFollowUpIn,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(require_create_permission),
):
    case = session.exec(select(NonScarCase).where(NonScarCase.ma_bn == ma_bn)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Bệnh nhân chưa có mã lưu trữ NSA")
    ngay = payload.ngay_kham or date.today().isoformat()
    fu = NonScarFollowUp(
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


@app.put("/cases/{ma_bn}/nonscar/followups/{followup_id}")
def save_nonscar_followup_data(
    ma_bn: str,
    followup_id: int,
    payload: DataIn,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(get_current_doctor),
):
    fu = session.get(NonScarFollowUp, followup_id)
    if not fu:
        raise HTTPException(status_code=404, detail="Không tìm thấy lần tái khám")
    fu.data = json.dumps(payload.data, ensure_ascii=False)
    fu.ngay_kham = parse_date(payload.data.get("ngayKham")) or fu.ngay_kham
    fu.da_dien_du_lieu = all_sections_filled(payload.data, FOLLOWUP_NONSCAR_SECTIONS)
    cap_nhat_cot_gpb(fu, payload.data)
    dong_bo_dong_mac_tu_tai_kham(session, fu, NonScarCase, payload.data)
    fu.dieu_tri = (payload.data.get("dieuTri") or "")[:255]
    session.add(fu)
    session.commit()
    return {"ok": True}


def to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def calc_lppai(data: dict):
    """LPPAI = trung bình 3 điểm triệu chứng (0-10) x tổng 4 điểm lâm sàng.
    Phải khớp đúng hàm calcLppai() bên frontend."""
    trieu_chung = [to_num(get_path(data, "lppai." + k)) for k in ("ngua", "dau", "cangDa")]
    lam_sang = [to_num(get_path(data, "lppai." + k)) for k in ("scaling", "erythema", "pullTest", "spreading")]
    if any(v is None for v in trieu_chung + lam_sang):
        return None
    return round(sum(trieu_chung) / 3 * sum(lam_sang), 2)


def mucdo_lppai(score):
    if score is None:
        return None
    return "Ổn định" if score <= 2.5 else "Đang hoạt động"


def calc_mgh(data: dict):
    """Tổng điểm MGH-HPS (7 câu, mỗi câu 0-4). Khớp hàm calcMgh() bên frontend."""
    vals = [to_num(get_path(data, "mgh.q%d" % i)) for i in range(1, 8)]
    if any(v is None for v in vals):
        return None
    return int(sum(vals))


def mucdo_mgh(total):
    if total is None:
        return None
    if total <= 7:
        return "Nhẹ"
    if total <= 15:
        return "Trung bình"
    return "Nặng"


# ---------- Rụng tóc sẹo (SA) ----------
@app.post("/cases/{ma_bn}/sa/create")
def create_sa_case(
    ma_bn: str,
    payload: CreateCaseIn,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(require_create_permission),
):
    if not session.get(Patient, ma_bn):
        raise HTTPException(status_code=404, detail="Bệnh nhân chưa tồn tại — tạo bệnh nhân trước")
    existing = session.exec(select(SACase).where(SACase.ma_bn == ma_bn)).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Bệnh nhân đã có mã lưu trữ SA: {existing.ma_luu_tru}")
    ma_luu_tru = next_ma_luu_tru(session, "SA", SACase)
    case = SACase(
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


@app.get("/cases/{ma_bn}/sa")
def get_sa_case(ma_bn: str, session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)):
    case = session.exec(select(SACase).where(SACase.ma_bn == ma_bn)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Bệnh nhân chưa có bệnh án rụng tóc không sẹo")
    followups = session.exec(
        select(SAFollowUp).where(SAFollowUp.case_id == case.id).order_by(SAFollowUp.ngay_kham)
    ).all()
    return {
        "ma_luu_tru": case.ma_luu_tru,
        "da_dien_du_lieu": case.da_dien_du_lieu,
        "bac_si_tao": case.bac_si_tao,
        "benh_an_moi": gan_dong_mac(refresh_images(json.loads(case.benh_an_moi)), case),
        "tai_khams": [
            {"id": f.id, "ngay_kham": f.ngay_kham, "da_dien_du_lieu": f.da_dien_du_lieu, "bac_si_tao": f.bac_si_tao, **gan_dong_mac(refresh_images(json.loads(f.data)), case)}
            for f in followups
        ],
        "updated_at": case.updated_at,
    }


@app.put("/cases/{ma_bn}/sa")
def save_sa_case_data(
    ma_bn: str, payload: DataIn, session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)
):
    case = session.exec(select(SACase).where(SACase.ma_bn == ma_bn)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Chưa có mã lưu trữ — bác sĩ cần tạo bệnh án trước")
    case.benh_an_moi = json.dumps(payload.data, ensure_ascii=False)
    case.da_dien_du_lieu = all_sections_filled(payload.data, NEW_SA_CASE_SECTIONS)
    cap_nhat_cot_gpb(case, payload.data)
    cap_nhat_cot_dong_mac(case, payload.data)
    case.muc_do_nang = mucdo_lppai(calc_lppai(payload.data))
    case.updated_at = datetime.utcnow()
    session.add(case)
    session.commit()
    return {"ok": True, "ma_luu_tru": case.ma_luu_tru}


@app.post("/cases/{ma_bn}/sa/followups/create")
def create_sa_followup(
    ma_bn: str,
    payload: CreateFollowUpIn,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(require_create_permission),
):
    case = session.exec(select(SACase).where(SACase.ma_bn == ma_bn)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Bệnh nhân chưa có mã lưu trữ SA")
    ngay = payload.ngay_kham or date.today().isoformat()
    fu = SAFollowUp(
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


@app.put("/cases/{ma_bn}/sa/followups/{followup_id}")
def save_sa_followup_data(
    ma_bn: str,
    followup_id: int,
    payload: DataIn,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(get_current_doctor),
):
    fu = session.get(SAFollowUp, followup_id)
    if not fu:
        raise HTTPException(status_code=404, detail="Không tìm thấy lần tái khám")
    fu.data = json.dumps(payload.data, ensure_ascii=False)
    fu.ngay_kham = parse_date(payload.data.get("ngayKham")) or fu.ngay_kham
    fu.da_dien_du_lieu = all_sections_filled(payload.data, FOLLOWUP_SA_SECTIONS)
    cap_nhat_cot_gpb(fu, payload.data)
    dong_bo_dong_mac_tu_tai_kham(session, fu, SACase, payload.data)
    fu.muc_do_nang = mucdo_lppai(calc_lppai(payload.data))
    fu.dieu_tri = (payload.data.get("dieuTri") or "")[:255]
    session.add(fu)
    session.commit()
    return {"ok": True}


# ---------- Tật nhổ tóc (TTM) ----------
@app.post("/cases/{ma_bn}/ttm/create")
def create_ttm_case(
    ma_bn: str,
    payload: CreateCaseIn,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(require_create_permission),
):
    if not session.get(Patient, ma_bn):
        raise HTTPException(status_code=404, detail="Bệnh nhân chưa tồn tại — tạo bệnh nhân trước")
    existing = session.exec(select(TTMCase).where(TTMCase.ma_bn == ma_bn)).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Bệnh nhân đã có mã lưu trữ TTM: {existing.ma_luu_tru}")
    ma_luu_tru = next_ma_luu_tru(session, "TTM", TTMCase)
    case = TTMCase(
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


@app.get("/cases/{ma_bn}/ttm")
def get_ttm_case(ma_bn: str, session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)):
    case = session.exec(select(TTMCase).where(TTMCase.ma_bn == ma_bn)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Bệnh nhân chưa có bệnh án rụng tóc không sẹo")
    followups = session.exec(
        select(TTMFollowUp).where(TTMFollowUp.case_id == case.id).order_by(TTMFollowUp.ngay_kham)
    ).all()
    return {
        "ma_luu_tru": case.ma_luu_tru,
        "da_dien_du_lieu": case.da_dien_du_lieu,
        "bac_si_tao": case.bac_si_tao,
        "benh_an_moi": gan_dong_mac(refresh_images(json.loads(case.benh_an_moi)), case),
        "tai_khams": [
            {"id": f.id, "ngay_kham": f.ngay_kham, "da_dien_du_lieu": f.da_dien_du_lieu, "bac_si_tao": f.bac_si_tao, **gan_dong_mac(refresh_images(json.loads(f.data)), case)}
            for f in followups
        ],
        "updated_at": case.updated_at,
    }


@app.put("/cases/{ma_bn}/ttm")
def save_ttm_case_data(
    ma_bn: str, payload: DataIn, session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)
):
    case = session.exec(select(TTMCase).where(TTMCase.ma_bn == ma_bn)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Chưa có mã lưu trữ — bác sĩ cần tạo bệnh án trước")
    case.benh_an_moi = json.dumps(payload.data, ensure_ascii=False)
    case.da_dien_du_lieu = all_sections_filled(payload.data, NEW_TTM_CASE_SECTIONS)
    cap_nhat_cot_gpb(case, payload.data)
    cap_nhat_cot_dong_mac(case, payload.data)
    case.muc_do_nang = mucdo_mgh(calc_mgh(payload.data))
    case.updated_at = datetime.utcnow()
    session.add(case)
    session.commit()
    return {"ok": True, "ma_luu_tru": case.ma_luu_tru}


@app.post("/cases/{ma_bn}/ttm/followups/create")
def create_ttm_followup(
    ma_bn: str,
    payload: CreateFollowUpIn,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(require_create_permission),
):
    case = session.exec(select(TTMCase).where(TTMCase.ma_bn == ma_bn)).first()
    if not case:
        raise HTTPException(status_code=404, detail="Bệnh nhân chưa có mã lưu trữ TTM")
    ngay = payload.ngay_kham or date.today().isoformat()
    fu = TTMFollowUp(
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


@app.put("/cases/{ma_bn}/ttm/followups/{followup_id}")
def save_ttm_followup_data(
    ma_bn: str,
    followup_id: int,
    payload: DataIn,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(get_current_doctor),
):
    fu = session.get(TTMFollowUp, followup_id)
    if not fu:
        raise HTTPException(status_code=404, detail="Không tìm thấy lần tái khám")
    fu.data = json.dumps(payload.data, ensure_ascii=False)
    fu.ngay_kham = parse_date(payload.data.get("ngayKham")) or fu.ngay_kham
    fu.da_dien_du_lieu = all_sections_filled(payload.data, FOLLOWUP_TTM_SECTIONS)
    cap_nhat_cot_gpb(fu, payload.data)
    dong_bo_dong_mac_tu_tai_kham(session, fu, TTMCase, payload.data)
    fu.muc_do_nang = mucdo_mgh(calc_mgh(payload.data))
    fu.dieu_tri = (payload.data.get("dieuTri") or "")[:255]
    session.add(fu)
    session.commit()
    return {"ok": True}


# ---------- địa chỉ thay thế: "nsa" trỏ về đúng các hàm của "nonscar" ----------
# Bản frontend cũ suy ra đường dẫn từ nhãn hiển thị nên gửi "nsa". Mở thêm lối vào này để
# máy nào còn giữ bản cũ trong bộ nhớ đệm vẫn dùng được, không phải chờ xoá cache.
app.add_api_route("/cases/{ma_bn}/nsa/create", create_nonscar_case, methods=["POST"], include_in_schema=False)
app.add_api_route("/cases/{ma_bn}/nsa", get_nonscar_case, methods=["GET"], include_in_schema=False)
app.add_api_route("/cases/{ma_bn}/nsa", save_nonscar_case_data, methods=["PUT"], include_in_schema=False)
app.add_api_route("/cases/{ma_bn}/nsa/followups/create", create_nonscar_followup, methods=["POST"], include_in_schema=False)
app.add_api_route("/cases/{ma_bn}/nsa/followups/{followup_id}", save_nonscar_followup_data, methods=["PUT"], include_in_schema=False)


# ---------- dashboard ----------
@app.get("/dashboard/today")
def dashboard_today(session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)):
    today = date.today()
    out = []
    for cfg in DISEASE_CONFIGS:
        CaseModel, FUModel, label = cfg["case_model"], cfg["followup_model"], cfg["label"]
        new_cases = session.exec(select(CaseModel).where(CaseModel.ngay_tao == today)).all()
        followups = session.exec(select(FUModel).where(FUModel.ngay_kham == today)).all()
        # nạp sẵn bệnh án cha của các lần tái khám, rồi nạp sẵn bệnh nhân của cả 2 nhóm
        cha = nap_benh_an(session, CaseModel, [f.case_id for f in followups])
        bn = nap_benh_nhan(
            session,
            [c.ma_bn for c in new_cases] + [c.ma_bn for c in cha.values()],
        )
        for c in new_cases:
            p = bn.get(c.ma_bn)
            d = json.loads(c.benh_an_moi)
            out.append({
                "loai": "Bệnh án mới", "ma_luu_tru": c.ma_luu_tru, "ma_bn": c.ma_bn, "benh": label,
                "dong_mac": c.dong_mac or "",
                "ho_ten": p.ho_ten if p else None, "da_dien_du_lieu": c.da_dien_du_lieu,
                "bac_si_tao": c.bac_si_tao, "followup_id": None, "dieu_tri": d.get("dieuTri", ""),
                "gpb_co": d.get("gpbCo"), "gpb_ngay_thuc_hien": d.get("gpbNgayThucHien"), "gpb_ket_qua": d.get("gpbKetQua"),
                "has_anh": bool(d.get("anh")),
            })
        for f in followups:
            c = cha.get(f.case_id)
            p = bn.get(c.ma_bn) if c else None
            fd = json.loads(f.data)
            out.append({
                "loai": "Tái khám", "ma_luu_tru": c.ma_luu_tru if c else None, "ma_bn": c.ma_bn if c else None, "benh": label,
                "dong_mac": (c.dong_mac or "") if c else "",
                "ho_ten": p.ho_ten if p else None, "da_dien_du_lieu": f.da_dien_du_lieu,
                "bac_si_tao": f.bac_si_tao, "followup_id": f.id, "dieu_tri": f.dieu_tri or "",
                "gpb_co": fd.get("gpbCo"), "gpb_ngay_thuc_hien": fd.get("gpbNgayThucHien"), "gpb_ket_qua": fd.get("gpbKetQua"),
                "has_anh": bool(fd.get("anh")),
            })
    return {"ngay": today.isoformat(), "tong_so": len(out), "danh_sach": out}


@app.get("/gpb/waitlist")
def gpb_waitlist(session: Session = Depends(get_session), doctor: Doctor = Depends(get_current_doctor)):
    """Danh sách chờ giải phẫu bệnh của cả 5 bệnh, mở cho mọi tài khoản đăng nhập.

    Lọc bằng cột gpb_cho_tu (đã có chỉ mục) thay vì mở JSON của toàn bộ bản ghi.
    Đo ở 15.000 lượt khám: cách cũ 0,9 giây và 213 MB; cách này còn vài mili giây.
    Màn hình này chạy lại sau MỖI lần bác sĩ bấm lưu nên khác biệt rất đáng kể."""
    out = []
    hom_nay = date.today()
    for cfg in DISEASE_CONFIGS:
        CaseModel, FUModel, label = cfg["case_model"], cfg["followup_model"], cfg["label"]
        cases = session.exec(select(CaseModel).where(CaseModel.gpb_cho_tu.is_not(None))).all()
        fus = session.exec(select(FUModel).where(FUModel.gpb_cho_tu.is_not(None))).all()
        cha = nap_benh_an(session, CaseModel, [f.case_id for f in fus])
        bn = nap_benh_nhan(session, [c.ma_bn for c in cases] + [c.ma_bn for c in cha.values()])

        # Số thứ tự "Tái khám N" phải đúng thứ tự khám. Chỉ lấy id và ngày khám của những
        # bệnh án có liên quan — không đọc cột JSON, nên rất nhẹ.
        thu_tu = {}
        for lo in _chia_lo({f.case_id for f in fus}):
            gom = {}
            for fid, cid, ngay in session.exec(
                select(FUModel.id, FUModel.case_id, FUModel.ngay_kham).where(FUModel.case_id.in_(lo))
            ).all():
                gom.setdefault(cid, []).append((ngay is not None, ngay or date.min, fid or 0))
            for ds in gom.values():
                for i, (_, _, fid) in enumerate(sorted(ds)):
                    thu_tu[fid] = i + 1

        for c in cases:
            p = bn.get(c.ma_bn)
            out.append({"loai": "Bệnh án mới", "benh": label, "dong_mac": c.dong_mac or "",
                        "ma_bn": c.ma_bn, "ho_ten": p.ho_ten if p else None, "ma_luu_tru": c.ma_luu_tru,
                        "days": max(0, (hom_nay - c.gpb_cho_tu).days), "followup_id": None})
        for f in fus:
            c = cha.get(f.case_id)
            if not c:
                continue
            p = bn.get(c.ma_bn)
            out.append({"loai": f"Tái khám {thu_tu.get(f.id, 1)}", "benh": label, "dong_mac": c.dong_mac or "",
                        "ma_bn": c.ma_bn, "ho_ten": p.ho_ten if p else None, "ma_luu_tru": c.ma_luu_tru,
                        "days": max(0, (hom_nay - f.gpb_cho_tu).days), "followup_id": f.id})
    # xếp theo số ngày chờ giảm dần; thêm khoá phụ để thứ tự luôn ổn định giữa các lần gọi
    out.sort(key=lambda r: (-r["days"], r["ma_bn"] or "", r["followup_id"] or 0))
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
    benh: Optional[str] = None,
    dieu_tri_chua: Optional[str] = None,
    xet_nghiem_co: Optional[str] = None,
    chi_chua_dien: Optional[bool] = None,
    so_luot_tai_kham_it_nhat: Optional[int] = None,
    session: Session = Depends(get_session),
    doctor: Doctor = Depends(get_current_doctor),
):
    benh = chuan_hoa_nhan_benh(benh)
    configs = [c for c in DISEASE_CONFIGS if not benh or c["label"] == benh]
    results = []

    # ---------- Chế độ thống kê theo số lượt tái khám tối thiểu ----------
    # Tìm bệnh nhân đủ số lượt tái khám (và đúng tên nếu có lọc thêm), trả về TOÀN BỘ
    # bản ghi của họ (T0 + mọi lần tái khám) — bỏ qua các bộ lọc ngày/mức độ/điều trị khác.
    if so_luot_tai_kham_it_nhat and so_luot_tai_kham_it_nhat > 0:
        for cfg in configs:
            CaseModel, FUModel, label = cfg["case_model"], cfg["followup_model"], cfg["label"]
            cases = session.exec(select(CaseModel)).all()
            bn = nap_benh_nhan(session, [c.ma_bn for c in cases])
            tk_theo_case = nap_tai_kham(session, FUModel, [c.id for c in cases])
            for c in cases:
                p = bn.get(c.ma_bn)
                if ten_bn and ten_bn.lower() not in ((p.ho_ten if p else "") or "").lower():
                    continue
                followups = tk_theo_case.get(c.id, [])
                so_luot_tk = len(followups)
                if so_luot_tk < so_luot_tai_kham_it_nhat:
                    continue
                d0 = json.loads(c.benh_an_moi)
                results.append({
                    "loai": "Bệnh án mới", "benh": label, "ma_luu_tru": c.ma_luu_tru, "ma_bn": c.ma_bn,
                    "dong_mac": c.dong_mac or "",
                    "ho_ten": p.ho_ten if p else None, "ngay": c.ngay_tao.isoformat() if c.ngay_tao else None,
                    "muc_do_nang": c.muc_do_nang, "da_dien_du_lieu": c.da_dien_du_lieu, "followup_id": None,
                    "so_luot_tai_kham": so_luot_tk,
                    "gpb_co": d0.get("gpbCo"), "gpb_ngay_thuc_hien": d0.get("gpbNgayThucHien"), "gpb_ket_qua": d0.get("gpbKetQua"),
                    "has_anh": bool(d0.get("anh")),
                })
                for i, f in enumerate(followups):
                    fd = json.loads(f.data)
                    results.append({
                        "loai": f"Tái khám {i + 1}", "benh": label, "ma_luu_tru": c.ma_luu_tru, "ma_bn": c.ma_bn,
                        "dong_mac": c.dong_mac or "",
                        "ho_ten": p.ho_ten if p else None, "ngay": f.ngay_kham.isoformat() if f.ngay_kham else None,
                        "muc_do_nang": f.muc_do_nang, "da_dien_du_lieu": f.da_dien_du_lieu, "followup_id": f.id,
                        "so_luot_tai_kham": so_luot_tk,
                        "gpb_co": fd.get("gpbCo"), "gpb_ngay_thuc_hien": fd.get("gpbNgayThucHien"), "gpb_ket_qua": fd.get("gpbKetQua"),
                        "has_anh": bool(fd.get("anh")),
                    })
        results.sort(key=lambda r: (r["ma_bn"] or "", r["ngay"] or ""))
        return {"tong_so": len(results), "ket_qua": results}

    for cfg in configs:
        CaseModel, FUModel, label = cfg["case_model"], cfg["followup_model"], cfg["label"]

        q = select(CaseModel)
        if chi_chua_dien is not None:
            q = q.where(CaseModel.da_dien_du_lieu == (not chi_chua_dien))
        if tu_ngay:
            q = q.where(CaseModel.ngay_tao >= tu_ngay)
        if den_ngay:
            q = q.where(CaseModel.ngay_tao <= den_ngay)
        ds_case = session.exec(q).all()
        bn = nap_benh_nhan(session, [c.ma_bn for c in ds_case])
        for c in ds_case:
            p = bn.get(c.ma_bn)
            if ten_bn and ten_bn.lower() not in ((p.ho_ten if p else "") or "").lower():
                continue
            if dieu_tri_chua and dieu_tri_chua.lower() not in str(get_json_path(c.benh_an_moi, "dieuTri") or "").lower():
                continue
            if xet_nghiem_co and not get_json_path(c.benh_an_moi, xet_nghiem_co):
                continue
            d0 = json.loads(c.benh_an_moi)
            results.append({
                "loai": "Bệnh án mới", "benh": label, "ma_luu_tru": c.ma_luu_tru, "ma_bn": c.ma_bn,
                "dong_mac": c.dong_mac or "",
                "ho_ten": p.ho_ten if p else None, "ngay": c.ngay_tao.isoformat() if c.ngay_tao else None,
                "muc_do_nang": c.muc_do_nang, "da_dien_du_lieu": c.da_dien_du_lieu, "followup_id": None,
                "gpb_co": d0.get("gpbCo"), "gpb_ngay_thuc_hien": d0.get("gpbNgayThucHien"), "gpb_ket_qua": d0.get("gpbKetQua"),
                "has_anh": bool(d0.get("anh")),
            })

        q2 = select(FUModel)
        if dieu_tri_chua:
            q2 = q2.where(FUModel.dieu_tri.like(f"%{dieu_tri_chua}%"))
        if chi_chua_dien is not None:
            q2 = q2.where(FUModel.da_dien_du_lieu == (not chi_chua_dien))
        if tu_ngay:
            q2 = q2.where(FUModel.ngay_kham >= tu_ngay)
        if den_ngay:
            q2 = q2.where(FUModel.ngay_kham <= den_ngay)
        ds_fu = [f for f in session.exec(q2).all()
                 if not (xet_nghiem_co and not get_json_path(f.data, xet_nghiem_co))]
        cha = nap_benh_an(session, CaseModel, [f.case_id for f in ds_fu])
        # gộp chung vào bảng bệnh nhân đã nạp ở trên, chỉ truy vấn thêm những mã còn thiếu
        thieu = [c.ma_bn for c in cha.values() if c.ma_bn not in bn]
        bn.update(nap_benh_nhan(session, thieu))
        for f in ds_fu:
            c = cha.get(f.case_id)
            p = bn.get(c.ma_bn) if c else None
            if ten_bn and ten_bn.lower() not in ((p.ho_ten if p else "") or "").lower():
                continue
            fd = json.loads(f.data)
            results.append({
                "loai": "Tái khám", "benh": label, "ma_luu_tru": c.ma_luu_tru if c else None, "ma_bn": c.ma_bn if c else None,
                "dong_mac": (c.dong_mac or "") if c else "",
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
    bn = nap_benh_nhan(session, [c.ma_bn for c in cases])
    tk_theo_case = nap_tai_kham(session, AAFollowUp, [c.id for c in cases])
    out = []
    for c in cases:
        p = bn.get(c.ma_bn)
        fu_count = len(tk_theo_case.get(c.id, []))
        salt = calc_salt(json.loads(c.benh_an_moi).get("vung", {}))
        out.append({"ma_luu_tru": c.ma_luu_tru, "ma_bn": c.ma_bn, "ho_ten": p.ho_ten if p else None, "salt": salt, "so_lan_tk": fu_count})
    return out


# ---------- xuất dữ liệu nghiên cứu (chỉ tài khoản được cấp quyền) ----------
@app.get("/export/raw")
def export_raw(benh: Optional[str] = None, session: Session = Depends(get_session), doctor: Doctor = Depends(require_export_permission)):
    """Trả về toàn bộ dữ liệu của cả 3 bệnh (mọi bệnh nhân) dạng JSON đầy đủ — dùng để dựng file Excel phía trình duyệt.
    Truyền benh=AA/AGA/NSA/SA/TTM để chỉ lấy đúng 1 bệnh."""
    benh = chuan_hoa_nhan_benh(benh)
    out = []
    for cfg in DISEASE_CONFIGS:
        if benh and cfg["label"] != benh:
            continue
        CaseModel, FUModel, label = cfg["case_model"], cfg["followup_model"], cfg["label"]
        cases = session.exec(select(CaseModel)).all()
        bn = nap_benh_nhan(session, [c.ma_bn for c in cases])
        tk_theo_case = nap_tai_kham(session, FUModel, [c.id for c in cases])
        for c in cases:
            p = bn.get(c.ma_bn)
            followups = tk_theo_case.get(c.id, [])
            out.append({
                "maBN": c.ma_bn,
                "benh": label,
                "patient": {
                    "hoTen": p.ho_ten if p else None, "gioiTinh": p.gioi_tinh if p else None,
                    "namSinh": p.nam_sinh if p else None, "danToc": p.dan_toc if p else None,
                    "ngaySinh": p.ngay_sinh.isoformat() if (p and p.ngay_sinh) else None,
                    "diaChi": p.dia_chi if p else None, "sdt": p.sdt if p else None,
                },
                "case": {
                    "maLuuTru": c.ma_luu_tru, "ngayTao": c.ngay_tao.isoformat() if c.ngay_tao else None,
                    "daDienDuLieu": c.da_dien_du_lieu,
                    "benhAnMoi": gan_dong_mac(refresh_images(json.loads(c.benh_an_moi)), c),
                    "taiKhams": [
                        {"id": f.id, "ngayKham": f.ngay_kham.isoformat() if f.ngay_kham else None,
                         "daDienDuLieu": f.da_dien_du_lieu, **gan_dong_mac(refresh_images(json.loads(f.data)), c)}
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
    ds_case = session.exec(q).all()
    bn = nap_benh_nhan(session, [c.ma_bn for c in ds_case])
    tk_theo_case = nap_tai_kham(session, AAFollowUp, [c.id for c in ds_case])
    for c in ds_case:
        p = bn.get(c.ma_bn)
        d = json.loads(c.benh_an_moi)
        salt = calc_salt(d.get("vung", {}))
        writer.writerow([c.ma_luu_tru, c.ma_bn, p.ho_ten if p else "", p.gioi_tinh if p else "", p.nam_sinh if p else "",
                          "T0", c.ngay_tao, salt, c.muc_do_nang, d.get("dieuTri", "")])

        followups = tk_theo_case.get(c.id, [])
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
