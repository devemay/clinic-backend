# Backend bệnh án nghiên cứu — module AA

## Chạy thử ngay (chưa cần MySQL/S3 thật)
pip install -r requirements.txt
python seed.py
uvicorn main:app --reload
Mở http://localhost:8000/docs để thử toàn bộ API.

## Khi đã có MySQL và AWS S3 thật
Copy `.env.example` thành `.env`, điền thông tin, chạy lại `python seed.py` rồi `uvicorn main:app --reload`.

## Mã lưu trữ
Mỗi bệnh án AA có 1 mã dạng AA-2026-0001 (bệnh-năm-số thứ tự), tự sinh khi bác sĩ tạo bệnh án mới. Mã này gắn với tất cả các lần tái khám sau đó của đúng đợt bệnh đó — dùng để tra cứu/lưu trữ độc lập với mã bệnh nhân của bệnh viện.

## Phân quyền (3 loại tài khoản)
- role="bac_si": tạo mã lưu trữ mới (bệnh án mới, lần tái khám mới). KHÔNG tự điền được nội dung khi tạo.
- role="hoc_vien": điền/sửa nội dung trong hồ sơ đã có mã (bệnh án mới lẫn tái khám). KHÔNG tạo được mã mới.
- can_export=True: cờ riêng, gắn thêm cho 1 tài khoản (mặc định "nghiencuu") mới được gọi /export/aa.csv — 2 role trên đều không có quyền này trừ khi được cấp thêm cờ này.

Đổi mật khẩu mặc định (123456), thêm/bớt tài khoản trực tiếp trong seed.py trước khi dùng thật.

## Luồng tạo hồ sơ (đúng theo quy trình phòng khám)
1. Bác sĩ khám xong, xác định là ca AA → POST /cases/{ma_bn}/aa/create → sinh mã lưu trữ, hồ sơ ở trạng thái "chưa điền" (da_dien_du_lieu=false)
2. Bệnh nhân làm xét nghiệm xong, học viên mở đúng mã đó → PUT /cases/{ma_bn}/aa với dữ liệu đầy đủ → chuyển trạng thái "đã điền"
3. Lần tái khám sau: bác sĩ POST .../followups/create sinh khung mới → học viên PUT .../followups/{id} điền nội dung

## Dashboard & tra cứu
- GET /dashboard/today — toàn bộ ca khám (mới + tái khám) trong ngày hôm nay, kèm cờ "đã điền dữ liệu" để biết ca nào còn thiếu, followup_id/ma_bn để mở thẳng vào sửa
- GET /cases/search?tu_ngay=&den_ngay=&muc_do=&dieu_tri_chua=&chi_chua_dien= — tra cứu theo ngày khám, mức độ nặng, nội dung điều trị (chứa từ khoá), hoặc lọc riêng các hồ sơ "bác sĩ đã tạo nhưng học viên chưa điền" để nhắc việc

## Xuất dữ liệu nghiên cứu (chỉ tài khoản can_export=True)
GET /export/aa.csv?tu_ngay=&den_ngay=&muc_do=&dieu_tri_chua= — CSV dạng long format (mỗi dòng 1 lần khám), lọc theo khoảng ngày/mức độ/nội dung điều trị, mở thẳng Excel hoặc nạp SPSS/R.

## Frontend đã nối vào backend này
File `aa_frontend.html` gửi kèm giờ gọi thẳng API thật (không còn dùng localStorage giả lập). Mặc định trỏ vào `http://localhost:8000` — nếu backend chạy ở địa chỉ khác (VD sau khi deploy), mở file `aa_frontend.html`, thêm dòng sau vào ngay trước thẻ `<script id="app-source">`:
```html
<script>window.AA_API_BASE = "https://ten-app-cua-ban.onrender.com";</script>
```

## Các API khác
- POST /auth/login, GET /auth/me
- POST /patients, GET /patients/{ma_bn}
- GET /cases/{ma_bn}/aa — xem toàn bộ hồ sơ (bệnh án mới + mọi lần tái khám)
- GET /cases/recent — 8 ca cập nhật gần nhất
- POST /images/upload?ma_bn=... — tải ảnh WebP lên S3 (hoặc uploads/ khi test)
- GET /export/raw — (chỉ tài khoản can_export) toàn bộ dữ liệu dạng JSON, dùng để dựng file Excel phía trình duyệt

## Backup dữ liệu — 3 lớp
1. **RDS automated backup**: khi tạo RDS, đặt "Backup retention period" = 7-14 ngày → AWS tự chụp mỗi ngày, khôi phục về bất kỳ thời điểm nào trong khoảng đó.
2. **S3 Versioning**: bật Versioning cho bucket ảnh (Properties → Bucket Versioning → Enable) → ảnh bị ghi đè/xoá nhầm vẫn còn bản cũ.
3. **Backup độc lập ngoài AWS** (quan trọng nhất): chạy `python backup.py` — xuất toàn bộ MySQL ra file .sql.gz, lưu cục bộ (giữ 14 bản gần nhất) và tải lên 1 bucket S3 riêng (khai `BACKUP_S3_BUCKET` trong .env — nên khác bucket ảnh, tốt nhất khác cả tài khoản AWS). Đặt lịch chạy mỗi ngày bằng cron (Linux/macOS) hoặc Task Scheduler (Windows) — hướng dẫn chi tiết trong đầu file backup.py.

## Khôi phục khi có sự cố
- Dữ liệu sai/mất do thao tác nhầm gần đây → khôi phục RDS về thời điểm trước đó (point-in-time recovery) qua AWS Console
- Mất cả RDS (sự cố nặng) → tạo RDS mới, `mysql < clinic_YYYYMMDD.sql` từ bản backup gần nhất trong thư mục backups/ hoặc trên S3
- Ảnh bị xoá nhầm → vào S3 Console, bật hiển thị "Show versions", khôi phục lại phiên bản trước


render.com → New Web Service → kết nối repo chứa code này → khai biến môi trường như .env → Start command: uvicorn main:app --host 0.0.0.0 --port 10000
