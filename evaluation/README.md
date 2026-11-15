# Đánh giá tìm kiếm

Nhãn và transcript cục bộ không được đưa lên Git. Trước khi chạy, tạo dataset
JSONL theo ví dụ bên dưới tại `evaluation/vi_queries.jsonl` và thu thập transcript
tương ứng. Nhãn do agent soạn cần được người dùng duyệt trước khi diễn giải kết
quả. Video/thời điểm tương đương có thể đúng nhưng chưa có nhãn; metric chỉ tính
đáp án đã biết. Report trong `data/` cũng được Git bỏ qua.

## Chạy từ root

Dùng môi trường/lock chung theo [README](../README.md):

```bash
uv sync --all-extras
uv run --locked python - <<'PY'
from yt_workflows.config import EvaluationRequest
from yt_workflows.evaluation import run_evaluation

run_evaluation(EvaluationRequest(
    dataset="evaluation/vi_queries.jsonl",
    models=("minilm", "e5-small", "qwen3", "jina-v5"),
    device="cuda",
    batch_size=4,
    max_windows=2048,
    output="data/evaluation/gpu-2048.json",
))
PY
```

Evaluation là API Python, không có lệnh `yt eval`. Request nhận `language="vi"`,
`transcript_source="auto"` (`auto|youtube|generate`), `source_mode`.
Bộ lọc chọn một transcript/video theo hợp đồng kiến trúc trước khi tạo snapshot;
engine khác Whisper phải chọn đích danh. Mặc định 512 cửa sổ,
`max_windows=0` dùng toàn kho. Tập con giữ cửa sổ có đáp án đã biết và thêm nhiễu
với seed 42; dễ hơn toàn kho, không chứng minh model tốt nhất nói chung.

Mọi model dùng cùng snapshot JSONL, kể cả khi collector commit thêm. Snapshot
phải có `language`, `source_mode`, `transcript_config`; bản cũ thiếu metadata v2
phải tạo lại. `corpus_file` chỉ nhận snapshot khớp bộ lọc yêu cầu. Nhãn có thể
chỉ rõ language/source_mode; source chỉ youtube/generate, không ghép bằng chứng
khác phiên bản transcript. Công cụ kiểm tra evidence và báo nếu transcript đổi.

Để dùng lại snapshot, đặt `corpus_file="data/evaluation/gpu-2048.corpus.jsonl"`
trong request; `rerank=True` bật Cross-Encoder và `offline=True` chỉ dùng model
đã có trên máy.

## Thuật toán và diễn giải

Đánh giá snapshot dùng hybrid embedding+BM25/RRF và rerank tùy chọn.
Eval mặc định **một cửa sổ/video**, không là phép đo tính năng nhiều đoạn mặc định
3/video của search.

- `known_video_hit@5`, MRR/nDCG: tìm lại video tham chiếu trong top; nhãn chưa đầy đủ.
- `known_moment_hit@5`: giao với bằng chứng ít nhất 10 giây, hoặc nửa độ dài nếu
  bằng chứng ngắn hơn 20 giây.
- `negative_false_return_rate`: tỷ lệ câu ngoài chủ đề vẫn có ứng viên; chưa đặt
  ngưỡng mặc định nên có thể cao. Nhãn âm cũng cần duyệt trước hiệu chỉnh sản phẩm.
- Mỗi model chạy tiến trình riêng; thời gian chuẩn bị gồm nạp model/chia/cache.
  P50/P95 sau một query làm nóng phản ánh phiên interactive, không gồm khởi động
  CLI mới. VRAM là `torch.cuda.max_memory_allocated`, không toàn bộ driver memory.
- Report giữ kết quả từng câu, cấu hình, hash dataset/snapshot. Chỉ so các lượt có
  cùng dữ liệu/phạm vi; không diễn giải tập con thành chất lượng toàn kho.

Ví dụ nhãn:

```json
{"id":"example-1","query":"nội dung cần tìm","relevant":[{"video_id":"VIDEO_ID","source":"youtube","language":"vi","source_mode":"auto_generated","start":120.0,"end":150.0,"evidence":"đoạn transcript tham chiếu"}]}
```

Không có đáp án dùng `"relevant": []`. `min_similarity` cần hiệu chỉnh riêng mỗi
model trên cùng snapshot; không dùng một ngưỡng cho mọi model. Kiểm tra giấy phép
và quyền truy cập model trước khi dùng. JSON/snapshot chi tiết nằm ở
`data/evaluation/`; không giả định artifact cũ luôn tồn tại.

Cấu hình model tham chiếu: [MiniLM/token budget](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/blob/main/sentence_bert_config.json),
[E5/prefix](https://huggingface.co/intfloat/multilingual-e5-small),
[Qwen3](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B),
[Jina v5](https://huggingface.co/jinaai/jina-embeddings-v5-text-small-retrieval),
[mMARCO reranker](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1).
