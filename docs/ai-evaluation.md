# AI diagnosis evaluation

Evaluation chạy offline, không gọi provider và không thay đổi cluster.

```bash
PYTHONPATH=. .venv/bin/python -m scripts.evaluate_ai_diagnosis export-verified /tmp/golden.jsonl
PYTHONPATH=. .venv/bin/python -m scripts.evaluate_ai_diagnosis score /tmp/golden.jsonl /tmp/predictions.jsonl
PYTHONPATH=. .venv/bin/python -m scripts.evaluate_ai_diagnosis production-report
```

Prediction JSONL dùng `id`, `action_id` hoặc `abstain=true`, và `confidence` từ 0 đến 1.
Report chỉ chấm nhãn do operator xác nhận: `CORRECT` tạo positive label,
`FALSE_POSITIVE` tạo diagnosis-negative/abstention label, `UNSAFE` tạo
abstention label. Execution outcome không được dùng thay cho correctness label.

Metric gồm coverage, action accuracy, abstention recall, unsafe rate trên tập
negative, unsafe rate tổng và diagnosis Brier score. Report production tách theo
provider, loại deterministic/unknown khỏi aggregate AI. Metric là `null` khi
chưa đủ operator label; không suy diễn nhãn từ command exit hoặc post-check.

Dataset export chỉ chứa nhãn/case metadata, không chứa evidence thô. Để replay
model cần một dataset redacted được operator duyệt riêng; file label này không
được mô tả như model input.
