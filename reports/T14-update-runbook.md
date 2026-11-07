# T14 – Runbook Vận Hành: Cập Nhật Dữ Liệu Tự Động (Automated Data Update)

> **Phiên bản:** 2.0
> **Module:** T14 – Automated Data Update & Quality Gates
> **Dự án:** PetroTwin
> **Nhánh thực thi:** `dmin-data`
> **Cập nhật lần cuối:** 2026-11

---

## Mục lục

1. [Tổng quan kiến trúc automation](#1-tổng-quan-kiến-trúc-automation)
2. [Điều kiện môi trường](#2-điều-kiện-môi-trường)
3. [Cài đặt triển khai trên máy chủ Linux sạch](#3-cài-đặt-triển-khai-trên-máy-chủ-linux-sạch)
4. [Cấu hình Collectors (`configs/collectors.yaml`)](#4-cấu-hình-collectors)
5. [Cấu hình MySQL Database](#5-cấu-hình-mysql-database)
6. [Hướng dẫn chạy thủ công từng Collector](#6-hướng-dẫn-chạy-thủ-công-từng-collector)
7. [Hướng dẫn chạy thủ công Pipeline](#7-hướng-dẫn-chạy-thủ-công-pipeline)
8. [Hướng dẫn chạy Orchestration Runner](#8-hướng-dẫn-chạy-orchestration-runner)
9. [Cấu hình Lập lịch tự động (Cron & Flock)](#9-cấu-hình-lập-lịch-tự-động-cron--flock)
10. [Kiểm tra và nghiệm thu Automation](#10-kiểm-tra-và-nghiệm-thu-automation)
11. [Đọc và phân tích Log vận hành](#11-đọc-và-phân-tích-log-vận-hành)
12. [Xử lý lỗi thường gặp theo từng thành phần](#12-xử-lý-lỗi-thường-gặp-theo-từng-thành-phần)
13. [Quy trình khôi phục sau sự cố (Disaster Recovery)](#13-quy-trình-khôi-phục-sau-sự-cố)
14. [Các giới hạn kỹ thuật còn tồn tại](#14-các-giới-hạn-kỹ-thuật-còn-tồn-tại)

---

## 1. Tổng quan kiến trúc automation

Hệ thống tự động hóa T14 đảm bảo toàn bộ chu trình nạp, xử lý và kiểm định dữ liệu vận hành khép kín, chống rò rỉ thông tin (leakage), bất biến (immutable), và có cơ chế cách ly lỗi:

```
┌────────────────────────────────────────────────────────────────────────┐
│                   Hệ thống Lập lịch Cron (Linux)                       │
│              (flock bảo vệ chống xung đột đa tiến trình)               │
└────────┬───────────────────────┬───────────────────────┬───────────────┘
         │ 06:00 ICT             │ 09:30 ICT (T2-T6)     │ 15:05-15:30 (Thứ 5)
         ▼                       ▼                       ▼
    brent.py                   fx.py              domestic_fuel.py
 (FRED/EIA API & CSV)     (Algolab SBV proxy)     (PVOIL retail scrape)
         │                       │                       │
         └───────────────────────┼───────────────────────┘
                                 ▼
                     data/raw/*.csv (Append-only)
                                 ▼
               10:00 ICT (T2-T4, T6-CN) / 16:35 ICT (T5)
                     petrotwin.collectors.runner
                                 │
                     ┌───────────┴───────────┐
                     ▼                       ▼
          petrotwin.data.pipeline     petrotwin.data.mysql_loader
          (Schema, Leakage, Panel)    (Idempotent DB ingestion)
                     │                       │
                     ▼                       ▼
           data/processed/runs/<id>     MySQL Target Tables
           (current.json pointer)    (dataset_current_pointer)
```

### Các luồng dữ liệu chính:

| Module Collector | Nguồn dữ liệu | Tần suất & Thời điểm | Tính chất nguồn | File CSV thô |
|---|---|---|---|---|
| `brent.py` | FRED / EIA (`DCOILBRENTEU`) | Hàng ngày 06:00 ICT | **Bắt buộc** | `data/raw/brent_crude_prices.csv` |
| `fx.py` | Algolab (`algolab_sbv_central_rate`) | Ngày làm việc 09:30 ICT | **Tùy chọn** (`optional: true`) | `data/raw/usd_vnd_rates.csv` |
| `domestic_fuel.py` | PVOIL công bố giá bán lẻ | Thứ Năm 15:05–15:30 ICT | **Bắt buộc** | `data/raw/data_price_petro.csv` |

---

## 2. Điều kiện môi trường

- **Hệ điều hành:** Linux x86_64 / aarch64 (Ubuntu 22.04 LTS / Debian 12 khuyến nghị).
- **Python:** Phiên bản `>= 3.11` (khuyến nghị Python 3.11 hoặc 3.12).
- **Tiện ích hệ thống:** `cron`, `util-linux` (cung cấp lệnh `flock`), `logrotate`, `curl`.
- **Database (Tùy chọn):** MySQL 8.0+ / MariaDB 10.6+ nếu cần tải dữ liệu vào kho lưu trữ cơ sở dữ liệu.
- **Quyền hạn:** User dịch vụ (ví dụ `petrotwin`) có quyền đọc/ghi trong thư mục project và quyền đọc file cấu hình an toàn `/etc/petrotwin/env`.

---

## 3. Cài đặt triển khai trên máy chủ Linux sạch

Dưới đây là các bước thiết lập từ đầu trên một máy chủ Ubuntu/Debian mới:

### Bước 1: Tạo user dịch vụ và cài đặt gói hệ điều hành
```bash
sudo adduser --disabled-password --gecos "" petrotwin
sudo apt-get update && sudo apt-get install -y \
    python3 python3-venv python3-pip git cron util-linux logrotate curl
```

### Bước 2: Clone repository và tạo môi trường ảo
```bash
sudo -u petrotwin git clone -b dmin-data https://github.com/dmin/PetroTwin-.git /home/petrotwin/PetroTwin-
cd /home/petrotwin/PetroTwin-
sudo -u petrotwin python3 -m venv /home/petrotwin/.venv
sudo -u petrotwin /home/petrotwin/.venv/bin/pip install --upgrade pip
sudo -u petrotwin /home/petrotwin/.venv/bin/pip install -e ".[collectors,mysql]"
```

### Bước 3: Tạo thư mục log và phân quyền
```bash
sudo -u petrotwin mkdir -p /home/petrotwin/PetroTwin-/logs
sudo -u petrotwin mkdir -p /home/petrotwin/PetroTwin-/data/interim/logs
```

### Bước 4: Tạo file biến môi trường bảo mật
```bash
sudo mkdir -p /etc/petrotwin
sudo tee /etc/petrotwin/env > /dev/null <<'EOF'
# API Keys
FRED_API_KEY=

# Database Configuration (để trống nếu chưa dùng MySQL)
PETROTWIN_DB_HOST=127.0.0.1
PETROTWIN_DB_PORT=3306
PETROTWIN_DB_USER=petrotwin
PETROTWIN_DB_PASSWORD=your_secure_password
PETROTWIN_DB_NAME=petrotwin

# Optional Overrides
# PETROTWIN_DATA_DIR=/home/petrotwin/PetroTwin-/data/raw
EOF
sudo chmod 600 /etc/petrotwin/env
sudo chown -R petrotwin:petrotwin /etc/petrotwin
```

---

## 4. Cấu hình Collectors

File cấu hình tập trung đặt tại [`configs/collectors.yaml`](../configs/collectors.yaml):

```yaml
brent:
  source_id: eia_fred
  series_id: DCOILBRENTEU
  source_url: https://fred.stlouisfed.org/data/DCOILBRENTEU
  publisher: "U.S. Energy Information Administration (EIA), distributed by FRED"
  license_reuse_terms: "unknown; verify with publisher"
  request_timeout_seconds: 30
  retry_count: 3
  retry_delay_seconds: 5

fx:
  source_id: algolab_sbv_central_rate
  source_name: Algolab
  source_status: enabled
  source_url: https://algolab.vn/macro/vn-fx-rate-ty-gia-sbv-trung-tam
  upstream_source: State Bank of Vietnam
  provenance_status: provider_declared
  rate_type: "SBV central reference rate"
  optional: true
  max_age_days: 7
  license_reuse_terms: "unknown; verify with publisher"
  request_timeout_seconds: 15
  retry_count: 3
  retry_delay_seconds: 5

domestic_fuel:
  source_id: pvoil
  source_url: https://pvoil.com.vn/tin-gia-xang-dau
  license_reuse_terms: "unknown; verify with publisher"
  request_timeout_seconds: 30
  retry_count: 2
  retry_delay_seconds: 10
  default_effective_time: "15:00"

runner:
  log_dir: logs
  log_filename: collector_run.log
  pipeline_module: petrotwin.data.pipeline
  loader_module: petrotwin.data.mysql_loader
```

- **Tính chất `optional: true` của FX:** Vì nguồn Algolab là nguồn proxy do nhà cung cấp tự công bố (`provider_declared`), khi nguồn FX gặp lỗi hoặc mạng timeout, pipeline vẫn tiếp tục xử lý các nguồn giá bắt buộc và ghi nhận trạng thái `validated_with_optional_source_missing`. Nếu file FX tồn tại nhưng dữ liệu bị cũ vượt quá `max_age_days` (đọc từ cấu hình collectors, mặc định 7 ngày), pipeline loại bỏ dữ liệu FX cũ không tích hợp vào bảng thị trường hay panel và ghi nhận trạng thái `validated_with_optional_source_stale` cùng thông tin metadata (`latest_observation_date`, `age_days`, `max_age_days`).
- **Nếu muốn bắt buộc FX:** Đổi `optional: false` trong `configs/collectors.yaml`. Khi đó pipeline sẽ fail-closed (báo `PipelineError`) nếu thiếu file, thiếu dữ liệu, hoặc dữ liệu FX bị cũ quá `max_age_days`.

---

## 5. Cấu hình MySQL Database

Nếu cấu hình nạp dữ liệu tự động vào cơ sở dữ liệu:
1. Đảm bảo MySQL server đã khởi động và user `petrotwin` có đủ quyền `SELECT, INSERT, UPDATE, CREATE, DROP` trên database `petrotwin`.
2. Kiểm tra kết nối và nạp thử:
   ```bash
   . /etc/petrotwin/env && /home/petrotwin/.venv/bin/python -m petrotwin.data.mysql_loader --check
   ```
3. Nếu biến môi trường `PETROTWIN_DB_HOST` chưa được thiết lập, loader sẽ tự động bỏ qua an toàn với trạng thái `no_db_config` mà không gây lỗi runner.

---

## 6. Hướng dẫn chạy thủ công từng Collector

Bạn có thể chạy thử bất kỳ collector nào từ thư mục gốc của repository:

```bash
cd /home/petrotwin/PetroTwin-
source /home/petrotwin/.venv/bin/activate
source /etc/petrotwin/env

# 1. Thu thập Brent
python -m petrotwin.collectors.brent

# 2. Thu thập Tỷ giá FX
python -m petrotwin.collectors.fx

# 3. Thu thập Giá xăng lẻ PVOIL
python -m petrotwin.collectors.domestic_fuel
```

*Mỗi collector đều có tính idempotent: nếu dữ liệu ngày hiện tại đã tồn tại trong CSV, script tự động ghi log `up_to_date` và thoát an toàn với mã 0 mà không ghi trùng dữ liệu.*

---

## 7. Hướng dẫn chạy thủ công Pipeline

Pipeline thực hiện kiểm định schema, kiểm tra ràng buộc rò rỉ (brent date < fuel event date), làm sạch, gắn nhãn quỹ bình ổn và tạo run bất biến mới:

```bash
python -m petrotwin.data.pipeline
```

Để chỉ định file cấu hình collectors tùy biến:
```bash
python -m petrotwin.data.pipeline --collectors-config configs/collectors.yaml
```
*(Nếu truyền `--collectors-config` tới file không tồn tại, pipeline sẽ báo lỗi `PipelineError` và dừng lại ngay lập tức).*

---

## 8. Hướng dẫn chạy Orchestration Runner

Runner điều phối tuần tự: chạy các collector → chạy pipeline → chạy loader (nếu có DB).

### Các chế độ thực thi của Runner:

1. **Chế độ mặc định (Full run):**
   ```bash
   python -m petrotwin.collectors.runner
   ```
   Chạy toàn bộ chu trình: gọi cả 3 collectors (`brent` → `fx` → `domestic_fuel`), sau đó chạy pipeline kiểm định và nạp MySQL nếu có cấu hình DB. Thích hợp cho vận hành thủ công hoặc kiểm tra toàn diện một lệnh duy nhất.

2. **Chế độ chỉ chạy Pipeline & Loader (`--pipeline-only`):**
   ```bash
   python -m petrotwin.collectors.runner --pipeline-only
   ```
   Bỏ qua việc gọi lại collectors; chỉ chạy kiểm tra pipeline, chuẩn hóa panel dữ liệu và nạp MySQL từ các file raw CSV hiện hành. **Đây là chế độ được khuyến nghị cho Cron Jobs 4a & 4b**, vì các collector đã được lên lịch chạy độc lập vào đúng thời điểm công bố của từng nguồn (06:00, 09:30, 15:05-15:30), tránh việc thu thập lặp lại tài nguyên mạng.

3. **Chế độ cào thử nghiệm (`--dry-run`):**
   ```bash
   python -m petrotwin.collectors.runner --dry-run
   ```
   Chỉ chạy cào dữ liệu raw từ các nguồn, bỏ qua bước chạy pipeline và DB loader. Dùng khi muốn thu thập dữ liệu thô mà chưa muốn đóng gói publication snapshot hay nạp DB.

4. **Chỉ định cấu hình collectors tùy biến (`--config`):**
   ```bash
   python -m petrotwin.collectors.runner --config /path/to/custom_collectors.yaml
   ```

**Quy tắc trạng thái trả về (Exit Codes):**
- **Exit code 0:** Thành công toàn bộ (`success`) hoặc chỉ lỗi collector tùy chọn (`partial`) trong khi pipeline và loader hoàn thành xuất sắc.
- **Exit code 1:** Thất bại khi một collector bắt buộc gặp lỗi, hoặc pipeline/loader gặp lỗi, hoặc trả về trạng thái không được phép.
- **Exit code 2:** Lỗi cú pháp tham số hoặc không đọc được file cấu hình.

---

## 9. Cấu hình Lập lịch tự động (Cron & Lock Đồng Bộ)

### 9.1 Bật dịch vụ cron
```bash
sudo systemctl enable cron
sudo systemctl start cron
```

### 9.2 Cơ chế File Lock dùng chung (Cross-process File Locking)
Hệ thống sử dụng module `petrotwin.collectors.locks` thực thi khóa mức hạt nhân `fcntl.flock` trên các file nằm trong thư mục `logs/locks/`:
- `brent.lock`: Bảo vệ collector Brent crude.
- `fx.lock`: Bảo vệ collector tỷ giá USD/VND.
- `domestic_fuel.lock`: Bảo vệ collector giá xăng bán lẻ.
- `runner.lock`: Bảo vệ orchestration runner toàn cục.
- `pipeline.lock`: Bảo vệ tiến trình pipeline và publication snapshot.

Cơ chế này đảm bảo dù tác vụ được kích hoạt bởi cron, runner subprocess, hay người vận hành gõ lệnh thủ công trong terminal, nếu một tiến trình đang xử lý thì tiến trình thứ hai sẽ phát hiện `lock_busy` và thoát an toàn ngay lập tức, không gây ghi đè hay hỏng dữ liệu.

### 9.3 Cài đặt file crontab
Mở crontab của service user:
```bash
sudo -u petrotwin crontab -e
```
Dán nội dung chuẩn từ [`scripts/crontab_example.txt`](../scripts/crontab_example.txt):

```cron
SHELL=/bin/bash
BASH_ENV=/etc/petrotwin/env
PATH=/home/petrotwin/.venv/bin:/usr/local/bin:/usr/bin:/bin
CRON_TZ=Asia/Ho_Chi_Minh

# JOB 1 — Brent Crude hàng ngày lúc 06:00 ICT (T+1 sau thị trường quốc tế)
0 6 * * * set -a && . /etc/petrotwin/env && set +a && mkdir -p /home/petrotwin/PetroTwin-/logs/locks && cd /home/petrotwin/PetroTwin- && /home/petrotwin/.venv/bin/python -m petrotwin.collectors.brent >> /home/petrotwin/PetroTwin-/logs/brent.log 2>&1

# JOB 2 — FX Rate ngày làm việc (T2–T6) lúc 09:30 ICT (sau khi NHNN công bố tỷ giá trung tâm)
30 9 * * 1-5 set -a && . /etc/petrotwin/env && set +a && mkdir -p /home/petrotwin/PetroTwin-/logs/locks && cd /home/petrotwin/PetroTwin- && /home/petrotwin/.venv/bin/python -m petrotwin.collectors.fx >> /home/petrotwin/PetroTwin-/logs/fx.log 2>&1

# JOB 3 — Domestic Fuel Polling Thứ Năm mỗi 5 phút (15:05–15:30 ICT)
5-30/5 15 * * 4 set -a && . /etc/petrotwin/env && set +a && mkdir -p /home/petrotwin/PetroTwin-/logs/locks && cd /home/petrotwin/PetroTwin- && /home/petrotwin/.venv/bin/python -m petrotwin.collectors.domestic_fuel >> /home/petrotwin/PetroTwin-/logs/domestic_fuel.log 2>&1

# JOB 4a — Pipeline & Loader ngày thường (CN, T2–T4, T6–T7 lúc 10:00 ICT)
0 10 * * 0,1,2,3,5,6 set -a && . /etc/petrotwin/env && set +a && mkdir -p /home/petrotwin/PetroTwin-/logs/locks && cd /home/petrotwin/PetroTwin- && /home/petrotwin/.venv/bin/python -m petrotwin.collectors.runner --pipeline-only >> /home/petrotwin/PetroTwin-/logs/runner.log 2>&1

# JOB 4b — Pipeline & Loader Thứ Năm sau kỳ điều hành giá (16:35 ICT)
35 16 * * 4 set -a && . /etc/petrotwin/env && set +a && mkdir -p /home/petrotwin/PetroTwin-/logs/locks && cd /home/petrotwin/PetroTwin- && /home/petrotwin/.venv/bin/python -m petrotwin.collectors.runner --pipeline-only >> /home/petrotwin/PetroTwin-/logs/runner.log 2>&1
```

> [!NOTE]
> - Cú pháp `set -a && . /etc/petrotwin/env && set +a` đảm bảo tất cả các biến cấu hình (như thông tin kết nối MySQL) được export hoàn toàn vào môi trường của các tiến trình con Python.
> - Chế độ `--pipeline-only` trong các Job 4a & 4b đảm bảo các nguồn dữ liệu không bị thu thập lặp vô ích sau khi các job riêng lẻ 1, 2, 3 đã hoàn thành.
> - Nếu Linux cron của máy chủ không hỗ trợ `CRON_TZ`, hãy cấu hình timezone hệ thống qua `sudo timedatectl set-timezone Asia/Ho_Chi_Minh`.

---

## 10. Kiểm tra và nghiệm thu Automation

### 10.1 Kiểm tra cron có thực sự chạy
```bash
# Xem log hệ thống của cron trên Ubuntu
sudo grep -i CRON /var/log/syslog | tail -20
# hoặc trên Debian / systemd
sudo journalctl -u cron -n 30 --no-pager
```

### 10.2 Kiểm tra dữ liệu thô mới cập nhật
```bash
# Kiểm tra dòng mới nhất trong các file CSV
tail -n 2 data/raw/brent_crude_prices.csv
tail -n 2 data/raw/usd_vnd_rates.csv
tail -n 10 data/raw/data_price_petro.csv
```

### 10.3 Kiểm tra snapshot publication của Pipeline
```bash
# Kiểm tra con trỏ publication hiện hành
cat data/processed/current.json
```

---

## 11. Đọc và phân tích Log vận hành

File `logs/collector_run.log` lưu nhật ký chạy JSON structured log mỗi lần runner được gọi:

```json
{
  "run_at_utc": "2026-11-06T03:00:07Z",
  "brent_new_rows": 1,
  "fx_new_rows": 1,
  "fuel_new_rows": 10,
  "pipeline_status": "ok",
  "loader_status": "no_db_config",
  "optional_collectors_failed": [],
  "error": null,
  "overall_status": "success"
}
```

**Các trường cần lưu ý:**
- `overall_status`: `"success"` (tất cả ok), `"partial"` (chỉ lỗi nguồn optional FX nhưng pipeline ok), `"failed"` (lỗi nguồn bắt buộc hoặc lỗi pipeline).
- `pipeline_status`: `"ok"`, `"skipped"`, hoặc `"failed"`.
- `loader_status`: `"ok"`, `"no_db_config"`, `"skipped"`, hoặc `"failed"`.
- `optional_collectors_failed`: Danh sách tên collector optional thất bại (ví dụ `["fx"]`).

---

## 12. Xử lý lỗi thường gặp theo từng thành phần

### 12.1 Brent collector gặp lỗi (`fetch_error`)
- **Nguyên nhân:** Lỗi mạng hoặc FRED rate limit.
- **Xử lý:** Kiểm tra kết nối internet bằng `curl -I https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILBRENTEU`. Nếu dùng `FRED_API_KEY`, kiểm tra key có bị hết hạn hay không.

### 12.2 FX collector gặp lỗi (`source_unavailable` hoặc `fetch_error`)
- **Nguyên nhân:** Trang Algolab thay đổi cấu trúc hoặc không truy cập được.
- **Xử lý:** Chạy kiểm tra riêng `python -m petrotwin.collectors.fx`. Vì FX là `optional: true`, hệ thống vẫn tiếp tục vận hành an toàn. Nếu cần khôi phục, kiểm tra cấu trúc thẻ HTML của Algolab và cập nhật `_parse_algolab_html`.

### 12.3 PVOIL collector gặp lỗi (`HTTP 403 Forbidden`)
- **Nguyên nhân:** Cloudflare bot challenge chặn IP trung tâm dữ liệu của server khi gọi cào trang `https://pvoil.com.vn/tin-gia-xang-dau`.
- **Xử lý:**
  1. Kiểm tra bằng `curl -I "https://pvoil.com.vn/tin-gia-xang-dau"`.
  2. Nếu bị Cloudflare chặn, cần cấu hình proxy dân cư sạch hoặc bổ sung header/cookie hợp lệ trong `_fetch_html()`.
  3. Collector fail-safe dừng lại với mã lỗi 1 và không làm hỏng file dữ liệu lịch sử.

---

## 13. Quy trình khôi phục sau sự cố

1. **Server bị reboot bất ngờ:**
   - Dịch vụ `cron` được kích hoạt tự động theo systemd.
   - Các file lock trong `/home/petrotwin/PetroTwin-/logs/*.lock` sẽ tự động giải phóng do `flock` gắn liền với file descriptor của tiến trình đã kết thúc.
2. **Pipeline bị ngắt giữa chừng:**
   - Pipeline dùng cơ chế thư mục staging tạm thời (`Path(tempfile.mkdtemp)`). Nếu tiến trình bị kill, thư mục staging bị bỏ rơi và `data/processed/current.json` không bị thay đổi, đảm bảo tính bất biến của run trước đó.
   - Chạy lại lệnh `python -m petrotwin.collectors.runner` để khôi phục trạng thái mới nhất.

---

## 14. Các giới hạn kỹ thuật còn tồn tại

1. **Nguồn PVOIL và Cloudflare WAF:** Trang web PVOIL sử dụng Cloudflare WAF challenge. Một số IP máy chủ nước ngoài hoặc datacenter sẽ bị trả về HTTP 403. Cần kiểm tra smoke test trực tiếp trên server thực tế trước khi kích hoạt job cron production.
2. **Tỷ giá FX từ Algolab:** Dữ liệu tỷ giá USD/VND trung tâm được cung cấp theo hình thức bên thứ ba tự công bố (`provider_declared`), chưa phải kết nối trực tiếp API của Ngân hàng Nhà nước Việt Nam (SBV).
