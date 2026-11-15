# YouTube transcript finder

Thu thập transcript YouTube vào SQLite rồi tìm video và các đoạn liên quan bằng
ngữ nghĩa hoặc từ khóa. Có CLI và web local; hai thư viện collector/search độc lập.

## Cài đặt

**Hiện chỉ hỗ trợ chạy trên Linux; chưa hỗ trợ Windows native.**

Cần Python 3.13+ và [uv](https://docs.astral.sh/uv/). Chạy từ root repo:

```bash
uv sync --all-extras
```

Toàn workspace dùng chung một `.venv` và `uv.lock` tại root. yt-dlp dùng Deno/EJS;
generate dùng Whisper. CUDA cần driver, CUDA 12/cuDNN phù hợp. Trọng số model có
thể tải ở lần chạy đầu; cần ổ đĩa đủ chỗ.

## Dùng nhanh

```bash
uv run --locked yt collect "https://www.youtube.com/watch?v=VIDEO_ID" --mode direct --language vi
uv run --locked yt search "nội dung cần tìm" --mode keyword
uv run --locked yt search "nội dung cần tìm"
uv run --locked yt-web
```

Thu thập transcript trước khi tìm kiếm. Search mặc định hybrid (MiniLM + BM25,
rerank), trả tối đa 5 video, mỗi video 3 đoạn; keyword không tải model.
Web local tại http://127.0.0.1:8000. Dữ liệu mặc định trong `data/`; database,
model, nhãn đánh giá, cookie và `.env` cục bộ không được đưa lên Git.

## Tài liệu

- [Cách dùng, tùy chọn và lệnh kiểm tra](docs/USAGE.md)
- [Kiến trúc, schema và hợp đồng dữ liệu](ARCHITECTURE.md)
- [Web/API local](apps/web/README.md)
- [Đánh giá tìm kiếm](evaluation/README.md)
- [Hướng dẫn cho agent](AGENTS.md)
