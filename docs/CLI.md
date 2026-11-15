# CLI

Chạy từ root bằng môi trường chung:

```bash
uv sync --all-extras
uv run --locked yt collect "https://www.youtube.com/watch?v=VIDEO_ID" --mode direct --language vi
uv run --locked yt search "insulin" --top 5 --moments-per-video 3
uv run --locked yt search '"kháng insulin"' --mode keyword --video-id VIDEO_ID --json
uv run --locked yt search "insulin" --mode keyword --where "language IN ('vi', 'en') AND source = 'youtube'"
uv run --locked yt search --interactive --offline
```

Alias: `yt-collect`, `yt-search`. `yt collect` nhận `--db` sau tên
lệnh; alias `yt-collect` dùng subcommand `collect`, nhận DB trước hoặc sau subcommand.
`--help` của từng lệnh là danh sách tham số đầy đủ.

`--where` thay toàn bộ bộ lọc video/transcript; không dùng cùng `--channel`,
`--language`, `--transcript-source`, `--source-mode` hoặc `--video-id`, kể cả giá trị
mặc định được ghi rõ. Biểu thức không có tiền tố `WHERE`, không ngầm lọc tiếng Việt
hoặc chỉ Whisper. `channel` là tên hiển thị kênh; dùng `channel_id`/`channel_url`
để lọc ID/URL. Model, top và số đoạn vẫn áp dụng. JSON và `/settings` hiển thị
WHERE đang dùng; [cú pháp và các trường hỗ trợ](../ARCHITECTURE.md#lọc-bằng-biểu-thức-where-trong-core).

`launcher.py` dùng Python `.venv` root cho subprocess, truyền argv/môi trường,
không tự cài dependency/model. `YT_FINDER_ROOT` chỉ repo khi không tìm được từ
source editable. Đường dẫn DB tương đối tính từ cwd người gọi.

`collector_runtime` và `search_session` là adapter ứng dụng sở hữu điều phối và
cleanup, gọi trực tiếp hai core; core không có `service.py`. Interactive giữ
model/ma trận: `/show N`, `/show N M`, `/results`, `/top N`, `/moments N`,
`/settings`, `/help`, `/quit`. JSON có `mode`, `video_ids`, `moments_per_video` và
`moments` theo video; trường top-level cũ là đoạn tốt nhất.

CLI giữ stdout cho kết quả/JSON, stderr cho tiến độ/log. `--log-file` đổi JSONL
mặc định `data/logs/cli.jsonl`; `--log-level` đổi console (mặc định INFO), file luôn
DEBUG. Log xoay có khóa liên tiến trình, không ghi query/transcript/credential
đầy đủ; handler được khôi phục sau lỗi/Ctrl+C.

Source nằm tại `src/yt_cli`, cùng project root với các thư viện nghiệp vụ.
Setup: [README root](../README.md). Model/mode, đường dẫn và kiểm tra:
[hướng dẫn sử dụng](USAGE.md).
Hợp đồng nghiệp vụ và dữ liệu: [ARCHITECTURE.md](../ARCHITECTURE.md).
