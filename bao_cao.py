"""Báo cáo hoạt động phòng khám hằng tháng: tính số liệu, dựng slide PowerPoint, gửi mail.

Tách thành module riêng (không nhét vào main.py) vì 3 phần này không phụ thuộc HTTP và cần
test độc lập. main.py chỉ khai báo endpoint và truyền vào cấu hình từng bệnh.

Quy ước đếm (khớp với dashboard "Khám hôm nay" đang dùng hằng ngày):
  - 1 LƯỢT KHÁM = 1 bệnh án mới (tính theo ngay_tao) hoặc 1 lần tái khám (theo ngay_kham).
  - Chỉ đưa vào báo cáo SỐ LIỆU TỔNG HỢP, không đưa tên/mã/SĐT bệnh nhân — slide sẽ đi qua mail.
"""
from __future__ import annotations

import calendar
import io
import json
import os
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from sqlmodel import Session, select

# Việt Nam không đổi giờ theo mùa -> dùng lệch cố định, không phụ thuộc dữ liệu múi giờ của máy chủ
GIO_VN = timezone(timedelta(hours=7))

NHOM_DIEU_TRI = [
    ("thuocUong", "Thuốc uống"),
    ("thuocBoi", "Thuốc bôi / xịt"),
    ("thuThuat", "Thủ thuật"),
]


# ======================================================================
# 1. Thời gian
# ======================================================================
def bay_gio_vn() -> datetime:
    return datetime.now(GIO_VN)


def thang_truoc(hom_nay: date) -> str:
    """'2026-09-02' -> '2026-08'. Dùng để biết cần báo cáo tháng nào."""
    dau_thang = hom_nay.replace(day=1)
    cuoi_thang_truoc = dau_thang - timedelta(days=1)
    return cuoi_thang_truoc.strftime("%Y-%m")


def khoang_thang(thang: str):
    """'2026-08' -> (date(2026,8,1), date(2026,8,31)). Báo lỗi dễ hiểu nếu sai định dạng."""
    try:
        nam, t = (int(x) for x in str(thang).split("-"))
        if not (1 <= t <= 12 and 2000 <= nam <= 2100):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError(f"Tháng không hợp lệ: {thang!r} (cần dạng YYYY-MM, VD 2026-08)")
    return date(nam, t, 1), date(nam, t, calendar.monthrange(nam, t)[1])


def nhan_thang(thang: str) -> str:
    d1, _ = khoang_thang(thang)
    return f"{d1.month:02d}/{d1.year}"


# ======================================================================
# 2. Tính số liệu
# ======================================================================
def _doc_json(chuoi) -> dict:
    if isinstance(chuoi, dict):
        return chuoi
    try:
        d = json.loads(chuoi or "{}")
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def ngay_kham_that(v) -> Optional[date]:
    """Đọc ô "Ngày khám" của phiếu (dạng 2026-08-31). Sai định dạng thì trả None."""
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def danh_sach_thuoc(v) -> List[str]:
    """Đọc được cả dữ liệu cũ (1 thuốc dạng chuỗi) lẫn dữ liệu mới (mảng nhiều thuốc)."""
    if isinstance(v, list):
        return [str(x).strip() for x in v if x and str(x).strip()]
    return [str(v).strip()] if v and str(v).strip() else []


def _thuoc_cua_luot(data: dict, nhom: str) -> List[str]:
    """Danh sách thuốc/thủ thuật của 1 lượt khám trong 1 nhóm. Mỗi loại chỉ đếm 1 lần/lượt.
    'Khác' được thay bằng nội dung bác sĩ ghi, để thấy thật sự đang dùng gì."""
    if data.get(nhom + "Co") != "Có":
        return []
    ra = []
    for ten in danh_sach_thuoc(data.get(nhom + "Loai")):
        if ten == "Khác":
            ghi = str(data.get(nhom + "Khac") or "").strip()
            ten = f"Khác: {ghi[:40]}" if ghi else "Khác (không ghi rõ)"
        if ten not in ra:
            ra.append(ten)
    return ra


