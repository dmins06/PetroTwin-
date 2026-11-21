# Baseline Specification – PetroTwin

## 1. Mục tiêu

Xây dựng hai mô hình baseline đơn giản để làm mốc so sánh với các mô hình dự báo chính trong PetroTwin:

- Naive Persistence Baseline.
- Weekly Seasonal Naive Baseline.

Hai baseline sử dụng cùng dữ liệu mục tiêu và phải được đánh giá trên cùng folds, forecast origins và tập quan sát theo thiết kế validation T21.

## 2. Naive Persistence Baseline

### 2.1. Nguyên tắc

Dự báo giá của sản phẩm tại forecast origin bằng giá gần nhất đã biết của chính sản phẩm đó.

Công thức:

\[
\hat{y}_{p,t}=y_{p,t^*}
\]

Trong đó:

- \(p\): mã sản phẩm.
- \(t\): thời điểm dự báo.
- \(t^*\): thời điểm quan sát gần nhất thỏa mãn \(t^* \leq t\).
- \(y_{p,t^*}\): giá thực tế gần nhất đã biết.

### 2.2. Giả định

- Mỗi sản phẩm có lịch sử giá độc lập.
- Không sử dụng giá có timestamp sau forecast origin.
- Nếu không có lịch sử phù hợp, mô hình báo lỗi.

## 3. Weekly Seasonal Naive Baseline

### 3.1. Nguyên tắc

Dự báo giá bằng quan sát gần nhất với thời điểm tương ứng một tuần trước forecast origin.

Công thức:

\[
\hat{y}_{p,t}=y_{p,t_s}
\]

Trong đó:

- \(t_s=t-7\text{ ngày}\).
- \(t_s\) là thời điểm mục tiêu của chu kỳ tuần trước.
- Quan sát được chọn phải thuộc cùng sản phẩm và nằm trong khoảng thời gian cho phép.

### 3.2. Cấu hình

| Tham số | Giá trị mặc định | Ý nghĩa |
|---|---|---|
| seasonal_period | 7D | Chu kỳ mùa vụ một tuần |
| tolerance | 1D | Sai lệch tối đa một ngày so với thời điểm mục tiêu |
| fallback | persistence | Dùng Naive Persistence nếu không tìm thấy quan sát mùa vụ phù hợp |

Nếu có nhiều quan sát phù hợp, chọn quan sát có khoảng cách thời gian nhỏ nhất. Nếu khoảng cách bằng nhau, ưu tiên quan sát mới hơn.

### 3.3. Giả định và giới hạn

- Dữ liệu giá có nhịp điều chỉnh chủ yếu theo tuần.
- Không phải mọi sản phẩm đều có đủ lịch sử để khớp chu kỳ.
- Weekly Seasonal Naive có thể tạo dự báo giống Naive Persistence khi giá gần nhất cũng là quan sát thuộc tuần trước.
- Cần báo cáo tỷ lệ fallback để phân biệt kết quả Seasonal Naive thực sự với kết quả sử dụng fallback.

## 4. Quy tắc chống rò rỉ dữ liệu

Cả hai baseline phải tuân thủ nguyên tắc:

- Chỉ sử dụng dữ liệu có thể được biết tại forecast origin.
- Không sử dụng giá tương lai để tạo dự báo.
- Không sử dụng dữ liệu validation hoặc test chưa có sẵn tại thời điểm dự báo.
- Không thay đổi cách chia tập và forecast origins đã được quy định trong T21.

**Lưu ý triển khai:** Code baseline hiện tại lọc lịch sử theo `effective_at_local <= forecast_origin_local`. Nó chưa tự kiểm tra trường `available_at` của từng quan sát. Vì vậy, tầng chuẩn bị dữ liệu/evaluator phải đảm bảo dữ liệu đầu vào chỉ chứa những quan sát thực sự có sẵn tại forecast origin. Không được xem điều kiện timestamp trên là bằng chứng đầy đủ về point-in-time availability.

## 5. Validation và đánh giá

Hai baseline phải sử dụng:

- Cùng validation folds theo T21.
- Cùng forecast origins.
- Cùng tập quan sát đánh giá.
- Cùng quy tắc xử lý dữ liệu thiếu và dữ liệu không hợp lệ.

Các metric dự kiến sử dụng theo thiết kế T21:

- MAE: metric chính.
- RMSE: metric phụ.
- MAPE: metric phụ.
- Directional Accuracy: đánh giá khả năng dự báo hướng thay đổi giá.

Việc tính toán và quy tắc xử lý mẫu số bằng 0 cần tuân theo metric specification của T24.

## 6. Kiểm thử

Các unit test hiện tại kiểm tra:

- Naive sử dụng giá gần nhất đã biết.
- Lịch sử của các sản phẩm được tách biệt.
- Không sử dụng quan sát tương lai.
- Báo lỗi khi chưa fit hoặc không có lịch sử phù hợp.
- Seasonal Naive lấy giá từ chu kỳ tuần trước.
- Fallback về persistence khi không tìm được quan sát mùa vụ.
- Có thể tắt fallback để yêu cầu báo lỗi khi thiếu quan sát mùa vụ.

Kết quả kiểm thử hiện tại:

| Nhóm kiểm thử | Số test | Kết quả |
|---|---:|---|
| Baseline | 10 | PASS |
| Feature Engineering (T22) | 11 | PASS |
| Tổng | 21 | PASS |

## 7. Giới hạn và hướng phát triển

- Kết quả unit test chưa thay thế cho đánh giá trên validation thực tế.
- Cần kiểm tra tỷ lệ dự báo trùng nhau giữa hai baseline.
- Cần thống kê tỷ lệ fallback của Weekly Seasonal Naive.
- Cần bảo đảm dữ liệu đầu vào đáp ứng quy tắc availability của T21 trước khi chạy benchmark.
- Không sử dụng final test để lựa chọn hoặc điều chỉnh cấu hình baseline.

## 8. Kết luận

Hai baseline cung cấp mốc so sánh đơn giản, dễ tái lập cho các mô hình dự báo giá xăng dầu PetroTwin. Naive Persistence đại diện cho phương pháp giữ nguyên giá gần nhất; Weekly Seasonal Naive kiểm tra giả thuyết chu kỳ điều chỉnh theo tuần.