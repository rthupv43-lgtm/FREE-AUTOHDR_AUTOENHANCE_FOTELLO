# T&T - Real Estate Photo Processing Tool

Automated multi-threaded HDR real estate photo upload and processing desktop application (supporting **AutoHDR**, **Autoenhance**, and **Fotello**).

## Key Features
- **Multi-threaded Upload & Enhance**: Automatically converts RAW photos (`.ARW`, `.CR2`, `.CR3`, `.NEF`, `.DNG`, etc.) and optimizes file size prior to uploading.
- **Full-Resolution Clean Downloads**: Automatically downloads processed images without watermarks while preserving original dimensions (`300 DPI`).
- **Project History & Batch Download**: Preview and re-download previously processed listings and orders in parallel.

## Pre-built Standalone Executable (`.exe`)
1. Go to the **[Releases](https://github.com/rthupv43-lgtm/FREE-AUTOHDR_AUTOENHANCE_FOTELLO/releases)** section on the right side of this GitHub repository (or click **[Direct Download T&T.exe](https://github.com/rthupv43-lgtm/FREE-AUTOHDR_AUTOENHANCE_FOTELLO/releases/download/v1/T.T.exe)**).
2. Run **`T&T.exe`** directly on Windows 10/11 (64-bit) - no Python installation required.

## Running from Source (For Developers)

### 1. Install Dependencies
Requires **Python 3.12+**:
```bash
pip install -r requirements.txt
```

### 2. Run the Application
```bash
python main.py
```

### 3. Build Standalone `.exe`
```bash
pyinstaller "T&T.spec" --noconfirm
```