def tinh_thong_ke(session: Session, cau_hinh_benh: List[dict], thang: str,
                  muc_du: Callable[[dict, list, str], bool]) -> dict:
    """Tính toàn bộ số liệu của 1 tháng.

    cau_hinh_benh: mỗi phần tử {label, case_model, followup_model, muc_moi, muc_tk}
                   — muc_moi/muc_tk là bảng các mục bắt buộc của phiếu mới / phiếu tái khám.
    muc_du:        hàm kiểm tra 1 mục đã điền đủ chưa (dùng lại đúng hàm của main.py để
                   "hoàn thiện" ở báo cáo trùng khớp với nhãn "Đã điền/Chưa điền" trong app).
    """
    d1, dN = khoang_thang(thang)
    theo_ngay = Counter()
    theo_benh = {c["label"]: {"tong": 0, "moi": 0, "tk": 0} for c in cau_hinh_benh}
    moi = tk = du = co_anh = co_gpb = 0
    muc_thieu, muc_co = Counter(), Counter()
    thuoc = {k: Counter() for k, _ in NHOM_DIEU_TRI}
    luot_co_thuoc = Counter()

    def dem(label, ngay, data, da_du, bang_muc, la_moi):
        nonlocal moi, tk, du, co_anh, co_gpb
        theo_ngay[ngay] += 1
        b = theo_benh[label]
        b["tong"] += 1
        if la_moi:
            moi += 1; b["moi"] += 1
        else:
            tk += 1; b["tk"] += 1
        if da_du:
            du += 1
        for ten, khoa in bang_muc.items():
            muc_co[ten] += 1
            if not muc_du(data, khoa, ten):
                muc_thieu[ten] += 1
        if isinstance(data.get("anh"), list) and any(data["anh"]):
            co_anh += 1
        if data.get("gpbCo") == "Có":
            co_gpb += 1
        for nhom, _ in NHOM_DIEU_TRI:
            ds = _thuoc_cua_luot(data, nhom)
            if ds:
                luot_co_thuoc[nhom] += 1
                thuoc[nhom].update(ds)

    # BỆNH ÁN MỚI: cột ngay_tao là ngày TẠO hồ sơ và không đổi khi bác sĩ sửa ô "Ngày khám"
    # (khác với tái khám — cột ngay_kham được cập nhật theo phiếu khi lưu). Ca khám 31/08 mà
    # tới 01/09 mới nhập thì ngay_tao = 01/09. Báo cáo phải tính theo NGÀY KHÁM ghi trong phiếu,
    # nếu không những ca nhập bù đầu tháng — lý do chọn gửi vào ngày 2 — sẽ bị đếm sang tháng sau.
    # Nhập bù thì ngày tạo luôn SAU ngày khám, nên chỉ cần lấy các hồ sơ tạo từ (đầu tháng - 31 ngày)
    # trở đi rồi lọc lại theo ngày khám thật; lùi 31 ngày để đỡ cả trường hợp sửa lùi ngày khám.
    tu_ngay_tao = d1 - timedelta(days=31)
    for c in cau_hinh_benh:
        CaseM, FUM = c["case_model"], c["followup_model"]
        for ca in session.exec(select(CaseM).where(CaseM.ngay_tao >= tu_ngay_tao)).all():
            data = _doc_json(ca.benh_an_moi)
            ngay = ngay_kham_that(data.get("ngayKham")) or ca.ngay_tao
            if ngay and d1 <= ngay <= dN:
                dem(c["label"], ngay, data, bool(ca.da_dien_du_lieu), c["muc_moi"], True)
        for f in session.exec(select(FUM).where(FUM.ngay_kham >= d1, FUM.ngay_kham <= dN)).all():
            dem(c["label"], f.ngay_kham, _doc_json(f.data), bool(f.da_dien_du_lieu), c["muc_tk"], False)

    tong = moi + tk
    pct = lambda a, b: round(a * 100.0 / b, 1) if b else 0.0
    ngay_co_kham = sorted(theo_ngay)
    if ngay_co_kham:
        cao = max(theo_ngay.values()); thap = min(theo_ngay.values())
        ngay_cao = [d.isoformat() for d in ngay_co_kham if theo_ngay[d] == cao]
        ngay_thap = [d.isoformat() for d in ngay_co_kham if theo_ngay[d] == thap]
    else:
        cao = thap = 0; ngay_cao = ngay_thap = []

    return {
        "thang": thang,
        "tu_ngay": d1.isoformat(), "den_ngay": dN.isoformat(),
        "tong_luot": tong,
        "so_ngay_co_kham": len(ngay_co_kham),
        # trung bình trên các NGÀY CÓ KHÁM (bỏ ngày nghỉ) — cùng nguyên tắc với "ngày thấp nhất"
        "tb_moi_ngay": round(tong / len(ngay_co_kham), 1) if ngay_co_kham else 0.0,
        "cao_nhat": {"so_luot": cao, "ngay": ngay_cao},
        "thap_nhat": {"so_luot": thap, "ngay": ngay_thap},
        # đủ mọi ngày trong tháng (kể cả 0) để vẽ biểu đồ không bị hụt ngày
        "theo_ngay": [{"ngay": (d1 + timedelta(days=i)).isoformat(),
                       "so_luot": theo_ngay.get(d1 + timedelta(days=i), 0)}
                      for i in range((dN - d1).days + 1)],
        "kham_moi": moi, "tai_kham": tk, "ty_le_tai_kham": pct(tk, tong),
        "theo_benh": [{"benh": k, **v, "ty_le": pct(v["tong"], tong),
                       "ty_le_moi": pct(v["moi"], v["tong"]), "ty_le_tk": pct(v["tk"], v["tong"])}
                      for k, v in theo_benh.items()],
        "hoan_thien": {"so_luot": du, "ty_le": pct(du, tong),
                       "muc_thieu": sorted(
                           [{"muc": m, "so_luot_thieu": n, "tren": muc_co[m], "ty_le": pct(n, muc_co[m])}
                            for m, n in muc_thieu.items() if n],
                           key=lambda x: (-x["so_luot_thieu"], x["muc"]))},
        "anh": {"so_luot": co_anh, "ty_le": pct(co_anh, tong)},
        "gpb": {"so_luot": co_gpb, "ty_le": pct(co_gpb, tong)},
        "dieu_tri": [{"nhom": k, "ten": ten, "so_luot_co": luot_co_thuoc[k],
                      "ty_le_luot": pct(luot_co_thuoc[k], tong),
                      "so_loai": len(thuoc[k]),
                      "top5": [{"ten": t, "so_luot": n} for t, n in
                               sorted(thuoc[k].items(), key=lambda x: (-x[1], x[0]))[:5]]}
                     for k, ten in NHOM_DIEU_TRI],
    }


# ======================================================================
# 3. Dựng slide PowerPoint
# ======================================================================
# Dựng bằng python-pptx vì máy chủ chạy Python. Biểu đồ là biểu đồ GỐC của PowerPoint (không
# phải ảnh chụp) để lãnh đạo mở ra vẫn bấm xem số, đổi màu, chép sang báo cáo khác được.
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

