import os
import re
import uuid
from datetime import datetime
from typing import Optional

import config

LOCAL_UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
PRESIGN_EXPIRES = 7 * 24 * 3600  # 7 ngày — mức tối đa cho phép với access key IAM user thường


def _new_key(ma_bn: str) -> str:
    today = datetime.utcnow().strftime("%Y%m%d")
    return f"aa/{ma_bn}/{today}/{uuid.uuid4().hex}.webp"


class LocalStorage:
    """Lưu tạm trên đĩa cục bộ khi chưa cấu hình AWS S3 — chỉ dùng để chạy thử."""

    def save(self, data: bytes, ma_bn: str) -> str:
        os.makedirs(LOCAL_UPLOAD_DIR, exist_ok=True)
        key = _new_key(ma_bn)
        path = os.path.join(LOCAL_UPLOAD_DIR, key.replace("/", "_"))
        with open(path, "wb") as f:
            f.write(data)
        return f"/uploads/{key.replace('/', '_')}"


class S3Storage:
    """Tải ảnh lên AWS S3 (bucket riêng tư) — trả về URL có chữ ký tạm thời (presigned URL),
    hết hạn sau PRESIGN_EXPIRES giây. Ảnh vẫn còn mãi trên S3 — chỉ URL truy cập là tạm thời,
    được cấp lại tự động mỗi khi ai đó mở lại hồ sơ (xem hàm refresh_url bên dưới)."""

    def __init__(self):
        import boto3

        self.client = boto3.client(
            "s3",
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
            region_name=config.AWS_REGION,
        )
        self.bucket = config.S3_BUCKET

    def _presign(self, key: str) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=PRESIGN_EXPIRES
        )

    def save(self, data: bytes, ma_bn: str) -> str:
        key = _new_key(ma_bn)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType="image/webp")
        return self._presign(key)


def get_storage():
    if config.USE_S3:
        return S3Storage()
    return LocalStorage()


_S3_URL_RE = None
def _s3_url_pattern():
    global _S3_URL_RE
    if _S3_URL_RE is None and config.USE_S3:
        # Lưu ý: boto3 mặc định tạo URL dạng "bucket.s3.amazonaws.com" (KHÔNG có tên vùng trong
        # tên miền), khác với địa chỉ endpoint dịch vụ "s3.<vùng>.amazonaws.com". Regex trước đây
        # bắt buộc phải có tên vùng nên không khớp URL thật, khiến hàm làm mới không hoạt động —
        # để phần "vùng" thành tuỳ chọn để khớp đúng cả 2 dạng URL boto3 có thể tạo ra.
        _S3_URL_RE = re.compile(
            rf"^https://{re.escape(config.S3_BUCKET)}\.s3(?:[.-]{re.escape(config.AWS_REGION)})?\.amazonaws\.com/([^?]+)"
        )
    return _S3_URL_RE


def refresh_url(url: Optional[str]) -> Optional[str]:
    """Nếu url là 1 object trong bucket S3 riêng tư của hệ thống, cấp lại URL có chữ ký mới
    (URL cũ có thể đã hết hạn sau 7 ngày). Nếu không phải (ảnh local, hoặc chuỗi khác), giữ nguyên."""
    if not url or not config.USE_S3:
        return url
    pattern = _s3_url_pattern()
    m = pattern.match(url) if pattern else None
    if not m:
        return url
    key = m.group(1)
    try:
        return S3Storage()._presign(key)
    except Exception:
        return url
