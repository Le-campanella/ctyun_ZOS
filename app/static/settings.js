(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  let revision = 0;
  let configured = false;

  function show(message, kind = "ok") {
    const box = $("message");
    box.textContent = message;
    box.className = `notice ${kind}`;
  }

  function clearSecrets() {
    $("access-key").value = "";
    $("secret-key").value = "";
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        Accept: "application/json",
        ...(options.body ? { "Content-Type": "application/json", "X-Settings-Request": "true" } : {}),
      },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error?.message || `${response.status} ${response.statusText}`);
    return body;
  }

  function renderCurrent(data) {
    revision = data.revision;
    configured = data.configured;
    $("revision").textContent = String(revision);
    $("configured-state").textContent = configured ? `已于 ${new Date(data.activated_at).toLocaleString()} 激活` : "尚未配置";
    const credentials = data.credentials;
    $("credential-state").textContent = credentials.secret_key_configured ? "已配置" : "未配置";
    $("credential-state").className = `badge ${credentials.secret_key_configured ? "ok" : "neutral"}`;
    $("access-key-masked").textContent = credentials.access_key_masked || "";
    if (data.last_connection_test) {
      $("last-test").textContent = `${data.last_connection_test.status.toUpperCase()} · ${new Date(data.last_connection_test.tested_at).toLocaleString()} · ${data.last_connection_test.latency_ms} ms`;
    } else {
      $("last-test").textContent = "尚无测试记录";
    }
    if (!data.config) return;
    $("endpoint-url").value = data.config.endpoint_url;
    $("bucket").value = data.config.bucket;
    $("public-base-url").value = data.config.public_base_url;
    $("connect-timeout").value = data.config.connect_timeout_seconds;
    $("read-timeout").value = data.config.read_timeout_seconds;
    $("max-attempts").value = data.config.max_attempts;
    $("verify-tls").checked = data.config.verify_tls;
    $("enable-metrics").checked = data.config.enable_bucket_metrics;
  }

  async function load() {
    try {
      renderCurrent(await api("/v1/settings/storage"));
    } catch (error) {
      show(error.message, "error");
    }
  }

  function payload(includeRevision) {
    const credentials = {};
    if ($("access-key").value) credentials.access_key = $("access-key").value;
    if ($("secret-key").value) credentials.secret_key = $("secret-key").value;
    const body = {
      provider: "ctyun_zos",
      provider_schema_version: 1,
      config: {
        endpoint_url: $("endpoint-url").value.trim(),
        bucket: $("bucket").value.trim(),
        public_base_url: $("public-base-url").value.trim(),
        connect_timeout_seconds: Number($("connect-timeout").value),
        read_timeout_seconds: Number($("read-timeout").value),
        max_attempts: Number($("max-attempts").value),
        verify_tls: $("verify-tls").checked,
        enable_bucket_metrics: $("enable-metrics").checked,
      },
    };
    if (Object.keys(credentials).length) body.credentials = credentials;
    if (includeRevision) body.expected_revision = revision;
    return body;
  }

  async function submit(path, method, button, success) {
    if (!$("storage-form").reportValidity()) return;
    button.disabled = true;
    try {
      const result = await api(path, { method, body: JSON.stringify(payload(method === "PUT")) });
      show(success(result), "ok");
      if (method === "PUT") renderCurrent(result);
    } catch (error) {
      show(error.message, "error");
    } finally {
      clearSecrets();
      button.disabled = false;
    }
  }

  $("test-button").addEventListener("click", () => submit(
    "/v1/settings/storage/test",
    "POST",
    $("test-button"),
    (result) => `连接测试成功，耗时 ${result.latency_ms} ms；此操作未保存配置。`,
  ));

  $("storage-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const target = `${$("endpoint-url").value.trim()} / ${$("bucket").value.trim()}`;
    if (!confirm(`确认保存并激活 ${target} 的 revision ${revision + 1}？`)) return;
    submit(
      "/v1/settings/storage",
      "PUT",
      $("save-button"),
      (result) => `revision ${result.revision} 已保存并激活。`,
    );
  });

  function suggestPublicUrl() {
    if ($("public-base-url").value || !$("bucket").value || !$("endpoint-url").value) return;
    try {
      const endpoint = new URL($("endpoint-url").value);
      const suggestion = `${endpoint.protocol}//${$("bucket").value}.${endpoint.host}`;
      $("url-suggestion").textContent = `建议值：${suggestion}`;
    } catch (_) {
      $("url-suggestion").textContent = "上传成功后用它拼接对象 Key。";
    }
  }
  $("endpoint-url").addEventListener("input", suggestPublicUrl);
  $("bucket").addEventListener("input", suggestPublicUrl);
  load();
})();