THU_MUC_ANH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
PHONG = "Calibri"  # có sẵn trong mọi bản Office, hiển thị đủ dấu tiếng Việt

XANH = RGBColor(0x1C, 0x4F, 0x8F)      # xanh chủ đạo của khoa (trùng màu giao diện phần mềm)
XANH_DAM = RGBColor(0x13, 0x3A, 0x6B)
XANH_NHAT = RGBColor(0x7F, 0xB0, 0xE3)
BANG = RGBColor(0xEE, 0xF4, 0xFB)      # nền thẻ số liệu
VIEN = RGBColor(0xD5, 0xE3, 0xF3)
CHU = RGBColor(0x1F, 0x29, 0x37)
CHU_MO = RGBColor(0x5B, 0x6B, 0x80)
TRANG = RGBColor(0xFF, 0xFF, 0xFF)
DO = RGBColor(0xC2, 0x41, 0x0C)
# màu từng bệnh — cùng quy ước với nhãn màu trong phần mềm
MAU_BENH = {"AA": RGBColor(0xDC, 0x26, 0x26), "AGA": RGBColor(0x25, 0x63, 0xEB),
            "TE": RGBColor(0xD9, 0x77, 0x06), "SA": RGBColor(0x7C, 0x3A, 0xED),
            "TTM": RGBColor(0x0D, 0x94, 0x88)}
TEN_BENH = {"AA": "Rụng tóc mảng", "AGA": "Rụng tóc Androgen", "TE": "Rụng tóc Telogen",
            "SA": "Rụng tóc sẹo", "TTM": "Tật nhổ tóc"}

RONG, CAO = Inches(13.333), Inches(7.5)


def so(x) -> str:
    """Số kiểu Việt Nam: 1.234 và 33,3"""
    if isinstance(x, float) and not x.is_integer():
        return f"{x:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{int(round(x)):,}".replace(",", ".")


def pt(x) -> str:
    return so(float(x)) + "%"


def ngay_vn(iso: str, co_nam=False) -> str:
    d = date.fromisoformat(iso)
    return d.strftime("%d/%m/%Y" if co_nam else "%d/%m")


def ds_ngay(ds: List[str]) -> str:
    if not ds:
        return "—"
    if len(ds) <= 3:
        return ", ".join(ngay_vn(x) for x in ds)
    return ", ".join(ngay_vn(x) for x in ds[:3]) + f" và {len(ds) - 3} ngày khác"


class _Slide:
    """Vài hàm vẽ dùng chung để mọi slide cùng lề, cùng cỡ chữ."""

    def __init__(self, prs, nen=TRANG):
        self.s = prs.slides.add_slide(prs.slide_layouts[6])  # layout trống
        bg = self.s.background.fill
        bg.solid(); bg.fore_color.rgb = nen

    def chu(self, x, y, w, h, noi_dung, co=16, dam=False, mau=CHU, canh=PP_ALIGN.LEFT,
            doc=MSO_ANCHOR.TOP, nghieng=False):
        tb = self.s.shapes.add_textbox(x, y, w, h)
        tf = tb.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = doc
        dong = noi_dung if isinstance(noi_dung, list) else [noi_dung]
        for i, d in enumerate(dong):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = canh
            r = p.add_run()
            r.text = str(d)
            f = r.font
            f.name, f.size, f.bold, f.italic = PHONG, Pt(co), dam, nghieng
            f.color.rgb = mau
        return tb

    def khoi(self, x, y, w, h, mau=BANG, vien=None, bo_goc=True, hinh=None):
        hinh = hinh or (MSO_SHAPE.ROUNDED_RECTANGLE if bo_goc else MSO_SHAPE.RECTANGLE)
        sh = self.s.shapes.add_shape(hinh, x, y, w, h)
        if hinh == MSO_SHAPE.ROUNDED_RECTANGLE:
            sh.adjustments[0] = 0.08
        sh.fill.solid(); sh.fill.fore_color.rgb = mau
        if vien:
            sh.line.color.rgb = vien; sh.line.width = Pt(1)
        else:
            sh.line.fill.background()
        sh.shadow.inherit = False
        return sh

    def tieu_de(self, chu, phu=None):
        self.chu(Inches(0.6), Inches(0.45), Inches(12.1), Inches(0.7), chu, co=30, dam=True, mau=XANH)
        if phu:
            self.chu(Inches(0.6), Inches(1.12), Inches(12.1), Inches(0.4), phu, co=14, mau=CHU_MO)

    def chan_trang(self, thang, trang):
        anh = os.path.join(THU_MUC_ANH, "nangtoc.png")
        if os.path.exists(anh):
            self.s.shapes.add_picture(anh, Inches(0.6), Inches(6.93), height=Inches(0.32))
        self.chu(Inches(1.0), Inches(6.98), Inches(9), Inches(0.3),
                 f"Phòng khám Rụng tóc · Báo cáo hoạt động tháng {nhan_thang(thang)}", co=10, mau=CHU_MO)
        self.chu(Inches(11.2), Inches(6.98), Inches(1.53), Inches(0.3), str(trang), co=10, mau=CHU_MO,
                 canh=PP_ALIGN.RIGHT)

    def the_so(self, x, y, w, h, nhan, gia_tri, phu="", mau_so=XANH):
        """Thẻ số liệu lớn: nhãn nhỏ phía trên, con số to, dòng giải thích phía dưới."""
        self.khoi(x, y, w, h)
        pad = Inches(0.25)
        self.chu(x + pad, y + Inches(0.2), w - 2 * pad, Inches(0.35), nhan, co=13, mau=CHU_MO)
        self.chu(x + pad, y + Inches(0.55), w - 2 * pad, Inches(0.9), gia_tri, co=40, dam=True, mau=mau_so)
        if phu:
            self.chu(x + pad, y + h - Inches(0.55), w - 2 * pad, Inches(0.4), phu, co=12, mau=CHU)

    def thanh_ty_le(self, x, y, w, ty_le, mau=XANH, h=Inches(0.16)):
        self.khoi(x, y, w, h, mau=VIEN)
        dai = int(w * max(0.0, min(ty_le, 100.0)) / 100.0)
        if dai > 0:
            self.khoi(x, y, max(dai, h), h, mau=mau)

    def bieu_do(self, loai, x, y, w, h, nhan: List[str], chuoi: Dict[str, List[float]],
                mau: List[RGBColor], nhan_so=True, chu_giai=False, dinh_dang='0', co_chu=11):
        cd = CategoryChartData()
        cd.categories = nhan
        for ten, gt in chuoi.items():
            cd.add_series(ten, gt)
        ch = self.s.shapes.add_chart(loai, x, y, w, h, cd).chart
        ch.font.name, ch.font.size = PHONG, Pt(co_chu)
        ch.font.color.rgb = CHU
        ch.has_title = False
        ch.has_legend = chu_giai
        if chu_giai:
            ch.legend.position = XL_LEGEND_POSITION.TOP
            ch.legend.include_in_layout = False
            ch.legend.font.size = Pt(12)
        for i, sr in enumerate(ch.plots[0].series):
            sr.format.fill.solid(); sr.format.fill.fore_color.rgb = mau[i % len(mau)]
        if nhan_so:
            pl = ch.plots[0]
            pl.has_data_labels = True
            dl = pl.data_labels
            dl.number_format, dl.number_format_is_linked = dinh_dang, False
            dl.font.size, dl.font.name = Pt(co_chu), PHONG
        return ch


