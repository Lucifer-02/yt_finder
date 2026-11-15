# BÁO CÁO TOÀN DIỆN VỀ RÀ SOÁT VÀ PHÂN TÍCH LỖI TIỀM NĂNG TRONG CORE YT-FINDER
*(Comprehensive Core Defect Audit Report — `yt-finder`)*

- **Dự án**: `yt-finder` (Hệ thống thu thập phụ đề và tìm kiếm ngữ nghĩa video YouTube)
- **Phạm vi kiểm toán (Audit Scope)**: Hai thư viện lõi tại `src/`:
  - `src/transcript_collector`
  - `src/video_search`
- **Tài liệu tham chiếu chuẩn (Architectural References)**: `AGENTS.md`, `ARCHITECTURE.md`, `PROJECT.md`
- **Môi trường thực thi & Kiểm chứng**: Python 3.13 (`.venv/bin/python`), Linux (Ubuntu/Debian)
- **Tình trạng mã nguồn**: Tuân thủ tuyệt đối nguyên tắc **Read-Only / Integrity Mandate** (0 dòng mã trong `src/` và `tests/` bị chỉnh sửa).

---

## 1. Tóm tắt điều hành (Executive Summary)

Đợt rà soát kỹ thuật chuyên sâu này được thực hiện nhằm kiểm tra toàn diện chất lượng kiến trúc, mức độ an toàn dữ liệu, tính bền vững của vòng đời tài nguyên và sự tuân thủ các ràng buộc nghiệp vụ trong hai package cốt lõi (`transcript_collector` và `video_search`) của dự án `yt-finder`.

### 1.1 Tình trạng hiện thời của hệ thống
- **Hiện trạng kiểm thử (Test Baseline)**: Bộ kiểm thử đơn vị hiện hành (`.venv/bin/python -m unittest discover tests`) vượt qua 100% (**255 tests passed in 21.6s**).
- **Kiểm tra cú pháp & Kiểu dữ liệu tĩnh**: Lệnh `ruff check` và `pyrefly check` trên hai package core đạt chuẩn sạch (**0 violations, 0 type errors**).
- **Phân tách ranh giới gói (Package Boundary)**: Cả hai package độc lập hoàn toàn, không import lẫn nhau, không import mã ứng dụng tầng trên (`yt_cli`, `yt_web`, `yt_workflows`), không sử dụng các hàm cấm (`print`, `input`, `SystemExit`, `os.environ`), và duy trì mô hình xử lý hoàn toàn đồng bộ (synchronous).

### 1.2 Kết quả rà soát chi tiết
Mặc dù hệ thống vượt qua bộ test suite cơ sở, việc phân tích tĩnh từng dòng mã (line-by-line static analysis) kết hợp kiểm thử động các ca biên (edge-case dynamic verification) đã phát hiện **26 lỗi tiềm ẩn (defects), khiếm khuyết thiết kế (design flaws), vi phạm hợp đồng dữ liệu (contract violations) và lỗ hổng xử lý ngoại lệ (unhandled exception leaks)**:
- **8 lỗi mức độ Nghiêm trọng Cao (High Severity)**: Có thể gây crash tiến trình đang chạy (`sqlite3.IntegrityError`, `TypeError`), gây lãng phí băng thông mạng dẫn tới việc YouTube khóa IP/rate limit HTTP 429 (`SourceBlocked`), từ chối các URL kênh YouTube hợp lệ, hoặc làm mất mát kết quả tìm kiếm (moment starvation) và crash khi đổi chế độ tìm kiếm.
- **10 lỗi mức độ Nghiêm trọng Trung bình (Medium Severity)**: Gây gián đoạn chuỗi vòng lặp quét phụ đề ứng viên khi gặp lỗi mạng/JSON, phân loại sai video không có giọng nói thành lỗi nghiêm trọng làm hỏng trạng thái job, phát sai thứ tự sự kiện telemetry (`video_finished` không có `video_started`), cho phép lưu các bản ghi phụ đề rác không thể tìm kiếm, làm giảm hiệu năng nghiêm trọng do FTS5 table thrashing, hoặc thiếu kiểm tra ranh giới ghi đè trên cơ sở dữ liệu phụ đề.
- **8 lỗi mức độ Nghiêm trọng Thấp (Low Severity)**: Liên quan đến cơ chế tự phát hiện CUDA phụ thuộc cứng vào cấu trúc thư mục pip wheel, nuốt lỗi hoặc phát sinh lỗi thứ cấp trong khối xử lý ngoại lệ, thiếu tiền kiểm tra tệp âm thanh đầu vào, và lỗi mất tính bằng nhau của bộ lọc khi tham số chứa số thực `NaN`/`Inf`.

Tất cả 26 lỗi đều đã được thẩm định nguyên nhân gốc rễ, đối chiếu trực tiếp với các điều khoản trong `AGENTS.md` và `ARCHITECTURE.md`, kèm theo đoạn mã vi phạm nguyên bản và kịch bản tái hiện độc lập.

---

## 2. Ma trận phân loại và mức độ nghiêm trọng (Defect Severity Matrix)

| ID | Package | Module & Dòng | Tên Lỗi / Khiếm Khuyết | Mức Độ | Điều Khoản Kiến Trúc Vi Phạm |
|:---|:---|:---|:---|:---:|:---|
| **TC-01** | `transcript_collector` | `pipeline.py:402–414` | Lấy siêu dữ liệu mạng dư thừa đối với các video playlist đã có trong cache | **High** | yt-dlp constraints & Playlist Caching |
| **TC-02** | `transcript_collector` | `youtube.py:220–226` | Chuẩn hóa URL kênh từ chối các tab YouTube phổ biến (`/about`, `/playlists`, v.v.) | **High** | Kênh YouTube & URL Normalization |
| **TC-03** | `transcript_collector` | `youtube.py:553–563`, `435–438` | `parse_json3` sập với `TypeError` khi gặp timestamp/duration là `null`, ngắt luôn vòng lặp fallback | **High** | Subtitle parsing & Fallback Hierarchy |
| **TC-04** | `transcript_collector` | `store.py:53–70` | Kiểm tra tính tương thích DB bỏ qua các bảng lạ, tự ý kích hoạt migration v1→v2 sai mục tiêu | **High** | SQLite schema v2 atomic migration |
| **TC-05** | `transcript_collector` | `pipeline.py:107–115` | `save_inventory` gọi ngoài `try...except`, vô hiệu hóa hàm kiểm tra ID và gây crash batch | **High** | Two-phase video commit & Error isolation |
| **TC-06** | `transcript_collector` | `youtube.py:433–438` | Lỗi mạng khi tải track hoặc lỗi JSON decode làm đứt ngang chuỗi duyệt track ứng viên | **Medium** | Subtitle candidate loop & Fallback |
| **TC-07** | `transcript_collector` | `engines/whisper.py:130–131`, `pipeline.py:221–257` | Video không có tiếng nói trong generate mode bị phân loại thành lỗi (`errors=1`) thay vì unavailable | **Medium** | Direct vs Generate mode symmetry |
| **TC-08** | `transcript_collector` | `pipeline.py:426–447` | Phát sự kiện `video_finished` nhưng không phát `video_started` khi phân giải video thất bại | **Medium** | Structured Telemetry & Event sequence |
| **TC-09** | `transcript_collector` | `store.py:222–231` | `save_video` không kiểm tra `source_mode` cho generate mode, tạo dữ liệu mồ côi ngoài tầm search | **Medium** | SQLite schema v2 & Cross-package contract |
| **TC-10** | `transcript_collector` | `youtube.py:231–234` | Phân giải kênh thất bại khi kênh có tab `/videos` rỗng mặc dù người dùng truyền đúng UC ID | **Medium** | Channel ID resolution resilience |
| **TC-11** | `transcript_collector` | `store.py:188–190` | `store.cached()` sinh câu lệnh SQL lỗi (`source_mode = NULL`) khi `source_mode=None` cho generate | **Medium** | SQL three-valued logic & Cache contract |
| **TC-12** | `transcript_collector` | `engines/whisper.py:36–53` | Cơ chế dò tìm CUDA phụ thuộc cứng vào cấu trúc thư mục bánh xe pip (`site-packages/nvidia`) | **Low** | Hardware acceleration discovery |
| **TC-13** | `transcript_collector` | `pipeline.py:258–259`, `436` | Lỗi thứ cấp `sqlite3.Error` trong `store.has_transcript()` làm sập khối bắt ngoại lệ | **Low** | Secondary error handling resilience |
| **TC-14** | `transcript_collector` | `engines/whisper.py:110–113` | Timestamp phân đoạn từ Whisper không được tiền kiểm tra tính hợp lệ trước khi lưu | **Low** | Data sanitization & Schema consistency |
| **TC-15** | `transcript_collector` | `pipeline.py:31–53` | `_validate_options` không từ chối tham số `language` trong chế độ `generate` | **Low** | API parameter validation consistency |
| **TC-16** | `transcript_collector` | `config.py:7` | Đường dẫn `DATA_DIR` phụ thuộc cứng vào vị trí mã nguồn `__file__` | **Low** | Configuration & Deployment portability |
| **TC-17** | `transcript_collector` | `engines/whisper.py:101` | Whisper engine không kiểm tra sự tồn tại của tệp âm thanh trước khi gọi mô hình | **Low** | Input sanity & Error classification |
| **VS-01** | `video_search` | `cache.py:174–194`, `search.py:85–90` | Sập `sqlite3.IntegrityError: UNIQUE constraint failed: lexical_rows.key` khi có passage trùng | **High** | Cache schema & BM25 indexing integrity |
| **VS-02** | `video_search` | `search.py:493–542`, `416–466` | Cắt xén ứng viên trong `_rerank` gây thiếu hụt đoạn trích (moment starvation) do triệt tiêu trùng lặp | **High** | Multi-moment aggregation contract |
| **VS-03** | `video_search` | `search.py:146–168`, `352–353` | Cache signature cũ khi chuyển đổi chế độ gây sập `RuntimeError: Embedding model was not initialized` | **High** | SearchEngine lifecycle & Mode switching |
| **VS-04** | `video_search` | `search.py:78–81` | Ngoại lệ chưa bắt `TypeError` trong `exact_scores` khi `passage["title"]` là `None` | **Medium** | Safe metadata normalization |
| **VS-05** | `video_search` | `models.py:56–58` | Chuỗi truy cập thuộc tính mong manh trong `RetrievalModel.__init__` gây lỗi trên mô hình tùy biến | **Medium** | HuggingFace model wrapper resilience |
| **VS-06** | `video_search` | `cache.py:156–206` | Xóa và tái tạo bảng ảo FTS5 toàn cục liên tục khi tìm kiếm trên các tập hợp video khác nhau | **Medium** | Persistent derived indexing & I/O churn |
| **VS-07** | `video_search` | `cache.py:32–55` | `EmbeddingStore` mở kết nối đọc-ghi trực tiếp mà không kiểm tra nguy cơ va chạm với DB phụ đề | **Medium** | Search read-only isolation contract |
| **VS-08** | `video_search` | `predicate.py:223–233` | Tham số `float('nan')` và `Inf` làm sai lệch ngữ nghĩa SQL và phá vỡ cơ chế tái sử dụng cache | **Low** | WHERE compiler type safety & Invalidation |
| **VS-09** | `video_search` | `search.py:509–519`, `models.py:130–132` | Không kiểm tra ngân sách token trước khi rerank cho các câu truy vấn dài với mô hình 512 token | **Low** | Cross-Encoder token budget handling |

