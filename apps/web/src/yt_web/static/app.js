"use strict";

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const terminalStatuses = new Set(["completed", "partial", "failed", "cancelled", "interrupted"]);
  const statuses = { queued: "Đang chờ", running: "Đang xử lý", completed: "Hoàn tất", partial: "Hoàn tất một phần", failed: "Có lỗi", cancelled: "Đã hủy", interrupted: "Bị gián đoạn" };
  const modes = { manual: "Phụ đề thủ công", auto_generated: "Phụ đề tự động", translated: "Bản dịch tự động", unknown: "Không rõ loại phụ đề", whisper: "Whisper" };
  const state = { view: "search", jobs: new Map(), selectedJob: null, events: new Map(), jobsTimer: null, jobsLoading: false, jobsReloadRequested: false, detailLoading: new Set(), page: 1, limit: 20, libraryGeneration: 0, toastTimer: null };
  const number = value => new Intl.NumberFormat("vi-VN").format(Number(value) || 0);
  const clock = value => { const seconds = Math.max(0, Math.floor(Number(value) || 0)); const hours = Math.floor(seconds / 3600); return hours ? `${hours}:${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}` : `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`; };
  const date = value => { if (!value) return ""; const parsed = new Date(value); return Number.isNaN(parsed.valueOf()) ? "" : parsed.toLocaleString("vi-VN", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }); };
  const briefTime = value => { const parsed = new Date(value); return Number.isNaN(parsed.valueOf()) ? "" : parsed.toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }); };

  function el(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = String(content);
    return node;
  }

  function link(text, href, className = "") {
    const node = el("a", className, text);
    node.href = href;
    node.target = "_blank";
    node.rel = "noopener noreferrer";
    return node;
  }

  function videoURL(videoId, start) {
    const url = new URL("https://www.youtube.com/watch");
    url.searchParams.set("v", String(videoId || ""));
    if (Number.isFinite(Number(start)) && Number(start) > 0) url.searchParams.set("t", `${Math.floor(Number(start))}s`);
    return url.href;
  }

  function notice(selector, message) {
    const node = $(selector);
    node.textContent = message || "";
    node.classList.toggle("hidden", !message);
  }

  function toast(message, error = false) {
    const node = $("#toast");
    clearTimeout(state.toastTimer);
    node.textContent = message;
    node.classList.toggle("error", error);
    node.classList.remove("hidden");
    state.toastTimer = setTimeout(() => node.classList.add("hidden"), 5000);
  }

  function errorMessage(body, fallback) {
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail)) return body.detail.map(item => `${(item.loc || []).filter(part => part !== "body").join(".")}: ${item.msg || "Giá trị không hợp lệ"}`).join("; ");
    if (typeof body?.error === "string") return body.error;
    return fallback;
  }

  async function api(path, options = {}) {
    const headers = { Accept: "application/json", ...options.headers };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    if (options.method && options.method !== "GET") headers["X-YT-Request"] = "1";
    let response;
    try { response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store" }); }
    catch (error) { if (error.name === "AbortError") throw error; throw new Error("Không kết nối được với ứng dụng. Kiểm tra web server rồi thử lại."); }
    const body = await response.json().catch(() => null);
    if (!response.ok) throw new Error(errorMessage(body, `Yêu cầu chưa thực hiện được (${response.status}).`));
    return body;
  }

  function emptyState(title, description, action) {
    const node = el("div", "empty-list");
    node.append(el("h3", "", title), el("div", "", description));
    if (action) { const anchor = el("a", "button secondary", action.label); anchor.href = action.href; node.append(anchor); }
    return node;
  }

  function setView() {
    const name = location.hash.slice(1);
    if (name === "main") return;
    const view = ["search", "collections", "library"].includes(name) ? name : "search";
    state.view = view;
    $$(".view").forEach(node => node.classList.toggle("hidden", node.id !== `view-${view}`));
    $$(".nav-link").forEach(node => { const active = node.dataset.view === view; node.classList.toggle("active", active); if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current"); });
    const title = { search: "Tìm kiếm", collections: "Thu thập", library: "Thư viện" }[view];
    document.title = `${title} · YT Finder`;
    if (view === "collections") loadJobs();
    if (view === "library") { loadLibraryMetadata(); loadVideos(); }
    if ($("#processing-logs")?.open && $("#log-selected-job")?.checked) syncLogStream();
  }

  async function health() {
    try {
      const data = await api("/api/health");
      const ready = data.status === "ok";
      $("#connection-dot").className = `connection-dot ${ready ? "ready" : "offline"}`;
      $("#connection-label").textContent = ready ? "Đã kết nối" : "Cần kiểm tra kết nối";
      const missing = [];
      if (data.components?.collector === false) missing.push("thu thập");
      if (data.components?.search === false) missing.push("tìm kiếm");
      notice("#global-notice", missing.length ? `Chức năng ${missing.join(" và ")} chưa sẵn sàng. Hãy chuẩn bị môi trường nghiệp vụ theo hướng dẫn khởi chạy.` : "");
    } catch (error) {
      $("#connection-dot").className = "connection-dot offline";
      $("#connection-label").textContent = "Mất kết nối";
      notice("#global-notice", error.message);
    }
  }

  function updateChannelSelect(selector, channels) {
    const select = $(selector);
    const previous = select.value;
    if (select.tagName === "INPUT") {
      const suggestions = $("#search-channels"); suggestions.replaceChildren();
      for (const channel of channels) { const option = el("option"); option.value = channel.id; option.label = channel.title || channel.id; suggestions.append(option); }
      return;
    }
    select.replaceChildren(el("option", "", "Tất cả kênh"));
    select.options[0].value = "";
    for (const channel of channels) { const option = el("option", "", channel.title || channel.id); option.value = channel.id; select.append(option); }
    if ([...select.options].some(option => option.value === previous)) select.value = previous;
  }

  async function loadLibraryMetadata() {
    try {
      const data = await api("/api/library");
      $$("[data-stat]").forEach(node => { node.textContent = number(data.stats?.[node.dataset.stat]); });
      updateChannelSelect("#search-channel", data.channels || []);
      updateChannelSelect("#library-channel", data.channels || []);
      $("#library-updated").textContent = data.stats?.last_updated ? `Transcript cập nhật gần nhất: ${date(data.stats.last_updated)}` : (data.has_database ? "Thư viện chưa có transcript" : "Thêm nội dung để bắt đầu xây thư viện");
    } catch (error) {
      $("#library-updated").textContent = "Chưa đọc được thư viện";
      if (state.view === "library") notice("#library-error", error.message);
    }
  }

  function updateSearchMode() {
    const mode = $("#search-mode").value;
    const usesDense = mode === "hybrid" || mode === "semantic";
    const hybrid = mode === "hybrid";
    for (const id of ["search-model", "search-device", "search-batch-size", "search-min-similarity", "search-offline"]) {
      const field = $("#" + id);
      field.disabled = !usesDense;
      field.closest(".field, .check-field")?.classList.toggle("hidden", !usesDense);
    }
    const rerankField = $("#search-rerank");
    rerankField.disabled = !hybrid;
    rerankField.closest(".check-field")?.classList.toggle("hidden", !hybrid);
    const notes = {
      hybrid: "Kết hợp BM25 và Vector (RRF), mặc định sắp xếp lại kết quả đầu bằng Cross-Encoder nhẹ.",
      semantic: "Vector search thuần: tìm các đoạn có ý nghĩa gần với câu hỏi theo cosine similarity.",
      keyword: 'Tìm đủ các từ trong transcript hoặc tiêu đề (BM25), không phân biệt dấu và chữ hoa. Dùng "cụm từ" để khớp đúng thứ tự từ; dấu câu như + không được phân biệt.',
      exact: "Khớp chính xác chuỗi con nguyên văn (giữ nguyên dấu tiếng Việt và dấu câu như C++, NAD+), không phân biệt chữ hoa/thường.",
    };
    $("#search-mode-note").textContent = notes[mode] || "";
  }
  $("#search-mode").addEventListener("change", updateSearchMode);
  updateSearchMode();

  async function search(event) {
    event?.preventDefault();
    const form = $("#search-form");
    if (!form.reportValidity()) return;
    const query = $("#query").value.trim();
    if (!query) { $("#query").focus(); return; }
    const submit = $("#search-submit");
    if (submit.disabled) return;
    submit.disabled = true;
    $("#search-results").setAttribute("aria-busy", "true");
    submit.querySelector("span").textContent = "Đang tìm…";
    notice("#search-error", "");
    $("#search-welcome").classList.add("hidden");
    $("#results-heading").classList.add("hidden");
    const loading = el("div", "loading-state");
    const mode = $("#search-mode").value;
    const usesDense = mode === "hybrid" || mode === "semantic";
    loading.append(el("span", "spinner"), el("strong", "", "Đang tìm trong thư viện…"), el("p", "", usesDense ? "Lần đầu có thể cần tải mô hình và chuẩn bị chỉ mục." : "Đang tìm trực tiếp trên transcript."));
    $("#search-results").replaceChildren(loading);
    const payload = {
      query, moments_per_video: Number($("#search-moments").value), video_ids: $("#search-video-ids").value.trim() ? $("#search-video-ids").value.trim().split(/[\s,]+/) : null, mode, rerank: mode === "hybrid" ? $("#search-rerank").checked : null, top: Number($("#search-top").value),
      language: $("#search-language").value.trim(), channel: $("#search-channel").value.trim() || null,
      model: $("#search-model").value, transcript_source: $("#search-source").value,
      source_mode: $("#search-source-mode").value.trim() || null, device: $("#search-device").value || null,
      batch_size: Number($("#search-batch-size").value), offline: $("#search-offline").checked,
      min_similarity: usesDense ? optionalNumber("#search-min-similarity") : null, ...pathOptions("search", ["db", "search_db"]),
    };
    try {
      const data = await api("/api/search", { method: "POST", body: JSON.stringify(payload) });
      renderResults(data, query);
    } catch (error) {
      $("#search-results").replaceChildren(emptyState("Chưa thể hoàn thành lượt tìm", "Bạn có thể điều chỉnh bộ lọc hoặc thử lại."));
      notice("#search-error", error.message);
    } finally {
      submit.disabled = false;
      $("#search-results").setAttribute("aria-busy", "false");
      submit.querySelector("span").textContent = "Tìm kiếm";
    }
  }

  function renderResults(data, query) {
    const results = Array.isArray(data?.results) ? data.results : [];
    const container = $("#search-results");
    container.replaceChildren();
    $("#results-heading").classList.remove("hidden");
    $("#result-count").textContent = number(results.length);
    $("#results-download").classList.toggle("hidden", !results.length);
    state.lastResults = data;
    $("#results-summary").textContent = `“${query}”${Number.isFinite(data.elapsed_seconds) ? ` · ${data.elapsed_seconds.toLocaleString("vi-VN", { maximumFractionDigits: 2 })} giây` : ""}`;
    if (!results.length) {
      container.append(emptyState("Chưa tìm thấy nội dung phù hợp", "Thử cách diễn đạt khác, nới bộ lọc hoặc thêm transcript vào thư viện.", { label: "Thu thập thêm nội dung", href: "#collections" }));
      return;
    }
    results.forEach((item, index) => {
      const card = el("article", "result-card");
      card.append(el("div", "result-index", String(index + 1).padStart(2, "0")));
      const main = el("div", "result-main");
      const top = el("div", "result-top");
      const title = el("div");
      title.append(link(item.title || item.video_id, videoURL(item.video_id), "result-title"), el("div", "result-channel", item.channel_title || item.channel_id));
      top.append(title);
      const tags = el("div", "result-tags");
      tags.append(el("span", "tag", item.language || "—"), el("span", "tag", item.transcript_source === "generate" ? "Tạo từ audio" : "YouTube"), el("span", "tag", modes[item.source_mode] || item.source_mode || ""));
      main.append(top, tags);
      const moments = Array.isArray(item.moments) && item.moments.length ? item.moments : [item];
      moments.forEach((moment, momentIndex) => {
        const section = el("section", "result-moment");
        const time = link(`Đoạn ${momentIndex + 1} · ▶ ${clock(moment.start)} – ${clock(moment.end)}`, videoURL(item.video_id, moment.start), "result-time");
        time.title = "Mở video từ đầu đoạn kết quả";
        const transcript = el("p", "transcript clamped", moment.text || "");
        const bottom = el("div", "result-bottom");
        const expand = el("button", "text-button", "Xem thêm");
        expand.type = "button";
        expand.setAttribute("aria-expanded", "false");
        expand.addEventListener("click", () => { const clamped = transcript.classList.toggle("clamped"); expand.textContent = clamped ? "Xem thêm" : "Thu gọn"; expand.setAttribute("aria-expanded", String(!clamped)); });
        bottom.append(expand);
        const metadata = el("details", "result-metadata");
        metadata.append(el("summary", "", "Chi tiết"));
        const values = { score: moment.score, similarity: moment.similarity, rerank_score: moment.rerank_score, language: item.language, source_mode: item.source_mode, transcript_config: item.transcript_config };
        const description = el("dl", "metadata-list");
        for (const [key, value] of Object.entries(values)) if (value !== undefined && value !== null) description.append(el("dt", "", key), el("dd", "", value === "" ? "—" : value));
        metadata.append(description);
        section.append(time, transcript, bottom, metadata);
        main.append(section);
      });
      card.append(main);
      container.append(card);
    });
  }

  function updateMode() {
    const generated = $("input[name=collection-mode]:checked").value === "generate";
    $("#direct-language-field").classList.toggle("hidden", generated);
    $("#generate-fields").classList.toggle("hidden", !generated);
    $("#collection-language").disabled = generated;
    $("#collection-language").required = !generated;
    $("#collection-engine").disabled = !generated;
    $("#collection-engine-model").disabled = !generated;
  }

  function collectionPayload() {
    const mode = $("input[name=collection-mode]:checked").value;
    return normalizeCollection({ target: $("#collection-target").value.trim(), mode, language: $("#collection-language").value.trim(), refresh: $("#collection-refresh").checked, engine: $("#collection-engine").value || "whisper", engine_model: $("#collection-engine-model").value || "medium", device: $("#collection-device").value || null, cookies: $("#collection-cookies").value.trim() || null, ...pathOptions("collection", ["db"]) });
  }

  function retryPayload(job) {
    const request = job.request || {};
    const mode = request.mode === "generate" ? "generate" : "direct";
    return normalizeCollection({ ...request, target: request.target || job.target, mode });
  }

  async function enqueue(payload) {
    const job = await api("/api/collections", { method: "POST", body: JSON.stringify(payload) });
    state.jobs.set(job.id, job);
    state.selectedJob = job.id;
    renderJobs();
    renderSelectedJob();
    await loadJobs();
    await loadJobEvents(job.id);
    toast("Đã thêm lượt thu thập vào danh sách.");
    return job;
  }

  async function submitCollection(event) {
    event.preventDefault();
    if (!$("#collection-form").reportValidity()) return;
    const button = $("#collection-submit");
    if (button.disabled) return;
    const payload = collectionPayload();
    if (!payload.target) { $("#collection-target").focus(); return; }
    button.disabled = true;
    notice("#collection-error", "");
    try { await enqueue(payload); }
    catch (error) { notice("#collection-error", error.message); }
    finally { button.disabled = false; }
  }

  function countSummary(counts = {}) {
    const node = el("div", "job-counts");
    for (const [key, label] of [["indexed", "đã lưu"], ["cached", "đã có"], ["unavailable", "không có phụ đề"], ["errors", "lỗi"]]) {
      if (!counts[key] && key !== "indexed" && key !== "cached") continue;
      const item = el("span");
      item.append(el("strong", "", number(counts[key])), document.createTextNode(` ${label}`));
      node.append(item);
    }
    return node;
  }

  function jobStatus(job) { return el("span", `status-badge ${job.status || "queued"}`, statuses[job.status] || job.status || "Đang chờ"); }

  function jobProgress(job) {
    const fragment = document.createDocumentFragment();
    const progress = job.progress || {};
    const counts = job.result?.counts || progress.counts || {};
    const total = Number(progress.total || counts.listed || 0);
    const position = Number(progress.position || 0);
    const done = ["indexed", "cached", "unavailable", "errors"].reduce((sum, key) => sum + (Number(counts[key]) || 0), 0);
    const current = progress.title || (job.status === "queued" ? "Sẵn sàng khi đến lượt" : job.status === "running" ? "Đang lấy thông tin nội dung…" : statuses[job.status] || "");
    fragment.append(el("div", "job-current", `${total ? `${number(Math.min(position || done, total))}/${number(total)} · ` : ""}${current}`));
    const bar = el("progress", "job-progress");
    bar.max = 100;
    bar.setAttribute("aria-label", "Tiến độ thu thập");
    if (total) bar.value = Math.min(100, done / total * 100);
    else if (job.status !== "running") bar.value = job.status === "completed" ? 100 : 0;
    fragment.append(bar, countSummary(counts));
    return fragment;
  }

  function renderJobs() {
    const container = $("#job-list");
    const jobs = [...state.jobs.values()].sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
    const active = jobs.filter(job => !terminalStatuses.has(job.status)).length;
    $("#active-job-count").textContent = number(active);
    $("#active-job-count").classList.toggle("hidden", active === 0);
    $("#jobs-description").textContent = jobs.length ? `${number(jobs.length)} lượt${active ? ` · ${number(active)} đang chờ hoặc xử lý` : ""}` : "Theo dõi tiến độ và kết quả xử lý";
    container.replaceChildren();
    if (!jobs.length) { container.append(emptyState("Chưa có lượt thu thập", "Thêm một liên kết ở bên cạnh để bắt đầu xây thư viện.")); return; }
    for (const job of jobs) {
      const card = el("article", `job-card${state.selectedJob === job.id ? " selected" : ""}`);
      const top = el("div", "job-card-top");
      const text = el("div");
      const title = el("div", "job-title", job.target || job.request?.target || "Lượt thu thập");
      title.title = title.textContent;
      const mode = job.request?.mode === "generate" ? (job.request?.engine_model ? `Whisper (${job.request.engine_model})` : "Whisper") : `Phụ đề ${job.request?.language || ""}`;
      text.append(title, el("div", "job-meta", `${mode} · ${date(job.created_at)}`));
      top.append(text, jobStatus(job));
      const actions = el("div", "job-actions");
      const details = el("button", "text-button", state.selectedJob === job.id ? "Đang xem" : "Chi tiết");
      details.type = "button";
      details.addEventListener("click", () => { state.selectedJob = job.id; renderJobs(); renderSelectedJob(); loadJobEvents(job.id); });
      const operations = el("div");
      if (terminalStatuses.has(job.status)) {
        const retry = el("button", "text-button", "Chạy lại"); retry.type = "button";
        retry.addEventListener("click", async () => { retry.disabled = true; try { await enqueue(retryPayload(job)); } catch (error) { toast(error.message, true); } finally { retry.disabled = false; } });
        operations.append(retry);
      } else {
        const cancel = el("button", "text-button cancel-button", "Hủy lượt"); cancel.type = "button";
        cancel.addEventListener("click", async () => { cancel.disabled = true; try { const updated = await api(`/api/collections/${encodeURIComponent(job.id)}/cancel`, { method: "POST" }); state.jobs.set(job.id, updated); renderJobs(); renderSelectedJob(); scheduleJobs(500); toast("Đã gửi yêu cầu hủy. Các transcript đã lưu được giữ lại."); } catch (error) { toast(error.message, true); cancel.disabled = false; } });
        operations.append(cancel);
      }
      actions.append(details, operations);
      card.append(top, jobProgress(job), actions);
      container.append(card);
    }
  }

  function renderSelectedJob() {
    const job = state.jobs.get(state.selectedJob);
    $("#job-detail").classList.toggle("hidden", !job);
    if ($("#processing-logs").open && $("#log-selected-job").checked) syncLogStream();
    if (!job) return;
    const detail = $("#job-detail-content");
    detail.replaceChildren(el("p", "detail-target", job.target || job.request?.target || ""));
    if (job.error) detail.append(el("p", "detail-error", job.error));
    renderEvents(job.id);
  }

  function eventDescription(item) {
    const event = item.payload || {};
    if (event.message) return String(event.message);
    if (event.kind === "video_started" || item.kind === "video_started") return `${event.cached ? "Dùng transcript đã có" : "Đang xử lý"}: ${event.title || event.video_id || "video"}`;
    if (event.kind === "video_finished" || item.kind === "video_finished") { const counts = event.counts || {}; return `${counts.indexed ? "Đã lưu" : counts.cached ? "Đã có transcript" : counts.unavailable ? "Không có phụ đề phù hợp" : counts.errors ? "Xử lý lỗi" : "Đã xử lý"}: ${event.title || event.video_id || "video"}`; }
    if (event.kind === "summary" || item.kind === "summary") return "Đã tổng hợp kết quả lượt thu thập.";
    const phase = event.phase || event.kind || item.kind;
    if (phase) return `${phase}${event.model ? ` · ${event.model}` : ""}${event.total ? ` · ${event.completed || 0}/${event.total}` : ""}`;
    return "Đã cập nhật trạng thái xử lý.";
  }

  function renderEvents(jobId) {
    if (state.selectedJob !== jobId) return;
    const container = $("#job-events");
    const events = state.events.get(jobId)?.items || [];
    container.replaceChildren();
    if (!events.length) { container.append(el("li", "", "Chưa có diễn biến xử lý.")); return; }
    for (const item of events) {
      const row = el("li");
      const time = el("time", "", briefTime(item.created_at));
      if (item.created_at) time.dateTime = item.created_at;
      row.append(time, el("span", "", eventDescription(item)));
      container.append(row);
    }
    container.scrollTop = container.scrollHeight;
  }

  async function loadJobEvents(jobId) {
    if (!jobId || state.detailLoading.has(jobId)) return;
    state.detailLoading.add(jobId);
    try {
      const saved = state.events.get(jobId) || { cursor: 0, items: [] };
      let pages = 0, more = false;
      do {
        const data = await api(`/api/collections/${encodeURIComponent(jobId)}/events?after=${saved.cursor}`);
        const items = data.items || [];
        const seen = new Set(saved.items.map(item => item.id));
        saved.items.push(...items.filter(item => !seen.has(item.id)));
        saved.items = saved.items.slice(-100);
        const next = Number(data.next_cursor || saved.cursor);
        more = items.length >= 200 && next > saved.cursor;
        saved.cursor = Math.max(saved.cursor, next);
        pages += 1;
      } while (more && pages < 5);
      state.events.set(jobId, saved);
      renderEvents(jobId);
      if (more && state.selectedJob === jobId) setTimeout(() => loadJobEvents(jobId), 150);
    } catch (error) {
      if (state.selectedJob === jobId) $("#job-events").replaceChildren(el("li", "", error.message));
    } finally { state.detailLoading.delete(jobId); }
  }

  function scheduleJobs(delay = 2000) { clearTimeout(state.jobsTimer); state.jobsTimer = setTimeout(loadJobs, delay); }

  async function loadJobs() {
    if (state.jobsLoading) { state.jobsReloadRequested = true; return; }
    state.jobsLoading = true;
    clearTimeout(state.jobsTimer);
    try {
      const data = await api("/api/collections");
      const jobs = data.items || [];
      const justFinished = jobs.some(job => terminalStatuses.has(job.status) && state.jobs.has(job.id) && !terminalStatuses.has(state.jobs.get(job.id).status));
      state.jobs = new Map(jobs.map(job => [job.id, job]));
      notice("#jobs-error", "");
      renderJobs();
      renderSelectedJob();
      if (state.selectedJob) await loadJobEvents(state.selectedJob);
      if (justFinished) { loadLibraryMetadata(); if (state.view === "library") loadVideos(); }
      if (jobs.some(job => !terminalStatuses.has(job.status))) scheduleJobs();
    } catch (error) {
      notice("#jobs-error", error.message);
      if ([...state.jobs.values()].some(job => !terminalStatuses.has(job.status))) scheduleJobs(5000);
    } finally {
      state.jobsLoading = false;
      if (state.jobsReloadRequested) { state.jobsReloadRequested = false; scheduleJobs(0); }
    }
  }

  async function loadVideos() {
    const generation = ++state.libraryGeneration;
    notice("#library-error", "");
    $("#library-videos").replaceChildren(el("div", "empty-list", "Đang đọc danh sách video…"));
    $("#library-prev").disabled = true;
    $("#library-next").disabled = true;
    const params = new URLSearchParams({ page: String(state.page), limit: String(state.limit), q: $("#library-query").value.trim() });
    if ($("#library-channel").value) params.set("channel", $("#library-channel").value);
    try {
      const data = await api(`/api/videos?${params}`);
      if (generation !== state.libraryGeneration) return;
      const items = data.items || [];
      const total = Number(data.total) || 0;
      const container = $("#library-videos");
      container.replaceChildren();
      if (!items.length) container.append(emptyState(total ? "Không có video ở trang này" : "Chưa có video phù hợp", "Thử đổi bộ lọc hoặc thêm nội dung vào thư viện.", { label: "Thu thập nội dung", href: "#collections" }));
      else {
        const table = el("table", "video-table");
        const head = el("thead"); const headRow = el("tr");
        for (const label of ["VIDEO", "KÊNH", "TRANSCRIPT"]) { const cell = el("th", "", label); cell.scope = "col"; headRow.append(cell); }
        head.append(headRow); table.append(head);
        const body = el("tbody");
        for (const item of items) {
          const row = el("tr");
          const video = el("td"); video.append(link(item.title || item.id, videoURL(item.id)));
          const channel = el("td", "", item.channel_title || item.channel_id);
          const transcript = el("td");
          const count = Number(item.transcripts) || 0;
          transcript.append(el("div", "", count ? `${number(count)} bản` : "Chưa có"));
          for (const language of item.languages || []) transcript.append(el("span", "video-language", language));
          row.append(video, channel, transcript); body.append(row);
        }
        table.append(body); container.append(table);
      }
      const first = total ? (state.page - 1) * state.limit + 1 : 0;
      const last = Math.min(state.page * state.limit, total);
      $("#library-page-description").textContent = `${number(first)}–${number(last)} / ${number(total)} video`;
      $("#library-prev").disabled = state.page <= 1;
      $("#library-next").disabled = state.page * state.limit >= total;
    } catch (error) {
      if (generation !== state.libraryGeneration) return;
      notice("#library-error", error.message);
      $("#library-videos").replaceChildren(emptyState("Chưa đọc được danh sách video", "Làm mới thư viện để thử lại."));
      $("#library-page-description").textContent = "";
    }
  }

  function optionalNumber(selector) { const value = $(selector).value.trim(); return value ? Number(value) : null; }

  function pathOptions(prefix, fields) {
    const options = {};
    for (const field of fields) { const value = $(`#${prefix}-${field.replaceAll("_", "-")}`)?.value.trim(); if (value) options[field] = value; }
    return options;
  }

  function normalizeCollection(request) {
    const mode = request.mode === "generate" ? "generate" : "direct";
    const result = { target: request.target, mode, language: mode === "direct" ? request.language || "vi" : null, refresh: Boolean(request.refresh) };
    for (const field of ["device", "cookies", "db"]) if (request[field]) result[field] = request[field];
    if (mode === "generate") { result.engine = request.engine || "whisper"; result.engine_model = request.engine_model || "medium"; }
    return result;
  }

  function downloadJSON(value, filename) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2) + "\n"], { type: "application/json" }));
    const anchor = el("a"); anchor.href = url; anchor.download = filename; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  const logState = { cursor: "", filter: "", source: null, seen: new Set(), generation: 0 };
  function selectedLogJob() { return state.selectedJob; }
  function closeLogStream() {
    if (logState.source) { logState.source.close(); logState.source = null; }
  }
  function appendLogEntry(item) {
    if (!item || (item.id && logState.seen.has(item.id))) return;
    if (item.id) {
      logState.seen.add(item.id);
      if (logState.seen.size > 600) {
        const oldest = logState.seen.values().next().value;
        logState.seen.delete(oldest);
      }
      logState.cursor = item.id;
    }
    const list = $("#log-entries");
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 35;
    const row = el("li", `log-entry log-${String(item.level || "INFO").toLowerCase()}`);
    const heading = el("div", "log-heading", `${item.timestamp || ""} · ${item.level || "INFO"} · ${item.logger || ""}`);
    row.append(heading, el("pre", "log-message", item.message || ""));
    const excluded = new Set(["id", "timestamp", "level", "logger", "message", "traceback"]);
    const extra = Object.fromEntries(Object.entries(item).filter(([key, value]) => !excluded.has(key) && value !== null));
    if (Object.keys(extra).length || item.traceback) {
      const details = el("details");
      details.append(el("summary", "", "Chi tiết"), el("pre", "log-context", JSON.stringify(extra, null, 2)));
      if (item.traceback) details.append(el("pre", "log-context", item.traceback));
      row.append(details);
    }
    list.append(row);
    while (list.childElementCount > 300) list.firstElementChild.remove();
    if (atBottom) list.scrollTop = list.scrollHeight;
  }
  function syncLogStream() {
    if (!$("#processing-logs").open) { closeLogStream(); return; }
    const selected = $("#log-selected-job").checked ? selectedLogJob() : null;
    if ($("#log-selected-job").checked && !selected) {
      closeLogStream();
      $("#log-entries").replaceChildren();
      logState.cursor = "";
      logState.filter = "";
      logState.seen.clear();
      notice("#logs-error", "Chọn một lượt thu thập để xem nhật ký riêng.");
      return;
    }
    const filter = `${$("#log-level").value}:${selected || ""}`;
    if (filter === logState.filter && logState.source && logState.source.readyState !== EventSource.CLOSED) return;
    closeLogStream();
    if (filter !== logState.filter) {
      logState.filter = filter;
      logState.cursor = "";
      logState.seen.clear();
      $("#log-entries").replaceChildren();
    }
    logState.generation += 1;
    const generation = logState.generation;
    const params = new URLSearchParams({ level: $("#log-level").value, limit: "200" });
    if (logState.cursor) params.set("after", logState.cursor);
    if (selected) params.set("job_id", selected);
    const source = new EventSource(`/api/logs/stream?${params}`);
    logState.source = source;
    source.addEventListener("open", () => {
      if (generation === logState.generation) notice("#logs-error", "");
    });
    source.addEventListener("meta", event => {
      if (generation !== logState.generation) return;
      const data = JSON.parse(event.data || "{}");
      $("#log-file").textContent = `${data.file ? `Tệp: ${data.file}` : ""}${data.truncated ? " · Chỉ hiển thị phần nhật ký gần nhất" : ""}`;
    });
    source.addEventListener("reset", () => {
      if (generation !== logState.generation) return;
      logState.seen.clear();
      $("#log-entries").replaceChildren();
    });
    source.addEventListener("log", event => {
      if (generation !== logState.generation) return;
      appendLogEntry(JSON.parse(event.data));
      notice("#logs-error", "");
    });
    source.addEventListener("error", () => {
      if (generation !== logState.generation) return;
      if (source.readyState === EventSource.CLOSED) notice("#logs-error", "Mất kết nối luồng nhật ký.");
    });
  }

  $("#search-form").addEventListener("submit", search);
  $("#query").addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("#search-form").requestSubmit(); } });
  $("#collection-form").addEventListener("submit", submitCollection);
  $$("input[name=collection-mode]").forEach(input => input.addEventListener("change", updateMode));
  $("#refresh-jobs").addEventListener("click", loadJobs);
  $("#close-job-detail").addEventListener("click", () => { state.selectedJob = null; renderJobs(); renderSelectedJob(); });
  $("#refresh-library").addEventListener("click", () => { loadLibraryMetadata(); loadVideos(); });
  $("#library-filter-form").addEventListener("submit", event => { event.preventDefault(); state.page = 1; loadVideos(); });
  $("#library-channel").addEventListener("change", () => { state.page = 1; loadVideos(); });
  $("#library-prev").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; loadVideos(); } });
  $("#library-next").addEventListener("click", () => { state.page += 1; loadVideos(); });
  document.addEventListener("keydown", event => { if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName) && !document.activeElement?.isContentEditable) { event.preventDefault(); if (state.view !== "search") location.hash = "search"; $("#query").focus(); } });
  window.addEventListener("hashchange", setView);
  window.addEventListener("focus", () => { health(); if (state.view === "collections") loadJobs(); });
  window.addEventListener("beforeunload", () => clearTimeout(state.jobsTimer));
  $("#results-download").addEventListener("click", () => downloadJSON(state.lastResults?.results || [], "search-results.json"));
  $("#processing-logs").addEventListener("toggle", syncLogStream);
  for (const selector of ["#log-level", "#log-selected-job"]) $(selector).addEventListener("change", () => { logState.filter = ""; logState.cursor = ""; logState.seen.clear(); closeLogStream(); $("#log-entries").replaceChildren(); syncLogStream(); });
  window.addEventListener("beforeunload", closeLogStream);
  updateMode();
  setView();
  health();
  loadLibraryMetadata();
  loadJobs();
})();