def _truc_gon(ch, an_truc_gia_tri=True):
    """Bỏ lưới và trục giá trị cho biểu đồ đã có nhãn số — đỡ rối mắt."""
    try:
        va = ch.value_axis
        va.has_major_gridlines = False
        va.visible = not an_truc_gia_tri
        ca = ch.category_axis
        ca.format.line.color.rgb = VIEN
        ca.tick_labels.font.size = Pt(11)
    except (AttributeError, ValueError):
        pass


def tao_pptx(tk: dict, ngay_tao: Optional[date] = None) -> bytes:
    ngay_tao = ngay_tao or bay_gio_vn().date()
    prs = Presentation()
    prs.slide_width, prs.slide_height = RONG, CAO
    thang = tk["thang"]
    trang = [1]

    def slide_moi():
        trang[0] += 1
        s = _Slide(prs)
        s.chan_trang(thang, trang[0])
        return s

    # ---------- 1. Bìa ----------
    s = _Slide(prs, nen=XANH)
    # nền tròn trắng cho logo (logo màu xanh đậm, đặt thẳng lên nền xanh sẽ chìm)
    s.khoi(Inches(0.9), Inches(1.55), Inches(2.6), Inches(2.6), mau=TRANG, hinh=MSO_SHAPE.OVAL)
    logo = os.path.join(THU_MUC_ANH, "logo_khoa.png")
    if os.path.exists(logo):
        s.s.shapes.add_picture(logo, Inches(1.05), Inches(1.7), width=Inches(2.3), height=Inches(2.3))
    s.chu(Inches(4.0), Inches(1.45), Inches(8.7), Inches(0.5), "PHÒNG KHÁM RỤNG TÓC", co=18, dam=True,
          mau=XANH_NHAT)
    s.chu(Inches(4.0), Inches(1.95), Inches(8.7), Inches(1.4),
          ["Báo cáo hoạt động", f"tháng {nhan_thang(thang)}"], co=44, dam=True, mau=TRANG)
    s.chu(Inches(4.0), Inches(3.55), Inches(8.7), Inches(0.8),
          ["Khoa Điều trị nội trú ban ngày", "Bệnh viện Da liễu Trung ương"], co=16, mau=TRANG)
    s.chu(Inches(0.9), Inches(5.6), Inches(11.5), Inches(0.9),
          [f"Số liệu từ {ngay_vn(tk['tu_ngay'], True)} đến {ngay_vn(tk['den_ngay'], True)}",
           f"Báo cáo do phần mềm bệnh án tự tổng hợp ngày {ngay_tao.strftime('%d/%m/%Y')}"],
          co=13, mau=XANH_NHAT)

    if tk["tong_luot"] == 0:
        s = slide_moi()
        s.tieu_de("Không có lượt khám nào trong tháng")
        s.chu(Inches(0.6), Inches(1.8), Inches(12), Inches(1.5),
              ["Phần mềm không ghi nhận bệnh án mới hay lần tái khám nào trong khoảng thời gian trên.",
               "Nếu phòng khám vẫn hoạt động, hãy kiểm tra lại việc nhập liệu trên phần mềm."], co=18)
        return _luu(prs)

    # ---------- 2. Tổng quan lượt khám ----------
    s = slide_moi()
    s.tieu_de("Tổng quan lượt khám",
              f"{so(tk['so_ngay_co_kham'])} ngày có khám trong tháng · 1 lượt = 1 bệnh án mới hoặc 1 lần tái khám")
    w, g, y = Inches(2.85), Inches(0.23), Inches(1.7)
    cao, thap = tk["cao_nhat"], tk["thap_nhat"]
    for i, (nhan, gt, phu) in enumerate([
        ("Tổng lượt khám", so(tk["tong_luot"]), f"{so(tk['kham_moi'])} mới · {so(tk['tai_kham'])} tái khám"),
        ("Trung bình mỗi ngày", so(tk["tb_moi_ngay"]), "lượt / ngày có khám"),
        ("Ngày cao nhất", so(cao["so_luot"]), ds_ngay(cao["ngay"])),
        ("Ngày thấp nhất", so(thap["so_luot"]), ds_ngay(thap["ngay"])),
    ]):
        s.the_so(Inches(0.6) + i * (w + g), y, w, Inches(1.75), nhan, gt, phu)
    nd = tk["theo_ngay"]
    ch = s.bieu_do(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.45), Inches(3.65), Inches(12.4), Inches(3.2),
                   [date.fromisoformat(x["ngay"]).strftime("%d") for x in nd],
                   {"Lượt khám": [x["so_luot"] for x in nd]}, [XANH], co_chu=10, dinh_dang='0;;;')
    ch.plots[0].gap_width = 40
    ch.has_title = True
    ch.chart_title.text_frame.text = "Số lượt khám theo ngày"
    ch.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(13)
    ch.chart_title.text_frame.paragraphs[0].runs[0].font.bold = False
    _truc_gon(ch)

    # ---------- 3. Khám mới và tái khám ----------
    s = slide_moi()
    s.tieu_de("Khám mới và tái khám")
    ch = s.bieu_do(XL_CHART_TYPE.DOUGHNUT, Inches(0.6), Inches(1.6), Inches(5.6), Inches(5.0),
                   ["Khám mới", "Tái khám"], {"Lượt": [tk["kham_moi"], tk["tai_kham"]]},
                   [XANH], nhan_so=False, chu_giai=True, co_chu=14)
    diem = ch.plots[0].series[0].points
    for i, m in enumerate([XANH, XANH_NHAT]):
        diem[i].format.fill.solid(); diem[i].format.fill.fore_color.rgb = m
    x0 = Inches(6.8)
    s.the_so(x0, Inches(1.7), Inches(2.85), Inches(1.75), "Khám mới", so(tk["kham_moi"]),
             pt(100 - tk["ty_le_tai_kham"]) + " tổng lượt")
    s.the_so(x0 + Inches(3.08), Inches(1.7), Inches(2.85), Inches(1.75), "Tái khám", so(tk["tai_kham"]),
             pt(tk["ty_le_tai_kham"]) + " tổng lượt", mau_so=XANH_DAM)
    s.khoi(x0, Inches(3.75), Inches(5.93), Inches(2.6), mau=BANG)
    s.chu(x0 + Inches(0.3), Inches(3.95), Inches(5.3), Inches(0.4), "Tỷ lệ khám lại", co=13, mau=CHU_MO)
    s.chu(x0 + Inches(0.3), Inches(4.35), Inches(5.3), Inches(1.1), pt(tk["ty_le_tai_kham"]), co=60, dam=True,
          mau=XANH)
    s.thanh_ty_le(x0 + Inches(0.3), Inches(5.55), Inches(5.3), tk["ty_le_tai_kham"])
    s.chu(x0 + Inches(0.3), Inches(5.85), Inches(5.3), Inches(0.4),
          "= số lần tái khám / tổng lượt khám trong tháng", co=11, mau=CHU_MO, nghieng=True)

    # ---------- 4. Theo từng bệnh ----------
    s = slide_moi()
    s.tieu_de("Phân bố theo bệnh", "Số lượt khám của từng bệnh, tách khám mới và tái khám")
    tb = [b for b in tk["theo_benh"]]
    ch = s.bieu_do(XL_CHART_TYPE.BAR_STACKED, Inches(0.45), Inches(1.6), Inches(5.9), Inches(5.1),
                   [b["benh"] for b in tb][::-1],
                   {"Khám mới": [b["moi"] for b in tb][::-1], "Tái khám": [b["tk"] for b in tb][::-1]},
                   [XANH, XANH_NHAT], chu_giai=True, dinh_dang='0;;;', co_chu=12)
    ch.plots[0].gap_width = 60
    ch.plots[0].overlap = 100
    ch.plots[0].data_labels.position = XL_LABEL_POSITION.CENTER
    ch.plots[0].data_labels.font.color.rgb = TRANG
    _truc_gon(ch)
    # bảng chi tiết
    cot = ["Bệnh", "Lượt", "Tỷ lệ", "Mới", "Tái khám", "% tái khám"]
    rong_cot = [Inches(2.3), Inches(0.7), Inches(0.8), Inches(0.65), Inches(0.85), Inches(0.95)]
    bang = s.s.shapes.add_table(len(tb) + 2, len(cot), Inches(6.45), Inches(1.75),
                                sum(rong_cot, Emu(0)), Inches(0.45) * (len(tb) + 2)).table
    for j, wc in enumerate(rong_cot):
        bang.columns[j].width = wc
    tong = {"benh": "Tổng", "tong": tk["tong_luot"], "ty_le": 100.0, "moi": tk["kham_moi"],
            "tk": tk["tai_kham"], "ty_le_tk": tk["ty_le_tai_kham"]}
    hang = [cot] + [[f"{b['benh']} · {TEN_BENH.get(b['benh'], '')}", so(b["tong"]), pt(b["ty_le"]),
                     so(b["moi"]), so(b["tk"]), pt(b["ty_le_tk"])] for b in tb + [tong]]
    hang[-1][0] = "Tổng"
    for i, dong in enumerate(hang):
        for j, gt in enumerate(dong):
            o = bang.cell(i, j)
            o.fill.solid()
            o.fill.fore_color.rgb = XANH if i == 0 else (BANG if i == len(hang) - 1 else TRANG)
            o.margin_left = o.margin_right = Inches(0.08)
            o.vertical_anchor = MSO_ANCHOR.MIDDLE
            p = o.text_frame.paragraphs[0]
            p.alignment = PP_ALIGN.LEFT if j == 0 else PP_ALIGN.RIGHT
            r = p.add_run(); r.text = gt
            r.font.name, r.font.size = PHONG, Pt(12)
            r.font.bold = i == 0 or i == len(hang) - 1
            r.font.color.rgb = TRANG if i == 0 else (MAU_BENH.get(dong[0][:3].strip(" ·"), CHU) if j == 0 and 0 < i < len(hang) - 1 else CHU)

    # ---------- 5. Chất lượng hồ sơ ----------
    s = slide_moi()
    s.tieu_de("Chất lượng hồ sơ bệnh án", "Tính trên toàn bộ lượt khám của tháng")
    ht = tk["hoan_thien"]
    for i, (nhan, ty_le, n, giai_thich) in enumerate([
        ("Bệnh án hoàn thiện", ht["ty_le"], ht["so_luot"], "Đủ mọi mục bắt buộc (nhãn “Đã điền” trong phần mềm)"),
        ("Có ảnh lâm sàng", tk["anh"]["ty_le"], tk["anh"]["so_luot"], "Có từ 1 ảnh trở lên"),
        ("Có thực hiện giải phẫu bệnh", tk["gpb"]["ty_le"], tk["gpb"]["so_luot"], "Mục GPB chọn “Có”"),
    ]):
        x = Inches(0.6) + i * Inches(4.1)
        s.khoi(x, Inches(1.8), Inches(3.9), Inches(4.4))
        s.chu(x + Inches(0.3), Inches(2.05), Inches(3.3), Inches(0.45), nhan, co=16, dam=True, mau=CHU)
        s.chu(x + Inches(0.3), Inches(2.65), Inches(3.3), Inches(1.2), pt(ty_le), co=60, dam=True, mau=XANH)
        s.thanh_ty_le(x + Inches(0.3), Inches(3.95), Inches(3.3), ty_le)
        s.chu(x + Inches(0.3), Inches(4.3), Inches(3.3), Inches(0.45),
              f"{so(n)} / {so(tk['tong_luot'])} lượt khám", co=14, mau=CHU)
        s.chu(x + Inches(0.3), Inches(5.2), Inches(3.3), Inches(0.8), giai_thich, co=12, mau=CHU_MO, nghieng=True)

    # ---------- 6. Mục còn thiếu ----------
    s = slide_moi()
    mt = ht["muc_thieu"]
    s.tieu_de("Các mục bệnh án còn thiếu",
              "Số lượt khám bị thiếu từng mục — mục nào nhiều nhất thì cần nhắc nhập liệu trước")
    if not mt:
        s.chu(Inches(0.6), Inches(2.2), Inches(12), Inches(1), "Tất cả lượt khám trong tháng đã điền đủ các mục bắt buộc.",
              co=20, mau=XANH)
    else:
        hien = mt[:12]
        ch = s.bieu_do(XL_CHART_TYPE.BAR_CLUSTERED, Inches(0.45), Inches(1.6), Inches(8.3), Inches(5.2),
                       [m["muc"] for m in hien][::-1], {"Số lượt thiếu": [m["so_luot_thieu"] for m in hien][::-1]},
                       [DO], co_chu=11)
        ch.plots[0].gap_width = 45
        _truc_gon(ch)
        x0 = Inches(9.05)
        s.khoi(x0, Inches(1.75), Inches(3.7), Inches(4.9))
        s.chu(x0 + Inches(0.25), Inches(1.95), Inches(3.2), Inches(0.4), "Tỷ lệ thiếu từng mục", co=14, dam=True)
        dong = [f"{m['muc']}: {pt(m['ty_le'])}  ({so(m['so_luot_thieu'])}/{so(m['tren'])})" for m in hien]
        s.chu(x0 + Inches(0.25), Inches(2.45), Inches(3.25), Inches(3.9), dong, co=11, mau=CHU)
        if len(mt) > len(hien):
            s.chu(x0 + Inches(0.25), Inches(6.2), Inches(3.2), Inches(0.35),
                  f"và {len(mt) - len(hien)} mục khác thiếu ít hơn", co=10, mau=CHU_MO, nghieng=True)

    # ---------- 7-9. Điều trị ----------
    for nhom in tk["dieu_tri"]:
        s = slide_moi()
        s.tieu_de(f"Điều trị — {nhom['ten']}", "5 loại được chỉ định nhiều nhất trong tháng")
        s.the_so(Inches(0.6), Inches(1.75), Inches(3.6), Inches(2.1), "Số loại khác nhau", so(nhom["so_loai"]),
                 "đã được chỉ định trong tháng")
        s.the_so(Inches(0.6), Inches(4.1), Inches(3.6), Inches(2.1), "Lượt khám có chỉ định",
                 so(nhom["so_luot_co"]), f"{pt(nhom['ty_le_luot'])} tổng lượt khám")
        top = nhom["top5"]
        if not top:
            s.chu(Inches(4.8), Inches(3.4), Inches(7.9), Inches(1), "Không có chỉ định nào trong tháng.",
                  co=20, mau=CHU_MO, canh=PP_ALIGN.CENTER)
            continue
        ch = s.bieu_do(XL_CHART_TYPE.BAR_CLUSTERED, Inches(4.6), Inches(1.65), Inches(8.2), Inches(4.8),
                       [t["ten"] for t in top][::-1], {"Số lượt": [t["so_luot"] for t in top][::-1]},
                       [XANH], co_chu=13)
        ch.plots[0].gap_width = 55
        _truc_gon(ch)
        ch.category_axis.tick_labels.font.size = Pt(13)
        s.chu(Inches(4.8), Inches(6.45), Inches(7.9), Inches(0.35),
              "Số lượt khám có chỉ định loại đó (1 lượt kê nhiều loại thì mỗi loại được đếm 1 lần)",
              co=10, mau=CHU_MO, nghieng=True)

    # ---------- 10. Cách tính ----------
    s = slide_moi()
    s.tieu_de("Ghi chú cách tính")
    s.chu(Inches(0.6), Inches(1.5), Inches(12.1), Inches(5.2), [
        "• Lượt khám: mỗi bệnh án mới (theo ngày tạo) hoặc mỗi lần tái khám (theo ngày khám) có ngày thuộc tháng báo cáo.",
        "• Trung bình, ngày cao nhất, ngày thấp nhất: chỉ tính trên các ngày có ít nhất 1 lượt khám (bỏ ngày nghỉ).",
        "• Tỷ lệ khám lại = số lần tái khám / tổng lượt khám.",
        "• Bệnh án hoàn thiện: đủ mọi mục bắt buộc — cùng quy tắc với nhãn “Đã điền / Chưa điền” trong phần mềm.",
        "• Mục còn thiếu: tỷ lệ tính trên số lượt khám có mục đó trong phiếu (phiếu mới và phiếu tái khám có số mục khác nhau).",
        "• Ảnh: lượt khám có từ 1 ảnh trở lên. Giải phẫu bệnh: lượt khám có mục GPB chọn “Có”.",
        "• Điều trị: chỉ đếm khi mục tương ứng chọn “Có”. Mục “Khác” hiện đúng nội dung bác sĩ ghi.",
        "• Báo cáo chỉ gồm số liệu tổng hợp, không chứa thông tin định danh người bệnh.",
    ], co=15, mau=CHU)
    return _luu(prs)