---

## 3. Đối chiếu tuân thủ kiến trúc và hợp đồng kỹ thuật (`AGENTS.md` & `ARCHITECTURE.md`)

### 3.1 Ranh giới phân tầng package (Package Boundaries & Isolation)
- **Hợp đồng quy định**:
  - `transcript_collector` và `video_search` là hai thư viện độc lập nằm trong `src/`, không được import chéo lẫn nhau, không import mã ứng dụng (`yt_cli`, `yt_web`, `yt_workflows`).
  - Tuyệt đối không chứa `service.py`, CLI entrypoint, transport worker, không gọi `print()`, `input()`, `SystemExit` và không tự ý thay đổi `os.environ`.
- **Kết quả đối chiếu**:
  - **ĐẠT**: Phân tích AST và quét biểu thức chính quy khẳng định cả hai package hoàn toàn không chứa bất kỳ câu lệnh import chéo hay import ứng dụng nào. Không có tệp `service.py`, không gọi hàm cấm. Mọi tương tác đều thông qua các lớp và hàm API thuần túy.
  - **Khiếm khuyết phát hiện**: Khởi tạo `EmbeddingStore` trong `cache.py` cho phép tạo bảng cache trên bất kỳ file SQLite nào mà không kiểm tra xem tệp đó có phải là cơ sở dữ liệu phụ đề hay không (Xem lỗi **VS-07**).

### 3.2 Hợp đồng lưu trữ SQLite schema v2 & Transaction (SQLite Schema v2 & Atomic Commits)
- **Hợp đồng quy định**:
  - Collector chịu trách nhiệm quản lý cấu trúc schema v2 (`channels`, `videos`, `transcripts`, `transcript_parts`) với `PRAGMA foreign_keys=ON` và WAL mode.
  - Chỉ nâng cấp (migrate) schema v1 đã biết lên v2 một cách nguyên tử; không được tự ý migrate hoặc xóa schema không nhận diện.
  - Quy trình commit hai pha: Cam kết thông tin video (`save_inventory`) trước khi tải media; cam kết phụ đề và các phân đoạn (`save_video`) trong cùng một transaction nguyên tử; khi cập nhật làm mới thất bại phải giữ nguyên bản ghi phụ đề cũ.
  - Search chỉ mở cơ sở dữ liệu phụ đề ở chế độ chỉ đọc (`?mode=ro`), thẩm tra `PRAGMA user_version == 2` và không bao giờ migrate cơ sở dữ liệu đầu vào.
- **Kết quả đối chiếu**:
  - **ĐẠT MỘT PHẦN**: `TranscriptStore.save_video` thực thi toàn bộ thao tác trong `with self.db:`, xóa phân đoạn cũ và chèn phân đoạn mới trong cùng transaction. `Corpus` trong `video_search` mở DB với URI `?mode=ro` và kiểm tra phiên bản.
  - **Khiếm khuyết nghiêm trọng phát hiện**:
    1. `store.py:53–70`: Vòng lặp kiểm tra tính tương thích của `TranscriptStore` chỉ duyệt qua các bảng trong `COLUMNS.keys()` mà không kiểm tra xem cơ sở dữ liệu có chứa bảng lạ nào khác không. Nếu người dùng trỏ nhầm vào một DB có chứa các bảng hệ thống khác kèm phiên bản `user_version = 1`, collector sẽ tự ý chạy `_migrate_v1()` và xóa/tạo lại bảng (Lỗi **TC-04**).
    2. `pipeline.py:107`: Lệnh `store.save_inventory([video])` nằm NGOÀI khối `try...except`, khiến cho lỗi khóa ngoại hoặc định dạng ID không được xử lý an toàn, vi phạm nguyên tắc lỗi một video phải tiếp tục xử lý các video tiếp theo (Lỗi **TC-05**).
    3. `store.py:222–231`: `save_video` bỏ quên bước kiểm tra giá trị `source_mode` cho nguồn `generate`, cho phép ghi các bản ghi có `source_mode` nằm trong danh sách đen của bộ tìm kiếm (`manual`, `auto_generated`, `translated`, `legacy`, `youtube`), tạo ra dữ liệu mồ côi (Lỗi **TC-09**).

### 3.3 Vòng đời tệp âm thanh tạm thời (Temporary Audio Lifecycle)
- **Hợp đồng quy định**:
  - Âm thanh tải về phục vụ nhận dạng giọng nói (`generate` mode) chỉ là tệp tạm thời và BẮT BUỘC phải được dọn dẹp sạch sẽ khi thành công, khi gặp ngoại lệ, hoặc khi người dùng ngắt bằng Ctrl+C (SIGINT).
- **Kết quả đối chiếu**:
  - **ĐẠT TUYỆT ĐỐI**: Trong `pipeline.py`, thao tác tải âm thanh được bọc chặt chẽ trong `with tempfile.TemporaryDirectory(prefix="yt-collect-") as temporary:`. Cơ chế Context Manager của Python đảm bảo phương thức `__exit__` luôn được kích hoạt trong quá trình unwinding stack, bất kể tiến trình kết thúc bình thường, gặp lỗi `Exception`, hay bị ngắt bởi tín hiệu `KeyboardInterrupt`. Thư mục tạm và toàn bộ tệp bên trong được xóa sạch hoàn toàn khỏi đĩa cứng.

### 3.4 Mô hình xử lý đồng bộ & Quản lý tiến trình (Synchronous Processing)
- **Hợp đồng quy định**:
  - Xử lý hoàn toàn đồng bộ, không dùng `async/await`, không tự tạo thread pool, worker thread hay tiến trình ngầm trong thư viện lõi.
  - Kết nối SQLite được khởi tạo và sử dụng tuần tự trên chính luồng gọi (thread-affinity).
- **Kết quả đối chiếu**:
  - **ĐẠT**: Không có bất kỳ coroutine `async/await`, `threading.Thread`, hay `concurrent.futures` nào trong cả hai thư viện core. Mọi thao tác truy vấn và ghi cơ sở dữ liệu đều diễn ra đồng bộ trên luồng hiện hành.
  - **Lưu ý**: Lớp `SearchEngine` tái sử dụng bộ nhớ embedding trong cùng một phiên xử lý, nhưng thiếu cơ chế dọn dẹp cache signature khi thay đổi chế độ tìm kiếm, dẫn tới ngoại lệ khi chuyển đổi giữa lexical và dense (Lỗi **VS-03**).

### 3.5 Ràng buộc yt-dlp & Chiến lược cache danh sách phát (yt-dlp & Playlist Caching)
- **Hợp đồng quy định**:
  - Sử dụng duy nhất `yt-dlp` cho việc trích xuất danh sách, phụ đề và tải âm thanh; không tự ý thêm backend hay engine ngoài.
  - Đảm bảo kiểm tra cache trước khi thực hiện các yêu cầu mạng đắt đỏ; tránh gửi các truy vấn thừa gây rate-limit HTTP 429 (`SourceBlocked`).
- **Kết quả đối chiếu**:
  - **VI PHẠM NGHIÊM TRỌNG**: Trong `pipeline.py` hàm `_index_videos` (dòng 402–414), khi thu thập playlist, vòng lặp luôn gọi `resolve()` (thực thi `_extract(extract_flat=False, writeautomaticsub=True)`) qua mạng đối với TOÀN BỘ video ID trước khi gọi `index_video` (nơi kiểm tra `store.cached`). Điều này dẫn tới việc quét lại một playlist đã lưu trong cache vẫn tạo ra hàng chục/hàng trăm HTTP requests thừa, gây trễ hàng phút và dẫn tới việc YouTube khóa IP (Lỗi **TC-01**).

### 3.6 Tính đối xứng giữa Direct Mode và Generate Mode (Mode Symmetry)
- **Hợp đồng quy định**:
  - `direct`: Bắt buộc chỉ định một ngôn ngữ cụ thể, chỉ lấy phụ đề có sẵn trên YouTube, không chuyển đổi mode khi thiếu phụ đề.
  - `generate`: Tự động nhận diện ngôn ngữ thông qua Whisper, không nhận tham số `--language`.
  - Phân loại lỗi nhất quán: Thiếu phụ đề hoặc âm thanh không có tiếng nói phải được tính là `unavailable` (không khả dụng), không được tính là lỗi tiến trình làm thất bại cả mẻ quét (`errors=1`).
- **Kết quả đối chiếu**:
  - **VI PHẠM**: Trong `engines/whisper.py`, khi tệp âm thanh không có giọng nói (video nhạc, video yên lặng), hàm ném `TranscriptionError("No speech detected in audio.")`. Trong khi đó, `pipeline.py` dòng 254 chỉ coi `CaptionsUnavailable` và `VideoNotReady` là `unavailable=1`, còn `TranscriptionError` bị tính vào `errors=1`. Hệ quả là mẻ quét bị đánh dấu thất bại (`CollectionResult.failed == True`) dù video hoàn toàn bình thường (Lỗi **TC-07**).

### 3.7 Bảo mật nhật ký, Không rò rỉ PII & Event Callbacks (Logging & Structured Events)
- **Hợp đồng quy định**:
  - Sử dụng thư viện tiêu chuẩn `logging` với cấu trúc `extra={...}`.
  - Tuyệt đối không ghi câu truy vấn người dùng (raw queries), nội dung phụ đề (transcript text), vector nhúng hay thông tin xác thực vào log.
  - Lỗi phát sinh từ callback người dùng phải được bao bọc trong `EventDeliveryError`.
- **Kết quả đối chiếu**:
  - **ĐẠT TUYỆT ĐỐI VỀ BẢO MẬT**: Kiểm toán toàn bộ 72 điểm gọi logging xác nhận không có bất kỳ văn bản phụ đề hay câu truy vấn người dùng nào bị lọt vào log; chỉ ghi lại độ dài chuỗi (`len(query)`), số lượng phân đoạn và kích thước ma trận. Callback lỗi được bọc chính xác trong `EventDeliveryError`.
  - **Khiếm khuyết chuỗi sự kiện**: Trong `pipeline.py:426–447`, nếu việc phân giải siêu dữ liệu video thất bại, hệ thống phát ngay sự kiện `video_finished` mà trước đó chưa từng phát sự kiện `video_started`, làm hỏng tính toàn vẹn của các bộ theo dõi trạng thái tiến trình giao diện (Lỗi **TC-08**).

---

## 4. Danh mục chi tiết các lỗi trong `src/transcript_collector`

