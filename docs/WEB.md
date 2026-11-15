# Web local

Source nằm tại `src/yt_web`. Chạy từ root; web và worker dùng cùng project và
`.venv` root với CLI:

```bash
uv sync --all-extras
uv run --locked yt-web
```

Mở http://127.0.0.1:8000. Không cần Node/build frontend. Một server/process,
không Uvicorn reload hoặc nhiều worker. `--help` xem cấu hình; có `--db`,
`--search-db`, `--state-db`, `--root`, `--port`, `--log-file`, `--log-level`.
Host chỉ `127.0.0.1|localhost`; đường dẫn tương đối của form tính từ root repo.

## Giao diện và API

Tìm kiếm, Thu thập và Thư viện cung cấp các tùy chọn CLI trong
mục **Nâng cao**, hàng đợi/lịch sử/hủy/chạy lại, tiến độ và tải JSON.
Thư viện đọc DB mặc định; yêu cầu collect/search có thể chọn DB riêng.

- Search chọn hybrid (mặc định, bật sẵn Cross-Encoder rerank và có nút tắt) /
  semantic (Vector) / keyword (BM25) / exact, model và các điều kiện chọn video.
  Mặc định 3 đoạn/video, hiển thị timestamp và
  trích dẫn từng đoạn; ngưỡng cosine dùng cho hybrid và semantic. `--top` tương ứng số video.
- Collect direct yêu cầu một ngôn ngữ; generate chọn engine/model và tự nhận dạng.
  Kênh thu toàn tab videos; thử nhanh bằng một URL video.

Khối **Điều kiện chọn video** không yêu cầu viết mã hay SQL:

- Chọn thuộc tính, phép so sánh và giá trị; kênh/ngôn ngữ/nguồn/loại phụ đề có
  lựa chọn sẵn từ thư viện. Kênh hiển thị tên, lọc theo ID để phân biệt tên trùng.
- Dùng **Thêm điều kiện**, **Thêm nhóm**, chọn **Tất cả/Bất kỳ** hoặc **Loại trừ
  nhóm**; nhóm có thể lồng nhau. Tiêu đề/mô tả nhập văn bản bình thường, phép
  “chứa” không yêu cầu ký tự wildcard; ngày chọn bằng lịch, tính theo UTC.
- Mặc định có điều kiện tiếng Việt, gồm cả biến thể vùng. Xóa điều kiện này để
  chọn ngôn ngữ khác; xóa hết để chọn mọi video/ngôn ngữ/nguồn. Điều kiện hoặc nhóm
  lồng chưa hoàn chỉnh sẽ chặn tìm kiếm, không âm thầm mở rộng phạm vi.

Model, cách tìm, số video và số đoạn vẫn áp dụng. Các tùy chọn model/đường dẫn nằm
trong **Tùy chọn nâng cao**.

Schema đầy đủ tại `/openapi.json`. POST cần `X-YT-Request: 1` và Origin cùng ứng
dụng nếu được gửi; UI đã cung cấp.

| Route | Chức năng |
| --- | --- |
| `/api/search` | query, bộ lọc hoặc `where`, `mode=hybrid|semantic|keyword|exact`, `rerank`, `moments_per_video` số nguyên dương (mặc định 3); kết quả nhóm `moments` theo video |
| `/api/collections` | collect và danh sách tác vụ |
| `/api/library`, `/api/videos` | thống kê/thư viện/video |
| `/api/logs`, `/api/logs/download` | tail theo cursor/mức/job và tải snapshot log hiện tại |

Search nhận `video_ids` list hoặc null: null là tất cả, list rỗng không video nào.
Link mở đầu cửa sổ, điểm không là xác suất đúng. Quy tắc chọn transcript và xếp hạng nằm trong
[ARCHITECTURE.md](../ARCHITECTURE.md#lọc-video-và-search).

Ví dụ body POST `/api/search` dùng biểu thức:

```json
{
  "query": "insulin",
  "mode": "keyword",
  "where": "channel IN ('Kênh sức khỏe', 'Kênh khoa học') AND language IN ('vi', 'en')"
}
```

Khi có `where`, không gửi cùng `channel`, `language`, `transcript_source`,
`source_mode` hoặc `video_ids`, kể cả giá trị mặc định `vi`/`auto` hoặc `null` được
ghi rõ. API trả 422 khi xung đột; phản hồi thành công có `where` đã dùng. Biểu thức
không có tiền tố `WHERE`; giá trị được viết trực tiếp như ví dụ, không dùng `?`.
`channel` là tên hiển thị; dùng `channel_id`/`channel_url` để lọc ID/URL.
[Các trường và toán tử hỗ trợ](../ARCHITECTURE.md#lọc-bằng-biểu-thức-where-trong-core).

## Worker, queue và log

Parent không import CLI/core. Các adapter `yt_web.workers.collector_runtime` và
`search_session` gọi core trực tiếp, sở hữu model/SQLite/cleanup; gateway JSON-lines
chạy bằng Python root. Factory injection `create_app`, `WebApplication` và gateway
được giữ. Input/cache/state/log kiểm tra cả alias và SQLite sidecars.

Generate chạy tuần tự và dùng khóa model chung, đóng search worker để
nhường GPU; direct có thể chạy cùng search. Queue/events ở `data/web.sqlite3`:
queued tiếp tục sau restart, running chuyển interrupted. Ctrl+C/SIGTERM dọn
nhóm worker/audio. SIGKILL server có thể để worker/audio tồn tại; restart không
quản lý worker cũ.

Panel **Nhật ký xử lý** có INFO/DEBUG, lọc job và tải log; ngừng polling khi đóng,
giữ 300 bản ghi gần nhất và báo khi cursor bị xoay/bỏ bản ghi. File DEBUG mặc định
`data/logs/web.jsonl` hoặc `logs/<tên-state>.jsonl` cạnh state DB đã đổi; 5 MiB + 3
backups. Không ghi toàn query/transcript/vector/cookie/token, log tách progress.
`HF_HOME` mặc định `data/models`, `HF_HUB_OFFLINE=1` chỉ dùng trọng số đã có.

## Kiểm tra

Chạy từ root sau `uv sync --all-extras`:

```bash
uv run --locked python -m unittest tests.test_web tests.test_web_parity tests.test_web_logging -q
uv run --locked python -m unittest tests.test_cli_e2e tests.test_web_e2e -q
```

Web E2E khởi động server HTTP và search worker thật, kiểm tra UI assets, thư viện,
tìm kiếm keyword/exact, bộ lọc, lỗi và lịch sử sau restart bằng SQLite fixture
riêng. CLI E2E chạy các entrypoint đã cài. Các test này không gọi YouTube thật
hoặc tải model; collection chỉ kiểm tra validation/lỗi, chưa xác minh thu thập
direct/generate thành công hoặc chất lượng tìm kiếm dùng model.

Kiểm tra thao tác giao diện bằng Chrome có thể chạy riêng; Chrome và công cụ
điều khiển trình duyệt không là dependency của bộ test chuẩn.
