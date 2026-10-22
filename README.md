# T&T - Real Estate Photo Processing Tool

Công cụ tự động Upload & Xử lý ảnh HDR bất động sản (hỗ trợ **AutoHDR**, **Autoenhance**, **Fotello**).

## Tính năng chính
- **Upload & Enhance đa luồng**: Tự động chuyển đổi ảnh RAW (`.ARW`, `.CR2`, `.CR3`, `.NEF`, `.DNG`...) và nén tối ưu trước khi tải lên.
- **Tải ảnh chất lượng gốc (Full Resolution)**: Tự động tải ảnh đã xử lý không dính watermark, giữ nguyên độ phân giải gốc (`300 DPI`).
- **Quản lý & Tải Listings cũ**: Xem trước và tải lại các dự án đã xử lý trước đó.

## Cách sử dụng bản đóng gói sẵn (.exe)
1. Vào mục **Releases** ở cột bên phải trang GitHub này.
2. Tải file **`T&T.exe`** về máy tính (Windows 10/11 64-bit) và mở chạy trực tiếp (không cần cài đặt Python).

## Cách chạy từ mã nguồn (Dành cho lập trình viên)

### 1. Cài đặt thư viện
Yêu cầu **Python 3.12+**:
```bash
pip install -r requirements.txt
```

### 2. Chạy ứng dụng
```bash
python main.py
```

### 3. Đóng gói ra file `.exe`
```bash
pyinstaller "T&T.spec" --noconfirm
```