### TC-01: Lấy siêu dữ liệu mạng dư thừa đối với các video playlist đã có trong cache
- **Mức độ nghiêm trọng**: **High**
- **Vị trí mã nguồn**: `src/transcript_collector/pipeline.py`, hàm `_index_videos`, dòng 402–414
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/pipeline.py:402-414
  for n, entry in enumerate(videos, 1):
      video_id = entry if isinstance(entry, str) else entry.id
      try:
          if isinstance(entry, str):
              if resolve is None:
                  raise ValueError("A video resolver is required for video IDs.")
              logger.info(
                  "Video %s metadata resolution started",
                  video_id,
                  extra={"phase": "metadata_resolve", "video_id": video_id},
              )
              channel, video = resolve(f"https://www.youtube.com/watch?v={entry}")
              store.save_channel(channel)
          else:
              video = entry
          result = index_video(
              source,
              store,
              video,
              language,
              refresh,
              transcriber,
              mode=mode,
              position=n,
              total=len(videos),
              on_event=on_event,
          )
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Trong quá trình thu thập danh sách phát (`index_playlist`), `source.playlist_videos(url)` trả về danh sách các chuỗi ID video (`list[str]`). Trong vòng lặp `_index_videos`, điều kiện `isinstance(entry, str)` luôn trả về `True`. Hàm `resolve()` thực thi `YouTube.resolve_video()`, gửi yêu cầu mạng `_extract(extract_flat=False, writeautomaticsub=True)` đến YouTube để lấy đầy đủ siêu dữ liệu TRƯỚC KHI gọi `index_video`. Trong khi đó, việc kiểm tra cache (`store.cached`) lại nằm bên trong `index_video`. Do đó, khi người dùng chạy lại lệnh quét trên một playlist đã được lưu trữ toàn bộ trong cơ sở dữ liệu địa phương, hệ thống vẫn thực hiện đầy đủ các yêu cầu mạng cho từng video một.
- **Rủi ro & Tác động vận hành**:
  Với playlist gồm 50–100 video đã có sẵn phụ đề trong SQLite, việc chạy lại tốn thêm từ 2–4 phút do khoảng nghỉ bắt buộc `sleep_interval_requests=2` của yt-dlp. Nguy hiểm hơn, việc gửi hàng loạt truy vấn mạng không cần thiết dễ dàng kích hoạt cơ chế bảo vệ của YouTube, gây lỗi HTTP 429 (`SourceBlocked`), khiến toàn bộ tiến trình quét playlist bị đình trệ vô cớ trên tập dữ liệu đã thu thập xong.
- **Đề xuất khắc phục**:
  Trong `_index_videos`, khi `isinstance(entry, str)` và `not refresh`, kiểm tra `store.cached(video_id, ...)` trước khi gọi `resolve()`. Nếu đã có trong cache, bỏ qua cuộc gọi mạng `resolve()` và chuyển tiếp vào `index_video` hoặc cập nhật biến đếm `counts["cached"] += 1`.
- **Kịch bản kiểm chứng độc lập**:
  ```bash
  .venv/bin/python -c '
  from unittest.mock import Mock
  from transcript_collector.contracts import Channel, Segment, Video
  from transcript_collector.pipeline import index_playlist
  from transcript_collector.store import TranscriptStore
  import tempfile
  from pathlib import Path

  tmp = tempfile.TemporaryDirectory()
  store = TranscriptStore(str(Path(tmp.name) / "test.db"))
  source = Mock(transcript_language="vi", transcript_mode="manual")
  source.playlist_videos.return_value = ["v1", "v2"]
  source.resolve_video.side_effect = lambda url: (Channel("c1", "title", "url"), Video(url.split("v=")[1], "c1", "title"))
  source.transcript.return_value = [Segment("hello", 0, 1)]

  index_playlist(source, store, "https://www.youtube.com/playlist?list=PL1", "vi", mode="direct")
  calls_run1 = source.resolve_video.call_count
  index_playlist(source, store, "https://www.youtube.com/playlist?list=PL1", "vi", mode="direct")
  calls_run2 = source.resolve_video.call_count
  print(f"Lần 1: {calls_run1} calls, Lần 2 (đã cached hoàn toàn): {calls_run2 - calls_run1} redundant calls")
  '
  # Kết quả: Lần 2 vẫn gọi resolve_video thêm 2 lần dù dữ liệu đã cached.
  ```

---

### TC-02: Chuẩn hóa URL kênh từ chối các tab YouTube phổ biến (`/about`, `/playlists`, v.v.)
- **Mức độ nghiêm trọng**: **High**
- **Vị trí mã nguồn**: `src/transcript_collector/youtube.py`, hàm `YouTube.resolve`, dòng 220–226
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/youtube.py:220-226
              path = unquote(parsed.path).rstrip("/")
              path = re.sub(r"/(videos|shorts|streams|featured)$", "", path)
              if not re.fullmatch(r"/(?:@[\w.-]+|(?:channel|c|user)/[\w.-]+)", path):
                  raise SourceError(
                      "This is not a channel URL. Use its @handle or /channel/UC… URL."
                  )
              url = f"https://www.youtube.com{path}"
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Biểu thức regex ở dòng 221 chỉ thực hiện cắt bỏ 4 hậu tố tab cụ thể: `/(videos|shorts|streams|featured)$`. Khi người dùng sao chép liên kết kênh từ trình duyệt web trong khi đang đọc thông tin kênh ở các tab chuẩn khác của YouTube như `/about`, `/playlists`, `/podcasts`, `/community` hoặc `/live` (ví dụ: `https://www.youtube.com/@HubermanLab/about`), hậu tố này không bị cắt bỏ. Khi đó, phép so khớp `re.fullmatch` ở dòng 222 thất bại, khiến hệ thống lập tức ném ngoại lệ `SourceError("This is not a channel URL. Use its @handle or /channel/UC… URL.")`.
- **Rủi ro & Tác động vận hành**:
  Người dùng cuối và các công cụ tự động sao chép URL kênh từ các tab thông tin giới thiệu hoặc danh sách phát của kênh sẽ bị từ chối thẳng thừng với thông báo lỗi gây hiểu nhầm, làm giảm trải nghiệm người dùng nghiêm trọng.
- **Đề xuất khắc phục**:
  Chuẩn hóa đường dẫn bằng cách trích xuất trực tiếp tiền tố định danh kênh thay vì loại trừ thủ công từng tab:
  ```python
  match = re.match(r"^/(?:@[\w.-]+|(?:channel|c|user)/[\w.-]+)", path)
  if not match:
      raise SourceError("This is not a channel URL. Use its @handle or /channel/UC… URL.")
  url = f"https://www.youtube.com{match.group(0)}"
  ```
- **Kịch bản kiểm chứng độc lập**:
  ```bash
  .venv/bin/python -c '
  from transcript_collector.youtube import YouTube
  yt = YouTube("vi")
  urls = [
      "https://www.youtube.com/@HubermanLab/about",
      "https://www.youtube.com/@HubermanLab/playlists",
      "https://www.youtube.com/@HubermanLab/podcasts",
      "https://www.youtube.com/@HubermanLab/community",
  ]
  for u in urls:
      try:
          yt.resolve(u)
      except Exception as e:
          print(f"{u} -> BỊ TỪ CHỐI: {e}")
  '
  # Kết quả: Cả 4 URL hợp lệ trên đều bị từ chối với SourceError.
  ```

---

### TC-03: `parse_json3` sập với `TypeError` khi gặp timestamp/duration là `null`, ngắt luôn vòng lặp fallback
- **Mức độ nghiêm trọng**: **High**
- **Vị trí mã nguồn**: `src/transcript_collector/youtube.py`, hàm `parse_json3`, dòng 553–563 và `YouTube.transcript`, dòng 435–438
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/youtube.py:553-563
  def parse_json3(data: dict) -> list[Segment]:
      segments = []
      for event in data.get("events", []):
          text = "".join(s.get("utf8", "") for s in event.get("segs", [])).strip()
          if text and "tStartMs" in event:
              start = float(event["tStartMs"]) / 1000
              duration = float(event.get("dDurationMs", 0)) / 1000
              if (
                  not math.isfinite(start)
                  or not math.isfinite(duration)
                  or start < 0
                  or duration < 0
              ):
                  raise SourceError("Invalid caption timing.")
              segments.append(Segment(text, start, duration))
      if not segments:
          raise CaptionsUnavailable("Transcript contains no text.")
      return segments
  ```
  Và tại khối gọi:
  ```python
  # src/transcript_collector/youtube.py:435-438
                                  try:
                                      segments = parse_json3(data)
                                  except CaptionsUnavailable:
                                      continue
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Trong cấu trúc dữ liệu JSON3 của phụ đề YouTube (đặc biệt là các đoạn kết thúc hoặc phụ đề tự động), trường `"dDurationMs"` hoặc `"tStartMs"` đôi khi mang giá trị `null` (trong Python parse thành `None`). Cú pháp `event.get("dDurationMs", 0)` sẽ trả về `None` nếu khóa `"dDurationMs"` tồn tại với giá trị `None`. Lệnh `float(None)` ngay lập tức ném ngoại lệ `TypeError: float() argument must be a string or a real number, not 'NoneType'`.
  Nghiêm trọng hơn, trong `YouTube.transcript`, khối `try` bao quanh `parse_json3` CHỈ BẮT duy nhất ngoại lệ `CaptionsUnavailable`. Do đó, lỗi `TypeError` (hoặc `SourceError("Invalid caption timing.")`) lập tức thoát ra khỏi vòng lặp duyệt các track ứng viên, rơi vào khối `except Exception` bên ngoài (dòng 479) và làm thất bại toàn bộ quá trình lấy phụ đề của video, hoàn toàn không chuyển sang thử các track dự phòng khác (như phụ đề tự động hay bản dịch).
- **Rủi ro & Tác động vận hành**:
  Chỉ vì một đoạn phụ đề có trường thời lượng null trong track phụ đề chính mà toàn bộ video bị coi là lỗi, không thể khai thác các track phụ đề thay thế hợp lệ khác.
- **Đề xuất khắc phục**:
  1. Trong `parse_json3`, kiểm tra an toàn:
     ```python
     raw_duration = event.get("dDurationMs")
     duration = float(raw_duration) / 1000 if raw_duration is not None else 0.0
     ```
  2. Trong `YouTube.transcript`, mở rộng khối bắt lỗi cho vòng lặp duyệt track:
     `except (CaptionsUnavailable, SourceError, ValueError, TypeError): continue`.
- **Kịch bản kiểm chứng độc lập**:
  ```bash
  .venv/bin/python -c '
  from transcript_collector.youtube import parse_json3
  try:
      parse_json3({"events": [{"tStartMs": 1000, "dDurationMs": None, "segs": [{"utf8": "hello"}]}]})
  except TypeError as e:
      print("parse_json3 CRASH với TypeError:", e)
  '
  # Kết quả: TypeError: float() argument must be a string or a real number, not 'NoneType'
  ```

---

