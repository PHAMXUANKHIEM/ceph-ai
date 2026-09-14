# Báo cáo baseline realtime cluster status — 2026-09-14

Server: 10.3.55.213
Repo: /root/ceph-ai
Cluster active: CS-LAB
Phạm vi: read-only, không chạy mutation.

## Kết luận nhanh

Đường đọc hiện tại chưa phải realtime. Một query trực tiếp phải đi qua Paramiko SSH, cephadm shell và Ceph CLI rồi mới render được dữ liệu.

| Đường đo | Kết quả |
|---|---:|
| Pools, 12 rows | 6.52–7.06 s |
| PGs, 385 rows | 16.61–19.88 s |
| Health live | 6.87 s ở lần đo cuối; các lần trước khoảng 6.7–8.9 s |
| Watcher poll interval | 15 s |
| Dashboard health refresh threshold | 60 s |
| Pools/PGs stale TTL | 900 s |
| Pools/PGs cache TTL mặc định | 3.600 s |

## Môi trường

- Container dashboard và Watcher đang healthy.
- .env đang dùng CEPH_EXEC_MODE=cephadm.
- WATCHER_POLL_INTERVAL_SECONDS=15.
- MON được cấu hình; topology cụ thể không ghi vào báo cáo.
- Cache persistent có 216 file, khoảng 880 KiB tại thời điểm kiểm tra.
- Chưa chạy benchmark 10 tab đồng thời để tránh tạo tải không cần thiết trên cluster.

## Kết quả query trực tiếp

Đã gọi trực tiếp các hàm production bằng .venv/bin/python, bỏ qua browser và cache page:

```text
cluster='CS-LAB'
pools sample1=6.519s rows=12
pools sample2=7.056s rows=12
pools min=6.519s median=6.788s max=7.056s
pgs sample1=16.607s rows=385
pgs sample2=19.882s rows=385
pgs min=16.607s median=18.245s max=19.882s
health_elapsed=6.871s
health=OK, osds=6/6, mons=3/3, servers=3/3, utilization=77%, pools=12, placement_groups=OKAY
```

Thời gian PGs dao động cao hơn Pools vì ceph pg dump có 385 rows và chịu overhead cephadm/lock. Health live cũng dao động theo SSH và tải MON.

## Đường đi gây chậm

### Pools

dashboard/routes/pgs.py gọi _query_pool_rows, sau đó watcher.ceph_client chạy một batch qua Paramiko SSH và cephadm shell:

- ceph osd pool ls detail --format json
- ceph df detail --format json
- ceph osd pool stats --format json
- ceph osd crush rule dump --format json

Backend còn phải normalize capacity, objects, IOPS và CRUSH rule thành rows. Đây là đường đọc đúng nhưng không nên nằm trên browser request.

### PGs

PGs lấy pool detail và ceph pg dump pgs. Payload nhiều rows nên mất 16–20 giây ở baseline này.

### Health

dashboard/routes/incidents.py batch các lệnh ceph -s, ceph osd perf, ceph osd dump và ceph orch host ls. API browser có thể trả nhanh từ cache, nhưng backend chỉ schedule live refresh khi cache quá 60 giây.

## Cache và frontend hiện tại

- Pools/PGs dùng shared.object_storage_cache.get_or_load.
- Cache này process-local; không phải snapshot trung tâm dùng chung cho mọi process.
- Cache miss production có thể trả fallback [] và schedule loader nền.
- pools.html/pgs.html chỉ meta refresh 3 giây khi cache_loading=true.
- Khi cache fresh, Pools không có polling API realtime riêng.
- WebSocket /ws/incidents chỉ fingerprint số Incident và max(updated_at), chưa publish pool/PG/health/CRUSH.

## RT-00 đã xác nhận

- [x] Xác định cluster active, container và exec mode.
- [x] Ghi cadence Watcher, health threshold, cache TTL/stale TTL.
- [x] Đo Pools và PGs lặp lại.
- [x] Đo health live read-only.
- [x] Xác định các lệnh và đường code gây chậm.
- [x] Không chạy mutation.
- [~] Chưa đo 1/5/10 tab đồng thời; để sau khi có snapshot collector để benchmark đúng kiến trúc đích.

## Baseline so sánh sau cải tiến

| Chỉ số | Hiện tại | Mục tiêu |
|---|---:|---:|
| API health/snapshot cache hit p95 | Chưa đo qua session | <200 ms |
| API Pools snapshot hit p95 | Chưa có | <200 ms |
| UI thấy health thay đổi | Phụ thuộc 15–60 s | <10 s p95 |
| UI thấy mutation sau verify | Reload/cache dependent | <2 s sau event |
| Ceph query từ page request | Có | 0 |
| Collector cùng cluster | Chưa có snapshot collector | 1 mỗi tier |

## Bước kế tiếp

RT-00 đã hoàn tất baseline đơn request và đã xác định bottleneck chính. Trước RT-01 cần chốt store snapshot:

1. Phase đầu: mở rộng shared/ceph_query_cache.py với envelope và generation.
2. Phase ổn định: thêm bảng ClusterSnapshot trong DB.

Khuyến nghị bắt đầu phase đầu để có vertical slice nhanh, sau đó chuyển sang DB khi đã đo payload và concurrency thực tế.
