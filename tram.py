"""Máy trạm ở phòng khám — chuyển tiếp phiếu khảo sát từ hệ thống bệnh viện về máy chủ.

Vì sao cần: api.dalieu.vn chặn máy chủ đặt ở nước ngoài (Render), nhưng vẫn mở cho máy ở
Việt Nam. Một hoặc NHIỀU máy tính (ở phòng khám, ở nhà...) chạy chương trình `tram_khao_sat.ps1`.

Cách hoạt động ("hỏi việc liên tục", long-polling):
  1. Mỗi máy trạm gọi GET /tram/cho-viec. Máy chủ giữ yêu cầu đó tối đa ~25 giây chờ việc.
  2. Bác sĩ bấm "Đồng bộ": máy chủ tạo 1 việc {id, mã BN} và GIAO CHO TẤT CẢ máy trạm đang bật.
  3. Mỗi máy trạm tự đọc phiếu trên bệnh viện rồi POST /tram/ket-qua.
  4. Máy chủ lấy KẾT QUẢ TỐT ĐẦU TIÊN, trả về cho bác sĩ như khi gọi thẳng.

Vì sao giao cho tất cả thay vì 1 máy: máy nào tắt, mất mạng, hay mạng đó không vào được bệnh viện
thì máy khác vẫn trả lời — bác sĩ không phải chờ hết hạn rồi bấm lại. Đổi lại bệnh viện nhận
thêm vài lượt đọc phiếu (vài trăm lượt/ngày, không đáng kể).

Ưu điểm so với mở đường hầm (ngrok…): máy trạm CHỈ kết nối ra ngoài tới máy chủ của mình —
không mở cổng, không cần tài khoản dịch vụ thứ ba, người ngoài không gọi được vào máy trạm.
Máy chủ chỉ gửi cho máy trạm MÃ BỆNH NHÂN; đường dẫn bệnh viện do máy trạm tự dựng và kiểm tra,
nên kể cả lộ khoá, người khác cũng không dùng máy trạm để truy cập trang nào khác được.

Toàn bộ nằm trong bộ nhớ của 1 tiến trình (Render chạy 1 tiến trình uvicorn) — không cần database.
"""
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import config

CHO_VIEC_GIAY = 25        # máy chủ giữ 1 lần "hỏi việc" tối đa bấy nhiêu giây
HAN_MAT_TIN_GIAY = 45     # quá lâu không thấy máy trạm hỏi việc -> coi như đang tắt
MA_HOP_LE = re.compile(r"^[0-9A-Za-z]{1,20}$")
_VN = timezone(timedelta(hours=7))


class TramKhongSan(Exception):
    """Máy trạm đang tắt / mất mạng / trả lời chậm — thông điệp đọc được cho bác sĩ."""


_cv = threading.Condition()
# id việc -> {"ma", "han", "da_giao": set(mã máy trạm), "loi": {mã máy trạm: lỗi}, "ket_qua": dict|None}
_viec: Dict[str, Dict[str, Any]] = {}
# mã máy trạm -> {"ten", "phien_ban", "lan_cuoi", "lan_cuoi_luc", "dang_hoi", "so_viec"}
_cac_tram: Dict[str, Dict[str, Any]] = {}


def bat() -> bool:
    return bool(config.TRAM_KHOA)


def _con_song(t: Dict[str, Any]) -> bool:
    return t["dang_hoi"] > 0 or (time.monotonic() - t["lan_cuoi"]) < HAN_MAT_TIN_GIAY


def _tram_con_song() -> List[str]:
    return [ma for ma, t in _cac_tram.items() if _con_song(t)]


def trang_thai() -> Dict[str, Any]:
    with _cv:
        # quên các máy đã tắt quá 7 ngày cho danh sách gọn
        for ma in [m for m, t in _cac_tram.items() if time.monotonic() - t["lan_cuoi"] > 7 * 86400]:
            _cac_tram.pop(ma, None)
        ds = sorted(_cac_tram.values(), key=lambda t: -t["lan_cuoi"])
        song = [t for t in ds if _con_song(t)]
        return {
            "bat": bat(),
            "hoat_dong": bat() and bool(song),
            "so_may_hoat_dong": len(song),
            "lan_cuoi": ds[0]["lan_cuoi_luc"] if ds else None,
            "ten_may": ", ".join(t["ten"] or "?" for t in song) or None,
            "phien_ban": song[0]["phien_ban"] if song else None,
            "so_viec_da_lam": sum(t["so_viec"] for t in ds),
            "cac_may": [{"ten": t["ten"], "hoat_dong": _con_song(t), "lan_cuoi": t["lan_cuoi_luc"],
                         "phien_ban": t["phien_ban"], "so_viec_da_lam": t["so_viec"]} for t in ds],
        }


def _danh_dau(ma_tram: str, ten_may: str, phien_ban: str) -> Dict[str, Any]:
    t = _cac_tram.setdefault(ma_tram, {"ten": None, "phien_ban": None, "lan_cuoi": 0.0,
                                       "lan_cuoi_luc": None, "dang_hoi": 0, "so_viec": 0})
    t["lan_cuoi"] = time.monotonic()
    t["lan_cuoi_luc"] = datetime.now(_VN).strftime("%H:%M %d/%m/%Y")
    if ten_may:
        t["ten"] = ten_may[:60]
    if phien_ban:
        t["phien_ban"] = phien_ban[:20]
    return t