### TC-04: Kiểm tra tính tương thích DB bỏ qua các bảng lạ, tự ý kích hoạt migration v1→v2 sai mục tiêu
- **Mức độ nghiêm trọng**: **High**
- **Vị trí mã nguồn**: `src/transcript_collector/store.py`, `TranscriptStore.__init__`, dòng 53–70
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/store.py:53-70
          tables = {
              r[0]
              for r in self.db.execute(
                  "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
              )
          }
          version = self.db.execute("PRAGMA user_version").fetchone()[0]
          if tables or version:
              if version not in {1, SCHEMA_VERSION} or any(
                  {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
                  != (columns - {"source_mode"} if version == 1 else columns)
                  for table, columns in COLUMNS.items()
              ):
                  raise ValueError(
                      "Incompatible collector database; choose a new --db path. Existing data was not migrated or deleted."
                  )
              if version == 1:
                  self._migrate_v1()
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Tập hợp `tables` được trích xuất từ `sqlite_master`, nhưng vòng lặp kiểm tra chỉ duyệt qua `for table, columns in COLUMNS.items()`. Mã nguồn hoàn toàn KHÔNG kiểm tra xem `tables` có chứa các bảng khác nằm ngoài `COLUMNS.keys()` hay không (`tables == set(COLUMNS.keys())`). Nếu một database của ứng dụng khác hoặc database chứa bảng phụ trợ có chứa 4 bảng trùng tên với cấu trúc khớp v1 và có `version == 1`, `TranscriptStore` sẽ coi đây là database schema v1 của collector và tự ý thực thi `_migrate_v1()`, xóa và tạo lại bảng.
  Điều này vi phạm trực tiếp ràng buộc trong `AGENTS.md`: *"Collector chỉ nâng schema đã biết v1→v2 nguyên tử, giữ nội dung... không migrate/xóa schema không nhận diện."*
- **Rủi ro & Tác động vận hành**:
  Nguy cơ biến đổi cấu trúc hoặc làm mất tính toàn vẹn dữ liệu khi trỏ nhầm `--db` vào cơ sở dữ liệu dùng chung của ứng dụng khác.
- **Đề xuất khắc phục**:
  Bổ sung kiểm tra nghiêm ngặt danh sách bảng:
  ```python
  if tables and tables != set(COLUMNS.keys()):
      raise ValueError("Incompatible collector database: unrecognized foreign tables detected.")
  ```
- **Kịch bản kiểm chứng độc lập**:
  ```bash
  .venv/bin/python -c '
  import sqlite3, tempfile
  from transcript_collector.store import TranscriptStore
  with tempfile.NamedTemporaryFile() as f:
      db = sqlite3.connect(f.name)
      db.execute("PRAGMA user_version=2")
      db.executescript("""
          CREATE TABLE channels(id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL);
          CREATE TABLE videos(id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, title TEXT NOT NULL);
          CREATE TABLE transcripts(video_id TEXT NOT NULL, source TEXT NOT NULL, source_mode TEXT NOT NULL, config TEXT NOT NULL, language TEXT NOT NULL, PRIMARY KEY(video_id, source, source_mode, config));
          CREATE TABLE transcript_parts(video_id TEXT NOT NULL, source TEXT NOT NULL, source_mode TEXT NOT NULL, language TEXT NOT NULL, start REAL NOT NULL, duration REAL NOT NULL, text TEXT NOT NULL);
          CREATE TABLE foreign_app_data(id INTEGER PRIMARY KEY, secret TEXT);
      """)
      db.close()
      store = TranscriptStore(f.name) # Chấp nhận mà không cảnh báo bảng lạ foreign_app_data!
      print("Store khởi tạo thành công dù có bảng lạ foreign_app_data!")
  '
  ```

---

### TC-05: `save_inventory` gọi ngoài `try...except`, vô hiệu hóa hàm kiểm tra ID và gây crash batch
- **Mức độ nghiêm trọng**: **High**
- **Vị trí mã nguồn**: `src/transcript_collector/pipeline.py`, hàm `index_video`, dòng 107–115
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/pipeline.py:107-115
      store.save_inventory([video])
      logger.info(
          "Video %s inventory committed",
          video.id,
          extra={"phase": "inventory_commit", "video_id": video.id},
      )
      try:
          if not re.fullmatch(r"[A-Za-z0-9_-]+", video.id):
              raise SourceError(f"Invalid video ID: {video.id}")
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Lệnh `store.save_inventory([video])` nằm ở dòng 107, ngay TRƯỚC khối `try:` bắt đầu ở dòng 113. Trong phương thức `_save_inventory`, hệ thống gọi `identifier(video.id)`. Nếu `video.id` chứa ký tự không hợp lệ, `identifier` sẽ ném `ValueError`. Nếu `video.channel_id` chưa tồn tại trong bảng `channels`, SQLite ném `sqlite3.IntegrityError` (vi phạm Foreign Key).
  Do nằm ngoài khối `try:`, các ngoại lệ này sẽ thoát thẳng ra ngoài mà không được xử lý; sự kiện `video_finished` không bao giờ được phát, và biến đếm lỗi không được ghi nhận. Đồng thời, dòng 114: `if not re.fullmatch(r"[A-Za-z0-9_-]+", video.id): raise SourceError(...)` trở thành mã chết (dead code) vì `identifier()` ở dòng 107 đã kiểm tra và ném `ValueError` từ trước.
- **Rủi ro & Tác động vận hành**:
  Làm sập đột ngột toàn bộ tiến trình quét hàng loạt khi gặp video có ID không hợp lệ hoặc thiếu channel tương ứng, vi phạm nguyên tắc *"Lỗi một video tiếp tục mục sau"*.
- **Đề xuất khắc phục**:
  Chuyển lệnh kiểm tra ID và gọi `save_inventory` vào bên trong khối `try:`:
  ```python
  try:
      if not re.fullmatch(r"[A-Za-z0-9_-]+", video.id):
          raise SourceError(f"Invalid video ID: {video.id}")
      store.save_inventory([video])
      logger.info(...)
  ```

---

### TC-06: Lỗi mạng khi tải track hoặc lỗi JSON decode làm đứt ngang chuỗi duyệt track ứng viên
- **Mức độ nghiêm trọng**: **Medium**
- **Vị trí mã nguồn**: `src/transcript_collector/youtube.py`, hàm `YouTube.transcript`, dòng 433–438
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/youtube.py:433-438
                                  with ydl.urlopen(track["url"]) as response:
                                      data = json.loads(response.read().decode("utf-8"))
                                  try:
                                      segments = parse_json3(data)
                                  except CaptionsUnavailable:
                                      continue
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Thao tác mở kết nối mạng `ydl.urlopen(track["url"])` và giải mã JSON `json.loads(...)` nằm ngoài khối `try...except CaptionsUnavailable`. Nếu CDN của YouTube gặp sự cố tạm thời (HTTP 500/503), rớt mạng, hoặc nội dung trả về bị cắt cụt khiến `json.JSONDecodeError` hoặc `UnicodeDecodeError` phát sinh, ngoại lệ không được bắt trong vòng lặp track. Nó sẽ nhảy thẳng ra khối `except Exception` bên ngoài ở dòng 479 và hủy bỏ toàn bộ quá trình tìm kiếm phụ đề của video.
- **Rủi ro & Tác động vận hành**:
  Một track phụ đề bị lỗi đường truyền phía YouTube khiến hệ thống từ bỏ ngay lập tức thay vì thử tiếp các track phụ đề tự động hoặc bản dịch có sẵn khác.
- **Đề xuất khắc phục**:
  Bọc `ydl.urlopen` và `json.loads` trong `try...except (SourceError, OSError, json.JSONDecodeError, UnicodeDecodeError): continue` ngay trong vòng lặp.

---

### TC-07: Video không có tiếng nói trong generate mode bị phân loại thành lỗi (`errors=1`) thay vì unavailable
- **Mức độ nghiêm trọng**: **Medium**
- **Vị trí mã nguồn**: `src/transcript_collector/engines/whisper.py:130–131`, `pipeline.py:221–222, 254–257`
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/engines/whisper.py:130-131
              if not segments:
                  raise TranscriptionError("No speech detected in audio.")
  ```
  Và trong pipeline:
  ```python
  # src/transcript_collector/pipeline.py:254-257
          if isinstance(exc, (CaptionsUnavailable, VideoNotReady)):
              counts["unavailable"] = 1
          else:
              counts["errors"] = 1
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Khi Whisper xử lý một video không có giọng nói (video nhạc không lời, video cảnh quan, video câm), mô hình không sinh ra phân đoạn nào. Khi đó, `WhisperEngine` ném `TranscriptionError("No speech detected in audio.")`. Tại `pipeline.py`, khối bắt lỗi chỉ phân loại `CaptionsUnavailable` và `VideoNotReady` vào `counts["unavailable"] = 1`; tất cả các lỗi khác bao gồm `TranscriptionError` đều bị xếp vào `counts["errors"] = 1`. Do đó, video câm bị đánh dấu là lỗi hệ thống (`errors=1`), khiến kết quả của mẻ quét bị coi là thất bại (`CollectionResult.failed == True`). Trong khi ở chế độ `direct`, nếu video không có phụ đề thì hệ thống ném `CaptionsUnavailable` và ghi nhận đúng là `unavailable=1`.
- **Rủi ro & Tác động vận hành**:
  Phá vỡ tính đối xứng giữa hai chế độ, khiến các tác vụ chạy theo lô (batch/channel) bị báo cáo thất bại sai lệch chỉ vì có video không chứa lời thoại.
- **Đề xuất khắc phục**:
  Trong `WhisperEngine.transcript`, khi `not segments`, trả về danh sách rỗng `[]` để dòng 221 của `pipeline.py` ném `CaptionsUnavailable("Transcript contains no text")`, hoặc trực tiếp ném `CaptionsUnavailable`.

---

### TC-08: Phát sự kiện `video_finished` nhưng không phát `video_started` khi phân giải video thất bại
- **Mức độ nghiêm trọng**: **Medium**
- **Vị trí mã nguồn**: `src/transcript_collector/pipeline.py`, hàm `_index_videos`, dòng 426–447
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/pipeline.py:426-447
          except (SourceError, OSError, ValueError, sqlite3.Error) as exc:
              logger.warning(...)
              result = _empty_counts()
              result["unavailable" if isinstance(exc, VideoNotReady) else "errors"] = 1
              result["blocked"] = int(isinstance(exc, SourceBlocked))
              result["retained"] = int(store.has_transcript(video_id))
              emit(
                  on_event,
                  CollectionEvent(
                      "video_finished",
                      message=f"[{n}/{len(videos)}] {video_id}: {exc}",
                      video_id=video_id,
                      position=n,
                      total=len(videos),
                      counts=result.copy(),
                  ),
              )
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Sự kiện `video_started` được phát ra bên trong hàm `index_video`. Tuy nhiên, nếu lệnh `resolve()` ở dòng 410 thất bại (ví dụ: video bị xóa, video riêng tư, lỗi mạng), luồng điều khiển nhảy ngay vào khối `except` ở dòng 426 và phát trực tiếp sự kiện `video_finished`. Trước đó, chưa từng có sự kiện `video_started` nào được phát ra cho `video_id` này.
- **Rủi ro & Tác động vận hành**:
  Các bộ lắng nghe sự kiện (event listeners), thanh tiến trình UI, hoặc các service quản lý job nền hoạt động dựa trên cặp sự kiện đối ứng (`video_started` -> `video_finished`) sẽ nhận được sự kiện kết thúc mồ côi, có thể gây lỗi đếm tiến trình hoặc tràn bộ nhớ theo dõi trạng thái.
- **Đề xuất khắc phục**:
  Phát sự kiện `video_started` trước khi gọi `resolve()`, hoặc bổ sung phát sự kiện bắt đầu ngay trước khi phát sự kiện kết thúc trong khối xử lý lỗi.

---

### TC-09: `save_video` không kiểm tra `source_mode` cho generate mode, tạo dữ liệu mồ côi ngoài tầm search
- **Mức độ nghiêm trọng**: **Medium**
- **Vị trí mã nguồn**: `src/transcript_collector/store.py`, `TranscriptStore.save_video`, dòng 222–231
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/store.py:222-231
          if (
              source not in {"youtube", "generate"}
              or (
                  source == "youtube"
                  and source_mode
                  not in {"manual", "auto_generated", "translated", "unknown"}
              )
              or not any(s.text.strip() for s in segments)
          ):
              raise ValueError("A valid source and non-empty transcript are required.")
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Mã nguồn chỉ kiểm tra `source_mode` khi `source == "youtube"`. Khi `source == "generate"`, `source_mode` hoàn toàn không được kiểm tra để loại trừ các giá trị dành riêng cho YouTube hoặc chế độ legacy (`"manual"`, `"auto_generated"`, `"translated"`, `"unknown"`, `"legacy"`, `"youtube"`).
  Trong khi đó, ở tầng tìm kiếm (`src/video_search/filtering.py` dòng 141), câu truy vấn SQLite loại bỏ nghiêm ngặt:
  ```sql
  (t.source='generate' AND t.source_mode <> ''
   AND t.source_mode NOT IN ('manual','auto_generated','translated','unknown','legacy','youtube'))
  ```
  Nếu một adapter hoặc caller truyền vào `source="generate"` với `source_mode="manual"`, bản ghi vẫn được lưu thành công vào database nhưng sẽ bị bộ tìm kiếm lọc bỏ vĩnh viễn trong mọi truy vấn.
- **Rủi ro & Tác động vận hành**:
  Dữ liệu phụ đề được thu thập và tốn tài nguyên GPU nhận dạng thành công nhưng lại vô hình hoàn toàn đối với người dùng tìm kiếm.
- **Đề xuất khắc phục**:
  Thêm điều kiện kiểm tra hợp lệ cho `source == "generate"`:
  ```python
  if source == "generate" and (
      not source_mode
      or source_mode in {"manual", "auto_generated", "translated", "unknown", "legacy", "youtube"}
  ):
      raise ValueError(f"Invalid generate source_mode: {source_mode!r}")
  ```

---

### TC-10: Phân giải kênh thất bại khi kênh có tab `/videos` rỗng mặc dù người dùng truyền đúng UC ID
- **Mức độ nghiêm trọng**: **Medium**
- **Vị trí mã nguồn**: `src/transcript_collector/youtube.py`, hàm `YouTube.resolve`, dòng 231–234
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/youtube.py:231-234
          info = self._extract(url + "/videos", playlistend=1) or {}
          identifier = info.get("channel_id")
          if not identifier:
              raise SourceError("Could not resolve this channel's ID.")
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Hàm luôn gắn thêm `"/videos"` vào cuối URL kênh để trích xuất `channel_id`. Nếu một kênh YouTube mới thành lập, hoặc kênh chỉ đăng video Shorts/Livestreams mà không có bất kỳ video tải lên truyền thống nào trong tab Videos, yt-dlp sẽ trả về lỗi và hàm `_extract` bắt lại rồi trả về `{"entries": []}`. Khi đó `info.get("channel_id")` là `None`, và hệ thống lập tức báo lỗi `SourceError("Could not resolve this channel's ID.")`, ngay cả khi người dùng đã truyền vào một UC ID hợp lệ 24 ký tự chuẩn.
- **Rủi ro & Tác động vận hành**:
  Không thể phân giải thông tin các kênh chuyên livestream hoặc shorts dù kênh hoàn toàn tồn tại thực tế trên YouTube.
- **Đề xuất khắc phục**:
  Nếu người dùng truyền vào chuỗi khớp định dạng `UC[\w-]{22}`, sử dụng trực tiếp chuỗi đó làm `channel.id`. Nếu trích xuất với `"/videos"` không có `channel_id`, thử fallback trích xuất URL gốc của kênh mà không có hậu tố `"/videos"`.

---

### TC-11: `store.cached()` sinh câu lệnh SQL lỗi (`source_mode = NULL`) khi `source_mode=None` cho generate
- **Mức độ nghiêm trọng**: **Medium**
- **Vị trí mã nguồn**: `src/transcript_collector/store.py`, `TranscriptStore.cached`, dòng 188–190
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/store.py:188-190
          else:
              condition = "config=? AND source_mode=?"
              params = (video_id, source, config, source_mode)
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Khi kiểm tra cache cho nguồn khác `"youtube"` (như `"generate"`) mà người gọi không truyền tham số `source_mode` (để mặc định là `None`), giá trị gắn vào tham số SQLite là `None`. Trong SQL, phép so sánh `source_mode = NULL` luôn trả về `NULL` (falsy trong mệnh đề WHERE). Không giống như `source == "youtube"` có kiểm tra nghiêm ngặt `if not language: raise ValueError(...)`, nhánh này không kiểm tra `source_mode`.
- **Rủi ro & Tác động vận hành**:
  Hàm âm thầm trả về `False` (báo trượt cache) dù bản ghi phụ đề đã tồn tại trong database, dẫn tới việc tải lại audio và chạy lại Whisper gây lãng phí tài nguyên tính toán.
- **Đề xuất khắc phục**:
  Bổ sung kiểm tra tham số: `if source != "youtube" and not source_mode: raise ValueError("Generate cache lookup requires a source_mode.")`.
- **Kịch bản kiểm chứng độc lập**:
  ```bash
  .venv/bin/python -c '
  from transcript_collector.store import TranscriptStore
  from transcript_collector.contracts import Video, Segment, Channel
  import tempfile, pathlib

  tmp = tempfile.TemporaryDirectory()
  store = TranscriptStore(str(pathlib.Path(tmp.name) / "test.db"))
  store.save_channel(Channel("c1", "Title", "url"))
  store.save_video(Video("v1", "c1", "Title"), "generate", "medium", [Segment("hi", 0, 1)], language="vi", source_mode="whisper")

  print("Truyền source_mode rõ ràng:", store.cached("v1", "generate", "medium", source_mode="whisper")) # True
  print("Không truyền source_mode:", store.cached("v1", "generate", "medium")) # False (bị trượt cache âm thầm)
  '
  ```

---

### TC-12: Cơ chế dò tìm CUDA phụ thuộc cứng vào cấu trúc thư mục bánh xe pip (`site-packages/nvidia`)
- **Mức độ nghiêm trọng**: **Low**
- **Vị trí mã nguồn**: `src/transcript_collector/engines/whisper.py`, `WhisperEngine._device`, dòng 36–53
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/engines/whisper.py:36-53
              libraries = (
                  "cublas/lib/libcublasLt.so.12",
                  "cublas/lib/libcublas.so.12",
                  "cudnn/lib/libcudnn.so.9",
              )
              for lib in libraries:
                  path = next(
                      Path(root) / "nvidia" / lib
                      for root in site.getsitepackages()
                      if (Path(root) / "nvidia" / lib).exists()
                  )
                  ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)
              return "cuda"
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Mã nguồn tìm kiếm các tệp thư viện `.so.12` và `.so.9` chỉ bên trong thư mục `site-packages/nvidia/`. Nếu máy chủ cài đặt CUDA ở cấp hệ thống (system-wide CUDA tại `/usr/local/cuda/lib64` hoặc qua các gói APT/RPM của Linux distribution), hàm `next(...)` sẽ ném ngoại lệ `StopIteration`. Khi đó:
  - Nếu chọn `--device auto`: Hệ thống âm thầm giáng cấp về CPU dù máy có GPU xịn.
  - Nếu chọn `--device cuda`: Hệ thống ném `TranscriptionError("CUDA unavailable: . Install the gpu extra or choose --device cpu.")` với chuỗi lỗi rỗng vì `str(StopIteration()) == ""`.