def _luu(prs) -> bytes:
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


# ======================================================================
# 4. Gửi mail
# ======================================================================
# Render bản MIỄN PHÍ chặn toàn bộ cổng SMTP (25/465/587) từ 09/2025, nên không thể đăng nhập
# Gmail rồi gửi thẳng từ máy chủ. Đường mặc định: máy chủ gọi 1 đoạn Google Apps Script chạy
# trong chính tài khoản Gmail của khoa (qua HTTPS, cổng 443 không bị chặn); script đó gửi mail.
# Nếu sau này dùng gói Render trả phí hoặc máy chủ mail của bệnh viện thì chỉ cần khai biến
# SMTP_* — code tự chọn đường gửi, không phải sửa gì.
import base64
import re
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage

MIME_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_MAU_MAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


class LoiGuiMail(Exception):
    """Lỗi có thông báo tiếng Việt đọc được, để hiện thẳng lên màn hình quản trị."""


def tach_danh_sach_mail(van_ban) -> List[str]:
    """Nhận danh sách mail cách nhau bởi dấu phẩy, chấm phẩy hoặc xuống dòng; bỏ trùng."""
    if isinstance(van_ban, list):
        van_ban = "\n".join(str(x) for x in van_ban)
    ra = []
    for m in re.split(r"[,;\s]+", str(van_ban or "")):
        m = m.strip().lower()
        if m and m not in ra:
            ra.append(m)
    return ra


