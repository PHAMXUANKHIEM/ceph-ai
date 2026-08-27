# AI diagnosis evaluation

Evaluation chạy offline, không gọi provider và không thay đổi cluster.

```bash
PYTHONPATH=. .venv/bin/python -m scripts.evaluate_ai_diagnosis export-verified /tmp/golden.jsonl
PYTHONPATH=. .venv/bin/python -m scripts.evaluate_ai_diagnosis score /tmp/golden.jsonl /tmp/predictions.jsonl
PYTHONPATH=. .venv/bin/python -m scripts.evaluate_ai_diagnosis production-report
```

Prediction JSONL dùng `id`, `action_id` hoặc `abstain=true`, và `confidence` từ 0 đến 1.
Report gồm độ phủ matched/total, action accuracy, abstention recall, unsafe-action rate và Brier score.
Dataset export chỉ chứa nhãn/case metadata đã verify, không xuất evidence thô hay credential.
`production-report` chấm trực tiếp các quyết định lịch sử so với outcome đã verify.
