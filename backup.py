"""
Backup hằng ngày cho database MySQL.
- Xuất toàn bộ database ra 1 file .sql (mysqldump)
- Nén lại (gzip) cho nhẹ
- Lưu 1 bản cục bộ (thư mục backups/, giữ lại 14 bản gần nhất)
- Nếu đã cấu hình S3, đẩy thêm 1 bản lên bucket backup (khuyến khích dùng bucket
  RIÊNG, khác với bucket lưu ảnh — hoặc tốt hơn là 1 tài khoản AWS khác hẳn,
  để nếu tài khoản chính có sự cố vẫn còn bản sao nơi khác)

Cách chạy thủ công:
    python backup.py

Cách đặt lịch tự động chạy mỗi ngày (Linux/macOS, dùng cron):
    crontab -e
    # thêm dòng sau để chạy lúc 2 giờ sáng mỗi ngày:
    0 2 * * * cd /duong/dan/toi/backend && /usr/bin/python3 backup.py >> backup.log 2>&1

Trên Windows: dùng Task Scheduler, tạo tác vụ chạy hằng ngày, action là:
    python C:\\duong\\dan\\backend\\backup.py
"""
import gzip
import os
import shutil
import subprocess
from datetime import datetime

import config

BACKUP_DIR = os.path.join(os.path.dirname(__file__), "backups")
KEEP_LOCAL_COPIES = 14  # giữ 14 bản gần nhất (khoảng 2 tuần), xoá bản cũ hơn
BACKUP_S3_BUCKET = config.BACKUP_S3_BUCKET


def dump_mysql(output_path: str):
    if not config.USE_MYSQL:
        raise RuntimeError(
            "Chưa cấu hình MySQL thật (.env) — backup chỉ chạy được khi đã nối MySQL thật, "
            "không áp dụng cho SQLite lúc chạy thử."
        )
    cmd = [
        "mysqldump",
        f"--host={config.MYSQL_HOST}",
        f"--port={config.MYSQL_PORT}",
        f"--user={config.MYSQL_USER}",
        f"--password={config.MYSQL_PASSWORD}",
        "--single-transaction",  # không khoá bảng khi đang backup, an toàn khi hệ thống đang chạy
        "--databases", config.MYSQL_DB,
    ]
    with open(output_path, "wb") as f:
        subprocess.run(cmd, stdout=f, check=True)


def compress(path: str) -> str:
    gz_path = path + ".gz"
    with open(path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(path)
    return gz_path


def cleanup_old_backups():
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".sql.gz")],
        reverse=True,
    )
    for old_file in files[KEEP_LOCAL_COPIES:]:
        os.remove(os.path.join(BACKUP_DIR, old_file))


def upload_to_s3(local_path: str, filename: str):
    if not config.USE_BACKUP_S3:
        print("Chưa cấu hình tài khoản AWS phụ (BACKUP_AWS_ACCESS_KEY_ID/...) — bỏ qua bước tải lên, chỉ lưu cục bộ.")
        return
    import boto3

    client = boto3.client(
        "s3",
        aws_access_key_id=config.BACKUP_AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.BACKUP_AWS_SECRET_ACCESS_KEY,
        region_name=config.BACKUP_AWS_REGION,
    )
    key = f"db-backups/{filename}"
    client.upload_file(local_path, BACKUP_S3_BUCKET, key)
    print(f"Đã tải backup lên s3://{BACKUP_S3_BUCKET}/{key}")


def main():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    sql_path = os.path.join(BACKUP_DIR, f"clinic_{timestamp}.sql")

    print(f"Đang xuất database... ({timestamp})")
    dump_mysql(sql_path)

    gz_path = compress(sql_path)
    print(f"Đã nén: {gz_path}")

    upload_to_s3(gz_path, os.path.basename(gz_path))
    cleanup_old_backups()
    print("Hoàn tất backup.")


if __name__ == "__main__":
    main()
