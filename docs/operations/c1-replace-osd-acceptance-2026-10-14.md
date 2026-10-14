# Nghiệm thu C1: thay OSD hỏng trên CS-STAGING (14/10/2026)

Kế hoạch: `Plan/in-progress/croit-parity-operations-plan-2026-10-10.md`, mục C1.
Cụm: CS-STAGING (cephadm, 3 node, fsid `1beef4a8-c2bb-11f1-baa4-fa163ea57b22`), OSD thử: `osd.2` trên
`rnd-khiempx-ceph-3.novalocal:/dev/vdb` (20 GB). Image chạy: `d6f8cfc8` (có #71, #80, #81).
Người duyệt: khiempx1207 (đồng ý qua Claude Code). Giờ trong bảng là UTC.

## Kết quả: ĐẠT

| # | Kịch bản | Action | Kết quả mong đợi | Kết quả thật |
|---|---|---|---|---|
| 1 | Thay osd.2 khi còn khoẻ, cụm 3 node, size 3 | (preflight từ chối, không tạo action) | Từ chối: không có host khác để giữ bản sao thứ 3 | ✅ Từ chối đúng lý do |
| 2 | Giả lập ổ hỏng: operator dừng osd.2 | — | PG vẫn `active` (undersized) | ✅ |
| 3 | Bắt đầu thay osd.2 đã down | `ca824350` 10:12:46 → 10:13:56 | `orch osd rm 2 --replace`, osd.2 `destroyed`, giữ ID, hàng đợi xoá trống | ✅ Xong sau 70 giây |
| 4 | Hoàn tất với đường dẫn ổ sai `/dev/vdz` | `e0d9baab` 10:17:08 → 10:17:31 | Dừng trước khi xoá gì | ✅ FAILED ở bước zap: `Error EINVAL: Device path '/dev/vdz' not found`; không zap gì, osd.2 vẫn `destroyed` |
| 5 | Hoàn tất với ổ đã ghi `/dev/vdb` | `737d23f3` 10:20:15 → 10:21:42 | Zap, tạo lại OSD **cùng ID 2**, chờ up/in | ✅ osd.2 `up`/`in`, weight 1.0 |
| 6 | Cụm tự phục hồi | — | `HEALTH_OK` | ✅ 10:28:00 (6 phút sau khi osd.2 up; ~3 300 object, 30% degraded lúc đầu) |

Các lần chạy bước 3 trước đó (12/10: `5a72b101`, `f9b60e52` FAILED; `03b0681a`, `59f0d20f`) đã dẫn tới
ba bản sửa #71 (target là MON thay vì `osd.2`), #80, #81 (destroy OSD đã down mà không chờ
`safe-to-destroy` vô hạn).

## Lỗi tìm thấy trong lúc nghiệm thu

1. **Trang Thay OSD kẹt sau một lần "Hoàn tất" thất bại** (action `0951b2c3`, `_hostname`/`_device` = null).
   Trang lấy host/ổ từ *hành động mới nhất*; sau bước 4, hành động mới nhất là lần hoàn tất thất bại, không
   có bước `osd_preflight`, nên lần hoàn tất kế tiếp không có host/ổ và Worker từ chối. Bước 5 được chạy
   bằng cách lấy host/ổ từ lần bắt đầu `ca824350`. Cần sửa: tìm lần bắt đầu đã EXECUTED gần nhất chưa có
   lần hoàn tất thành công, cho cả trang và route `/replace-osd/finish`; thêm test "hoàn tất sai ổ → hoàn tất
   lại".
2. **Ổ sai chỉ bị phát hiện sau khi duyệt** (bước 4). Nên kiểm tra ổ có trong `ceph orch device ls` của host
   ngay lúc tạo đề xuất hoàn tất.

## Việc còn lại của C1

- Hai sửa lỗi ở trên.
- Nối cảnh báo "OSD sắp hỏng" với đề xuất `replace_failed_osd`.
- Chỉ dùng cho CS-LAB sau khi hai sửa lỗi trên đã qua CS-STAGING.
