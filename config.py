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

# ---------- auth ----------
SECRET_KEY = os.environ.get("CLINIC_SECRET_KEY", "change-this-secret-in-production")
