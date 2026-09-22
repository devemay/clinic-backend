"""Đọc phiếu khảo sát điện tử do BỆNH NHÂN TỰ ĐIỀN trên hệ thống của bệnh viện
(api.dalieu.vn) và chuyển thành các trường của bệnh án nghiên cứu.

Vì sao gọi API từ backend chứ không gọi thẳng từ trình duyệt:
  1. Trình duyệt sẽ bị CORS chặn (API bệnh viện không cấp quyền cho tên miền GitHub Pages).
  2. Chỉ cần sửa 1 chỗ khi API bệnh viện đổi địa chỉ/tham số.
  3. Dùng lại được logic dò mã bệnh nhân bị gõ tắt (bỏ số 0 ở đầu).

Dùng urllib của thư viện chuẩn Python — KHÔNG thêm thư viện mới vào requirements.txt,
tránh phải cài thêm gói trên Render.
"""

import io
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import config
import tram


# ---------- 1. Gọi API bệnh viện ----------

def loi_tu_benh_vien(ma_http: int, than) -> str:
    """Đổi lỗi HTTP của hệ thống bệnh viện thành câu dễ hiểu.

    api.dalieu.vn dùng khung ASP.NET Boilerplate: lỗi nghiệp vụ (VD không tìm thấy bệnh nhân,
    bệnh nhân chưa có lượt khám ở phòng này...) được trả về dạng HTTP 500 kèm JSON
    {"success": false, "error": {"message": "...", "details": "..."}}. Chỉ báo "lỗi 500" thì bác sĩ
    không biết vì sao — ở đây lấy nguyên câu thông báo của bệnh viện ra hiển thị."""
    if isinstance(than, bytes):
        than = than.decode("utf-8", errors="replace")
    loi = None
    try:
        body = json.loads(than or "", parse_constant=lambda _c: None)
        loi = body.get("error") if isinstance(body, dict) else None
    except ValueError:
        pass
    if isinstance(loi, dict):
        thong_bao = " ".join(str(loi.get("message") or "").split())
        chi_tiet = " ".join(str(loi.get("details") or "").split())
        if chi_tiet and chi_tiet != thong_bao:
            thong_bao = f"{thong_bao} — {chi_tiet}" if thong_bao else chi_tiet
        if thong_bao:
            return f"Hệ thống bệnh viện báo: “{thong_bao[:300]}” (mã lỗi {ma_http})"
    return (f"Hệ thống bệnh viện trả lỗi {ma_http}. Thử mở phiếu bằng trình duyệt (nút “Dán phiếu thủ công” "
            "→ “Mở phiếu khảo sát”) để xem bệnh viện báo gì.")


def url_phieu(ma_bn: str) -> str:
    """Đường dẫn phiếu khảo sát trên hệ thống bệnh viện — dùng cho nút "Mở phiếu" (dán tay):
    trình duyệt của bác sĩ ở Việt Nam mở được dù máy chủ Render bị chặn."""
    ma = (ma_bn or "").strip()
    if ma.isdigit() and len(ma) < 10:
        ma = ma.zfill(10)
    params = urllib.parse.urlencode({
        "RoomId": config.SURVEY_ROOM_ID,
        "CheckValue": ma,
        "FindType": "3",  # 3 = tra theo mã bệnh nhân
    })
    return f"{config.SURVEY_API_BASE}/api/services/app/ClinicSurveyAnswer/CheckSurvey?{params}"


