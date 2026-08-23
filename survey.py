"""Đọc phiếu khảo sát điện tử do BỆNH NHÂN TỰ ĐIỀN trên hệ thống của bệnh viện
(api.dalieu.vn) và chuyển thành các trường của bệnh án nghiên cứu.

Vì sao gọi API từ backend chứ không gọi thẳng từ trình duyệt:
  1. Trình duyệt sẽ bị CORS chặn (API bệnh viện không cấp quyền cho tên miền GitHub Pages).
  2. Chỉ cần sửa 1 chỗ khi API bệnh viện đổi địa chỉ/tham số.
  3. Dùng lại được logic dò mã bệnh nhân bị gõ tắt (bỏ số 0 ở đầu).

Dùng urllib của thư viện chuẩn Python — KHÔNG thêm thư viện mới vào requirements.txt,
tránh phải cài thêm gói trên Render.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import config


# ---------- 1. Gọi API bệnh viện ----------

def _call_api(ma_bn: str) -> Optional[dict]:
    params = urllib.parse.urlencode({
        "RoomId": config.SURVEY_ROOM_ID,
        "CheckValue": ma_bn,
        "FindType": "3",  # 3 = tra theo mã bệnh nhân
    })
    url = f"{config.SURVEY_API_BASE}/api/services/app/ClinicSurveyAnswer/CheckSurvey?{params}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "benh-an-nghien-cuu/1.0"})
    with urllib.request.urlopen(req, timeout=config.SURVEY_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("success"):
        return None
    return body.get("result") or None


def _ma_bn_candidates(ma_bn: str) -> List[str]:
    """Mã BN thật có số 0 ở đầu (0030294637) nhưng bác sĩ hay gõ tắt (30294637).
    Thử lần lượt: mã đã gõ -> mã đã đệm 0 cho đủ 10 ký tự."""
    out = [ma_bn]
    digits = ma_bn.strip()
    if digits.isdigit() and len(digits) < 10:
        padded = digits.zfill(10)
        if padded not in out:
            out.append(padded)
    return out


def fetch_survey(ma_bn: str) -> Dict[str, Any]:
    """Trả về {'found': bool, 'result': dict|None, 'loi': str|None}.
    KHÔNG ném lỗi ra ngoài — API bệnh viện chậm/hỏng không được làm treo màn nhập bệnh án."""
    last_error = None
    for candidate in _ma_bn_candidates(ma_bn):
        try:
            result = _call_api(candidate)
        except urllib.error.HTTPError as e:
            last_error = f"Hệ thống bệnh viện trả lỗi {e.code}"
            continue
        except urllib.error.URLError as e:
            return {"found": False, "result": None, "loi": f"Không kết nối được hệ thống bệnh viện ({e.reason})"}
        except Exception as e:  # timeout, JSON hỏng...
            return {"found": False, "result": None, "loi": f"Không đọc được dữ liệu từ hệ thống bệnh viện ({e})"}
        if result:
            return {"found": True, "result": result, "loi": None}
    return {"found": False, "result": None, "loi": last_error or "Hệ thống bệnh viện không có hồ sơ với mã này"}


# ---------- 2. Công cụ đọc giá trị trong phiếu ----------

def _yn(v) -> Optional[str]:
    """Chuyển đáp án Có/Không/Không rõ của phiếu sang đúng chữ dùng trong bệnh án."""
    if v is True:
        return "Có"
    if v is False or v is None:
        return None
    s = str(v).strip().lower()
    if s in ("yes", "y", "true", "1", "có", "co"):
        return "Có"
    if s in ("no", "n", "false", "0", "không", "khong"):
        return "Không"
    if s in ("unknown", "notsure", "không rõ", "khong ro", "không biết"):
        return "Không biết"
    return None


def _is_yes(a: dict, key: str) -> bool:
    return _yn(a.get(key)) == "Có"


def _any_yes(a: dict, keys: List[str]) -> bool:
    return any(_is_yes(a, k) for k in keys)


def _txt(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _num(v):
    if v is None or v == "":
        return None
    try:
        n = float(v)
        return int(n) if n == int(n) else n
    except (TypeError, ValueError):
        return None


# Ba mức trả về cho bảng "Tiền sử bản thân" (Có / Không / Không biết).
# Trả None nghĩa là phiếu không hỏi -> KHÔNG ghi đè, để bác sĩ tự điền.
def _yn_or_none(a: dict, *keys: str) -> Optional[str]:
    vals = [_yn(a.get(k)) for k in keys if k in a]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    if "Có" in vals:
        return "Có"
    if "Không" in vals:
        return "Không"
    return "Không biết"


DLQI_KEYS = [
    "dlqi_scalpItchingPainOrStinging",
    "dlqi_embarrassmentOrSelfConsciousness",
    "dlqi_impactOnShoppingAndDailyActivities",
    "dlqi_impactOnClothingOrHeadwearChoice",
    "dlqi_impactOnSocialOrLeisureActivities",
    "dlqi_impactOnSportsOrPhysicalActivities",
    "dlqi_impactOnWorkOrStudy",
    "dlqi_impactOnPersonalRelationships",
    "dlqi_impactOnSexualLife",
    "dlqi_treatmentInconvenience",
]


def dlqi_total(a: dict) -> Optional[int]:
    """Tổng điểm DLQI 0-30. Trả None nếu bệnh nhân chưa trả lời câu nào."""
    vals = [_num(a.get(k)) for k in DLQI_KEYS]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return int(sum(vals))


# ---------- 3. Tóm tắt lịch sử điều trị thành đoạn văn cho ô "Bệnh sử các đợt trước" ----------

# Phiếu lưu tần suất dưới dạng mã tiếng Anh — dịch sang tiếng Việt cho bác sĩ dễ đọc.
_FREQ_LABELS = {
    "1/day": "1 lần/ngày",
    "2/day": "2 lần/ngày",
    "1/week": "1 lần/tuần",
    "2/week": "2 lần/tuần",
    "3/week": "3 lần/tuần",
    "alternate": "cách ngày",
    "other": "khác",
}

_MINOXIDIL_FIELDS = [
    ("_concentration", "nồng độ {}%"),
    ("_freq", "tần suất {}"),
    ("_dose", "liều {}"),
    ("_duration", "dùng {}"),
    ("_regular", "dùng đều: {}"),
    ("_effective", "hiệu quả: {}"),
    ("_initialShedding", "rụng tăng lúc đầu: {}"),
    ("_sideEffects", "tác dụng phụ: {}"),
    ("_sideEffects_list", "({})"),
]

_OTHER_TREATMENTS = [
    ("other_biotin", "Biotin", "other_biotin_dose"),
    ("other_spiro", "Spironolactone/Aldactone", "other_spiro_dose"),
    ("other_dutasteride", "Finasteride hoặc Dutasteride", "other_dutasteride_dose"),
    ("other_ledcap", "Mũ LED / liệu pháp ánh sáng", None),
    ("other_ketoconazole", "Dầu gội Ketoconazole/Nizoral", None),
    ("other_antibiotics", "Kháng sinh", "other_antibiotics_dose"),
    ("other_scalpInjection", "Tiêm da đầu", "other_injection_visitsEstimate"),
    ("other_iron", "Bổ sung sắt (1 tháng gần đây)", "other_iron_dose"),
    ("other_vitamins", "Bổ sung kẽm/vitamin D/B12 (1 tháng gần đây)", "other_vitamins_list"),
    ("topicalHerbalSupp", "Dầu thoa/thảo dược/thực phẩm bổ sung", "topicalHerbalSupp_list"),
]

_INJECTION_TYPES = [
    ("other_injection_prp", "PRP"),
    ("other_injection_prf", "PRF"),
    ("other_injection_steroid", "Corticoid nội tổn thương"),
    ("other_injection_other", "Khác"),
]

_SIDE_EFFECTS = [
    ("sidefx_dizziness", "chóng mặt"),
    ("sidefx_unwantedHair", "mọc lông ngoài ý muốn"),
    ("sidefx_breastEnlarge", "to tuyến vú"),
    ("sidefx_scalpIrritation", "kích ứng da đầu"),
]


def _minoxidil_line(a: dict, prefix: str, label: str) -> Optional[str]:
    if not _is_yes(a, prefix):
        return None
    bits = []
    for suffix, template in _MINOXIDIL_FIELDS:
        raw = a.get(prefix + suffix)
        if suffix in ("_regular", "_effective", "_initialShedding", "_sideEffects"):
            val = _yn(raw)
        elif suffix == "_freq":
            val = _FREQ_LABELS.get(_txt(raw), _txt(raw))
            if val == "khác":
                val = _txt(a.get(prefix + "_freq_other")) or val
        else:
            val = _txt(raw)
        if val:
            bits.append(template.format(val))
    return f"{label}: đang dùng" + (f" — {', '.join(bits)}" if bits else "")


# Mục 5.2 của phiếu: các biến cố trong 6 tháng TRƯỚC khi bắt đầu rụng tóc.
# Chỉ đưa vào đoạn văn tóm tắt (không tự tick vào ô nào) vì nhiều câu hỏi gộp
# nhiều ý (VD "Tăng/giảm cân > 4,5kg" không phân biệt tăng hay giảm).
_PRE_EVENTS = [
    ("pre_highFever", "sốt cao"),
    ("pre_prolongedHighFever", "sốt cao kéo dài"),
    ("pre_weightChange", "tăng/giảm cân > 4,5 kg"),
    ("pre_severeStress", "căng thẳng tâm lý nghiêm trọng"),
    ("pre_startStopOCP", "bắt đầu/ngừng thuốc tránh thai"),
    ("pre_startStopHormoneTherapy", "bắt đầu/ngừng liệu pháp hormone"),
    ("pre_startStopBetaBlocker", "bắt đầu/ngừng thuốc chẹn beta"),
    ("pre_diabetesOrInsulinResistance", "đái tháo đường / kháng insulin"),
    ("pre_endStageChronicKidneyDisease", "bệnh thận mạn giai đoạn cuối"),
    ("pre_chronicLiverDisease", "bệnh gan mạn tính"),
    ("pre_acuteSystemicIllness", "bệnh lý hệ thống cấp tính"),
    ("pre_lowProteinDiet", "chế độ ăn ít đạm"),
    ("pre_severeInfection", "nhiễm trùng nặng"),
    ("pre_chronicFlare", "đợt bùng phát bệnh mạn tính"),
    ("pre_majorSurgery", "phẫu thuật lớn / gây mê toàn thân"),
    ("pre_systemicLupusErythematosus", "lupus ban đỏ hệ thống"),
    ("pre_syphilis", "giang mai"),
    ("pre_polycysticOvarySyndrome", "buồng trứng đa nang"),
    ("pre_childbirth", "sinh con"),
    ("pre_ironDeficiency", "thiếu sắt trong máu"),
    ("pre_thyroidDisease", "bệnh tuyến giáp"),
]

_HAIR_HABITS = [
    ("habit_tightHairstyle", "buộc tóc đuôi ngựa/tết/xoăn/nối tóc/tóc giả"),
    ("habit_heat", "dùng nhiệt trực tiếp lên tóc (sấy, ép/uốn nóng)"),
    ("habit_chemicals", "dùng hoá chất cho tóc (nhuộm, duỗi, ép)"),
]

# Các mục thể hiện bệnh nhân ĐÃ THỰC SỰ điều trị (khác với "đã đi khám bác sĩ khác")
_TREATED_KEYS = [
    "topicalMinoxidil", "oralMinoxidil", "other_scalpInjection", "other_biotin",
    "other_spiro", "other_dutasteride", "other_ledcap", "other_ketoconazole",
    "other_antibiotics", "topicalHerbalSupp", "allTreatmentsTried",
]


def build_history_text(a: dict) -> str:
    """Gộp toàn bộ mục 6 (Lịch sử điều trị) của phiếu thành đoạn văn tiếng Việt,
    để bác sĩ đọc nhanh thay vì mở từng ô."""
    lines = []
    seen = _yn(a.get("seenOtherDoctor"))
    if seen:
        lines.append(f"Đã khám bác sĩ khác về rụng tóc: {seen}")

    for line in (_minoxidil_line(a, "topicalMinoxidil", "Minoxidil bôi/xịt"),
                 _minoxidil_line(a, "oralMinoxidil", "Minoxidil uống")):
        if line:
            lines.append(line)

    for key, label, detail_key in _OTHER_TREATMENTS:
        if not _is_yes(a, key):
            continue
        detail = _txt(a.get(detail_key)) if detail_key else ""
        if key == "other_scalpInjection":
            types = [name for k, name in _INJECTION_TYPES if a.get(k) is True]
            extra = _txt(a.get("other_injection_other_text"))
            if extra:
                types.append(extra)
            detail = ", ".join(types) + (f", ~{detail} lần" if detail else "")
        lines.append(f"{label}: có" + (f" ({detail})" if detail else ""))

    shampoo_set = _txt(a.get("other_shampooSet_name"))
    if shampoo_set:
        lines.append(f"Bộ dầu gội/dầu xả dành cho rụng tóc: {shampoo_set}")

    if _is_yes(a, "allTreatmentsTried"):
        lines.append("Các phương pháp đã từng thử: " + (_txt(a.get("allTreatmentsTried_list")) or "chưa ghi rõ"))

    if _is_yes(a, "sidefx"):
        fx = [name for k, name in _SIDE_EFFECTS if a.get(k) is True]
        if a.get("sidefx_other") is True and _txt(a.get("sidefx_other_text")):
            fx.append(_txt(a.get("sidefx_other_text")))
        lines.append("Tác dụng phụ đã gặp: " + (", ".join(fx) if fx else "có (chưa ghi rõ)"))

    if _is_yes(a, "moreEffectiveMethod"):
        lines.append("Phương pháp thấy hiệu quả hơn: " + (_txt(a.get("moreEffectiveMethod_list")) or "chưa ghi rõ"))

    tests = _yn(a.get("testsDone"))
    if tests:
        lines.append(f"Đã làm xét nghiệm trước đó: {tests}")
    biopsy = _yn(a.get("biopsyDone"))
    if biopsy:
        lines.append(f"Đã sinh thiết da đầu trước đó: {biopsy}")

    events = [label for key, label in _PRE_EVENTS if _is_yes(a, key)]
    if events:
        lines.append("Trong 6 tháng trước khi rụng tóc: " + ", ".join(events))

    habits = [label for key, label in _HAIR_HABITS if _is_yes(a, key)]
    if habits:
        lines.append("Thói quen chăm sóc tóc: " + ", ".join(habits))
    wash = _txt(a.get("washPerWeek"))
    shampoo = _txt(a.get("shampoo"))
    if wash or shampoo:
        lines.append(f"Gội đầu {wash or '?'} lần/tuần" + (f", dầu gội: {shampoo}" if shampoo else ""))

    locs = [label for key, label in _LOC_LABELS.items() if a.get(key) is True]
    if locs:
        lines.append("Vị trí rụng tóc bệnh nhân tự khai: " + ", ".join(locs) + " (bác sĩ tự khám và đánh giá lại)")

    cause = _txt(a.get("cause"))
    if cause:
        lines.append(f"Bệnh nhân nghĩ nguyên nhân rụng tóc là: {cause}")
    goal = _txt(a.get("treatmentGoal"))
    if goal:
        lines.append(f"Mong muốn/mục tiêu điều trị: {goal}")

    if lines:
        lines.insert(0, "[Bệnh nhân tự khai trong phiếu khảo sát]")
    return "\n".join(lines)


def _current_meds_text(a: dict) -> Optional[str]:
    """Ô 'Thuốc đang sử dụng' (bệnh án AA) — mục 5.4 của phiếu: thuốc có thể gây rụng tóc."""
    status = _yn(a.get("currentMeds"))
    if status is None:
        return None
    if status != "Có":
        return "Không"
    return _txt(a.get("currentMeds_list")) or "Có (chưa ghi rõ tên thuốc)"


# ---------- 4. Bảng ánh xạ: câu hỏi trong phiếu -> trường trong bệnh án ----------
# Trả về dict PHẲNG dạng {"đường.dẫn.trường": giá_trị} để frontend áp bằng setPath()
# — cùng cơ chế với mọi thao tác sửa form khác, không cần merge sâu riêng.

# CỐ Ý KHÔNG ánh xạ câu "Vị trí rụng tóc" của phiếu sang bất kỳ ô nào của bệnh án
# (viTriRungToc / viTriTonThuong / rungLongMayMi) — bác sĩ tự khám và đánh giá vị trí.
# Câu hỏi trong phiếu lại gộp nhiều vùng ("Vùng đỉnh / vùng trước đầu", "Hai bên của đầu")
# nên tự tick sẽ tạo dữ liệu nghiên cứu không chính xác.
# Câu trả lời của bệnh nhân vẫn xem được đầy đủ ở tab "Phiếu khảo sát" và trong
# đoạn tóm tắt bệnh sử (dạng chữ, ghi rõ là bệnh nhân tự khai).
_LOC_LABELS = {
    "loc_topFront": "vùng đỉnh / vùng trước đầu",
    "loc_sides": "hai bên của đầu",
    "loc_occipital": "sau gáy",
    "loc_armpit": "nách",
    "loc_groin": "bẹn",
    "loc_eyebrow": "lông mày",
    "loc_eyelash": "lông mi",
    "loc_beard": "vùng râu",
}
_NAIL_AA = {
    "nail_pitting": "Chấm lõm",
    "nail_whiteSpots": "Vệt trắng móng",
    "nail_longitudinal": "Khía dọc móng",
    "nail_ridges": "Móng thô ráp",
}


def _map_common(a: dict, result: dict) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    ngay_kham = _txt(result.get("treatmentDate"))[:10]
    if ngay_kham:
        out["ngayKham"] = ngay_kham
    for field, key in (("chieuCao", "heightCm"), ("canNang", "weightKg")):
        v = _num(a.get(key))
        if v is not None:
            out[field] = v
    history = build_history_text(a)
    if history:
        out["benhSuTruoc"] = history
    return out


def _onset_text(a: dict) -> Optional[str]:
    months = _num(a.get("hairLossOnsetText"))
    return f"{months} tháng" if months is not None else None


def _family_hairloss(a: dict):
    """Tiền sử gia đình rụng tóc (dùng cho AGA và Rụng tóc không sẹo)."""
    male, female = _yn(a.get("fam_maleHairLoss")), _yn(a.get("fam_femaleHairLoss"))
    if male is None and female is None:
        return None, None
    status = "Có" if "Có" in (male, female) else "Không"
    details = []
    if male == "Có":
        rel = _txt(a.get("fam_male_relation"))
        details.append(f"Nam giới: {rel}" if rel else "Có nam giới trong gia đình bị rụng tóc")
    if female == "Có":
        rel = _txt(a.get("fam_female_relation"))
        details.append(f"Nữ giới: {rel}" if rel else "Có nữ giới trong gia đình bị rụng tóc")
    return status, "; ".join(details)


def map_aa(a: dict, result: dict) -> Dict[str, Any]:
    out = _map_common(a, result)

    onset = _onset_text(a)
    if onset:
        out["thoiGianMacBenh"] = onset

    nails = [v for k, v in _NAIL_AA.items() if _is_yes(a, k)]
    if nails:
        out["tonThuongMong"] = nails
    elif any(_yn(a.get(k)) == "Không" for k in _NAIL_AA):
        out["tonThuongMong"] = ["Không"]

    co_nang = []
    if _is_yes(a, "scalp_itch"):
        co_nang.append("Ngứa")
    if _is_yes(a, "scalp_pain"):
        co_nang.append("Đau rát/bỏng rát")
    if _is_yes(a, "scalp_crawling"):
        co_nang.append("Châm chích/kiến bò")
    if co_nang:
        out["trieuChungCoNang"] = co_nang
    elif any(_yn(a.get(k)) == "Không" for k in ("scalp_itch", "scalp_pain", "scalp_crawling")):
        out["trieuChungCoNang"] = ["Không triệu chứng"]

    # "Đã đi khám bác sĩ khác" KHÔNG đồng nghĩa với "đã điều trị" — chỉ tính khi
    # bệnh nhân khai có dùng ít nhất 1 phương pháp điều trị cụ thể.
    chi_tiet = []
    if _any_yes(a, ["topicalMinoxidil", "oralMinoxidil"]):
        chi_tiet.append("Minoxidil")
    if _is_yes(a, "other_scalpInjection") and a.get("other_injection_steroid") is True:
        chi_tiet.append("Tiêm nội tổn thương")
    if _any_yes(a, _TREATED_KEYS):
        out["dieuTriTruocDoStatus"] = "Có"
        if chi_tiet:
            out["dieuTriTruocChiTiet"] = chi_tiet
    elif any(_yn(a.get(k)) is not None for k in _TREATED_KEYS):
        out["dieuTriTruocDoStatus"] = "Không"
        out["dieuTriTruocChiTiet"] = ["Không điều trị"]

    meds = _current_meds_text(a)
    if meds:
        out["thuocDangDung"] = meds

    for row, keys, detail_key in (
        ("Dị ứng thuốc", ("drugAllergy",), "drugAllergy_list"),
        ("Vảy nến", ("mh_psoriasis",), None),
    ):
        status = _yn_or_none(a, *keys)
        if status:
            out[f"tienSuBanThan.{row}.status"] = status
            detail = _txt(a.get(detail_key)) if detail_key else ""
            if detail:
                out[f"tienSuBanThan.{row}.detail"] = detail
    return out


def map_aga(a: dict, result: dict) -> Dict[str, Any]:
    out = _map_common(a, result)

    onset = _onset_text(a)
    if onset:
        out["thoiGianKhoiPhat"] = onset

    dau_hieu = []
    if _is_yes(a, "mh_cysticAcne"):
        dau_hieu.append("Mụn")
    if _is_yes(a, "scalp_oily"):
        dau_hieu.append("Da dầu")
    if _any_yes(a, ["mh_bodyHair", "mh_facialHair"]):
        dau_hieu.append("Rậm lông")
    if _txt(a.get("period_irregular_desc")) or _yn(a.get("period_regular")) == "Không":
        dau_hieu.append("Kinh nguyệt bất thường (nữ)")
    if dau_hieu:
        out["dauHieuCuongAndrogen"] = dau_hieu

    status, detail = _family_hairloss(a)
    if status:
        out["tienSuGiaDinh.status"] = status
        if detail:
            out["tienSuGiaDinh.detail"] = detail

    for row, keys in (
        ("Buồng trứng đa nang (nữ)", ("mh_pcos", "pre_polycysticOvarySyndrome")),
        ("Rậm lông (nữ)", ("mh_bodyHair", "mh_facialHair")),
        ("Bệnh lý tuyến giáp", ("pre_thyroidDisease",)),
        ("Trứng cá nặng", ("mh_cysticAcne",)),
        ("Đái tháo đường", ("pre_diabetesOrInsulinResistance",)),
    ):
        s = _yn_or_none(a, *keys)
        if s:
            out[f"tienSuBanThan.{row}.status"] = s
    return out


def map_nonscar(a: dict, result: dict) -> Dict[str, Any]:
    out = _map_common(a, result)

    onset = _onset_text(a)
    if onset:
        out["thoiGianKhoiPhat"] = onset

    co_nang = []
    if _is_yes(a, "scalp_pain"):
        co_nang.append("Đau chân tóc")
    if _any_yes(a, ["scalp_itch", "scalp_crawling"]):
        co_nang.append("Ngứa/dị cảm da đầu")
    if co_nang:
        out["coNangDaDau"] = co_nang
    elif any(_yn(a.get(k)) == "Không" for k in ("scalp_pain", "scalp_itch", "scalp_crawling")):
        out["coNangDaDau"] = ["Không"]

    an_kieng = []
    if _is_yes(a, "vegetarianOrSpecialDiet"):
        an_kieng.append("Ăn chay")
    if _any_yes(a, ["weightLossDiet", "currentlyLowProtein", "otherDiet"]):
        an_kieng.append("Ăn kiêng nghiêm ngặt")
    # Cố ý KHÔNG tự tick "Sụt cân nhanh >4,5 kg": phiếu hỏi gộp "Tăng/giảm cân > 4,5kg"
    # nên không phân biệt được tăng hay giảm — thông tin này nằm trong đoạn tóm tắt bệnh sử.
    if an_kieng:
        out["anKiengGiamCan"] = an_kieng
    elif all(_yn(a.get(k)) == "Không" for k in
             ("vegetarianOrSpecialDiet", "weightLossDiet", "currentlyLowProtein", "otherDiet")):
        out["anKiengGiamCan"] = ["Không"]

    nail_status = _yn_or_none(a, *_NAIL_AA.keys())
    if nail_status:
        out["mongBatThuong"] = "Có" if nail_status == "Có" else "Không"

    status, detail = _family_hairloss(a)
    if status:
        out["tienSuGiaDinh.status"] = status
        if detail:
            out["tienSuGiaDinh.detail"] = detail

    for row, keys in (
        ("Mụn trứng cá", ("mh_cysticAcne",)),
        ("Mang thai hoặc trong 6 tháng sau sinh (nữ)", ("pre_childbirth",)),
        ("Bệnh lý tuyến giáp", ("pre_thyroidDisease",)),
        ("Buồng trứng đa nang (nữ)", ("mh_pcos", "pre_polycysticOvarySyndrome")),
        ("Đái tháo đường/kháng insulin/lupus ban đỏ hệ thống",
         ("pre_diabetesOrInsulinResistance", "pre_systemicLupusErythematosus")),
        ("Bệnh thận mạn giai đoạn cuối/gan mạn tính/giang mai",
         ("pre_endStageChronicKidneyDisease", "pre_chronicLiverDisease", "pre_syphilis")),
        ("Bệnh lý hệ thống cấp tính/sốt cao kéo dài/phẫu thuật lớn",
         ("pre_acuteSystemicIllness", "pre_prolongedHighFever", "pre_highFever", "pre_majorSurgery")),
        ("Sử dụng thuốc gây rụng tóc (Lithium, Valproate, Retinoids liều cao, Warfarin, Betablockers)",
         ("currentMeds",)),
        ("Bổ sung vi chất (Sắt, Kẽm, Vitamin D, B12) trong 1 tháng gần nhất",
         ("other_iron", "other_vitamins")),
    ):
        s = _yn_or_none(a, *keys)
        if s:
            out[f"tienSuBanThan.{row}.status"] = s
    detail = _txt(a.get("currentMeds_list"))
    if detail:
        out["tienSuBanThan.Sử dụng thuốc gây rụng tóc (Lithium, Valproate, Retinoids liều cao, Warfarin, Betablockers).detail"] = detail
    return out


# Thêm bệnh thứ 4: chỉ cần thêm 1 dòng vào đây (khớp key với DISEASE_CONFIGS trong main.py).
SURVEY_MAPPERS = {
    "aa": map_aa,
    "aga": map_aga,
    "nonscar": map_nonscar,
}


def map_survey(answers: Optional[dict], result: dict, benh: str) -> Dict[str, Any]:
    if not answers:
        return {}
    mapper = SURVEY_MAPPERS.get((benh or "").lower())
    if not mapper:
        return {}
    return mapper(answers, result)


# ---------- 5. Thông tin hành chính lấy từ phiếu ----------

def _gioi_tinh(a: Optional[dict]) -> Optional[str]:
    g = _txt((a or {}).get("gender")).lower()
    if g in ("female", "nu", "nữ", "f"):
        return "Nữ"
    if g in ("male", "nam", "m"):
        return "Nam"
    return None


def build_response(ma_bn: str, benh: str) -> Dict[str, Any]:
    fetched = fetch_survey(ma_bn)
    if not fetched["found"]:
        return {"found": False, "loi": fetched["loi"], "co_khao_sat": False,
                "khao_sat": None, "mapped": {}, "dlqi_tong": None}

    result = fetched["result"]
    answers = result.get("answers") or None
    ngay_sinh = _txt(result.get("patientBirthDay"))[:10]
    mapped = map_survey(answers, result, benh)
    return {
        "found": True,
        "loi": None,
        "ma_bn": result.get("patientCode"),
        "ho_ten": _txt(result.get("patientName")) or None,
        "ngay_sinh": ngay_sinh or None,
        "nam_sinh": int(ngay_sinh[:4]) if ngay_sinh[:4].isdigit() else None,
        "gioi_tinh": _gioi_tinh(answers),
        "ngay_kham": _txt(result.get("treatmentDate"))[:10] or None,
        "ma_luot_kham": result.get("treatmentCode"),
        "co_khao_sat": bool(answers),
        "khao_sat": answers,
        "dlqi_tong": dlqi_total(answers) if answers else None,
        "mapped": mapped,
        "so_truong_dien": len(mapped),
    }