- **Rủi ro & Tác động vận hành**:
  Không tận dụng được GPU trên các môi trường cài đặt CUDA qua hệ điều hành thay vì cài gói wheel pip `nvidia-*`.
- **Đề xuất khắc phục**:
  Cho phép fallback thử nạp trực tiếp qua trình liên kết động của hệ điều hành `ctypes.CDLL("libcublas.so.12")` trước khi từ bỏ sang CPU.

---

### TC-13: Lỗi thứ cấp `sqlite3.Error` trong `store.has_transcript()` làm sập khối bắt ngoại lệ
- **Mức độ nghiêm trọng**: **Low**
- **Vị trí mã nguồn**: `src/transcript_collector/pipeline.py`, dòng 258–259 và 436
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/pipeline.py:258-259
      except (SourceError, TranscriptionError, OSError, ValueError, sqlite3.Error) as exc:
          ...
          if store.has_transcript(video.id):
              counts["retained"] = 1
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Nếu ngoại lệ ban đầu khiến tiến trình rơi vào khối `except` chính là `sqlite3.Error` (ví dụ: đĩa cứng đầy, kết nối bị khóa hoặc cơ sở dữ liệu bị hỏng), việc gọi tiếp `store.has_transcript(video.id)` mà không có `try...except` sẽ tiếp tục phát sinh một ngoại lệ `sqlite3.Error` thứ hai, làm sập chương trình ngay bên trong khối xử lý lỗi.
- **Rủi ro & Tác động vận hành**:
  Gây crash không kiểm soát khi có sự cố cơ sở dữ liệu.
- **Đề xuất khắc phục**:
  Bọc `store.has_transcript(video.id)` trong `try...except sqlite3.Error: False`.

---

### TC-14: Timestamp phân đoạn từ Whisper không được tiền kiểm tra tính hợp lệ trước khi lưu
- **Mức độ nghiêm trọng**: **Low**
- **Vị trí mã nguồn**: `src/transcript_collector/engines/whisper.py`, dòng 110–113
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/engines/whisper.py:110-113
                  if part.text.strip():
                      segments.append(
                          Segment(part.text.strip(), part.start, part.end - part.start)
                      )
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  `WhisperEngine` không kiểm tra `part.start >= 0` hay `part.end >= part.start`. Nếu mô hình hallucinate tạo ra timestamp phân đoạn bị đảo ngược hoặc số âm, khi chuyển sang `store.save_video`, lệnh kiểm tra ở tầng lưu trữ sẽ ném `ValueError("Transcript timing must be finite and nonnegative.")`.
- **Rủi ro & Tác động vận hành**:
  Video bị lỗi lưu trữ do phân đoạn thời gian không hợp lệ.
- **Đề xuất khắc phục**:
  Ràng buộc và tiền kiểm tra: `start = max(0.0, float(part.start))`, `duration = max(0.0, float(part.end) - start)`.

---

### TC-15: `_validate_options` không từ chối tham số `language` trong chế độ `generate`
- **Mức độ nghiêm trọng**: **Low**
- **Vị trí mã nguồn**: `src/transcript_collector/pipeline.py`, hàm `_validate_options`, dòng 31–53
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/pipeline.py:31-53
  def _validate_options(mode: str, language: str | None, transcriber: TranscriptionEngine | None) -> None:
      if mode not in {"direct", "generate"}:
          raise ValueError("Choose direct or generate as collection mode.")
      if mode == "direct" and (not language or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language)):
          raise ValueError("Direct mode requires one --language, for example vi or en.")
      if mode == "generate" and transcriber is None:
          raise ValueError("Generate mode requires a transcription engine.")
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Trong `config.py`, lớp `CollectionRequest.validate()` có dòng kiểm tra: `if mode == "generate" and language is not None: raise ValueError(...)`. Tuy nhiên, trong `_validate_options` của `pipeline.py`, điều kiện này bị bỏ sót. Nếu gọi trực tiếp qua API Python vào `index_video` hoặc `index_channel`, tham số `language` truyền vào chế độ `generate` sẽ bị bỏ qua trong im lặng thay vì báo lỗi.
- **Rủi ro & Tác động vận hành**:
  Bất nhất về hợp đồng kiểm tra tham số giữa tầng config và tầng pipeline API.