def _ma_tram(ma_tram: str, ten_may: str) -> str:
    # Bản máy trạm cũ không gửi mã riêng -> dùng tên máy tính làm mã
    return (ma_tram or ten_may or "khong-ten")[:80]


def cho_viec(ten_may: str = "", phien_ban: str = "", cho_giay: float = CHO_VIEC_GIAY,
             ma_tram: str = "") -> Optional[Dict[str, Any]]:
    """Máy trạm gọi: trả 1 việc {id, ma} mà máy này CHƯA nhận, hoặc None sau `cho_giay` giây."""
    ma_tram = _ma_tram(ma_tram, ten_may)
    han = time.monotonic() + cho_giay
    with _cv:
        t = _danh_dau(ma_tram, ten_may, phien_ban)
        t["dang_hoi"] += 1
        try:
            while True:
                _danh_dau(ma_tram, ten_may, phien_ban)
                bay_gio = time.monotonic()
                for vid, v in _viec.items():
                    if v["ket_qua"] is None and v["han"] > bay_gio and ma_tram not in v["da_giao"]:
                        v["da_giao"].add(ma_tram)
                        return {"id": vid, "ma": v["ma"]}
                con = han - time.monotonic()
                if con <= 0:
                    return None
                _cv.wait(con)
        finally:
            t["dang_hoi"] -= 1
            _danh_dau(ma_tram, ten_may, phien_ban)


def nop_ket_qua(viec_id: str, ket_qua: Dict[str, Any], ma_tram: str = "", ten_may: str = "") -> bool:
    """Máy trạm gọi: gửi kết quả. Trả False nếu việc không còn ai chờ (đã xong / quá hạn)."""
    ma_tram = _ma_tram(ma_tram, ten_may)
    with _cv:
        v = _viec.get(viec_id)
        if v is None or v["ket_qua"] is not None:
            return False
        if ma_tram in _cac_tram:
            _cac_tram[ma_tram]["so_viec"] += 1
        if ket_qua.get("loi"):
            v["loi"][ma_tram] = str(ket_qua["loi"])[:200]
        elif int(ket_qua.get("ma_http") or 0) != 200:
            # bệnh viện trả lỗi cho máy này (VD 500) — giữ làm dự phòng, chờ máy khác có kết quả tốt
            v["du_phong"] = ket_qua
            v["loi"][ma_tram] = f"HTTP {ket_qua.get('ma_http')}"
        else:
            v["ket_qua"] = ket_qua
        _cv.notify_all()
        return True


def _thong_diep_loi(loi: str) -> str:
    if any(t in loi.lower() for t in ("canceled", "cancelled", "timed out", "timeout")):
        return ("Hệ thống bệnh viện phản hồi quá chậm (trên 8 giây). Thử lại sau ít phút, "
                "hoặc bấm “Dán phiếu thủ công”.")
    return (f"Máy trạm không đọc được hệ thống bệnh viện ({loi}). "
            "Thử lại, hoặc bấm “Dán phiếu thủ công”.")


def hoi(ma: str, han_giay: float) -> Dict[str, Any]:
    """Máy chủ gọi (thay cho gọi thẳng bệnh viện). Trả {"ma_http": int, "noi_dung": str}
    hoặc ném TramKhongSan kèm thông điệp tiếng Việt."""
    if not MA_HOP_LE.match(ma or ""):
        raise TramKhongSan("Mã bệnh nhân không hợp lệ.")
    viec_id = uuid.uuid4().hex
    with _cv:
        if not _tram_con_song():
            lan_cuoi = max((t["lan_cuoi"], t["lan_cuoi_luc"]) for t in _cac_tram.values())[1] if _cac_tram else None
            raise TramKhongSan(
                "Máy trạm đang tắt hoặc mất mạng"
                + (f" (liên lạc lần cuối lúc {lan_cuoi})" if lan_cuoi else " (chưa liên lạc lần nào)")
                + ". Bật máy trạm, hoặc bấm “Dán phiếu thủ công”.")
        han = time.monotonic() + max(1.0, han_giay)
        v = {"ma": ma, "han": han, "da_giao": set(), "loi": {}, "ket_qua": None, "du_phong": None}
        _viec[viec_id] = v
        _cv.notify_all()
        try:
            while v["ket_qua"] is None:
                # Mọi máy đang bật đều đã nhận việc và đều báo lỗi -> báo ngay, không chờ hết hạn
                song = set(_tram_con_song())
                het_han = han - time.monotonic() <= 0
                if (v["loi"] and song and song <= set(v["loi"])) or (het_han and v["loi"]):
                    if v["du_phong"] is not None:
                        v["ket_qua"] = v["du_phong"]
                        break
                    raise TramKhongSan(_thong_diep_loi(next(iter(v["loi"].values()))))
                con = han - time.monotonic()
                if con <= 0:
                    if v["loi"]:
                        raise TramKhongSan(_thong_diep_loi(next(iter(v["loi"].values()))))
                    raise TramKhongSan(
                        "Máy trạm không trả lời kịp (mạng phòng khám chậm hoặc máy trạm vừa tắt). "
                        "Thử lại, hoặc bấm “Dán phiếu thủ công”.")
                _cv.wait(min(con, 1.0))
            kq = v["ket_qua"]
        finally:
            _viec.pop(viec_id, None)
    return {"ma_http": int(kq.get("ma_http") or 0), "noi_dung": kq.get("noi_dung") or ""}


def _xoa_sach_cho_test():
    with _cv:
        _viec.clear()
        _cac_tram.clear()
