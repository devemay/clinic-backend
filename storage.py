import os
import re
import uuid
from datetime import datetime
from typing import Optional
from urllib.parse import unquote

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
        from botocore.config import Config

        # Ký bằng SigV4 và dùng địa chỉ THEO VÙNG (bucket.s3.<vùng>.amazonaws.com):
        #  - boto3 mặc định ký đường dẫn ảnh kiểu cũ SigV2. AWS đã thông báo bucket tạo sau
        #    24/06/2020 không nhận SigV2 -> bucket mới ở tài khoản khoa có thể không mở được ảnh nào.
        #  - bucket mới tạo mà gọi qua địa chỉ chung (bucket.s3.amazonaws.com, không có vùng) có
        #    thể bị báo sai chữ ký cho tới khi DNS của AWS cập nhật xong.
        # SigV4 + địa chỉ theo vùng hoạt động với MỌI bucket, cũ lẫn mới. Hạn tối đa của SigV4 là
        # 7 ngày — đúng bằng PRESIGN_EXPIRES.
        self.client = boto3.client(
            "s3",
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
            region_name=config.AWS_REGION,
            endpoint_url=f"https://s3.{config.AWS_REGION}.amazonaws.com",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
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
    """Regex nhận ra đường dẫn ảnh của CHÍNH hệ thống (bucket hiện tại + các bucket cũ khai báo
    trong S3_BUCKET_CU). Khớp cả các dạng URL S3 có thể gặp:
      - https://BUCKET.s3.amazonaws.com/KEY                 (boto3 mặc định — KHÔNG có tên vùng)
      - https://BUCKET.s3.ap-southeast-1.amazonaws.com/KEY  / BUCKET.s3-ap-southeast-1...
      - https://s3.ap-southeast-1.amazonaws.com/BUCKET/KEY  (kiểu đường dẫn, bản cũ có thể tạo)
    Vùng để tuỳ ý (không cố định theo AWS_REGION) vì tài khoản cũ có thể ở vùng khác."""
    global _S3_URL_RE
    if _S3_URL_RE is None and config.USE_S3:
        ten = sorted({config.S3_BUCKET, *config.S3_BUCKET_CU}, key=len, reverse=True)
        nhom = "|".join(re.escape(b) for b in ten)
        vung = r"(?:[.-][a-z0-9-]+)?"
        _S3_URL_RE = re.compile(
            rf"^https://(?:(?:{nhom})\.s3{vung}\.amazonaws\.com/|s3{vung}\.amazonaws\.com/(?:{nhom})/)([^?#]+)"
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
    # Đường dẫn có chữ ký mã hoá ký tự đặc biệt trong tên file (VD dấu cách -> %20). Phải giải mã
    # trước khi ký lại, nếu không sẽ bị mã hoá 2 lần và trỏ tới một file không tồn tại.
    key = unquote(m.group(1))
    try:
        # luôn ký theo bucket HIỆN TẠI: sau khi chuyển, ảnh cũ đã được chép sang với nguyên tên file
        return S3Storage()._presign(key)
    except Exception:
        return url
