(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    items: [],
    providers: [],
    selected: null,
    detail: null,
    creating: false,
  };

  function show(message, kind = "ok") {
    const box = $("message");
    box.textContent = message;
    box.className = `notice ${kind}`;
  }

  function clearSecrets() {
    $("access-key").value = "";
    $("secret-key").value = "";
  }

  function providerById(providerId = $("provider").value) {
    return state.providers.find((provider) => provider.id === providerId);
  }

  function providerName(providerId) {
    return providerById(providerId)?.display_name || providerId || "未配置";
  }

  function renderProviderInfo() {
    const provider = providerById();
    if (!provider) {
      $("provider-name").textContent = "存储服务能力";
      $("provider-description").textContent = "选择 Provider 后显示兼容范围。";
      $("provider-capability").textContent = "等待选择";
      return;
    }
    const capabilities = provider.capabilities || {};
    $("provider-name").textContent = provider.display_name;
    $("provider-description").textContent = provider.description || "使用该 Provider 连接对象存储服务。";
    $("provider-capability").textContent = [
      capabilities.s3_compatible ? "S3 兼容" : null,
      capabilities.public_read ? "公网对象" : null,
      capabilities.bucket_metrics ? "扩展指标" : null,
    ].filter(Boolean).join(" · ") || "专用协议";
    const metrics = Boolean(capabilities.bucket_metrics);
    $("enable-metrics").disabled = !metrics;
    if (!metrics) $("enable-metrics").checked = false;
    $("metrics-note").textContent = metrics
      ? "该 Provider 支持厂商扩展 Bucket 指标；关闭不影响上传和本地统计。"
      : "该 Provider 只使用标准 S3 能力，不提供厂商扩展 Bucket 指标。";
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        Accept: "application/json",
        ...(options.body ? {
          "Content-Type": "application/json",
          "X-Settings-Request": "true",
        } : {}),
      },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(body.error?.message || `${response.status} ${response.statusText}`);
    }
    return body;
  }

  function renderList() {
    const list = $("preset-list");
    list.replaceChildren();
    if (!state.items.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "尚未创建预设";
      list.append(empty);
      return;
    }
    state.items.forEach((preset) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `preset-item ${preset.preset_key === state.selected ? "active" : ""}`;
      button.addEventListener("click", () => selectPreset(preset.preset_key));
      const title = document.createElement("strong");
      title.textContent = preset.display_name;
      const key = document.createElement("code");
      key.textContent = preset.preset_key;
      const meta = document.createElement("span");
      meta.textContent = [
        preset.is_default ? "默认" : null,
        preset.enabled ? "启用" : "禁用",
        `配置 r${preset.config_revision ?? 0}`,
        `状态 r${preset.state_revision}`,
      ].filter(Boolean).join(" · ");
      const service = document.createElement("span");
      service.className = "preset-service";
      service.textContent = providerName(preset.provider);
      const destination = document.createElement("span");
      destination.textContent = `${preset.endpoint_host || "未配置 Endpoint"} / ${preset.bucket || "未配置 Bucket"}`;
      button.append(title, key, service, destination, meta);
      list.append(button);
    });
  }

  function resetForm() {
    $("storage-form").reset();
    $("preset-key").disabled = false;
    $("revision").textContent = "0";
    $("state-revision").textContent = "0";
    $("configured-state").textContent = "新预设";
    $("preset-state").textContent = "尚未保存";
    $("credential-state").textContent = "未配置";
    $("credential-state").className = "badge neutral";
    $("access-key-masked").textContent = "";
    $("last-test").textContent = "尚无测试记录";
    $("save-button").textContent = "创建预设";
    $("preset-state-panel").classList.add("hidden");
    renderProviderInfo();
    clearSecrets();
  }

  function syncStateButtons() {
    if (!state.detail || state.creating) return;
    $("toggle-preset-button").disabled = state.detail.is_default && state.detail.enabled;
    $("default-preset-button").disabled = state.detail.is_default || !state.detail.enabled;
  }

  function newPreset() {
    state.creating = true;
    state.selected = null;
    state.detail = null;
    renderList();
    resetForm();
    $("preset-key").focus();
    show("填写候选配置并先测试连接；创建时必须提供完整 AK/SK。", "warning");
  }

  function renderDetail(data) {
    state.creating = false;
    state.detail = data;
    $("preset-key").value = data.preset_key;
    $("preset-key").disabled = true;
    $("display-name").value = data.display_name;
    $("provider").value = data.provider;
    renderProviderInfo();
    $("revision").textContent = String(data.revision);
    $("state-revision").textContent = String(data.state_revision);
    $("configured-state").textContent = `已于 ${new Date(data.activated_at).toLocaleString()} 激活`;
    $("preset-state").textContent = [
      data.is_default ? "默认预设" : null,
      data.enabled ? "已启用" : "已禁用",
    ].filter(Boolean).join(" · ");
    const credentials = data.credentials;
    $("credential-state").textContent = credentials.secret_key_configured ? "已配置" : "未配置";
    $("credential-state").className = `badge ${credentials.secret_key_configured ? "ok" : "neutral"}`;
    $("access-key-masked").textContent = credentials.access_key_masked || "";
    $("last-test").textContent = data.last_connection_test
      ? `${data.last_connection_test.status.toUpperCase()} · ${new Date(data.last_connection_test.tested_at).toLocaleString()} · ${data.last_connection_test.latency_ms} ms`
      : "尚无测试记录";
    const config = data.config;
    $("endpoint-url").value = config.endpoint_url;
    $("bucket").value = config.bucket;
    $("public-base-url").value = config.public_base_url;
    $("connect-timeout").value = config.connect_timeout_seconds;
    $("read-timeout").value = config.read_timeout_seconds;
    $("max-attempts").value = config.max_attempts;
    $("verify-tls").checked = config.verify_tls;
    $("enable-metrics").checked = config.enable_bucket_metrics;
    $("save-button").textContent = "保存新配置 revision";
    $("preset-state-panel").classList.remove("hidden");
    $("toggle-preset-button").textContent = data.enabled ? "禁用预设" : "启用预设";
    syncStateButtons();
    clearSecrets();
  }

  async function selectPreset(presetKey) {
    state.selected = presetKey;
    renderList();
    try {
      const detail = await api(`/v1/settings/storage/presets/${encodeURIComponent(presetKey)}`);
      if (state.selected !== presetKey) return;
      renderDetail(detail);
    } catch (error) {
      show(error.message, "error");
    }
  }

  async function loadPresets(preferred) {
    const data = await api("/v1/settings/storage/presets");
    state.items = data.items;
    const target = preferred
      || (state.selected && state.items.some((item) => item.preset_key === state.selected) ? state.selected : null)
      || state.items.find((item) => item.is_default)?.preset_key
      || state.items[0]?.preset_key;
    if (target) await selectPreset(target);
    else newPreset();
  }

  async function loadProviders() {
    const data = await api("/v1/settings/storage/providers");
    state.providers = data.items;
    const select = $("provider");
    select.replaceChildren();
    state.providers.forEach((provider) => {
      const option = document.createElement("option");
      option.value = provider.id;
      option.textContent = provider.display_name;
      select.append(option);
    });
    if (!state.providers.length) {
      throw new Error("服务端没有可用的 Storage Provider");
    }
    renderProviderInfo();
  }

  function storagePayload() {
    const credentials = {};
    if ($("access-key").value) credentials.access_key = $("access-key").value;
    if ($("secret-key").value) credentials.secret_key = $("secret-key").value;
    const provider = providerById();
    const body = {
      provider: provider.id,
      provider_schema_version: provider.schema_version,
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
    return body;
  }

  async function run(button, action, clear = false) {
    button.disabled = true;
    try {
      await action();
    } catch (error) {
      show(error.message, "error");
    } finally {
      if (clear) clearSecrets();
      button.disabled = false;
      syncStateButtons();
    }
  }

  $("test-button").addEventListener("click", () => {
    if (!$("storage-form").reportValidity()) return;
    run($("test-button"), async () => {
      const body = storagePayload();
      if (!state.creating) body.preset_key = state.selected;
      const result = await api("/v1/settings/storage/test", {
        method: "POST",
        body: JSON.stringify(body),
      });
      show(`连接测试成功，耗时 ${result.latency_ms} ms；此操作未保存配置。`);
    }, true);
  });

  $("storage-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!$("storage-form").reportValidity()) return;
    const key = $("preset-key").value.trim();
    if (state.creating && (!$("access-key").value || !$("secret-key").value)) {
      show("创建预设必须提供完整 Access Key 和 Secret Key。", "error");
      return;
    }
    const target = `${$("endpoint-url").value.trim()} / ${$("bucket").value.trim()}`;
    if (!confirm(`确认保存 ${key} 的 ${target}？`)) return;
    run($("save-button"), async () => {
      const body = storagePayload();
      let result;
      if (state.creating) {
        body.preset_key = key;
        body.display_name = $("display-name").value.trim();
        result = await api("/v1/settings/storage/presets", {
          method: "POST",
          body: JSON.stringify(body),
        });
      } else {
        body.expected_revision = state.detail.revision;
        result = await api(`/v1/settings/storage/presets/${encodeURIComponent(key)}`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
      }
      show(`${result.display_name} 的配置 revision ${result.revision} 已激活。`);
      await loadPresets(result.preset_key);
    }, true);
  });

  $("save-name-button").addEventListener("click", () => {
    if (!$("display-name").reportValidity()) return;
    run($("save-name-button"), async () => {
      const result = await api(`/v1/settings/storage/presets/${encodeURIComponent(state.selected)}`, {
        method: "PATCH",
        body: JSON.stringify({
          expected_state_revision: state.detail.state_revision,
          display_name: $("display-name").value.trim(),
        }),
      });
      show(`显示名称已更新，状态 revision ${result.state_revision}。`);
      await loadPresets(state.selected);
    });
  });

  $("toggle-preset-button").addEventListener("click", () => run(
    $("toggle-preset-button"),
    async () => {
      const enabled = !state.detail.enabled;
      if (!confirm(`确认${enabled ? "启用" : "禁用"}预设 ${state.selected}？`)) return;
      const result = await api(`/v1/settings/storage/presets/${encodeURIComponent(state.selected)}`, {
        method: "PATCH",
        body: JSON.stringify({
          expected_state_revision: state.detail.state_revision,
          enabled,
        }),
      });
      show(`预设已${result.enabled ? "启用" : "禁用"}。`);
      await loadPresets(state.selected);
    },
  ));

  $("default-preset-button").addEventListener("click", () => run(
    $("default-preset-button"),
    async () => {
      const current = state.items.find((item) => item.is_default);
      if (!current || !confirm(`确认把 ${state.selected} 设为默认预设？`)) return;
      const result = await api("/v1/settings/storage/default", {
        method: "PUT",
        body: JSON.stringify({
          preset_key: state.selected,
          expected_default_preset: current.preset_key,
          expected_state_revision: state.detail.state_revision,
        }),
      });
      show(`${result.display_name} 已成为默认预设。`);
      await loadPresets(state.selected);
    },
  ));

  function suggestPublicUrl() {
    if ($("public-base-url").value || !$("bucket").value || !$("endpoint-url").value) return;
    try {
      const endpoint = new URL($("endpoint-url").value);
      $("url-suggestion").textContent = `建议值：${endpoint.protocol}//${$("bucket").value}.${endpoint.host}`;
    } catch (_) {
      $("url-suggestion").textContent = "上传成功后用它拼接对象 Key。";
    }
  }

  $("new-preset-button").addEventListener("click", newPreset);
  $("provider").addEventListener("change", () => {
    renderProviderInfo();
    clearSecrets();
    if (!state.creating && state.detail?.provider !== $("provider").value) {
      show("切换存储服务类型需要重新提供完整 AK/SK，并会创建新的配置 revision。", "warning");
    }
  });
  $("endpoint-url").addEventListener("input", suggestPublicUrl);
  $("bucket").addEventListener("input", suggestPublicUrl);
  (async () => {
    await loadProviders();
    await loadPresets();
  })().catch((error) => show(error.message, "error"));
})();