def _call_api(ma_bn: str, han_giay: Optional[float] = None) -> Optional[dict]:
    params = urllib.parse.urlencode({
        "RoomId": config.SURVEY_ROOM_ID,
        "CheckValue": ma_bn,
        "FindType": "3",  # 3 = tra theo mã bệnh nhân
    })
    url = f"{config.SURVEY_API_BASE}/api/services/app/ClinicSurveyAnswer/CheckSurvey?{params}"
    if tram.bat():
        # Nhờ máy trạm ở phòng khám đọc hộ (bệnh viện chặn máy chủ ở nước ngoài)
        kq = tram.hoi(ma_bn, han_giay if han_giay is not None else config.SURVEY_DEADLINE)
        if kq["ma_http"] != 200:
            # giữ nguyên nội dung lỗi bệnh viện gửi kèm (thường có câu thông báo) để hiện cho bác sĩ
            raise urllib.error.HTTPError(url, kq["ma_http"] or 502, "bệnh viện báo lỗi", None,
                                         io.BytesIO((kq["noi_dung"] or "").encode("utf-8")))
        raw = kq["noi_dung"]
    else:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "benh-an-nghien-cuu/1.0"})
        with urllib.request.urlopen(req, timeout=config.SURVEY_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    # parse_constant: JSON của Python MẶC ĐỊNH chấp nhận NaN/Infinity, nhưng FastAPI thì
    # KHÔNG đóng gói được 2 giá trị này -> đổi thành None ngay từ lúc đọc.
    body = json.loads(raw, parse_constant=lambda _c: None)
    if not body.get("success"):
        return None
    return json_safe(body.get("result")) or None


def json_safe(value):
    """Dọn dữ liệu từ hệ thống bệnh viện cho an toàn khi đóng gói JSON trả về trình duyệt.

    Hai thứ làm FastAPI ném lỗi khi đóng gói — và lỗi lúc đóng gói thì Starlette trả 500
    KHÔNG kèm header CORS, nên trình duyệt chỉ báo "Không kết nối được tới backend",
    trông y hệt như server sập:
      1. Số NaN / Infinity  -> ValueError: Out of range float values are not JSON compliant
      2. Chuỗi chứa ký tự Unicode hỏng (lone surrogate) -> UnicodeEncodeError: surrogates not allowed
    """
    if isinstance(value, float):
        return None if (value != value or value in (float("inf"), float("-inf"))) else value
    if isinstance(value, str):
        return value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, dict):
        return {json_safe(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


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


def _fetch_blocking(ma_bn: str, deadline: float) -> Dict[str, Any]:
    last_error = None
    for candidate in _ma_bn_candidates(ma_bn):
        if time.monotonic() >= deadline:
            break
        try:
            result = _call_api(candidate, deadline - time.monotonic())
        except tram.TramKhongSan as e:
            return {"found": False, "result": None, "loi": str(e)}
        except urllib.error.HTTPError as e:
            try:
                than = e.read()
            except Exception:
                than = b""
            last_error = loi_tu_benh_vien(e.code, than)
            continue
        except urllib.error.URLError as e:
            return {"found": False, "result": None, "loi": f"Không kết nối được hệ thống bệnh viện ({e.reason})"}
        except Exception as e:  # timeout đọc dữ liệu, JSON hỏng...
            return {"found": False, "result": None, "loi": f"Không đọc được dữ liệu từ hệ thống bệnh viện ({e})"}
        if result:
            return {"found": True, "result": result, "loi": None}
    return {"found": False, "result": None, "loi": last_error or "Hệ thống bệnh viện không có hồ sơ với mã này"}


def fetch_survey(ma_bn: str) -> Dict[str, Any]:
    """Trả về {'found': bool, 'result': dict|None, 'loi': str|None}.

    KHÔNG bao giờ ném lỗi và KHÔNG bao giờ chạy quá `SURVEY_DEADLINE` giây.

    Vì sao cần hạn chót cứng chứ không chỉ dựa vào timeout của urllib: tham số `timeout`
    của urlopen chỉ tính cho từng thao tác đọc/ghi socket, KHÔNG tính bước phân giải tên
    miền (DNS). Nếu máy chủ không phân giải hoặc không ra được api.dalieu.vn, lời gọi sẽ
    treo vô hạn -> Render cắt kết nối bằng lỗi 502 KHÔNG kèm header CORS -> trình duyệt
    báo "Không kết nối được tới backend", tưởng nhầm là server sập.
    Ở đây chạy lời gọi trong một luồng phụ và bỏ luồng đó nếu quá hạn, để endpoint luôn
    trả về HTTP 200 kèm thông báo tiếng Việt dễ hiểu.
    """
    box: Dict[str, Any] = {}
    deadline = time.monotonic() + config.SURVEY_DEADLINE

    def worker():
        try:
            box["res"] = _fetch_blocking(ma_bn, deadline)
        except Exception as e:
            box["res"] = {"found": False, "result": None, "loi": f"Lỗi khi gọi hệ thống bệnh viện ({e})"}

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(config.SURVEY_DEADLINE)
    if "res" not in box:
        if tram.bat():
            return {"found": False, "result": None, "loi": (
                f"Máy trạm ở phòng khám không trả lời sau {int(config.SURVEY_DEADLINE)} giây. "
                "Thử lại, hoặc bấm “Dán phiếu thủ công”.")}
        return {"found": False, "result": None, "loi": (
            f"Hệ thống bệnh viện không phản hồi sau {int(config.SURVEY_DEADLINE)} giây. "
            "Có thể máy chủ đặt ở nước ngoài (Render) không gọi ra được api.dalieu.vn. "
            "Mở /survey/_ping trên trình duyệt để kiểm tra, hoặc nhập tay và thử lại sau."
        )}
    return box["res"]


def ping() -> Dict[str, Any]:
    """Kiểm tra máy chủ có gọi ra được hệ thống bệnh viện không.
    Dùng mã bệnh nhân không tồn tại nên KHÔNG trả về bất kỳ thông tin bệnh nhân nào."""
    out: Dict[str, Any] = {"dia_chi": config.SURVEY_API_BASE, "phong": config.SURVEY_ROOM_ID,
                           "gioi_han_giay": config.SURVEY_DEADLINE,
                           "che_do": "qua máy trạm ở phòng khám" if tram.bat() else "gọi thẳng từ máy chủ"}
    if tram.bat():
        out["may_tram"] = tram.trang_thai()
    box: Dict[str, Any] = {}
    t0 = time.monotonic()

    def worker():
        try:
            _call_api("0000000000", config.SURVEY_DEADLINE - 0.5)
            box["ket_qua"] = {"goi_duoc": True, "loi": None}
        except urllib.error.HTTPError as e:
            box["ket_qua"] = {"goi_duoc": True, "loi": f"HTTP {e.code} (vẫn kết nối được)"}
        except Exception as e:
            box["ket_qua"] = {"goi_duoc": False, "loi": f"{type(e).__name__}: {e}"}

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(config.SURVEY_DEADLINE)
    out["mili_giay"] = int((time.monotonic() - t0) * 1000)
    if "ket_qua" not in box:
        out.update({"goi_duoc": False,
                    "loi": f"Treo quá {int(config.SURVEY_DEADLINE)} giây — máy chủ này không ra được api.dalieu.vn"})
    else:
        out.update(box["ket_qua"])
    out["ket_luan"] = ("Máy chủ gọi được hệ thống bệnh viện — nút Đồng bộ sẽ chạy."
                       if out["goi_duoc"] else
                       "Máy chủ KHÔNG gọi được hệ thống bệnh viện — nút Đồng bộ sẽ báo lỗi.")
    return out


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
    if not isinstance(a, dict):
        return None
    vals = [_num(a.get(k)) for k in DLQI_KEYS]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return int(sum(vals))


# Thang MGH-HPS (Massachusetts General Hospital Hairpulling Scale) — phần 2 của phiếu tật nhổ tóc.
# Thứ tự khớp đúng 7 câu q1..q7 trong bảng MGH-HPS của bệnh án TTM.
MGH_KEYS = [
    "mgh_urgeFrequency",   # q1 tần suất thôi thúc
    "mgh_urgeIntensity",   # q2 cường độ thôi thúc
    "mgh_control",         # q3 khả năng kiểm soát
    "mgh_distress",        # q4 đau khổ
    "mgh_timeSpent",       # q5 thời gian nhổ tóc
    "mgh_socialImpact",    # q6 ảnh hưởng xã hội
    "mgh_workImpact",      # q7 ảnh hưởng công việc/học tập
]


def mgh_total(a: dict) -> Optional[int]:
    """Tổng điểm MGH-HPS 0-28. Chỉ tính khi bệnh nhân trả lời ĐỦ 7 câu (giống bệnh án TTM),
    thiếu câu nào thì trả None để không hiện một con số sai."""
    if not isinstance(a, dict):
        return None
    vals = [_num(a.get(k)) for k in MGH_KEYS]
    if any(v is None for v in vals):
        return None
    return int(sum(vals))


def muc_do_mgh(t: Optional[int]) -> Optional[str]:
    if t is None:
        return None
    return "Nhẹ" if t <= 7 else ("Trung bình" if t <= 15 else "Nặng")


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
    ("habit_tightHairstyle", "kiểu tóc tạo áp lực (buộc/tết/búi chặt)"),
    ("habit_heat", "dùng nhiệt trực tiếp lên tóc (sấy, ép/uốn nóng)"),
    ("habit_chemicals", "dùng hoá chất cho tóc (nhuộm, duỗi, ép)"),
    ("habit_chemicalRelaxer", "hoá chất làm thẳng/duỗi tóc"),
    ("habit_wigExtensions", "tóc giả / nối tóc"),
    ("habit_dyeBleach", "thuốc nhuộm / thuốc tẩy tóc"),
    ("habit_scalpOil", "thoa dầu/mỡ lên da đầu thường xuyên"),
]

# Phần "bệnh nhân tự nhận thấy" (mục 2 mở rộng của phiếu — chủ yếu phục vụ rụng tóc sẹo).
# Đây là bệnh nhân TỰ KHAI, không phải kết quả khám -> chỉ ghi vào đoạn tóm tắt,
# không tự tick vào các ô khám thực thể.
_FIRST_SITE = [
    ("obs_firstSite_vertex", "đỉnh"),
    ("obs_firstSite_frontal", "trán"),
    ("obs_firstSite_temporal", "thái dương"),
    ("obs_firstSite_occipital", "chẩm/gáy"),
    ("obs_firstSite_diffuse", "lan toả"),
    ("obs_firstSite_other", "khác"),
]
_PRIOR_AT_SITE = [
    ("obs_prior_folliculitis", "Viêm nang lông", "viêm nang lông"),
    ("obs_prior_infection", "Nhiễm trùng", "nhiễm trùng"),
    ("obs_prior_autoimmune", "Tự miễn", "bệnh tự miễn"),
    ("obs_prior_radiationBurnTrauma", "Sau tia xạ / bỏng / chấn thương", "tia xạ/bỏng/chấn thương"),
    ("obs_prior_drug", "Thuốc", "dùng thuốc"),
    ("obs_prior_unknown", "Không rõ", "không rõ"),
    ("obs_prior_other", "Khác", "khác"),
]
_LESION_PROGRESS = {"spreading": "Lan rộng", "stable": "Dừng lại"}
_SELF_SEEN = [
    ("obs_shinyScalp", "da đầu vùng rụng nhẵn bóng"),
    ("obs_tuftedHairs", "nhiều sợi tóc mọc chung 1 lỗ (búi tóc)"),
    ("obs_pustulesDischarge", "mụn mủ / chảy dịch vùng rụng"),
    ("obs_eyebrowThinning", "lông mày thưa/rụng"),
    ("obs_eyelashThinning", "lông mi thưa/rụng"),
    ("obs_frontalRecession", "đường chân tóc trán lùi dần"),
]

# Phiếu tật nhổ tóc (ttmAnswers) — bảng đổi đáp án sang đúng lựa chọn trong bệnh án TTM
TTM_WHO = {"self": "Tự nhổ", "other": "Người khác nhổ hộ", "both": "Cả hai"}
TTM_FREQ = {"daily": "Hàng ngày", "fewPerWeek": "Vài lần/tuần", "occasional": "Thỉnh thoảng"}
TTM_ONSET = {"stress": "Sau stress", "afterStress": "Sau stress", "unknown": "Không rõ lý do",
             "habit": "Thói quen dần dần", "gradual": "Thói quen dần dần", "other": "Khác"}
TTM_TIME = [
    ("ttm_time_morning", "Sáng"), ("ttm_time_noon", "Trưa"), ("ttm_time_evening", "Tối"),
    ("ttm_time_night", "Đêm"), ("ttm_time_noFixed", "Không theo giờ cố định"),
]
TTM_SITUATION = [
    ("ttm_sit_screen", "Xem TV / điện thoại"), ("ttm_sit_reading", "Đọc sách / học bài"),
    ("ttm_sit_bed", "Nằm trên giường"), ("ttm_sit_stress", "Khi căng thẳng / lo âu"),
    ("ttm_sit_bored", "Khi buồn chán"), ("ttm_sit_alone", "Khi một mình"),
    ("ttm_sit_none", "Không theo tình huống cụ thể"), ("ttm_sit_other", "Khác"),
]
TTM_AWARENESS = [  # đúng tên dòng bảng "Nhận thức và kiểu nhổ tóc" (NHAN_THUC_TTM)
    ("ttm_selfAware", "BN biết / nhận thức / thừa nhận hành vi nhổ tóc"),
    ("ttm_familyWitnessed", "Gia đình quan sát thấy hành vi nhổ tóc"),
    ("ttm_automatic", "Nhổ tóc VÔ THỨC khi làm việc khác (đọc sách, xem TV...)"),
    ("ttm_focused", "Nhổ tóc CÓ CHỦ ĐÍCH (thôi thúc, cưỡng bức)"),
    ("ttm_reliefAfter", "Cảm giác thoải mái / thoả mãn SAU khi nhổ tóc"),
    ("ttm_tensionBefore", "Căng thẳng / bồn chồn TRƯỚC khi nhổ (tension buildup)"),
    ("ttm_guiltAfter", "Cảm giác tội lỗi / xấu hổ sau nhổ tóc"),
]
TTM_AFTER = [  # HANH_VI_SAU_NHO_TTM
    ("ttm_inspectRoot", "Kiểm tra chân tóc (soi, ngửi, sờ)"),
    ("ttm_biteHair", "Cắn / nhai thân tóc"),
    ("ttm_eatHair", "Ăn tóc (trichophagia)"),
]
TTM_BFRB = [  # BFRB_TTM
    ("bfrb_nailBiting", "Cắn móng tay / da quanh móng (onychophagia)"),
    ("bfrb_eyebrowPulling", "Nhổ lông mày"),
    ("bfrb_eyelashPulling", "Nhổ lông mi"),
    ("bfrb_bodyHairPulling", "Nhổ lông vùng kín / nách / chân"),
    ("bfrb_skinPicking", "Gãi / cào da"),
    ("bfrb_lipCheekBiting", "Cắn môi / má trong miệng"),
]
# 3 câu gần với tiêu chuẩn DSM-5 — CHỈ ghi vào tóm tắt, không tự chấm "Đạt" (bác sĩ đánh giá).
TTM_DSM_HINTS = [
    ("ttm_visibleLoss", "nhổ tóc tới mức thấy rõ vùng rụng"),
    ("ttm_failedToStop", "đã cố ngừng/giảm nhổ tóc nhưng không được"),
    ("ttm_distressImpairment", "nhổ tóc gây khó chịu/ảnh hưởng sinh hoạt, công việc"),
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

    if _is_yes(a, "habit_noSpecialCare") and not habits:
        lines.append("Thói quen chăm sóc tóc: không chăm sóc gì đặc biệt")

    age = _num(a.get("hairLossOnsetAge"))
    if age is not None:
        lines.append(f"Tuổi bắt đầu rụng tóc: {age}")
    sites = [label for key, label in _FIRST_SITE if a.get(key) is True]
    other_site = _txt(a.get("obs_firstSite_other_text"))
    if other_site:
        sites = [x for x in sites if x != "khác"] + [other_site]
    if sites:
        lines.append("Vị trí rụng đầu tiên (bệnh nhân tự khai): " + ", ".join(sites))
    progress = _LESION_PROGRESS.get(_txt(a.get("obs_lesionProgress")))
    if progress:
        lines.append(f"Vùng rụng tóc từ lúc khởi phát tới nay: {progress.lower()}")
    prior = [txt for key, _o, txt in _PRIOR_AT_SITE if a.get(key) is True]
    prior_other = _txt(a.get("obs_prior_other_text"))
    if prior_other:
        prior = [x for x in prior if x != "khác"] + [prior_other]
    if prior:
        lines.append("Tình trạng tại vùng rụng trước khi rụng tóc: " + ", ".join(prior))
    seen_self = [label for key, label in _SELF_SEEN if _is_yes(a, key)]
    if seen_self:
        lines.append("Bệnh nhân tự nhận thấy: " + ", ".join(seen_self))

    if _is_yes(a, "ttm_pullsHair"):
        ttm = []
        who = TTM_WHO.get(_txt(a.get("ttm_who")))
        if who:
            ttm.append(who.lower())
        freq = TTM_FREQ.get(_txt(a.get("ttm_frequency")))
        if freq:
            ttm.append(freq.lower())
        ttm += [label for key, label in TTM_DSM_HINTS if _is_yes(a, key)]
        lines.append("Có hành vi nhổ tóc" + (f" ({'; '.join(ttm)})" if ttm else ""))
        t = mgh_total(a)
        if t is not None:
            lines.append(f"Điểm MGH-HPS bệnh nhân tự chấm: {t}/28 ({muc_do_mgh(t).lower()})")

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


def _ghi_bang(out: dict, a: dict, root: str, rows, ba_muc: bool = True) -> None:
    """Điền bảng Có/Không(/Không biết) kiểu tienSuBanThan.<dòng>.status.
    rows: [(tên dòng trong bệnh án, (các key trong phiếu), key chi tiết hoặc None)].
    ba_muc=False với bảng chỉ có Có/Không -> bỏ qua đáp án "Không rõ" để bác sĩ tự hỏi lại."""
    for row, keys, detail_key in rows:
        status = _yn_or_none(a, *keys)
        if status == "Không biết" and not ba_muc:
            status = None
        if status:
            out[f"{root}.{row}.status"] = status
            detail = _txt(a.get(detail_key)) if detail_key else ""
            if detail and status == "Có":
                out[f"{root}.{row}.detail"] = detail


def _tuoi_khoi_phat(out: dict, a: dict) -> None:
    age = _num(a.get("hairLossOnsetAge"))
    if age is not None:
        out["tuoiKhoiPhat"] = str(age)


# Nghề nghiệp / trình độ học vấn: hệ thống bệnh viện trả dạng MÃ ở phần đầu hồ sơ
# (result.occupation, result.educationLevel), không nằm trong answers.
# Chỉ đổi những mã đã gặp trong phiếu thật; mã khác (hoặc "other") để trống cho bác sĩ tự chọn.
# Bệnh án AA và TTM có bộ lựa chọn khác nhau.
NGHE_NGHIEP = {
    "aa": {"office": "Lao động trí óc", "student": "Học sinh/sinh viên", "manual": "Lao động chân tay",
           "worker": "Lao động chân tay", "farmer": "Lao động chân tay", "retired": "Mất sức LĐ/hưu trí"},
    "ttm": {"office": "Nhân viên văn phòng", "student": "Học sinh / Sinh viên", "manual": "Lao động chân tay",
            "worker": "Lao động chân tay", "farmer": "Lao động chân tay", "housewife": "Nội trợ"},
}
TRINH_DO = {
    "aa": {"primary": "Phổ thông và dưới", "secondary": "Phổ thông và dưới", "highschool": "Phổ thông và dưới",
           "university": "Đại học và sau đại học", "college": "Đại học và sau đại học",
           "postgraduate": "Đại học và sau đại học"},
    "ttm": {"primary": "Tiểu học", "secondary": "THCS", "highschool": "THPT",
            "university": "Đại học / Sau ĐH", "postgraduate": "Đại học / Sau ĐH"},
}


def _nghe_trinh_do(out: dict, result: dict, benh: str) -> None:
    nghe = NGHE_NGHIEP[benh].get(_txt(result.get("occupation")))
    if nghe:
        out["ngheNghiep"] = nghe
    td = TRINH_DO[benh].get(_txt(result.get("educationLevel")))
    if td:
        out["trinhDo"] = td


def _sdt(v) -> Optional[str]:
    """Chỉ nhận số điện thoại trông hợp lệ (8-15 chữ số), tránh điền rác vào hồ sơ."""
    s = _txt(v)
    so = "".join(c for c in s if c.isdigit())
    return s if 8 <= len(so) <= 15 and all(c.isdigit() or c in "+ .-()" for c in s) else None


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

    _tuoi_khoi_phat(out, a)
    _nghe_trinh_do(out, result, "aa")
    _ghi_bang(out, a, "tienSuBanThan", (
        ("Viêm da cơ địa", ("mh_atopicDermatitis",), None),
        ("Viêm mũi dị ứng", ("mh_allergicRhinitis",), None),
        ("Hen phế quản", ("mh_asthma",), None),
        ("Hashimoto", ("mh_hashimoto",), None),
        ("Basedow", ("mh_basedow",), None),
        ("Bạch biến", ("mh_vitiligo",), None),
        ("Dị ứng thuốc", ("drugAllergy",), "drugAllergy_list"),
        ("Vảy nến", ("mh_psoriasis",), None),
        ("Bệnh Celiac", ("mh_celiac",), None),
        ("Bệnh tự miễn khác", ("mh_lupus",), None),
    ))
    _ghi_bang(out, a, "tienSuGiaDinh", (
        ("Rụng tóc từng mảng", ("fam_alopecia_areata",), None),
        ("Bệnh lý cơ địa", ("fam_atopy",), None),
        ("Bệnh lý tự miễn", ("fam_autoimmune",), None),
        ("Bạch biến", ("fam_vitiligo",), None),
    ))
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


def map_sa(a: dict, result: dict) -> Dict[str, Any]:
    """Rụng tóc sẹo. Phiếu mở rộng (09/2026) đã có đủ phần tiền sử, thói quen chăm sóc tóc,
    bệnh cảnh trước khi rụng — ánh xạ vào đúng các bảng của bệnh án rụng tóc sẹo.
    Vị trí khởi phát / tổn thương ngoài da đầu / lùi chân tóc là phần KHÁM -> không tự điền."""
    out = _map_common(a, result)
    onset = _onset_text(a)
    if onset:
        out["thoiGianMacBenh"] = onset
    _tuoi_khoi_phat(out, a)

    yeu_to = [opt for key, opt, _t in _PRIOR_AT_SITE if a.get(key) is True]
    if yeu_to:
        out["yeuToKhoiPhat"] = yeu_to
    progress = _LESION_PROGRESS.get(_txt(a.get("obs_lesionProgress")))
    if progress:
        out["tienTrienTonThuong"] = progress

    co_nang = []
    for key, label in (("scalp_itch", "Ngứa"), ("scalp_pain", "Đau"),
                       ("scalp_burning", "Rát bỏng"), ("scalp_tightness", "Căng da")):
        if _is_yes(a, key):
            co_nang.append(label)
    if co_nang:
        out["trieuChungCoNang"] = co_nang
    elif all(_yn(a.get(k)) == "Không" for k in ("scalp_itch", "scalp_pain")) and \
            not any(_is_yes(a, k) for k in ("scalp_burning", "scalp_tightness")):
        out["trieuChungCoNang"] = ["Không triệu chứng"]

    # LPPAI: mức độ ngứa/đau/căng da bệnh nhân tự chấm (thang 0-10) nếu phiếu có hỏi
    for field, key in (("lppai.ngua", "scalp_itch_scale"), ("lppai.dau", "scalp_pain_scale"),
                       ("lppai.cangDa", "scalp_tightness_scale")):
        v = _num(a.get(key))
        if v is not None:
            out[field] = v

    if _any_yes(a, _TREATED_KEYS):
        out["dieuTriTruocDoStatus"] = "Có"
    elif any(_yn(a.get(k)) is not None for k in _TREATED_KEYS):
        out["dieuTriTruocDoStatus"] = "Không"

    _ghi_bang(out, a, "tienSuBanThan", (
        ("Lupus ban đỏ hệ thống (SLE)", ("mh_lupus", "pre_systemicLupusErythematosus"), None),
        ("Lichen phẳng ngoài da đầu (thân mình/miệng/móng)", ("mh_lichenPlanus",), None),
        ("Viêm tuyến giáp tự miễn (Hashimoto/Basedow)", ("mh_hashimoto", "mh_basedow"), None),
        ("Bạch biến", ("mh_vitiligo",), None),
        ("Viêm nang lông tái phát / nhọt da đầu", ("mh_recurrentFolliculitis",), None),
        ("Viêm da tiết bã nặng / Rosacea", ("mh_seborrheicRosacea",), None),
        ("Hidradenitis suppurativa / Acne inversa", ("mh_hidradenitis",), None),
        ("Viêm da cơ địa / Vảy nến", ("mh_atopicDermatitis", "mh_psoriasis"), None),
        ("Bỏng / chấn thương da đầu / tia xạ vùng đầu", ("mh_headBurnTraumaRadiation",), None),
    ))
    _ghi_bang(out, a, "thoiQuenChamSocToc", (
        ("Hóa chất làm thẳng/duỗi tóc", ("habit_chemicalRelaxer",), None),
        ("Tóc giả / nối tóc / weave / wigs", ("habit_wigExtensions",), None),
        ("Kiểu tóc tạo áp lực (tết chặt, búi chặt, cornrow)", ("habit_tightHairstyle",), None),
        ("Nhiệt thường xuyên (duỗi, uốn, sấy)", ("habit_heat",), None),
        ("Thuốc nhuộm / thuốc tẩy tóc", ("habit_dyeBleach",), None),
        ("Dầu/mỡ thoa da đầu thường xuyên", ("habit_scalpOil",), None),
        ("Không chăm sóc đặc biệt", ("habit_noSpecialCare",), None),
    ), ba_muc=False)
    _ghi_bang(out, a, "tienSuGiaDinh", (
        ("Rụng tóc sẹo", ("fam_scarring_alopecia",), None),
        ("Lupus / bệnh tự miễn", ("fam_autoimmune", "fam_thyroid_anemia_pcos_lupus"), None),
        ("Bệnh lý cơ địa (VDCĐ, hen, dị ứng)", ("fam_atopy",), None),
        ("Bệnh lý nang lông / mụn trứng cá nặng", ("fam_folliculitis_acne",), None),
    ))
    return out


def map_ttm(a: dict, result: dict) -> Dict[str, Any]:
    """Tật nhổ tóc. Dùng phần chung của phiếu rụng tóc + phiếu riêng về tật nhổ tóc
    (ttmAnswers: hành vi nhổ tóc, MGH-HPS, BFRBs) nếu bệnh nhân đã điền.
    Bảng tiêu chuẩn DSM-5 và vị trí tổn thương là phần bác sĩ đánh giá -> không tự điền."""
    out = _map_common(a, result)
    onset = _onset_text(a)
    if onset:
        out["thoiGianMacBenh"] = onset
    _tuoi_khoi_phat(out, a)
    _nghe_trinh_do(out, result, "ttm")

    _ghi_bang(out, a, "tienSuBanThan", (
        ("Bệnh lý tâm thần (lo âu, trầm cảm, OCD, ADHD...)",
         ("mh_anxiety", "mh_depression", "mh_ocd", "mh_adhd"), None),
        ("Tiền sử hen phế quản", ("mh_asthma",), None),
        ("Tiền sử dị ứng thuốc / thức ăn", ("drugAllergy",), "drugAllergy_list"),
        ("Đang có thai / cho con bú (nữ)", ("mh_pregnantOrBreastfeeding",), None),
    ), ba_muc=False)
    _ghi_bang(out, a, "tienSuGiaDinh", (
        ("Tật nhổ tóc / nhổ lông (BFRBs)", ("fam_trichotillomania",), None),
        ("Rối loạn tâm thần kinh (lo âu, OCD, Tourette, ADHD)", ("fam_psychiatric",), None),
        ("Rụng tóc (AA, FPHL, RTS...)", ("fam_alopecia_areata", "fam_maleHairLoss",
                                         "fam_femaleHairLoss", "fam_scarring_alopecia"), None),
    ), ba_muc=False)

    # ---- Phiếu riêng tật nhổ tóc ----
    who = TTM_WHO.get(_txt(a.get("ttm_who")))
    if who:
        out["aiNhoToc"] = who
    onset_ctx = TTM_ONSET.get(_txt(a.get("ttm_onsetContext")))
    if onset_ctx:
        out["hoanCanhKhoiPhat"] = onset_ctx
    freq = TTM_FREQ.get(_txt(a.get("ttm_frequency")))
    if freq:
        out["tanSuatNhoToc"] = freq
    times = [label for key, label in TTM_TIME if a.get(key) is True]
    if times:
        out["thoiDiemTrongNgay"] = times
    sits = [label for key, label in TTM_SITUATION if a.get(key) is True]
    if sits:
        out["tinhHuongKichHoat"] = sits
        other = _txt(a.get("ttm_sit_other_text"))
        if other and "Khác" in sits:
            out["tinhHuongKhac"] = other
    # Bảng Có/Không: chỉ điền khi bệnh nhân có vào phần tật nhổ tóc (tránh điền "Không" hàng loạt
    # từ các ô trống của phiếu chung)
    if _yn(a.get("ttm_pullsHair")) == "Có":
        for root, rows in (("nhanThuc", TTM_AWARENESS), ("hanhViSauNho", TTM_AFTER), ("bfrb", TTM_BFRB)):
            _ghi_bang(out, a, root, [(row, (key,), None) for key, row in rows], ba_muc=False)
    for i, key in enumerate(MGH_KEYS, start=1):
        v = _num(a.get(key))
        if v is not None and 0 <= v <= 4:
            out[f"mgh.q{i}"] = str(int(v))
    return out


# Thêm bệnh mới: chỉ cần thêm 1 dòng vào đây (khớp key với DISEASE_CONFIGS trong main.py).
SURVEY_MAPPERS = {
    "aa": map_aa,
    "aga": map_aga,
    "nonscar": map_nonscar,
    "sa": map_sa,
    "ttm": map_ttm,
}


def map_survey(answers: Optional[dict], result: dict, benh: str) -> Dict[str, Any]:
    if not isinstance(answers, dict) or not answers:
        return {}
    mapper = SURVEY_MAPPERS.get((benh or "").lower())
    if not mapper:
        return {}
    return mapper(answers, result)


# ---------- 5. Thông tin hành chính lấy từ phiếu ----------

def _gioi_tinh(a: Optional[dict]) -> Optional[str]:
    if not isinstance(a, dict):
        return None
    g = _txt(a.get("gender")).lower()
    if g in ("female", "nu", "nữ", "f"):
        return "Nữ"
    if g in ("male", "nam", "m"):
        return "Nam"
    return None


def parse_answers(raw) -> Optional[dict]:
    """Đọc phần `answers` của phiếu khảo sát.

    Hệ thống bệnh viện trả trường này dưới dạng CHUỖI JSON (có lúc còn bị mã hoá lồng
    nhiều lớp), không phải đối tượng. Bản trước coi mặc định là đối tượng nên gặp bệnh
    nhân ĐÃ điền phiếu là sập với `'str' object has no attribute 'get'` — còn bệnh nhân
    chưa điền (answers = null) thì vẫn chạy, nên lỗi trông rất khó hiểu.
    Trả None nếu không đọc được, để phần mềm coi như chưa có phiếu thay vì báo lỗi.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        return json_safe(raw)
    for _ in range(3):  # phòng trường hợp chuỗi JSON lồng nhiều lớp
        if not isinstance(raw, str):
            break
        try:
            raw = json.loads(raw, parse_constant=lambda _c: None)
        except (ValueError, TypeError):
            return None
    return json_safe(raw) if isinstance(raw, dict) else None


def build_response(ma_bn: str, benh: str) -> Dict[str, Any]:
    fetched = fetch_survey(ma_bn)
    if not fetched["found"]:
        return {"found": False, "loi": fetched["loi"], "co_khao_sat": False,
                "khao_sat": None, "mapped": {}, "dlqi_tong": None, "url_phieu": url_phieu(ma_bn)}
    out = _tao_phan_hoi(fetched["result"], benh)
    out["url_phieu"] = url_phieu(ma_bn)
    return out


def _bo_so_0(ma) -> str:
    return str(ma or "").strip().lstrip("0")


def tu_noi_dung_dan(ma_bn: str, noi_dung: str, benh: str) -> Dict[str, Any]:
    """Cách dự phòng khi máy chủ không gọi được bệnh viện: bác sĩ tự mở phiếu trên trình duyệt
    (ở Việt Nam nên không bị chặn), bấm Ctrl+A, Ctrl+C rồi dán vào phần mềm.

    Chấp nhận cả khi dán lẫn chữ thừa (VD dòng "Pretty-print" của Chrome) — chỉ lấy phần từ dấu
    { đầu tiên tới dấu } cuối cùng. BẮT BUỘC mã bệnh nhân trong phiếu khớp mã đang mở, để không
    bao giờ điền nhầm phiếu của người khác vào hồ sơ này."""
    def loi(thong_diep):
        return {"found": False, "loi": thong_diep, "co_khao_sat": False, "khao_sat": None,
                "mapped": {}, "dlqi_tong": None, "url_phieu": url_phieu(ma_bn)}

    text = (noi_dung or "").strip()
    if len(text) > 3_000_000:
        return loi("Nội dung dán vào quá dài — có lẽ đã dán nhầm thứ khác.")
    dau, cuoi = text.find("{"), text.rfind("}")
    if dau < 0 or cuoi <= dau:
        return loi("Nội dung dán vào không phải phiếu khảo sát. Mở phiếu, bấm Ctrl+A rồi Ctrl+C, "
                   "quay lại đây bấm Ctrl+V.")
    try:
        body = json.loads(text[dau:cuoi + 1], parse_constant=lambda _c: None)
    except ValueError:
        return loi("Nội dung dán vào bị thiếu hoặc lẫn chữ lạ. Mở lại phiếu, bấm Ctrl+A rồi Ctrl+C "
                   "và dán lại toàn bộ.")
    if not isinstance(body, dict):
        return loi("Nội dung dán vào không phải phiếu khảo sát.")
    if "result" in body:
        if body.get("success") is False:
            tb = loi_tu_benh_vien(500, json.dumps(body, ensure_ascii=False))
            return loi(tb.replace(" (mã lỗi 500)", "") if tb.startswith("Hệ thống bệnh viện báo:")
                       else "Hệ thống bệnh viện báo lỗi trong trang vừa mở — thử mở lại phiếu.")
        result = body.get("result")
    elif "patientCode" in body or "answers" in body:
        result = body
    else:
        return loi("Nội dung dán vào không phải phiếu khảo sát.")
    if not result:
        return loi("Hệ thống bệnh viện không có hồ sơ với mã này.")
    result = json_safe(result)
    ma_phieu = result.get("patientCode")
    if ma_phieu and _bo_so_0(ma_phieu) != _bo_so_0(ma_bn):
        return loi(f"Phiếu vừa dán là của bệnh nhân mã {ma_phieu}, KHÔNG khớp mã {ma_bn} đang mở. "
                   "Không điền để tránh nhầm hồ sơ — kiểm tra lại trang vừa mở.")
    out = _tao_phan_hoi(result, benh)
    out["url_phieu"] = url_phieu(ma_bn)
    out["nguon"] = "dan_tay"
    return out


def gop_phieu_ttm(answers: Optional[dict], ttm: Optional[dict]) -> Optional[dict]:
    """Gộp phiếu riêng tật nhổ tóc (trường `ttmAnswers`) vào phiếu chung.
    Phiếu chung cũng có vài câu ttm_* (để trống nếu bệnh nhân không nhổ tóc) — câu nào phiếu
    riêng CÓ trả lời thì lấy theo phiếu riêng."""
    if not ttm:
        return answers
    gop = dict(answers or {})
    for k, v in ttm.items():
        if v is not None and v != "":
            gop[k] = v
        else:
            gop.setdefault(k, v)
    return gop


def _tao_phan_hoi(result: dict, benh: str) -> Dict[str, Any]:
    answers_chung = parse_answers(result.get("answers"))
    ttm = parse_answers(result.get("ttmAnswers"))
    answers = gop_phieu_ttm(answers_chung, ttm)
    khong_doc_duoc = (bool(result.get("answers")) and not answers_chung) or \
                     (bool(result.get("ttmAnswers")) and not ttm)
    ngay_sinh = _txt(result.get("patientBirthDay"))[:10]
    mapped = map_survey(answers, result, benh)
    return {
        "found": True,
        "loi": ("Đọc được hồ sơ nhưng KHÔNG hiểu được định dạng phiếu khảo sát — "
                "hệ thống bệnh viện có thể đã đổi cách trả dữ liệu." if khong_doc_duoc else None),
        "ma_bn": result.get("patientCode"),
        "ho_ten": _txt(result.get("patientName")) or None,
        "ngay_sinh": ngay_sinh or None,
        "nam_sinh": int(ngay_sinh[:4]) if ngay_sinh[:4].isdigit() else None,
        "gioi_tinh": _gioi_tinh(answers),
        "dan_toc": " ".join(_txt(result.get("ethnicName")).split()) or None,
        "dia_chi": " ".join(_txt(result.get("address")).split()) or None,
        "sdt": _sdt(result.get("phone")),
        "ngay_kham": _txt(result.get("treatmentDate"))[:10] or None,
        "ma_luot_kham": result.get("treatmentCode"),
        "co_khao_sat": bool(answers),
        "khao_sat": answers,
        "dlqi_tong": dlqi_total(answers) if answers else None,
        "co_phieu_ttm": bool(ttm),
        "mgh_tong": mgh_total(answers) if answers else None,
        "mapped": mapped,
        "so_truong_dien": len(mapped),
    }
