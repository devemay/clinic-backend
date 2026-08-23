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

# ---------- auth ----------
SECRET_KEY = os.environ.get("CLINIC_SECRET_KEY", "change-this-secret-in-production")
