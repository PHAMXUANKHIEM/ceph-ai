# AI diagnosis evaluation

Evaluation chạy offline, không gọi provider và không thay đổi cluster.

```bash
PYTHONPATH=. .venv/bin/python -m scripts.evaluate_ai_diagnosis export-verified /tmp/golden.jsonl
PYTHONPATH=. .venv/bin/python -m scripts.evaluate_ai_diagnosis score /tmp/golden.jsonl /tmp/predictions.jsonl --group-by health_code
PYTHONPATH=. .venv/bin/python -m scripts.evaluate_ai_diagnosis production-report
```

Prediction JSONL dùng `id`, `action_id` hoặc `abstain=true`, `confidence` từ 0 đến 1
và tùy chọn `cost_usd` (chi phí provider của lần chẩn đoán đó).
Report chỉ chấm nhãn do operator xác nhận: `CORRECT` tạo positive label,
`FALSE_POSITIVE` tạo diagnosis-negative/abstention label, `UNSAFE` tạo
abstention label. Execution outcome không được dùng thay cho correctness label.

Metric gồm coverage, action accuracy, action precision (tỉ lệ đề xuất đúng trên
các đề xuất đã có nhãn), abstention recall, unsafe rate trên tập negative, unsafe
rate tổng, diagnosis Brier score, expected calibration error (10 bin), hallucination
rate (đề xuất `action_id` không có trong catalogue `worker/policy/action_policy.yaml`)
và cost per correct diagnosis (khi prediction có `cost_usd`). Output `score` nằm
dưới khóa `overall`; `--group-by health_code` (hoặc `prompt_version`) thêm báo cáo
theo nhóm. Report production tách theo provider, health code và prompt version,
loại deterministic/unknown khỏi aggregate AI. Metric là `null` khi
chưa đủ operator label; không suy diễn nhãn từ command exit hoặc post-check.

Dataset export chỉ chứa nhãn/case metadata, không chứa evidence thô. Để replay
model cần một dataset redacted được operator duyệt riêng; file label này không
được mô tả như model input.

## Operator verdicts (nhãn thật)

Chỉ verdict do người ghi mới là nhãn đúng/sai của chẩn đoán; mọi thứ khác
(exit code, post-check, weak label) chỉ là tín hiệu phụ.

- **Telegram, một chạm:** sau mỗi Duyệt/Từ chối, bot hỏi "Chẩn đoán của AI có
  đúng không?" với nút `CORRECT`/`FALSE_POSITIVE`/`UNSAFE`/`INCONCLUSIVE`; verdict
  âm hỏi thêm lý do (F1/F2/F3/E1). Chỉ người trong chat tin cậy của cluster hoặc
  `TELEGRAM_APPROVAL_USER_IDS` ghi được; actor lưu là `telegram:<username|id>`.
- **Alert Center (`/alerts`):** cột Verdict, nút "✓ Đúng" hoặc chọn verdict khác
  kèm ghi chú (bắt buộc cho verdict âm), bộ lọc "chưa có nhãn". Chỉ admin.
- **Nhắc có chủ đích:** mỗi ngày sau `VERDICT_NUDGE_HOUR` (mặc định 9h VN) bot gửi
  tối đa `VERDICT_NUDGE_LIMIT` (5) case chưa nhãn trong 14 ngày, ưu tiên outcome rõ,
  AI tự tin nhưng bị từ chối, và family ít nhãn; không nhắc lại case đã nhắc.
- Ghi đè verdict có audit (giá trị cũ/mới, actor, ghi chú). Dashboard và Telegram
  dùng chung `shared/remediation_cases.record_verdict`.

Mốc dùng nhãn: **≥ 20** nhãn chồng lấp để đặt lại trọng số weak label
(`scripts/auto_label_report.py`), **≥ 200** nhãn trước khi học chính sách
execute/escalate (WP6.3). Weak label (`shared/auto_labels.py`) không bao giờ ghi
`operator_verdict` và không mở quyền tự thực thi.

## Online learning (River v2) và nâng cấp model

Luồng nhãn: watcher ghi mỗi mẫu CPU/RAM vào `online_learner_audit`; forecast
evaluator đối chiếu dự báo với telemetry thật và ghi nhãn `READY` vào
`online_learner_labels`; `apply_ready_labels()` (mỗi lượt quét node, chỉ trong
canary scope `ONLINE_LEARNING_CANARY_*`) đưa nhãn vào learner và đổi nhãn sang
`CONSUMED`. Model vẫn **SHADOW_ONLY**: học nhưng không điều khiển cảnh báo.

Bằng chứng nâng cấp (`shared/river_v2_evidence.py`, CLI
`scripts/river_v2_promotion_evidence.py`) được tạo tự động mỗi thứ Hai trong
digest AI Ops và lưu `river-v2-evidence-<năm>-W<tuần>.json` vào
`LEARNING_EVIDENCE_REPORT_DIR` (mặc định `/var/lib/ceph-ai/learning-evidence`,
ngoài repo vì có tên host). Verdict chỉ thành `ELIGIBLE_FOR_REVIEW` khi đủ
**≥ 100** kết quả xác minh, **≥ 3** scope, **≥ 2** cluster, **≥ 20** kết quả mỗi
scope và tỉ lệ `READY_TO_LEARN` **≥ 0,8**; đó là lời mời operator xem xét, không
phải tự nâng cấp. Nâng cấp thật còn cần holdout theo thời gian
(`scripts/river_linear_v2_runtime_replay.py`, verdict `CANDIDATE_BETTER` ổn định
≥ 2 tuần), operator duyệt và diễn tập rollback.

Trạng thái tổng hợp (online learning, evidence, quyết định, FRR, OPE) hiển thị
ở thẻ "Tiến độ tự vận hành" trên `/ai-learning` (`/api/ai-learning/autonomy-status`).

## Diễn tập lỗi có kiểm soát (chaos, WP5)

**Chưa có.** Không chạy kịch bản lỗi nào trên cluster này. Khi được làm, runner
phải: chỉ chạy với cluster id trong allowlist staging, cần token xác nhận và cửa
sổ thời gian, ghi ground truth (kịch bản, thời điểm, mục tiêu), tự hoàn tác sau T
phút, ghi audit, và từ chối mọi cluster production. Nhãn từ diễn tập chỉ dùng để
chấm chẩn đoán, không thay operator verdict.
