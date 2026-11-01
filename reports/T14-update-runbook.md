# T14 – Runbook Vận Hành: Cập Nhật Dữ Liệu Tự Động (Automated Data Update)

> **Phiên bản:** 1.0  
> **Module:** T14 – Automated Data Update  
> **Dự án:** PetroTwin  
> **Cập nhật lần cuối:** 2026-11

---

## Mục lục

1. [Tổng quan hệ thống tự động](#1-tổng-quan-hệ-thống-tự-động)
2. [Các biến môi trường bắt buộc](#2-các-biến-môi-trường-bắt-buộc)
3. [Cài đặt cron tự động](#3-cài-đặt-cron-tự-động)
4. [Giám sát log tự động](#4-giám-sát-log-tự-động)
5. [Xử lý sự cố và tự phục hồi](#5-xử-lý-sự-cố-và-tự-phục-hồi)
6. [Xác minh cập nhật tự động thành công](#6-xác-minh-cập-nhật-tự-động-thành-công)
7. [Tham chiếu nhanh – Exit codes và trạng thái](#7-tham-chiếu-nhanh)

---

## 1. Tổng quan hệ thống

Module T14 tự động thu thập dữ liệu từ 3 nguồn và đưa vào pipeline xử lý:

```
┌─────────────────────────────────────────────────────┐
│                  Cron scheduler                     │
└───────┬──────────────┬──────────────┬───────────────┘
        ▼              ▼              ▼
   brent.py         fx.py     domestic_fuel.py
  (FRED/EIA)    (Vietcombank)     (PVOIL)
        │              │              │
        └──────────────┴──────────────┘
                       ▼
              data/raw/*.csv  (append-only)
                       ▼
           petrotwin.data.pipeline
                       ▼
           petrotwin.data.mysql_loader
                       ▼
                  MySQL / DB
```

| Collector | Nguồn dữ liệu | Tần suất | File CSV đích |
|-----------|--------------|----------|---------------|
| `brent.py` | FRED / EIA (series DCOILBRENTEU) | Hàng ngày (06:00 ICT) | `data/raw/brent_crude_prices.csv` |
| `fx.py` | Vietcombank API → SBV central rate | Ngày làm việc (09:30 ICT) | `data/raw/usd_vnd_rates.csv` |
| `domestic_fuel.py` | PVOIL website scrape | Thứ Năm 15:05–15:30 ICT | `data/raw/data_price_petro.csv` |

**Runner tổng hợp:** `runner.py` chạy cả 3 collector, sau đó pipeline và DB loader (10:00 ICT hàng ngày).

---

## 2. Các biến môi trường bắt buộc

Lưu các biến trong file `/etc/petrotwin/env` (chmod 600):

```bash
# ── Bắt buộc cho DB loader ──────────────────────────────
PETROTWIN_DB_HOST=your_mysql_host          # bắt buộc để loader chạy
PETROTWIN_DB_USER=petrotwin
PETROTWIN_DB_PASSWORD=your_db_password
PETROTWIN_DB_NAME=petrotwin

# ── Tùy chọn ────────────────────────────────────────────
FRED_API_KEY=your_fred_api_key             # nếu không có: dùng text download (không cần đăng ký)
PETROTWIN_DATA_DIR=/custom/path/data/raw   # mặc định: <project_root>/data/raw
```

> [!IMPORTANT]
> Nếu `PETROTWIN_DB_HOST` **không được set**, bước `mysql_loader` sẽ bị **bỏ qua tự động** (status = `no_db_config`). Đây không phải lỗi.

> [!CAUTION]
> **Không commit** file `/etc/petrotwin/env` vào git. File này chứa credentials nhạy cảm.

---

## 3. Cài đặt cron tự động trên Server

Hệ thống hoạt động hoàn toàn tự động thông qua crontab trên Linux. Các script collector và pipeline không cần thao tác thủ công.

### 3.1 Thiết lập trên Ubuntu/Debian

```bash
# 1. Tạo thư mục và file env (chỉ làm 1 lần khi triển khai)
sudo mkdir -p /etc/petrotwin
sudo nano /etc/petrotwin/env   # điền các biến ở mục 2
sudo chmod 600 /etc/petrotwin/env

# 2. Tạo thư mục logs
mkdir -p /home/petrotwin/PetroTwin-/logs

# 3. Kích hoạt crontab của service user
sudo -u petrotwin crontab -e
```
sudo nano /etc/petrotwin/env   # điền các biến ở mục 2
sudo chmod 600 /etc/petrotwin/env

# 2. Tạo thư mục logs
mkdir -p /home/petrotwin/PetroTwin-/logs

# 3. Mở crontab của service user
sudo -u petrotwin crontab -e
```

### 3.2 Nội dung crontab chuẩn

Xem file đầy đủ tại: [`scripts/crontab_example.txt`](../scripts/crontab_example.txt)

Tóm tắt lịch tự động (ICT = UTC+7):

| Job | Lịch ICT | Lịch UTC | Cron expression |
|-----|----------|----------|-----------------|
| Brent crude | 06:00 hàng ngày | 23:00 UTC (hôm trước) | `0 23 * * *` |
| FX rate | 09:30 ngày làm việc | 02:30 UTC | `30 2 * * 1-5` |
| Fuel price (poll) | 15:05–15:30 Thứ Năm | 08:05–08:30 UTC Thứ Năm | `5,10,15,20,25,30 8 * * 4` |
| Full runner | 10:00 hàng ngày | 03:00 UTC | `0 3 * * *` |

### 3.3 Log rotation tự động

Tạo file `/etc/logrotate.d/petrotwin`:

```
/home/petrotwin/PetroTwin-/logs/*.log {
    daily
    rotate 30
    compress
    missingok
    notifempty
    create 640 petrotwin petrotwin
}
```

---

## 4. Giám sát log tự động

### 5.1 Xem log theo thời gian thực

```bash
# Runner log (tổng hợp tất cả collector + pipeline)
tail -f /home/petrotwin/PetroTwin-/logs/runner.log

# Log từng collector
tail -f /home/petrotwin/PetroTwin-/logs/brent.log
tail -f /home/petrotwin/PetroTwin-/logs/fx.log
tail -f /home/petrotwin/PetroTwin-/logs/domestic_fuel.log
```

### 5.2 Xem structured log của runner

File `logs/collector_run.log` chứa một JSON object mỗi dòng:

```jsonc
{
  "run_at_utc": "2026-11-01T03:00:05Z",
  "brent_new_rows": 1,
  "fx_new_rows": 1,
  "fuel_new_rows": 10,
  "pipeline_status": "ok",
  "loader_status": "ok",
  "error": null
}
```

**Đọc 10 lần chạy gần nhất:**

```bash
tail -10 logs/collector_run.log | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if line:
        obj = json.loads(line)
        print(obj['run_at_utc'],
              'brent='+str(obj['brent_new_rows']),
              'fx='+str(obj['fx_new_rows']),
              'fuel='+str(obj['fuel_new_rows']),
              'pipeline='+obj['pipeline_status'],
              'loader='+obj['loader_status'],
              'err='+str(obj['error']))
"
```

### 5.3 Kiểm tra lỗi nhanh

```bash
# Tìm các lần chạy có lỗi
grep '"error": [^n]' logs/collector_run.log

# Tìm pipeline failed
grep '"pipeline_status": "failed"' logs/collector_run.log
```

### 5.4 Các event quan trọng trong log collector

Mỗi collector ghi JSON log ra stdout. Các `event` cần chú ý:

| Event | Ý nghĩa |
|-------|---------|
| `collector_start` | Bắt đầu thu thập |
| `fetch_start` | Bắt đầu gọi HTTP |
| `fetch_done` | HTTP thành công |
| `fetch_error` | **LỖI** gọi HTTP |
| `parse_error` | **LỖI** phân tích dữ liệu (domestic_fuel) |
| `up_to_date` | Không có dữ liệu mới – bình thường |
| `skip_weekend` | Bỏ qua cuối tuần – bình thường |
| `collector_done` | Hoàn thành, `new_rows` = số hàng mới |
| `write_error` | **LỖI** ghi file CSV |

---

## 5. Xử lý sự cố và tự phục hồi

### 5.1 `brent.py` thất bại

**Triệu chứng:** `"event": "fetch_error"` trong log, hoặc exit code 1.

**Nguyên nhân phổ biến:**
- FRED API timeout → tăng `request_timeout_seconds` trong `configs/collectors.yaml`
- `FRED_API_KEY` sai → kiểm tra key tại [fred.stlouisfed.org](https://fred.stlouisfed.org/) (hoặc xóa key để hệ thống tự động fallback sang text download)
- Mất kết nối internet trên server

**Cơ chế tự phục hồi:**
- Runner tự động retry theo số lần cấu hình trong `collectors.yaml`.
- Nếu vẫn thất bại, hệ thống dừng an toàn mà không làm hỏng dữ liệu hiện có; scheduler sẽ chạy lại ở chu kỳ tiếp theo.

> [!NOTE]
> FRED không có dữ liệu cho ngày cuối tuần và ngày lễ Mỹ. Khi log ghi `up_to_date` vào những ngày này là **hoàn toàn bình thường**.

---

### 5.2 `fx.py` thất bại

**Triệu chứng:** `"fetch_primary_failed"` và `"fetch_fallback_failed"` trong log.

**Nguyên nhân phổ biến:**
- API Vietcombank (`vapi.vnappmob.com`) bảo trì → script tự động kích hoạt fallback sang XML portal của Vietcombank.
- Cả hai endpoint đều không phản hồi.

**Cơ chế tự phục hồi:**
- Tự động bỏ qua Thứ Bảy, Chủ Nhật.
- Nếu cả 2 API đều gián đoạn, runner ghi log lỗi và giữ nguyên snapshot hiện tại, đợi chu kỳ lập lịch kế tiếp để thử lại tự động.

---

### 5.3 `domestic_fuel.py` thất bại

**Triệu chứng:** `"parse_error"` trong log — website PVOIL có thể đã thay đổi cấu trúc HTML.

**Cơ chế tự bảo vệ an toàn (Fail-Safe):**
- Collector áp dụng nguyên tắc fail-safe nghiêm ngặt: nếu không phân tích được dữ liệu hợp lệ, script lập tức dừng và exit code 1.
- **Tuyệt đối không ghi dữ liệu rác, không ghi hàng trống hoặc thiếu trường vào CSV**.
- Pipeline và database pointer giữ nguyên trạng thái hợp lệ trước đó, API và Dashboard không bị ảnh hưởng.
- Sau khi cập nhật lại parser trong `domestic_fuel.py`, hệ thống tự động tiếp tục chu kỳ cào ở lần chạy tiếp theo.

---

### 5.4 Pipeline thất bại

**Triệu chứng:** `"pipeline_status": "failed"` trong `collector_run.log`.

**Nguyên nhân:**
- Vi phạm kiểm định chất lượng (schema mismatch, duplicate conflict, giá trị ngoài phạm vi cho phép).

**Cơ chế an toàn:**
- Pipeline tự động dọn dẹp thư mục staging tạm và không cập nhật `current.json`.

---

### 5.5 Loader (MySQL) thất bại

**Triệu chứng:** `"loader_status": "failed"` trong `collector_run.log`.

**Nguyên nhân:**
- Mất kết nối tới database hoặc sai thông tin xác thực trong `/etc/petrotwin/env`.

**Cơ chế an toàn:**
- Loader chạy trong một transaction duy nhất. Nếu có lỗi, transaction tự động rollback 100%, con trỏ `dataset_current_pointer` giữ nguyên tại run hợp lệ gần nhất.

---

## 6. Xác minh cập nhật tự động thành công

### 6.1 Kiểm tra nhanh – dòng cuối của mỗi CSV

```bash
cd /home/petrotwin/PetroTwin-

# Brent: dòng cuối (ngày gần nhất)
tail -1 data/raw/brent_crude_prices.csv

# FX: dòng cuối
tail -1 data/raw/usd_vnd_rates.csv

# Fuel: số dòng mới nhất
tail -5 data/raw/data_price_petro.csv
```

### 6.2 Kiểm tra ngày cuối trong mỗi file

```bash
python3 - <<'EOF'
import csv
from pathlib import Path

files = {
    "brent": "data/raw/brent_crude_prices.csv",
    "fx":    "data/raw/usd_vnd_rates.csv",
    "fuel":  "data/raw/data_price_petro.csv",
}
for name, path in files.items():
    p = Path(path)
    if not p.exists():
        print(f"{name}: FILE NOT FOUND")
        continue
    dates = []
    with p.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = row.get("date", "").strip()
            if d:
                dates.append(d)
    last = max(dates) if dates else "NO DATA"
    print(f"{name}: last_date={last}  rows={len(dates)}")
EOF
```

### 6.3 Kiểm tra run log cuối

```bash
# Lần chạy gần nhất của runner
tail -1 logs/collector_run.log | python3 -m json.tool
```

**Kết quả thành công trông như thế này:**
```json
{
  "run_at_utc": "2026-11-06T03:00:07Z",
  "brent_new_rows": 1,
  "fx_new_rows": 1,
  "fuel_new_rows": 10,
  "pipeline_status": "ok",
  "loader_status": "ok",
  "error": null
}
```

### 6.4 Kiểm tra run folder pipeline được tạo

```bash
# Pipeline tạo run folder trong data/runs/ (immutable, timestamped)
ls -lt data/runs/ | head -5
```

---

## 7. Tham chiếu nhanh

### Exit codes

| Code | Ý nghĩa |
|------|---------|
| `0` | Thành công (kể cả "already up to date") |
| `1` | Lỗi: HTTP error, parse error, hoặc write error |

### Trạng thái trong `collector_run.log`

| Trường | Giá trị | Ý nghĩa |
|--------|---------|---------|
| `pipeline_status` | `"ok"` | Pipeline chạy thành công |
| | `"failed"` | Pipeline báo lỗi |
| | `"skipped"` | Bỏ qua (collector lỗi hoặc dry-run) |
| `loader_status` | `"ok"` | Loader chạy thành công |
| | `"failed"` | Loader báo lỗi |
| | `"skipped"` | Bỏ qua |
| | `"no_db_config"` | `PETROTWIN_DB_HOST` chưa set – bình thường |

### Cấu trúc file quan trọng

```
PetroTwin-/
├── configs/collectors.yaml          # Cấu hình collectors
├── data/raw/
│   ├── brent_crude_prices.csv       # Append-only (Brent)
│   ├── usd_vnd_rates.csv            # Append-only (FX)
│   └── data_price_petro.csv         # Append-only (Fuel)
├── data/runs/                       # Immutable pipeline run folders
├── logs/
│   ├── collector_run.log            # Structured JSON log (runner)
│   ├── brent.log                    # Stdout từ cron job Brent
│   ├── fx.log                       # Stdout từ cron job FX
│   └── domestic_fuel.log            # Stdout từ cron job Fuel
├── scripts/crontab_example.txt      # Template crontab
└── src/petrotwin/collectors/
    ├── brent.py
    ├── fx.py
    ├── domestic_fuel.py
    └── runner.py
```

---

*Runbook này được tạo tự động bởi Antigravity agent cho module T14. Cập nhật khi có thay đổi về nguồn dữ liệu hoặc cấu trúc hệ thống.*