- **Đề xuất khắc phục**:
  Bổ sung vào `_validate_options`: `if mode == "generate" and language is not None: raise ValueError("--language is only valid in direct mode.")`.

---

### TC-16: Đường dẫn `DATA_DIR` phụ thuộc cứng vào vị trí mã nguồn `__file__`
- **Mức độ nghiêm trọng**: **Low**
- **Vị trí mã nguồn**: `src/transcript_collector/config.py`, dòng 7
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/config.py:7
  DATA_DIR = Path(__file__).resolve().parents[2] / "data"
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Định nghĩa giả định rằng `config.py` luôn nằm sâu 2 tầng thư mục bên trong mã nguồn git checkout. Khi thư viện được đóng gói thành wheel và cài đặt vào môi trường production (`site-packages`), `parents[2]` sẽ trỏ vào thư mục nội bộ của Python runtime thay vì thư mục làm việc của dự án.
- **Rủi ro & Tác động vận hành**:
  Lỗi đường dẫn mặc định khi triển khai dưới dạng package độc lập.
- **Đề xuất khắc phục**:
  Khởi tạo đường dẫn dữ liệu mặc định dựa trên thư mục hiện hành: `DATA_DIR = Path.cwd() / "data"`.

---

### TC-17: Whisper engine không kiểm tra sự tồn tại của tệp âm thanh trước khi gọi mô hình
- **Mức độ nghiêm trọng**: **Low**
- **Vị trí mã nguồn**: `src/transcript_collector/engines/whisper.py`, dòng 101
- **Đoạn mã vi phạm**:
  ```python
  # src/transcript_collector/engines/whisper.py:101
  parts, info = self.model.transcribe(str(audio), vad_filter=True)
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Nếu đường dẫn `audio` bị thiếu hoặc tệp bị xóa sớm, `faster_whisper` sẽ phát sinh lỗi nội bộ và bị bắt ở dòng 151 rồi bọc thành `TranscriptionError(f"Audio transcription failed: {exc}")`. Điều này làm lu mờ bản chất lỗi (tệp không tồn tại bị nhầm thành lỗi suy luận mô hình AI).
- **Rủi ro & Tác động vận hành**:
  Gây khó khăn cho việc phân loại và khắc phục sự cố vận hành.
- **Đề xuất khắc phục**:
  Kiểm tra `if not Path(audio).is_file(): raise TranscriptionError(f"Audio file does not exist: {audio}")` trước khi thực thi.

---

## 5. Danh mục chi tiết các lỗi trong `src/video_search`

### VS-01: Sập `sqlite3.IntegrityError: UNIQUE constraint failed: lexical_rows.key` khi có passage trùng
- **Mức độ nghiêm trọng**: **High**
- **Vị trí mã nguồn**: `src/video_search/cache.py`, `EmbeddingStore.lexical_scores`, dòng 174–194 và `src/video_search/search.py`, `lexical_scores`, dòng 85–90
- **Đoạn mã vi phạm**:
  ```python
  # src/video_search/cache.py:174-194
  for position, passage in enumerate(passages):
      key = hashlib.sha256(
          json.dumps(passage, ensure_ascii=False, sort_keys=True).encode()
      ).hexdigest()
      existing = current.pop(key, None)
      if existing is not None:
          if existing[1] != position:
              positions.append((position, existing[0]))
          continue
      added += 1
      cursor = self.db.execute(
          "INSERT INTO lexical_rows(key,position) VALUES(?,?)",
          (key, position),
      )
      self.db.execute(
          "INSERT INTO lexical_windows(rowid,text) VALUES(?,?)",
          (
              cursor.lastrowid,
              " ".join(tokens(passage["text"] + " " + passage["title"])),
          ),
      )
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Bảng `lexical_rows` được định nghĩa với ràng buộc duy nhất: `key TEXT NOT NULL UNIQUE`. Trong hàm `EmbeddingStore.lexical_scores`, vòng lặp duyệt qua danh sách `passages`. Nếu trong danh sách đầu vào có chứa 2 phần tử passage trùng nhau (cùng văn bản và thuộc tính), ở lần duyệt đầu tiên, `current.pop(key, None)` lấy phần tử ra khỏi từ điển `current`. Đến lần duyệt thứ hai gặp lại cùng một passage, `current.pop(key, None)` trả về `None`. Đoạn mã tiếp tục thực hiện `INSERT INTO lexical_rows(key, position) VALUES(?, ?)`. Do `key` đã được chèn vào bảng ở lần duyệt đầu tiên, SQLite lập tức ném ngoại lệ không thể xử lý:
  `sqlite3.IntegrityError: UNIQUE constraint failed: lexical_rows.key`.
  Mặc dù hàm nội bộ `SearchEngine.prepare_windows` có thực hiện lọc trùng passage, hàm công khai cấp module `video_search.search.lexical_scores(query, passages)` (dòng 85–90 của `search.py`) lại chuyển thẳng danh sách `passages` của người gọi vào `EmbeddingStore.lexical_scores` mà không qua bước deduplicate.
- **Rủi ro & Tác động vận hành**:
  Gây crash ứng dụng ngay lập tức khi tính điểm từ khóa BM25 đối với bất kỳ tập dữ liệu nào chứa đoạn trích trùng lặp hoặc khi được gọi từ các kịch bản đánh giá/kiểm thử bên ngoài.
- **Đề xuất khắc phục**:
  1. Trong `video_search.search.lexical_scores`, thực hiện deduplicate danh sách `passages` trước khi chuyển vào cache.
  2. Trong `EmbeddingStore.lexical_scores`, theo dõi tập hợp các khóa đã gặp trong mẻ xử lý hiện tại (`seen_keys = set()`) hoặc sử dụng cấu trúc lưu trữ cho phép một key ánh xạ tới nhiều position.
- **Kịch bản kiểm chứng độc lập**:
  ```bash
  .venv/bin/python -c "
  from video_search.search import lexical_scores
  from tests.test_search import window
  doc = window('hello world', 'v1')
  try:
      lexical_scores('hello', [doc, doc])
  except Exception as e:
      print('CRASH THỰC TẾ:', type(e), e)
  "
  # Kết quả: CRASH THỰC TẾ: <class 'sqlite3.IntegrityError'> UNIQUE constraint failed: lexical_rows.key
  ```

---

### VS-02: Cắt xén ứng viên trong `_rerank` gây thiếu hụt đoạn trích (moment starvation) do triệt tiêu trùng lặp
- **Mức độ nghiêm trọng**: **High**
- **Vị trí mã nguồn**: `src/video_search/search.py`, `SearchEngine._rerank` (dòng 493–542) và `SearchEngine._search` (dòng 416–466)
- **Đoạn mã vi phạm**:
  ```python
  # src/video_search/search.py:502-508
          for i in order:
              if len(candidates) >= max(top, rerank_top):
                  break
              video = self.passages[i]["video_id"]
              if counts.get(video, 0) < max(3, moments_per_video) and i not in candidates:
                  candidates.append(i)
                  counts[video] = counts.get(video, 0) + 1
  ...
  # src/video_search/search.py:534
          order = sorted(candidates, key=lambda i: (-rerank_scores[i], -scores[i], i))
          return order, rerank_scores
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  1. Trong `SearchEngine._rerank`, vòng lặp tích lũy ứng viên `candidates` tăng biến đếm `counts[video]` lên đến `moments_per_video` mà HOÀN TOÀN KHÔNG kiểm tra xem các cửa sổ (window) ứng viên đó có bị gối đầu trùng lặp thời gian với nhau hay không. Vì bước nhảy cửa sổ (stride) là 30s trong khi chiều dài cửa sổ là 60s, các cửa sổ liền kề của một đoạn hội thoại điểm cao (ví dụ: tại 0s, 10s, 20s) đều được chọn vào danh sách ứng viên rerank.
  2. Sau khi rerank, dòng 534 gán lại: `order = sorted(candidates, ...)` — hoàn toàn loại bỏ toàn bộ các cửa sổ còn lại trong kết quả xếp hạng dense/lexical ban đầu.
  3. Khi hàm `_search` thực hiện gom cụm và triệt tiêu đoạn trùng lặp tham lam (`max(start1, start2) < min(end1, end2)`), các cửa sổ liền kề bị loại bỏ hết. Vì trong danh sách `order` không còn bất kỳ cửa sổ nào khác của video đó nữa, video bị thiếu hụt đoạn trích nghiêm trọng (chỉ trả về 1 moment duy nhất dù người dùng yêu cầu `moments_per_video=3` và video có thừa các đoạn trích không trùng lặp khác).
- **Rủi ro & Tác động vận hành**:
  Làm suy giảm nghiêm trọng độ phủ kết quả (recall) và vi phạm trực tiếp hợp đồng hiển thị đa đoạn trích (multi-moment per video) khi bật tính năng reranker.
- **Đề xuất khắc phục**:
  1. Kiểm tra triệt tiêu trùng lặp ngay khi chọn ứng viên rerank.
  2. Giữ lại danh sách các ứng viên chưa được rerank ở phía sau mảng `order`:
     `order = sorted(candidates, key=...) + [i for i in original_order if i not in set(candidates)]`.
- **Kịch bản kiểm chứng độc lập**:
  ```bash
  .venv/bin/python -c "
  from unittest.mock import Mock, patch
  import types
  from video_search.search import SearchEngine
  from video_search.cache import EmbeddingStore
  from tests.fakes import fake_model, fake_reranker
  from tests.test_search import window

  cache = EmbeddingStore(':memory:')
  module = types.ModuleType('sentence_transformers')
  setattr(module, 'SentenceTransformer', Mock(return_value=fake_model()))
  setattr(module, 'CrossEncoder', Mock(return_value=fake_reranker()))

  docs = [
      window('query keyword 1', 'v1', start=0),
      window('query keyword 2', 'v1', start=10),
      window('query keyword 3', 'v1', start=20),
      window('query keyword 4', 'v1', start=120),
      window('query keyword 5', 'v1', start=180),
  ]
  with patch.dict('sys.modules', {'sentence_transformers': module}):
      engine = SearchEngine(cache, mode='hybrid', reranker_name='dummy')
      res = engine.search_windows('query', docs, top=1, rerank_top=3, moments_per_video=3)
      print('Số moments trả về khi có reranker:', len(res[0]['moments']), '(Kỳ vọng là 3, nhưng bị đói chỉ còn 1)')
  "
  # Kết quả: Số moments trả về khi có reranker: 1
  ```

---

### VS-03: Cache signature cũ khi chuyển đổi chế độ gây sập `RuntimeError: Embedding model was not initialized`
- **Mức độ nghiêm trọng**: **High**
- **Vị trí mã nguồn**: `src/video_search/search.py`, `SearchEngine.prepare_windows`, dòng 146–168 và `_search`, dòng 352–353
- **Đoạn mã vi phạm**:
  ```python
  # src/video_search/search.py:146-168
          version = lexical_signature(passages)
          if version == self._signature:
              logger.debug("Prepared windows reused: windows=%d", len(self.passages), ...)
              return
          self._videos = None
          if not passages:
              ...
              return
          if self.mode in LEXICAL_MODES:
              self.passages = passages
              self._lexical_signature = version if self.mode == "keyword" else None
              self._signature = version
              return
          # ... khởi tạo model và mã hóa vector chỉ diễn ra dưới đoạn này ...
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  `SearchEngine.prepare_windows()` kiểm tra tái sử dụng cache bằng điều kiện `if version == self._signature: return`. Nếu một đối tượng `SearchEngine` được khởi tạo ban đầu ở chế độ lexical (`keyword` hoặc `exact`), `self._signature` được gán bằng `version`, nhưng `self.model` vẫn là `None` và ma trận vector rỗng.
  Nếu sau đó người gọi đổi `engine.mode = "semantic"` hoặc `"hybrid"` trên cùng một tập dữ liệu cửa sổ, lệnh `prepare_windows()` thấy `version == self._signature` nên return ngay lập tức mà không khởi tạo mô hình nhúng hay mã hóa vector. Khi tiến hành tìm kiếm, hàm `_search` chạy đến dòng 352:
  `if self.model is None: raise RuntimeError("Embedding model was not initialized.")` và làm sập chương trình.
