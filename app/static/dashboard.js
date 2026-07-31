(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = { hours: 24, chart: null, presets: [] };
  const statusNames = {
    succeeded: "成功",
    failed: "失败",
    uploading: "上传中",
    unknown: "待确认",
  };
  const objectStatusNames = {
    pending: "待确认",
    present: "对象存在",
    absent: "对象不存在",
    legacy_unverified: "历史未验证",
    deleting: "删除中",
    deleted: "已删除",
    delete_unknown: "删除待确认",
  };

  const range = () => {
    const to = new Date();
    return {
      from: new Date(to.getTime() - state.hours * 3600000).toISOString(),
      to: to.toISOString(),
    };
  };

  async function api(path, params = {}) {
    const url = new URL(path, location.origin);
    Object.entries(params).forEach(([key, value]) => url.searchParams.set(key, value));
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.error?.message || `${response.status} ${response.statusText}`);
    }
    return response.json();
  }

  const formatBytes = (value) => {
    if (value == null) return "—";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let size = Number(value);
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
  };
  const formatDuration = (value) => value == null ? "—" : value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(1)} s`;
  const formatTime = (value) => value ? new Date(value).toLocaleString() : "—";
  const setText = (id, value) => { $(id).textContent = String(value ?? "—"); };

  function showError(error) {
    const target = $("page-error");
    target.textContent = error.message;
    target.classList.remove("hidden");
  }
  function clearError() { $("page-error").classList.add("hidden"); }

  async function loadSummary() {
    const data = await api("/v1/dashboard/summary", range());
    const checks = data.service.checks;
    setText("health-process", data.service.status === "ok" ? "正常" : "可响应");
    setText("health-database", checks.database.status === "ok" ? "正常" : "异常");
    setText("health-temp", checks.temp_dir.status === "ok" ? `正常 · ${formatBytes(checks.temp_dir.free_bytes)}` : "空间不足");
    setText("health-storage", checks.storage.status === "ok" ? `正常 · ${formatTime(checks.storage.last_checked_at)}` : "不可用");
    setText("health-preset", checks.config.preset_key || "未配置");
    setText("health-provider", checks.config.provider || "未配置");
    setText("health-revision", checks.config.revision || 0);
    const badge = $("service-badge");
    badge.textContent = data.service.ready ? "READY" : "DEGRADED";
    badge.className = `badge ${data.service.ready ? "ok" : "warning"}`;

    const uploads = data.uploads;
    setText("metric-total", uploads.attempt_count);
    setText("metric-success", uploads.success_count);
    setText("metric-failed", uploads.failure_count);
    setText("metric-pending", uploads.uploading_count + uploads.unknown_count);
    setText("metric-pending-detail", `${uploads.uploading_count} 上传中 · ${uploads.unknown_count} 待确认`);
    setText("metric-success-rate", `成功率 ${uploads.success_rate == null ? "—" : `${(uploads.success_rate * 100).toFixed(1)}%`}`);
    setText("metric-bytes", formatBytes(uploads.successful_upload_bytes));
    setText("metric-average", formatDuration(uploads.average_duration_ms));
    setText("metric-p95", `P95 ${formatDuration(uploads.p95_duration_ms)}`);
    setText("updated-at", `更新于 ${formatTime(data.generated_at)}`);
  }

  async function loadPresets() {
    const data = await api("/v1/settings/storage/presets");
    state.presets = data.items.filter((preset) => preset.enabled);
    const select = $("receive-test-preset");
    const previous = select.value;
    select.replaceChildren();
    state.presets.forEach((preset) => {
      const option = document.createElement("option");
      option.value = preset.preset_key;
      option.textContent = `${preset.display_name} (${preset.preset_key})${preset.is_default ? " · 默认" : ""}`;
      option.selected = preset.preset_key === previous
        || (!previous && preset.is_default);
      select.append(option);
    });
    select.disabled = state.presets.length === 0;
  }

  async function loadTraffic() {
    const data = await api("/v1/dashboard/traffic", {
      ...range(),
      interval: state.hours > 48 ? "day" : "hour",
    });
    const labels = data.points.map((point) => new Date(point.start).toLocaleString([], {
      month: "2-digit",
      day: "2-digit",
      hour: data.interval === "hour" ? "2-digit" : undefined,
    }));
    const chartData = {
      labels,
      datasets: [
        {
          type: "bar",
          label: "成功上传",
          data: data.points.map((point) => point.successful_upload_bytes),
          backgroundColor: "rgba(74, 168, 255, .58)",
          borderRadius: 4,
          yAxisID: "bytes",
        },
        {
          type: "line",
          label: "尝试数",
          data: data.points.map((point) => point.attempt_count),
          borderColor: "#42d3c7",
          backgroundColor: "#42d3c7",
          tension: .25,
          yAxisID: "count",
        },
        {
          type: "line",
          label: "失败数",
          data: data.points.map((point) => point.failure_count),
          borderColor: "#ff7484",
          backgroundColor: "#ff7484",
          tension: .25,
          yAxisID: "count",
        },
      ],
    };
    if (state.chart) state.chart.destroy();
    state.chart = new Chart($("traffic-chart"), {
      data: chartData,
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { labels: { color: "#b7c6d8", usePointStyle: true } } },
        scales: {
          x: { ticks: { color: "#91a4ba", maxRotation: 0 }, grid: { color: "rgba(145,164,186,.08)" } },
          bytes: { position: "left", ticks: { color: "#91a4ba", callback: formatBytes }, grid: { color: "rgba(145,164,186,.08)" } },
          count: { position: "right", beginAtZero: true, ticks: { color: "#91a4ba", precision: 0 }, grid: { display: false } },
        },
      },
    });
  }

  function cell(row, value, className) {
    const td = document.createElement("td");
    if (className) td.className = className;
    td.textContent = value;
    row.append(td);
    return td;
  }

  async function loadTasks() {
    const data = await api("/v1/upload-tasks", { limit: 30, offset: 0 });
    const body = $("task-rows");
    body.replaceChildren();
    if (!data.items.length) {
      const row = document.createElement("tr");
      const empty = cell(row, "还没有上传任务", "empty");
      empty.colSpan = 8;
      body.append(row);
      return;
    }
    data.items.forEach((task) => {
      const row = document.createElement("tr");
      cell(row, task.filename, "file");
      cell(row, statusNames[task.status] || task.status, `status status-${task.status}`);
      cell(row, `${task.storage_preset}\n${task.storage_provider || "—"} / r${task.storage_config_revision ?? "—"}`, "object-meta");
      cell(row, `ETag ${task.etag || "—"}\nVersion ${task.version_id || "—"}`, "object-meta");
      cell(
        row,
        `${objectStatusNames[task.object_status] || task.object_status}${task.delete_error_code ? `\n${task.delete_error_code}` : ""}`,
        `status object-${task.object_status}`,
      );
      cell(row, `${formatBytes(task.size_bytes)}\n${formatDuration(task.duration_ms)}`, "object-meta");
      cell(row, formatTime(task.created_at));
      const result = cell(row, "", "task-result");
      if (task.public_url && task.object_status === "present") {
        const link = document.createElement("a");
        link.href = task.public_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "打开公网链接";
        result.append(link);
        const key = document.createElement("code");
        key.textContent = task.object_key;
        key.title = task.object_key;
        result.append(key);
      } else {
        result.textContent = task.error_code || task.delete_error_code || objectStatusNames[task.object_status] || "—";
      }
      body.append(row);
    });
  }

  async function loadLogs() {
    const data = await api("/v1/dashboard/logs", { min_level: $("log-level").value, limit: 100 });
    const list = $("log-list");
    list.replaceChildren();
    if (!data.items.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "当前筛选条件下没有日志";
      list.append(empty);
      return;
    }
    data.items.forEach((item) => {
      const entry = document.createElement("article");
      entry.className = "log-entry";
      const level = document.createElement("span");
      level.className = `log-level ${item.level_name}`;
      level.textContent = item.level_name;
      const time = document.createElement("time");
      time.dateTime = item.created_at;
      time.textContent = formatTime(item.created_at);
      const content = document.createElement("div");
      const message = document.createElement("p");
      message.textContent = `${item.event} · ${item.message}`;
      const meta = document.createElement("span");
      meta.className = "log-meta";
      meta.textContent = [item.request_id && `request ${item.request_id}`, item.task_id && `task ${item.task_id}`, item.error_code].filter(Boolean).join(" · ");
      content.append(message, meta);
      entry.append(level, time, content);
      list.append(entry);
    });
  }

  async function refresh(parts = [loadSummary, loadTraffic, loadTasks, loadLogs, loadPresets]) {
    clearError();
    const results = await Promise.allSettled(parts.map((load) => load()));
    const failed = results.find((result) => result.status === "rejected");
    if (failed) showError(failed.reason);
  }

  document.querySelectorAll(".range").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll(".range").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.hours = Number(button.dataset.hours);
    refresh([loadSummary, loadTraffic]);
  }));
  $("refresh").addEventListener("click", () => refresh());
  $("log-level").addEventListener("change", () => refresh([loadLogs]));
  $("receive-test-real-upload").addEventListener("change", (event) => {
    const real = event.target.checked;
    $("receive-test-mode").textContent = real ? "真实上传已开启" : "仅接收，不上传";
    $("receive-test-mode").className = `badge ${real ? "warning" : "ok"}`;
    $("receive-test-preset-field").classList.toggle("hidden", !real);
    $("receive-test-help").textContent = real
      ? "文件将通过正式上传接口写入所选预设，并创建任务记录、返回公网链接。"
      : "使用与正式上传相同的接收、大小限制和临时文件流程，不连接对象存储，也不创建任务记录。";
    $("receive-test-button").textContent = real ? "上传到 ZOS" : "提交接收测试";
  });
  $("receive-test-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = $("receive-test-file").files[0];
    if (!file) return;
    const button = $("receive-test-button");
    const toggle = $("receive-test-real-upload");
    const result = $("receive-test-result");
    button.disabled = true;
    toggle.disabled = true;
    const real = toggle.checked;
    const preset = $("receive-test-preset").value;
    if (real && !preset) {
      result.value = JSON.stringify({ error: { message: "没有可用的启用预设" } }, null, 2);
      button.disabled = false;
      toggle.disabled = false;
      return;
    }
    result.value = real ? "正在上传到 ZOS…" : "正在接收并校验文件…";
    try {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch(real ? "/v1/uploads" : "/v1/uploads/validate", {
        method: "POST",
        body: form,
        headers: {
          Accept: "application/json",
          ...(real ? { "X-Storage-Preset": preset } : {}),
        },
      });
      const body = await response.json();
      if (body.delete_token) body.delete_token = "[REDACTED]";
      result.value = JSON.stringify(body, null, 2);
    } catch (error) {
      result.value = JSON.stringify({ error: { message: error.message } }, null, 2);
    } finally {
      button.disabled = false;
      toggle.disabled = false;
    }
  });
  refresh();
  setInterval(() => refresh([loadLogs]), 10000);
  setInterval(() => refresh([loadTasks]), 15000);
  setInterval(() => refresh([loadSummary]), 30000);
  setInterval(() => refresh([loadTraffic]), 60000);
})();