def mail_sai_dinh_dang(ds: List[str]) -> List[str]:
    return [m for m in ds if not _MAU_MAIL.match(m)]


def cach_gui_hien_tai(cfg) -> Optional[str]:
    if getattr(cfg, "MAIL_RELAY_URL", None) and getattr(cfg, "MAIL_RELAY_KEY", None):
        return "apps_script"
    if getattr(cfg, "SMTP_HOST", None) and getattr(cfg, "SMTP_USER", None) and getattr(cfg, "SMTP_PASSWORD", None):
        return "smtp"
    return None


def noi_dung_mail(tk: dict) -> str:
    """Thân mail tóm tắt vài con số chính — lãnh đạo đọc trên điện thoại không cần mở file."""
    dong = [
        ("Tổng lượt khám", f"{so(tk['tong_luot'])} lượt · {so(tk['so_ngay_co_kham'])} ngày có khám"),
        ("Trung bình", f"{so(tk['tb_moi_ngay'])} lượt/ngày có khám"),
        ("Khám mới / tái khám", f"{so(tk['kham_moi'])} / {so(tk['tai_kham'])} (tái khám {pt(tk['ty_le_tai_kham'])})"),
        ("Bệnh án hoàn thiện", pt(tk["hoan_thien"]["ty_le"])),
        ("Có ảnh lâm sàng", pt(tk["anh"]["ty_le"])),
        ("Có giải phẫu bệnh", pt(tk["gpb"]["ty_le"])),
    ]
    hang = "".join(
        f'<tr><td style="padding:6px 12px;color:#5b6b80">{a}</td>'
        f'<td style="padding:6px 12px;font-weight:600;color:#1c4f8f">{b}</td></tr>' for a, b in dong)
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1f2937;max-width:620px">'
        f'<p>Kính gửi Ban lãnh đạo Khoa,</p>'
        f'<p>Phần mềm bệnh án gửi báo cáo hoạt động <b>Phòng khám Rụng tóc tháng {nhan_thang(tk["thang"])}</b> '
        f'(từ {ngay_vn(tk["tu_ngay"], True)} đến {ngay_vn(tk["den_ngay"], True)}). Một số số liệu chính:</p>'
        f'<table style="border-collapse:collapse;background:#eef4fb;border-radius:8px">{hang}</table>'
        '<p>Chi tiết theo từng bệnh, các mục bệnh án còn thiếu và 5 thuốc/thủ thuật dùng nhiều nhất '
        'nằm trong file PowerPoint đính kèm.</p>'
        '<p style="color:#5b6b80;font-size:12px">Mail gửi tự động, chỉ gồm số liệu tổng hợp và không chứa '
        'thông tin định danh người bệnh. Vui lòng không trả lời mail này.</p></div>'
    )


