# Ghi chú cho agent

## Planner và workers

- Planner ưu tiên phân rã yêu cầu, chốt hợp đồng API, chia phạm vi, điều phối, tích hợp và review. Giao workers đọc code, triển khai chi tiết và chạy kiểm tra để giảm token/ngữ cảnh ở planner; planner chỉ trực tiếp làm việc rất nhỏ, độc lập hoặc tích hợp chung cần thứ tự. Không ép song song các phần phụ thuộc nhau.
- Giao mỗi worker mục tiêu, phạm vi file sở hữu, đầu vào/đầu ra, ràng buộc và kiểm tra cần chạy. Mỗi file có một chủ sở hữu; tích hợp thay đổi chung theo thứ tự. Không ghi đè/reset thay đổi của người dùng hoặc agent khác.
- Workers đọc hướng dẫn và code liên quan, báo sớm xung đột/blocker; không tự mở rộng phạm vi, thêm dependency hoặc worker con ngoài kế hoạch. Bàn giao ngắn gọn thay đổi/diff, file đã sửa, lệnh và kết quả kiểm tra thực tế, giả định và rủi ro còn lại.
- Planner dựa vào bàn giao để review diff và phần code liên quan, tránh đọc hoặc làm lại toàn bộ phần worker. Review tính đúng, regression, hợp đồng dữ liệu và vòng đời tài nguyên; giao lại worker sở hữu khi cần sửa. Chỉ hoàn thành sau tích hợp và kiểm tra liên quan, không chỉ chuyển tiếp báo cáo.
- Chỉ tách module khi có trách nhiệm và ranh giới phụ thuộc phân biệt rõ; không chia file chỉ để giảm số dòng.
- Hỏi người dùng khi cần quyết định thiết kế đáng kể; tiếp tục phần độc lập trong lúc chờ. Chạy kiểm tra theo phạm vi thay đổi; phân biệt test adapter/kiểm tra tĩnh, E2E ứng dụng và kiểm tra trình duyệt.

## Ràng buộc bền vững

- Toàn repo là **một project `yt-finder`**, dùng `pyproject.toml`, `.venv` và `uv.lock` tại root; các package thư viện/ứng dụng đều nằm trong `src/`. Cài bằng `uv sync --all-extras`, chạy từ root bằng `uv run --locked ...`; các nhóm mặc định giữ dev/Whisper/GPU. Không tạo project, môi trường hoặc lock riêng cho từng package.
- `transcript_collector` và `video_search` là hai package thư viện trong `src/`, đóng gói chung bởi project `yt-finder` tại root; không có project/`pyproject.toml` riêng. Giữ ranh giới nghiệp vụ: không import nhau hoặc ứng dụng, không có `service.py`, CLI, transport worker, `print/input/SystemExit` hoặc tự đổi `os.environ`.
- Điều phối, khởi tạo/đóng tài nguyên, terminal và môi trường tiến trình thuộc CLI/web. Web parent không import core/CLI; chỉ worker adapter import core tương ứng. `src/yt_workflows` cung cấp đánh giá, không là thư viện nghiệp vụ thứ ba. Giữ injection `yt_web.create_app` và các gateway.
- Collector chỉ lưu inventory/transcript vào SQLite schema v2. Video là đơn vị xử lý nhỏ nhất, tuần tự; kênh chỉ tab `videos`. Dùng duy nhất yt-dlp cho danh sách/phụ đề/audio, không tự thêm backend, engine, thread pool hay tùy chọn retry/giới hạn video. Audio generate chỉ là file tạm và phải dọn khi thành công/lỗi/Ctrl+C.
- Giữ quy tắc direct/generate, chọn ngôn ngữ/track, cache và transaction trong [ARCHITECTURE.md](ARCHITECTURE.md). Direct bắt buộc một ngôn ngữ; generate tự nhận dạng. Không dùng ngôn ngữ khác hoặc tự chuyển mode khi thiếu phụ đề. Không thêm lại nguồn `legacy`.
- Search tách `VideoFilter.select(VideoFilters)` → danh sách video kèm transcript bất biến → `SearchEngine.search(query, videos)`. Ranker không đọc DB transcript hay tự lọc lại. Giữ semantic/keyword riêng, mặc định MiniLM; nhiều đoạn không chồng lấn, mặc định 3/video, `--top` tính video. Hybrid kết hợp BM25/vector và rerank tùy chọn theo [ARCHITECTURE.md](ARCHITECTURE.md).
- Web chọn điều kiện bằng giao diện no-code, không yêu cầu người dùng nhập SQL; core/CLI giữ API và cú pháp WHERE.
- Hợp đồng SQLite v2 và cache nằm trong [ARCHITECTURE.md](ARCHITECTURE.md): collector chỉ migrate schema đã biết, search chỉ đọc và không migrate input. Inventory commit trước tải, transcript+parts cùng transaction; refresh lỗi giữ bản cũ. Cache/search/state/log/report phải khác input, kể cả symlink/hardlink và SQLite sidecars.
- Không tự xóa dữ liệu lưu bền, cache, model hoặc database. Audio tạm phải được dọn theo lifecycle generate khi thành công/lỗi/Ctrl+C. Đổi chuẩn hóa/mã hóa phải tăng phiên bản cache liên quan. Test fixture search tạo schema độc lập với triển khai collector.
- Giữ xử lý đồng bộ, search session dùng tuần tự trên luồng tạo/đóng SQLite. Web có một chủ sở hữu state DB, queued tiếp tục sau restart/running chuyển interrupted; generate nhường GPU bằng cách đóng search worker và dùng khóa chung. Giữ cleanup nhóm tiến trình/audio cho shutdown có thể xử lý.
- Core dùng stdlib logging/callback có cấu trúc; lỗi callback collector là `EventDeliveryError`, không là lỗi nguồn. Ứng dụng sở hữu handler/rotation; không ghi query/transcript/vector/credential vào log hoặc log vào DB nghiệp vụ. Không tắt cảnh báo Pyrefly hàng loạt.

## Xác minh và tài liệu

- Cài đặt nhanh: [README.md](README.md). Lệnh chạy/kiểm tra và tùy chọn: [docs/USAGE.md](docs/USAGE.md). Hợp đồng chi tiết: [ARCHITECTURE.md](ARCHITECTURE.md). Đánh giá và giới hạn nhãn: [evaluation/README.md](evaluation/README.md).
- Chạy kiểm tra phù hợp thay đổi, không tải model/collect toàn kênh chỉ để test. YouTube thật: chọn rõ một URL video, dùng DB riêng trong `/tmp`; không dùng đầu vào kênh để thử nhanh. Mô hình/audio lớn không đặt cache trên tmpfs nhỏ.
- Test giả không chứng minh kết nối YouTube, chất lượng transcript hay chất lượng toàn kho. Snapshot đánh giá cần metadata v2, cùng tập cửa sổ cho các model; nhãn agent chưa được người dùng duyệt. Không giả định artifact `/tmp` còn tồn tại; không dùng số đo cũ để khẳng định chất lượng hiện tại.