- **Rủi ro & Tác động vận hành**:
  Gây crash không thể phục hồi trong các luồng đánh giá đa chế độ (multi-mode evaluation), trong các phiên tương tác REPL, hoặc trong các tiến trình worker duy trì đối tượng engine sống lâu (warm service workers).
- **Đề xuất khắc phục**:
  Đưa thông tin loại chế độ (lexical hay dense) và tên mô hình vào `_signature`:
  ```python
  self._signature = (version, self.mode in DENSE_MODES, self.model_name if self.mode in DENSE_MODES else None)
  ```
- **Kịch bản kiểm chứng độc lập**:
  ```bash
  .venv/bin/python -c "
  from video_search.search import SearchEngine
  from video_search.cache import EmbeddingStore
  from tests.fakes import fake_model
  from tests.test_search import window
  from unittest.mock import Mock, patch
  import types

  cache = EmbeddingStore(':memory:')
  module = types.ModuleType('sentence_transformers')
  setattr(module, 'SentenceTransformer', Mock(return_value=fake_model()))
  docs = [window('test text', 'v1')]

  with patch.dict('sys.modules', {'sentence_transformers': module}):
      engine = SearchEngine(cache, mode='keyword')
      engine.prepare_windows(docs)
      engine.mode = 'semantic'
      engine.prepare_windows(docs)
      try:
          engine.search_windows('test', docs)
      except RuntimeError as e:
          print('CRASH DO SIGNATURE CŨ:', e)
  "
  # Kết quả: CRASH DO SIGNATURE CŨ: Embedding model was not initialized.
  ```

---

### VS-04: Ngoại lệ chưa bắt `TypeError` trong `exact_scores` khi `passage["title"]` là `None`
- **Mức độ nghiêm trọng**: **Medium**
- **Vị trí mã nguồn**: `src/video_search/search.py`, hàm `exact_scores`, dòng 78–81
- **Đoạn mã vi phạm**:
  ```python
  # src/video_search/search.py:78-81
      for index, passage in enumerate(passages):
          text_hits = _normalize_exact(passage["text"]).count(needle)
          title_hit = 1 if needle in _normalize_exact(passage.get("title", "")) else 0
          if text_hits or title_hit:
              scores[index] = float(text_hits) + (0.5 if title_hit else 0.0)
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Khi từ điển passage chứa cặp khóa-giá trị `"title": None` (xảy ra khi siêu dữ liệu video không có tiêu đề), lệnh `passage.get("title", "")` trả về `None` (vì khóa `"title"` có tồn tại trong dict). Giá trị `None` được chuyển vào `_normalize_exact(None)`, gọi tới `unicodedata.normalize("NFC", None)` và lập tức ném lỗi:
  `TypeError: normalize() argument 2 must be str, not None`. Trong khi đó, ở các vị trí khác như `embedding_text` (dòng 55), mã nguồn đã xử lý cẩn thận bằng cú pháp `str(passage.get("title") or "")`.
- **Rủi ro & Tác động vận hành**:
  Gây crash chế độ tìm kiếm chính xác (`exact`) khi cơ sở dữ liệu có các video bị thiếu tiêu đề.
- **Đề xuất khắc phục**:
  Sửa dòng 79 thành: `title_hit = 1 if needle in _normalize_exact(passage.get("title") or "") else 0`.
- **Kịch bản kiểm chứng độc lập**:
  ```bash
  .venv/bin/python -c "
  from video_search.search import exact_scores
  passages = [{'text': 'chào buổi sáng', 'title': None}]
  try:
      exact_scores('buổi sáng', passages)
  except TypeError as e:
      print('CRASH VỚI TYPEERROR:', e)
  "
  # Kết quả: CRASH VỚI TYPEERROR: normalize() argument 2 must be str, not None
  ```

---

### VS-05: Chuỗi truy cập thuộc tính mong manh trong `RetrievalModel.__init__` gây lỗi trên mô hình tùy biến
- **Mức độ nghiêm trọng**: **Medium**
- **Vị trí mã nguồn**: `src/video_search/models.py`, `RetrievalModel.__init__`, dòng 56–58
- **Đoạn mã vi phạm**:
  ```python
  # src/video_search/models.py:56-58
          "revision": getattr(
              getattr(self.model[0], "auto_model").config, "_commit_hash", None
          ),
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Lệnh `getattr(self.model[0], "auto_model")` bên trong không có giá trị mặc định (`default=None`). Nếu `self.model[0]` là một module tùy biến, mô hình lượng tử hóa, hoặc kiến trúc sentence-transformers mà thành phần đầu tiên không chứa thuộc tính `auto_model`, Python ném ngay `AttributeError`. Hơn nữa, nếu `auto_model` trả về `None`, việc truy cập `.config` sẽ ném tiếp `AttributeError: 'NoneType' object has no attribute 'config'`.
- **Rủi ro & Tác động vận hành**:
  Không thể tích hợp hoặc nạp các mô hình ONNX, mô hình lượng tử hóa (quantized) hoặc các pipeline SentenceTransformers tùy biến.
- **Đề xuất khắc phục**:
  Truy cập an toàn theo chuỗi:
  ```python
  auto_model = getattr(self.model[0], "auto_model", None)
  config = getattr(auto_model, "config", None) if auto_model is not None else None
  revision = getattr(config, "_commit_hash", None) if config is not None else None
  ```

---

### VS-06: Xóa và tái tạo bảng ảo FTS5 toàn cục liên tục khi tìm kiếm trên các tập hợp video khác nhau
- **Mức độ nghiêm trọng**: **Medium**
- **Vị trí mã nguồn**: `src/video_search/cache.py`, `EmbeddingStore.lexical_scores`, dòng 156–206
- **Đoạn mã vi phạm**:
  ```python
  # src/video_search/cache.py:165-171
                      self.db.execute("DELETE FROM lexical_windows")
                      self.db.execute("DELETE FROM lexical_rows")
                  current = {
                      key: (rowid, position)
                      for key, rowid, position in self.db.execute(
                          "SELECT key,id,position FROM lexical_rows"
                      )
                  }
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Bảng FTS5 `lexical_windows` và bảng ánh xạ `lexical_rows` trong `data/search.sqlite3` lưu trữ chỉ mục từ khóa toàn cục, trong đó `lexical_rows.position` gắn chặt với chỉ số vị trí mảng `passages` của câu truy vấn hiện tại. Khi người dùng tìm kiếm trên một tập hợp con video (ví dụ: kênh A), bảng FTS5 được cập nhật cho các đoạn trích của kênh A. Nếu câu truy vấn tiếp theo tìm kiếm trên kênh B, hệ thống thấy fingerprint thay đổi và tiến hành xóa sạch toàn bộ các hàng của kênh A để chèn các hàng của kênh B. Bảng ảo FTS5 bị xóa và dựng lại liên tục giữa các truy vấn lọc khác nhau.
- **Rủi ro & Tác động vận hành**:
  Gây khuếch đại ghi đĩa cực lớn (disk write amplification) và suy giảm nghiêm trọng tốc độ phản hồi khi nhiều người dùng hoặc tiến trình tìm kiếm luân phiên trên các bộ lọc khác nhau.
- **Đề xuất khắc phục**:
  Tách rời việc lưu trữ chỉ mục FTS5 lâu dài theo định danh passage hash bất biến thay vì lưu theo chỉ số mảng vị trí tạm thời của từng câu truy vấn.

---

### VS-07: `EmbeddingStore` mở kết nối đọc-ghi trực tiếp mà không kiểm tra nguy cơ va chạm với DB phụ đề
- **Mức độ nghiêm trọng**: **Medium**
- **Vị trí mã nguồn**: `src/video_search/cache.py`, `EmbeddingStore.__init__`, dòng 32–55
- **Đoạn mã vi phạm**:
  ```python
  # src/video_search/cache.py:32-38
      def __init__(self, path: str):
          db_path = Path(path).expanduser().resolve()
          if path != ":memory:":
              db_path.parent.mkdir(parents=True, exist_ok=True)
          self.db = sqlite3.connect(
              ":memory:" if path == ":memory:" else str(db_path), timeout=60
          )
          self.db.executescript("""
              CREATE TABLE IF NOT EXISTS embedding_vectors ...
          """)
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Mặc dù tầng cấu hình CLI/Web có kiểm tra `db != search_db`, lớp `EmbeddingStore` là một thành phần công khai cốt lõi của package `video_search`. Nếu một ứng dụng hoặc lập trình viên khởi tạo trực tiếp `EmbeddingStore(path)` với đường dẫn trỏ tới cơ sở dữ liệu phụ đề (hoặc một symlink/hardlink của nó), `EmbeddingStore` sẽ mở file ở chế độ đọc-ghi và thực thi lệnh DDL, tạo ra các bảng cache và bảng ảo FTS5 ngay bên trong database phụ đề. Điều này vi phạm nguyên tắc bất biến: *"Search: strictly read-only, never migrates input DB, search cache/state/reports distinct from input DB."*
- **Rủi ro & Tác động vận hành**:
  Làm ô nhiễm cấu trúc schema v2 của cơ sở dữ liệu phụ đề và tiềm ẩn nguy cơ khóa cơ sở dữ liệu (lock contention).
- **Đề xuất khắc phục**:
  Trong `EmbeddingStore.__init__`, kiểm tra các bảng hiện có của database trước khi thực thi DDL:
  ```python
  if path != ":memory:":
      tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
      if "transcripts" in tables or "channels" in tables:
          self.db.close()
          raise ValueError("Vector cache path cannot point to a transcript database.")
  ```

---