def gui_mail(cfg, nguoi_nhan: List[str], tieu_de: str, html: str, ten_tep: str, tep: bytes) -> str:
    """Gửi 1 mail có đính kèm. Trả về mô tả ngắn khi thành công, ném LoiGuiMail khi thất bại."""
    if not nguoi_nhan:
        raise LoiGuiMail("Chưa có địa chỉ người nhận. Vào màn Báo cáo tháng để nhập.")
    sai = mail_sai_dinh_dang(nguoi_nhan)
    if sai:
        raise LoiGuiMail("Địa chỉ mail không hợp lệ: " + ", ".join(sai))
    cach = cach_gui_hien_tai(cfg)
    ten_gui = getattr(cfg, "MAIL_FROM_NAME", None) or "Phòng khám Rụng tóc"

    if cach == "apps_script":
        goi = json.dumps({
            "khoa": cfg.MAIL_RELAY_KEY, "den": nguoi_nhan, "tieu_de": tieu_de, "html": html,
            "ten_nguoi_gui": ten_gui, "ten_tep": ten_tep, "mime": MIME_PPTX,
            "tep_base64": base64.b64encode(tep).decode("ascii"),
        }).encode("utf-8")
        req = urllib.request.Request(cfg.MAIL_RELAY_URL, data=goi, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            # Apps Script trả về qua 1 lần chuyển hướng 302 — urllib tự đi theo và đọc kết quả
            with urllib.request.urlopen(req, timeout=60) as r:
                tho = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise LoiGuiMail(f"Apps Script trả lỗi HTTP {e.code}. Kiểm tra lại địa chỉ Web app "
                             f"(phải kết thúc bằng /exec) và quyền truy cập 'Bất kỳ ai'.")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LoiGuiMail(f"Không kết nối được tới Apps Script: {e}")
        try:
            kq = json.loads(tho)
        except ValueError:
            raise LoiGuiMail("Apps Script không trả về kết quả đúng định dạng — thường do Web app "
                             "chưa triển khai với quyền 'Bất kỳ ai' hoặc chưa cấp quyền gửi mail.")
        if not kq.get("ok"):
            raise LoiGuiMail("Apps Script báo lỗi: " + str(kq.get("loi") or "không rõ"))
        con = kq.get("con_lai_hom_nay")
        return f"Đã gửi qua Gmail của khoa" + (f" (hạn mức còn {con} mail hôm nay)" if con is not None else "")

    if cach == "smtp":
        msg = EmailMessage()
        msg["Subject"] = tieu_de
        msg["From"] = f"{ten_gui} <{cfg.SMTP_USER}>"
        msg["To"] = ", ".join(nguoi_nhan)
        msg.set_content("Báo cáo hoạt động phòng khám — xem nội dung HTML hoặc file đính kèm.")
        msg.add_alternative(html, subtype="html")
        msg.add_attachment(tep, maintype="application",
                           subtype="vnd.openxmlformats-officedocument.presentationml.presentation",
                           filename=ten_tep)
        cong = int(getattr(cfg, "SMTP_PORT", 465) or 465)
        # Gmail cấp "mật khẩu ứng dụng" dạng "abcd efgh ijkl mnop" — bỏ dấu cách cho chắc
        mk = str(cfg.SMTP_PASSWORD).replace(" ", "")
        try:
            if cong == 465:
                with smtplib.SMTP_SSL(cfg.SMTP_HOST, cong, context=ssl.create_default_context(), timeout=30) as sv:
                    sv.login(cfg.SMTP_USER, mk); sv.send_message(msg)
            else:
                with smtplib.SMTP(cfg.SMTP_HOST, cong, timeout=30) as sv:
                    sv.starttls(context=ssl.create_default_context())
                    sv.login(cfg.SMTP_USER, mk); sv.send_message(msg)
        except smtplib.SMTPAuthenticationError:
            raise LoiGuiMail("Máy chủ mail từ chối đăng nhập — kiểm tra SMTP_USER / SMTP_PASSWORD "
                             "(với Gmail phải dùng 'mật khẩu ứng dụng', không phải mật khẩu thường).")
        except (OSError, smtplib.SMTPException) as e:
            raise LoiGuiMail(f"Không gửi được qua SMTP ({cfg.SMTP_HOST}:{cong}): {e}. "
                             "Lưu ý: Render bản miễn phí chặn cổng SMTP — hãy dùng Apps Script.")
        return f"Đã gửi qua SMTP {cfg.SMTP_HOST}"

    raise LoiGuiMail("Máy chủ chưa được cấu hình cách gửi mail (thiếu MAIL_RELAY_URL / MAIL_RELAY_KEY "
                     "trên Render). Xem hướng dẫn cài đặt báo cáo tháng.")


def ten_tep_bao_cao(thang: str) -> str:
    d1, _ = khoang_thang(thang)
    return f"Bao_cao_phong_kham_rung_toc_T{d1.month:02d}_{d1.year}.pptx"


def tieu_de_mail(thang: str) -> str:
    return f"[Phòng khám Rụng tóc] Báo cáo hoạt động tháng {nhan_thang(thang)}"
