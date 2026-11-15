# Hướng dẫn sử dụng

Chạy các lệnh từ root repo sau `uv sync --all-extras`. Mọi thành phần dùng chung
một `.venv` và `uv.lock` tại root.

Các nhóm mặc định `dev/whisper/gpu` giữ công cụ phát triển và extra collector khi
dùng `uv run --locked`; không tạo môi trường/lock ở từng project. Collector giữ
`av<19` để tương thích faster-whisper. Model/audio có thể lớn; chọn ổ đĩa đủ chỗ
và không đặt cache model trên tmpfs nhỏ.

## Thu thập

```bash
uv run --locked yt collect "https://www.youtube.com/watch?v=VIDEO_ID" --mode direct --language vi
uv run --locked yt collect "https://www.youtube.com/@HANDLE" --mode direct --language vi
uv run --locked yt collect "https://www.youtube.com/playlist?list=PLAYLIST_ID" --mode direct --language en
uv run --locked yt collect "https://www.youtube.com/watch?v=VIDEO_ID" --mode generate --engine whisper --engine-model medium
```

Direct chỉ lấy phụ đề theo **một** ngôn ngữ: thủ công → tự động gốc → dịch tự động
sang ngôn ngữ yêu cầu; thiếu thì bỏ qua, không tự dùng audio hoặc tiếng khác.
Generate không nhận `--language`, tự nhận dạng từ audio tạm và dọn sau khi chạy.
`--refresh` thay đúng bản khi thành công, giữ bản cũ nếu lỗi. Transcript cache được
bỏ qua trước tải; terminal ghi `[n/tổng]` và `[cached]` khi phù hợp.

Kênh lấy toàn bộ tab `videos`, playlist lấy toàn bộ mục yt-dlp liệt kê; không có
cờ giới hạn số video. Thử nhanh bằng **một URL video** với DB riêng:

```bash
uv run --locked yt collect "https://www.youtube.com/watch?v=VIDEO_ID" --mode direct --language vi --db /tmp/yt-trial.sqlite3
```

