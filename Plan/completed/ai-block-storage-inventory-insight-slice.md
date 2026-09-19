# Completed: AI Block Storage Inventory Insight slice

## Phạm vi đã hoàn tất

Slice read-only cho RBD inventory đã được triển khai và kiểm thử:

- Phát hiện volume stale/unattached dựa trên watcher/attachment và I/O history,
  fail-closed khi thiếu evidence.
- Phát hiện snapshot policy gap, parent/child clone dependency và partial RBD
  evidence.
- Đối chiếu volume với `BackupJob`, snapshot policy và `restore_drill` để phát
  hiện backup chưa thành công, backup lỗi/trễ và restore drill gap.
- Chuẩn hóa recommendation contract: `ADVISORY`, `read_only=true`,
  `action_id=null`, expected saving, impact và evidence TTL 15 phút.
- API Volumes và UI hiển thị reason, recommendation, evidence gaps và trạng thái
  cache; không tạo Action và không thực thi mutation Ceph.
- Kiểm thử cluster isolation giữa default và secondary cluster.

## Bằng chứng nghiệm thu

- `pytest tests/test_block_storage_insights.py tests/test_volume_dependency_ui.py`
  — pass.
- Nhóm regression Volumes liên quan — 19 test pass.
- Compile Python và `git diff --check` — pass.

## Commit

- `6b41e6f6` — volume backup protection gap insights
- `114fb8b1` — advisory recommendation contract
- `7d3ce750` — cluster isolation coverage

## Giới hạn đã ghi nhận

Các nội dung sau không thuộc completed slice và vẫn được theo dõi trong
`Plan/in-progress/ai-missing-features-roadmap.md`: tenant/project ownership từ
Cinder/CSI, xác minh artifact ở backup target ngoài, restore-drill coverage theo
từng volume, retention policy đầy đủ và kiểm chứng dữ liệu thực tế trên nhiều
Ceph release.
