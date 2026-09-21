import os
from dotenv import load_dotenv

load_dotenv()


# ---------- MySQL ----------
MYSQL_HOST = os.environ.get("MYSQL_HOST")
MYSQL_PORT = os.environ.get("MYSQL_PORT", "3306")
MYSQL_USER = os.environ.get("MYSQL_USER")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD")
MYSQL_DB = os.environ.get("MYSQL_DB", "clinic")

USE_MYSQL = bool(MYSQL_HOST and MYSQL_USER and MYSQL_DB)

# ---------- AWS S3 ----------
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-1")
S3_BUCKET = os.environ.get("S3_BUCKET")

USE_S3 = bool(AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY and S3_BUCKET)

# Tên các bucket CŨ (cách nhau bởi dấu phẩy) — chỉ dùng khi chuyển ảnh sang bucket/tài khoản AWS
# mới. Database lưu ảnh dưới dạng đường dẫn đầy đủ CÓ TÊN BUCKET CŨ; khai báo ở đây để phần mềm
# vẫn nhận ra các đường dẫn đó và cấp lại đường dẫn trỏ sang bucket mới (ảnh đã được chép sang
# với nguyên tên file). Không khai báo thì toàn bộ ảnh cũ sẽ hiện lỗi sau khi chuyển.
S3_BUCKET_CU = [x.strip() for x in os.environ.get("S3_BUCKET_CU", "").split(",") if x.strip()]

# ---------- AWS phụ, chỉ dùng để lưu backup (nên KHÁC tài khoản AWS chính) ----------
BACKUP_AWS_ACCESS_KEY_ID = os.environ.get("BACKUP_AWS_ACCESS_KEY_ID")
BACKUP_AWS_SECRET_ACCESS_KEY = os.environ.get("BACKUP_AWS_SECRET_ACCESS_KEY")
BACKUP_AWS_REGION = os.environ.get("BACKUP_AWS_REGION", "ap-southeast-1")
BACKUP_S3_BUCKET = os.environ.get("BACKUP_S3_BUCKET")

USE_BACKUP_S3 = bool(BACKUP_AWS_ACCESS_KEY_ID and BACKUP_AWS_SECRET_ACCESS_KEY and BACKUP_S3_BUCKET)

# ---------- Phiếu khảo sát điện tử của bệnh viện (bệnh nhân tự điền trước khi vào khám) ----------
# Đổi được bằng biến môi trường nếu bệnh viện chuyển địa chỉ hoặc đổi phòng khám.
SURVEY_API_BASE = os.environ.get("SURVEY_API_BASE", "https://api.dalieu.vn")
SURVEY_ROOM_ID = os.environ.get("SURVEY_ROOM_ID", "2283")
def _so_giay(ten, mac_dinh):
    try:
        return float(os.environ.get(ten, str(mac_dinh)))
    except ValueError:
        return float(mac_dinh)

# timeout cho từng thao tác đọc/ghi socket
SURVEY_TIMEOUT = _so_giay("SURVEY_TIMEOUT", 6)
# hạn chót cứng cho TOÀN BỘ lần tra cứu (kể cả bước phân giải tên miền, thứ mà
# timeout của urllib không bao được) — quá hạn thì bỏ và trả thông báo dễ hiểu
SURVEY_DEADLINE = _so_giay("SURVEY_DEADLINE", 12)

# ---------- Máy trạm ở phòng khám (chuyển tiếp phiếu khảo sát) ----------
# Hệ thống bệnh viện chặn máy chủ ở nước ngoài (Render). Một máy tính ở phòng khám (mạng Việt Nam)
# chạy chương trình "tram_khao_sat.ps1": nó liên tục hỏi máy chủ "có việc không", tự đọc phiếu từ
# bệnh viện rồi gửi kết quả về. Có đặt TRAM_KHOA thì máy chủ KHÔNG gọi thẳng bệnh viện nữa mà
# nhờ máy trạm. Xoá biến này = quay lại gọi thẳng như cũ.
TRAM_KHOA = (os.environ.get("TRAM_KHOA") or "").strip() or None

# ---------- auth ----------
SECRET_KEY = os.environ.get("CLINIC_SECRET_KEY", "change-this-secret-in-production")

# ---------- Báo cáo tháng tự động ----------
# Cách 1 (dùng được trên Render miễn phí): chuyển tiếp qua Google Apps Script trong Gmail của khoa.
MAIL_RELAY_URL = os.environ.get("MAIL_RELAY_URL")   # địa chỉ Web app, kết thúc bằng /exec
MAIL_RELAY_KEY = os.environ.get("MAIL_RELAY_KEY")   # khoá bí mật, phải trùng với trong Apps Script
# Cách 2 (chỉ khi máy chủ được phép dùng cổng SMTP: Render trả phí, mail bệnh viện...)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = os.environ.get("SMTP_PORT", "465")
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "Phòng khám Rụng tóc")
# Khoá để cron-job.org gọi vào địa chỉ hẹn giờ gửi báo cáo. Không có khoá thì địa chỉ đó tắt hẳn.
REPORT_CRON_KEY = os.environ.get("REPORT_CRON_KEY")