`--cookies FILE` cấp cookie cho yt-dlp; YouTube vẫn có thể chặn hoặc trả 429.
Collector xử lý tuần tự, giữ phần đã commit và dọn audio tạm khi Ctrl+C.
Quy tắc nguồn, retry, transaction và schema: [ARCHITECTURE.md](../ARCHITECTURE.md#collector).

## Tìm kiếm

```bash
uv run --locked yt search "vai trò của insulin" --top 5 --moments-per-video 3
uv run --locked yt search '"kháng insulin"' --mode keyword --json
uv run --locked yt search "insulin" --video-id VIDEO_ID_1 --video-id VIDEO_ID_2
uv run --locked yt search --interactive --offline
```

| Tùy chọn                        | Hành vi                                                                                  |
| ------------------------------- | ---------------------------------------------------------------------------------------- |
| `--mode hybrid`                 | Mặc định: BM25 + vector (MiniLM L12) qua RRF, kèm Cross-Encoder rerank nhẹ               |
| `--mode semantic`               | Vector search thuần (MiniLM L12 mặc định, cosine trên tiêu đề+transcript)                |
| `--mode keyword`                | BM25 trên transcript+tiêu đề, đủ các từ; `"cụm từ"` giữ thứ tự; không tải model          |
| `--mode exact`                  | Khớp chuỗi con nguyên văn (giữ dấu tiếng Việt & dấu câu như `C++`), không phân biệt hoa/thường |
| `--rerank` / `--no-rerank`      | Bật/tắt sắp xếp lại bằng Cross-Encoder trong `hybrid` (mặc định bật)                     |
| `--model`                       | minilm, e5-small, qwen3, jina-v5, embeddinggemma                                         |
| `--language vi`                 | Mặc định tiếng Việt, gồm biến thể vùng; mỗi video dùng một transcript              |
| `--transcript-source auto`      | YouTube manual → auto_generated → translated → unknown → Whisper                   |
| `--source-mode`                 | Lọc loại phụ đề hoặc tên engine; `--transcript-source youtube\|generate` lọc nguồn |
| `--channel`                     | ID kênh, tên chính xác hoặc URL canonical đã có trong DB                           |
| `--video-id`                    | Lặp lại để chọn nhiều ID; kết hợp với các bộ lọc khác                              |
| `--top` / `--moments-per-video` | Số video / số đoạn không chồng lấn mỗi video; mặc định 5 / 3                       |

Các đoạn xếp theo độ liên quan, mỗi đoạn có timestamp và trích dẫn riêng; khi
chồng lấn giữ cửa sổ điểm cao hơn, không gộp. Link mở đầu cửa sổ khoảng 60 giây,
có thể sớm hơn đúng câu. JSON nhóm `moments` trong video, trường cũ ở cấp video là
đoạn tốt nhất. Search tìm đoạn liên quan, chưa tổng hợp câu trả lời.

Keyword bỏ dấu tiếng Việt/chữ hoa/dấu câu; không là khớp chuỗi nguyên văn.
Ngữ nghĩa (và nhánh dense của `hybrid`) ghép tiêu đề video vào cửa sổ và chia đủ nội
dung theo giới hạn tokenizer; `--batch-size` mặc định 16, `--device cpu|cuda` chọn
thiết bị. `--min-similarity` dùng cho `hybrid` và `semantic`, cần hiệu chỉnh riêng từng
model; chưa có ngưỡng mặc định nên câu ngoài kho vẫn có thể nhận kết quả. Điểm không là
xác suất đúng và không so trực tiếp giữa model/chế độ.

Interactive giữ model/ma trận và cập nhật khi collector commit. `/show N` xem đủ
các đoạn của video N, `/show N M` xem đoạn M, `/results` xem lại, `/top N` và
`/moments N` đổi giới hạn lượt sau, `/settings`, `/help`, `/quit`; dòng trống/EOF
thoát. `--interactive --json` xuất một JSON mỗi dòng, không xử lý lệnh terminal.

## Web local

```bash
uv run --locked yt-web
```

Web local tại http://127.0.0.1:8000 có collect/search/thư viện, tham số nâng cao,
queue, tải JSON và nhật ký. Xem [hướng dẫn web](../apps/web/README.md).
Quy trình đánh giá offline nằm ở `apps/workflows`; cách chạy và giới hạn:
[evaluation](../evaluation/README.md).

## Dữ liệu, log và kiểm tra

| Vị trí mặc định                                   | Cách đổi                            |
| ------------------------------------------------- | ----------------------------------- |
| `data/index.sqlite3` — transcript schema v2       | `--db`                              |
| `data/search.sqlite3` — vector/FTS/cache dẫn xuất | `--search-db`                       |
| `data/web.sqlite3` — queue/events                 | web `--state-db`                    |
| `data/models/` — trọng số model                   | `HF_HOME`                           |
| Thư mục audio tạm                                 | `TMPDIR`                            |
| `data/logs/cli.jsonl`, `data/logs/web.jsonl`      | `--log-file`, console `--log-level` |

Search chỉ đọc input v2, không tự migrate. Collector migrate v1 đã biết nguyên tử;
không đoán/xóa schema khác. Input/cache/state/log/report được bảo vệ alias và
sidecars SQLite. Không tự xóa model, cache hoặc dữ liệu để chữa lỗi. File log
DEBUG xoay ở 5 MiB + 3 backups; không log query/transcript/credential đầy đủ.

Sau `uv sync --all-extras`, kiểm tra từ cùng môi trường root:

```bash
uv run --locked ruff check apps transcript_collector/src video_search/src tests
uv run --locked ruff format --check apps transcript_collector/src video_search/src tests
uv run --locked pyrefly check --python-interpreter-path .venv/bin/python --min-severity=warn
uv run --locked python -m unittest tests.test_applications tests.test_core tests.test_filtering tests.test_moments tests.test_search tests.test_services tests.test_corpus_v2 tests.test_workflows tests.test_interactive tests.test_cli_logging tests.test_search_logging tests.test_collector tests.test_collector_logging tests.test_collector_migration tests.test_collection_resilience tests.test_video_collection tests.test_playlist_collection -q
```

Đây là nhóm core/CLI/workflows; theo yêu cầu hiện tại chưa chạy test web app.
Test giả không chứng minh kết nối YouTube, chất lượng transcript hoặc toàn kho.
Không dùng tập con hay số đo cũ để khẳng định model tốt nhất. Quy trình planner/
workers: [AGENTS.md](../AGENTS.md); chi tiết hợp đồng: [ARCHITECTURE.md](../ARCHITECTURE.md).