### VS-08: Tham số `float('nan')` và `Inf` làm sai lệch ngữ nghĩa SQL và phá vỡ cơ chế tái sử dụng cache
- **Mức độ nghiêm trọng**: **Low**
- **Vị trí mã nguồn**: `src/video_search/predicate.py`, hàm `compile_where`, dòng 223–233
- **Đoạn mã vi phạm**:
  ```python
  # src/video_search/predicate.py:223-227
      if any(
          value is not None and not isinstance(value, (str, int, float))
          for value in params
      ):
          raise ValueError("WHERE parameters must be strings, numbers or None.")
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Hàm kiểm tra chấp nhận kiểu `float`. Trong Python, `isinstance(float('nan'), float)` và `isinstance(float('inf'), float)` đều trả về `True`.
  Khi giá trị `float('nan')` được truyền vào tham số truy vấn:
  1. Trình điều khiển `sqlite3` của Python tự động chuyển đổi `float('nan')` thành `NULL`, làm thay đổi âm thầm ngữ nghĩa của phép so sánh (ví dụ: `col = ?` trở thành `col = NULL`, luôn trả về false trong SQL).
  2. Trong `CompiledPredicate`, do tính chất `float('nan') != float('nan')`, phép so sánh bằng giữa hai đối tượng predicate giống hệt nhau luôn trả về `False`, phá vỡ cơ chế kiểm tra `_signature` để tái sử dụng cache của `VideoFilter`.
- **Rủi ro & Tác động vận hành**:
  Làm sai lệch kết quả lọc và vô hiệu hóa cơ chế cache bộ lọc khi xuất hiện giá trị NaN.
- **Đề xuất khắc phục**:
  Kiểm tra số thực hữu hạn:
  ```python
  import math
  if any(isinstance(v, float) and not math.isfinite(v) for v in params):
      raise ValueError("WHERE float parameters must be finite numbers (not NaN or Inf).")
  ```
- **Kịch bản kiểm chứng độc lập**:
  ```bash
  .venv/bin/python -c "
  from video_search.predicate import compile_where
  import sqlite3
  p1 = compile_where('x = ?', (float('nan'),))
  p2 = compile_where('x = ?', (float('nan'),))
  print('Hai predicate giống nhau có bằng nhau không:', p1 == p2) # False
  db = sqlite3.connect(':memory:')
  db.execute('CREATE TABLE t(x REAL)')
  db.execute('INSERT INTO t VALUES(?)', p1.params)
  print('Giá trị SQLite thực tế nhận được:', db.execute('SELECT x FROM t').fetchone()) # (None,)
  "
  ```

---

### VS-09: Không kiểm tra ngân sách token trước khi rerank cho các câu truy vấn dài với mô hình 512 token
- **Mức độ nghiêm trọng**: **Low**
- **Vị trí mã nguồn**: `src/video_search/search.py`, `SearchEngine._rerank`, dòng 509–519 và `models.py`, dòng 130–132
- **Đoạn mã vi phạm**:
  ```python
  # src/video_search/search.py:510-519
          reserved = len(tokenizer.encode(query, add_special_tokens=False))
          reserved += tokenizer.num_special_tokens_to_add(
              pair=True
          ) - tokenizer.num_special_tokens_to_add(pair=False)
          chunks = split_texts(
              tokenizer,
              [embedding_text(self.passages[i]) for i in candidates],
              512,
              reserved_tokens=reserved,
          )
  ```
- **Mô tả & Phân tích nguyên nhân gốc rễ**:
  Hàm `split_texts` ràng buộc: `budget = limit - overhead - reserved_tokens - 1; if budget < 8: raise ValueError("Model token budget is too small for transcript search.")`. Đối với các mô hình reranker có giới hạn 512 token, nếu người dùng nhập một câu truy vấn rất dài (~500 token), câu truy vấn vẫn vượt qua bước tìm kiếm ngữ nghĩa dense ban đầu, nhưng khi sang bước rerank cặp `(query, passage)`, `budget < 8` kích hoạt và ném ngoại lệ `ValueError`.
- **Rủi ro & Tác động vận hành**:
  Truy vấn tìm kiếm dài bị đứt gãy đột ngột tại pha rerank thay vì được cắt ngắn an toàn.
- **Đề xuất khắc phục**:
  Trong `_rerank`, kiểm tra nếu `reserved` vượt quá ngưỡng an toàn (ví dụ: `reserved > 400`), tự động cắt bớt token truy vấn cho reranker hoặc bỏ qua bước rerank với một dòng log cảnh báo.

---

## 6. Lộ trình khắc phục và khuyến nghị kỹ thuật (Remediation Roadmap)

Để giải quyết triệt để 26 khiếm khuyết được phát hiện mà vẫn bảo đảm tính toàn vẹn hệ thống và giữ nguyên vẹn các bài test hiện có, đề xuất lộ trình sửa lỗi chia làm 3 giai đoạn:

```
[ Giai đoạn 1: Sửa 8 lỗi High ]
  ├── Ngăn chặn sập tiến trình SQLite & Lỗi cú pháp (VS-01, TC-03, VS-03)
  ├── Triệt tiêu request mạng dư thừa & Tránh rate-limit HTTP 429 (TC-01)
  ├── Mở rộng hỗ trợ URL kênh chuẩn YouTube (TC-02)
  ├── Bảo vệ toàn vẹn dữ liệu và cách ly lỗi batch (TC-04, TC-05)
  └── Đảm bảo độ phủ kết quả và tránh đói đoạn trích rerank (VS-02)

[ Giai đoạn 2: Sửa 10 lỗi Medium ]
  ├── Cải thiện khả năng phục hồi vòng lặp duyệt track phụ đề (TC-06)
  ├── Đồng bộ hóa phân loại lỗi âm thanh không lời trong generate mode (TC-07)
  ├── Chuẩn hóa chuỗi sự kiện telemetry video_started / video_finished (TC-08)
  ├── Ngăn chặn sinh bản ghi mồ côi ngoài tầm tìm kiếm (TC-09)
  ├── Tăng cường độ bền phân giải kênh YouTube rỗng tab /videos (TC-10)
  ├── Sửa lỗi logic ba giá trị SQL trong kiểm tra cache generate (TC-11)
  ├── Xử lý giá trị None an toàn trong exact scoring (VS-04)
  ├── Gia cố việc nạp thuộc tính mô hình sentence-transformers (VS-05)
  ├── Tối ưu hóa bảng FTS5 tránh ghi đĩa liên tục (VS-06)
  └── Ngăn chặn mở kết nối ghi lên DB phụ đề từ cache engine (VS-07)

[ Giai đoạn 3: Hoàn thiện 8 lỗi Low & Nâng cao năng lực Test Suite ]
  ├── Hỗ trợ CUDA cài đặt cấp hệ điều hành (TC-12)
  ├── Bọc an toàn lỗi thứ cấp trong exception handler (TC-13)
  ├── Kiểm tra tính hợp lệ của timestamp và tệp âm thanh đầu vào (TC-14, TC-17)
  ├── Đồng bộ hóa kiểm tra tham số API và cấu hình đường dẫn (TC-15, TC-16)
  ├── Kiểm soát tham số số thực NaN/Inf và ngân sách token rerank (VS-08, VS-09)
  └── Bổ sung các ca kiểm thử hồi quy (Regression Test Suite) cho toàn bộ 26 lỗi
```

---

## 7. Kết luận và Phương pháp kiểm chứng độc lập (Conclusion & Independent Verification)

### 7.1 Kết luận
Hai thư viện lõi `src/transcript_collector` và `src/video_search` có nền tảng thiết kế cấu trúc rất vững chắc: tuân thủ nghiêm ngặt ranh giới phân tách gói, thực thi hoàn toàn đồng bộ, quản lý tệp âm thanh tạm thời an toàn 100%, bảo mật tuyệt đối nhật ký và bao bọc sự kiện chặt chẽ. Bộ test suite 255 ca kiểm thử hiện tại đã bảo vệ tốt các luồng xử lý thông thường.

Tuy nhiên, 26 lỗi tiềm ẩn được xác định trong báo cáo này đại diện cho các ca biên nghiêm trọng về độ bền vận hành, khả năng chịu lỗi mạng, và tính toàn vẹn của dữ liệu trong môi trường sản xuất thực tế. Việc áp dụng lộ trình khắc phục theo khuyến nghị sẽ nâng cao vượt bậc độ ổn định và độ tin cậy của toàn bộ dự án `yt-finder`.

### 7.2 Hướng dẫn kiểm chứng độc lập dành cho Kiểm toán viên (Forensic Auditor & QA)

1. **Kiểm tra trạng thái nguyên vẹn của mã nguồn**:
   ```bash
   git status --porcelain src/ tests/
   ```
   *Kết quả xác nhận*: Không có bất kỳ tệp nào trong `src/` bị sửa đổi (`0 modifications`).

2. **Chạy toàn bộ bộ kiểm thử đơn vị cơ sở**:
   ```bash
   .venv/bin/python -m unittest discover tests
   ```
   *Kết quả xác nhận*: `Ran 255 tests ... OK`.

3. **Chạy công cụ kiểm tra cú pháp và kiểu dữ liệu tĩnh**:
   ```bash
   .venv/bin/ruff check src/transcript_collector src/video_search
   .venv/bin/pyrefly check src/transcript_collector src/video_search
   ```
   *Kết quả xác nhận*: `All checks passed!`, `0 errors`.

4. **Tái hiện các lỗi cốt lõi đại diện (Verification Scripts)**:
   - Tái hiện lỗi crash `IntegrityError` trong search cache (**VS-01**):
     ```bash
     .venv/bin/python -c "from video_search.search import lexical_scores; from tests.test_search import window; doc = window('test', 'v1'); lexical_scores('test', [doc, doc])"
     ```
   - Tái hiện lỗi từ chối URL kênh chuẩn (**TC-02**):
     ```bash
     .venv/bin/python -c "from transcript_collector.youtube import YouTube; YouTube('vi').resolve('https://www.youtube.com/@HubermanLab/about')"
     ```
   - Tái hiện lỗi sập `TypeError` trong `parse_json3` (**TC-03**):
     ```bash
     .venv/bin/python -c "from transcript_collector.youtube import parse_json3; parse_json3({'events': [{'tStartMs': 1000, 'dDurationMs': None, 'segs': [{'utf8': 'hi'}]}]})"
     ```
   - Tái hiện lỗi sập `exact_scores` với title None (**VS-04**):
     ```bash
     .venv/bin/python -c "from video_search.search import exact_scores; exact_scores('hi', [{'text': 'hi', 'title': None}])"
     ```
   - Tái hiện lỗi đói đoạn trích rerank (**VS-02**):
     ```bash
     .venv/bin/python -c "from unittest.mock import Mock, patch; import types; from video_search.search import SearchEngine; from video_search.cache import EmbeddingStore; from tests.fakes import fake_model, fake_reranker; from tests.test_search import window; m = types.ModuleType('sentence_transformers'); setattr(m, 'SentenceTransformer', Mock(return_value=fake_model())); setattr(m, 'CrossEncoder', Mock(return_value=fake_reranker())); docs = [window('q 1', 'v1', start=0), window('q 2', 'v1', start=10), window('q 3', 'v1', start=20), window('q 4', 'v1', start=120), window('q 5', 'v1', start=180)]; p = patch.dict('sys.modules', {'sentence_transformers': m}); p.start(); engine = SearchEngine(EmbeddingStore(':memory:'), mode='hybrid', reranker_name='dummy'); res = engine.search_windows('q', docs, top=1, rerank_top=3, moments_per_video=3); p.stop(); print('Moments:', len(res[0]['moments']))"
     ```
