# Forecast benchmark offline

Benchmark này chạy ngoài Watcher/Worker poll loop và chỉ đọc dữ liệu CSV. Nó
không ghi database, không mở alert, không gửi notification, không chạy
remediation và không thay đổi model production.

## Dữ liệu

`ceph-node-cpu-anonymized.csv` là dữ liệu tổng hợp theo hình dạng NAB
(`timestamp,value`) có thêm cột `anomaly` để kiểm thử. Không có cluster name,
hostname, IP, volume ID hoặc thông tin Ceph thật.

Có thể dùng file nhãn NAB riêng với hai cột `window_start,window_end`:

```bash
python scripts/forecast_benchmark.py data.csv --labels labels.csv --output report.json
```

## Detector và scoring

- `robust_baseline`: median/MAD rolling baseline.
- `river_half_space_trees`: detector River nhẹ, chấm điểm trước rồi mới
  `learn_one` để tránh leakage.
- `pyod_iforest`: tùy chọn, chỉ chạy khi cài benchmark extra.

Báo cáo gồm precision, recall, false-positive rate, event recall, detection
delay trung bình và CPU time. Event recall dùng cửa sổ NAB-like một giờ trước
khi event bắt đầu; đây là scoring tham khảo, không phải bằng chứng đủ để
promotion production.

Cài dependency tùy chọn trong môi trường benchmark riêng:

```bash
pip install -e '.[benchmark]'
python scripts/forecast_benchmark.py docs/benchmark/ceph-node-cpu-anonymized.csv
```

StatsForecast được tách riêng khỏi extra PyOD và không được cài vào image
Watcher/Worker:

```bash
pip install -e '.[benchmark-forecast]'
python scripts/forecast_benchmark.py docs/benchmark/ceph-node-cpu-anonymized.csv
```
