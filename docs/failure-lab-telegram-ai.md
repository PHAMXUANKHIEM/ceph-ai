# Failure Lab: yêu cầu lỗi qua Telegram

Kênh được chọn tại **Settings → Cụm Staging (Failure Lab)** có thể nhận `/lab <mô tả lỗi>` hoặc tin nhắn mô tả lỗi. Trong nhóm Telegram bật Privacy Mode, dùng `/lab` để bot nhận được yêu cầu. Đây là kênh riêng; cảnh báo CS-LAB không được gửi vào đó. Chỉ Telegram user ID có trong `TELEGRAM_CHATBOX_FULL_ACCESS_USER_IDS` mới được gửi yêu cầu và bấm duyệt. Thiết lập “mọi thành viên chat là operator” **không** mở quyền Failure Lab.

Luồng: bot nhận yêu cầu → AI chỉ chọn một kịch bản trong `worker/policy/failure_lab_faults.yaml` → bot gửi thẻ xem trước với tên cụm, FSID, kịch bản và giới hạn thời gian → operator bấm **Duyệt gây lỗi** hoặc **Bỏ qua** → Worker kiểm tra lại kênh, cụm, FSID và các chốt an toàn → tiến trình Failure Lab áp dụng lỗi, quan sát, gỡ lỗi và gửi kết quả. Không có lệnh shell/Ceph tự do từ AI. Hiện chỉ hỗ trợ `osd_down_fault` và `osd_nearfull_fault`; yêu cầu khác bị từ chối.

Thẻ duyệt hết hạn sau 15 phút. Worker chỉ nhận thẻ đã duyệt trong 5 phút gần nhất; nếu hàng đợi bị kẹt hoặc Worker khởi động lại muộn, phải gửi yêu cầu mới.

Trước khi bấm duyệt, operator phải kiểm tra cụm staging không có workload thật, FSID đã ghim và **Cho phép gây lỗi thật** đã bật. Nếu cấu hình đổi sau khi tạo thẻ, Worker từ chối. Không bật gây lỗi để “thử bot”: có thể gửi yêu cầu để xem bản đề xuất khi flag đang tắt, nhưng nút duyệt sẽ từ chối.

Kết quả và artifact nằm trong `/var/lib/ceph-ai/failure-lab/`; đề xuất nằm trong thư mục `proposals/`. Nếu runner bị ngắt hoặc cụm không phục hồi, xem file `HALT`, trạng thái Ceph và log Worker; không tự xóa `HALT` trước khi xác minh/hoàn tác. CI chỉ dùng mô phỏng; cần thử nghiệm trên cụm staging được phê duyệt trước khi coi luồng gây lỗi là đã kiểm chứng thực tế.

Deploy theo quy trình và restart Worker từ Dashboard bị từ chối khi một lượt gây lỗi đang giữ khóa Failure Lab. Worker chuyển SIGTERM cho runner để runner gỡ lỗi nếu có lệnh dừng trực tiếp; Podman dành tối đa 930 giây trước khi SIGKILL. Mất điện, SIGKILL hoặc hỏng host vẫn cần operator kiểm tra/hoàn tác thủ công; không có cam kết rằng phần mềm có thể tự phục hồi trong các trường hợp đó.
