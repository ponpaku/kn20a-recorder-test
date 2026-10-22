const token = window.KN20A_TOKEN;
const $ = (selector) => document.querySelector(selector);
let stateTimer = null;
let guardFormDirty = false;
let guardFormSubmitting = false;
let cameraCapabilities = null;

function formatBytes(value) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let number = Number(value || 0);
  let i = 0;
  while (number >= 1024 && i < units.length - 1) { number /= 1024; i += 1; }
  return `${number.toFixed(i < 2 ? 0 : 1)} ${units[i]}`;
}

function formatDate(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit"
  }).format(new Date(value));
}

function localInput(date) {
  const parts = new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
  }).formatToParts(date).reduce((acc, part) => ({...acc, [part.type]: part.value}), {});
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {"Content-Type": "application/json", "X-KN20A-Token": token, ...(options.headers || {})}
  });
  const value = await response.json().catch(() => ({error: `HTTP ${response.status}`}));
  if (!response.ok || value.ok === false) throw new Error(value.error || `HTTP ${response.status}`);
  return value;
}

function notify(message, kind = "ok") {
  const element = $("#notice");
  element.textContent = message;
  element.className = `notice ${kind}`;
  element.hidden = false;
  window.clearTimeout(element._hideTimer);
  element._hideTimer = window.setTimeout(() => { element.hidden = true; }, 7000);
}

function statusLabel(status) {
  if (status === "reconnecting") return "再接続中";
  if (status === "recovery_required") return "復旧確認が必要";
  return ({pending:"待機", starting:"接続中", recording:"録画中", stopping:"停止処理中", finalizing:"MP4変換中", postprocess_failed:"変換失敗", completed:"完了", missed:"未実行", cancelled:"取消", failed:"失敗"})[status] || status;
}

function renderState(data) {
  cameraCapabilities = data.capabilities;
  const supportsPresets = cameraCapabilities?.presets?.recall !== false;
  const supportsExposure = cameraCapabilities?.exposure?.read !== false;
  $(".preset-card").hidden = !supportsPresets;
  $(".exposure-card").hidden = !supportsExposure;
  $("#schedule-preset").disabled = !supportsPresets;
  $("#preset-policy").disabled = !supportsPresets;
  $("#capability-note").textContent = !supportsExposure && !supportsPresets ? "Generic：録画のみ。画角・露出・夕景AEは非対応です。" : "";
  $("#acknowledge-control").hidden = !data.control?.uncertain;
  $("#input-check-state").textContent = ({not_checked:"未検査",checking:"検査中",ok:"映像入力を確認",unavailable:"入力を確認できません",deferred:"録画受信後に検査",disabled:"検査OFF",cancelled:"録画を優先して検査を中断"})[data.input_check?.state] || "未検査";
  const recorder = data.recorder;
  const stateMap = {
    idle: ["待機中", "idle", "予約時刻になると受信を開始します"],
    waiting_for_camera: ["カメラ待ち", "waiting", "映像入力を待っています"],
    recording: ["録画中", "recording", `${formatBytes(recorder.bytes_written)} 保存済み`],
    finalizing: ["MP4変換中", "waiting", "映像はコピーのままMP4へ仕上げています"]
  };
  const [label, cls, detail] = stateMap[recorder.state] || [recorder.state, "waiting", "—"];
  $("#service-pill").className = `pill ${cls}`;
  $("#service-pill").innerHTML = `<span></span>${label}`;
  $("#metric-state").textContent = label;
  $("#metric-detail").textContent = detail;
  $("#stop-recording").hidden = !["recording", "waiting_for_camera"].includes(recorder.state);
  $("#metric-next").textContent = data.next_schedule ? formatDate(data.next_schedule.start) : "予約なし";
  $("#metric-next-title").textContent = data.next_schedule?.title || "—";
  $("#metric-disk").textContent = formatBytes(data.disk.free);
  $("#metric-output").textContent = data.config.output_dir;
  $("#clock").textContent = `現在 ${formatDate(data.now)} JST`;
  if (data.exposure_guard && supportsExposure) renderGuard(data.exposure_guard);
  renderPresets(data.presets || []);

  const rows = $("#schedule-rows");
  if (!data.schedules.length) {
    rows.innerHTML = '<tr><td colspan="7" class="empty">予約はまだありません</td></tr>';
    return;
  }
  const presetNames = new Map((data.presets || []).map(item => [item.number, item.name]));
  rows.replaceChildren(...data.schedules.slice().reverse().map(item => {
    const row = document.createElement("tr");
    const activeRecording = item.id === recorder.schedule_id && ["starting", "recording"].includes(item.status);
    const cancellable = ["pending", "starting", "recording", "reconnecting"].includes(item.status);
    row.innerHTML = `
      <td><span class="status status-${item.status}">${statusLabel(item.status)}</span></td>
      <td><strong></strong><small></small></td>
      <td class="preset-cell"></td>
      <td>${formatDate(item.start)}</td><td>${formatDate(item.end)}</td>
      <td class="output-cell"></td>
      <td>${cancellable ? `<button type="button" class="${activeRecording ? "stop-row" : "cancel"} ghost">${activeRecording ? "録画停止" : "取消"}</button>` : ''}</td>`;
    row.querySelector("td strong").textContent = item.title;
    row.querySelector("td small").textContent = [item.last_error, item.control_warning, item.media_warning, item.gaps?.length ? "受信欠落あり" : "", item.finalization === "verified" ? "MP4検証済み" : ""].filter(Boolean).join(" / ");
    if (["postprocess_failed", "recovery_required"].includes(item.status)) {
      const retry = document.createElement("button"); retry.textContent = "後処理を再試行";
      retry.addEventListener("click", async () => { try { await api(`/api/schedules/${item.id}/finalize`, {method:"POST", body:"{}"}); await refreshState(); } catch(error) { notify(error.message, "error"); } });
      row.lastElementChild.append(retry);
    }
    row.querySelector(".preset-cell").textContent = item.preset_number == null ? "指定なし" : `#${item.preset_number} ${presetNames.get(item.preset_number) || ""}`;
    row.querySelector(".output-cell").textContent = item.output_dir || "—";
    row.querySelector(".cancel")?.addEventListener("click", () => cancelSchedule(item));
    row.querySelector(".stop-row")?.addEventListener("click", stopRecording);
    return row;
  }));
}

async function stopRecording() {
  if (!window.confirm("現在の録画をここで終了しますか？MKVを正常に閉じた後、MP4へ変換します。")) return;
  const button = $("#stop-recording");
  button.disabled = true;
  try {
    await api("/api/recording/stop", {method:"POST", body:"{}"});
    notify("録画を停止しました。MP4への仕上げを開始します");
    await refreshState(true);
  } catch (error) { notify(`録画停止: ${error.message}`, "error"); }
  finally { button.disabled = false; }
}

$("#stop-recording").addEventListener("click", stopRecording);

async function refreshState(showError = false) {
  try { renderState(await api("/api/state")); }
  catch (error) { if (showError) notify(error.message, "error"); }
}

async function cancelSchedule(item) {
  if (!window.confirm(`「${item.title}」を取り消しますか？録画中ならファイルを閉じて停止します。`)) return;
  try {
    await api(`/api/schedules/${item.id}/cancel`, {method:"POST", body:"{}"});
    notify("予約を取り消しました");
    await refreshState(true);
  } catch (error) { notify(error.message, "error"); }
}

function setDefaultTimes() {
  const start = new Date(Date.now() + 5 * 60 * 1000);
  start.setSeconds(0, 0);
  const end = new Date(start.getTime() + 60 * 60 * 1000);
  $("#start").value = localInput(start);
  $("#end").value = localInput(end);
}

$("#schedule-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    await api("/api/schedules", {method:"POST", body:JSON.stringify({
      title: $("#title").value, preset_number: $("#schedule-preset").value,
      preset_failure_policy: $("#preset-policy").value || undefined,
      start: $("#start").value, end: $("#end").value
    })});
    notify("録画予約を追加しました");
    $("#title").value = "";
    setDefaultTimes();
    await refreshState(true);
  } catch (error) { notify(error.message, "error"); }
  finally { button.disabled = false; }
});

$("#quick-test").addEventListener("click", async () => {
  if (!window.confirm("今から30秒間、Main Streamのテスト録画を開始しますか？")) return;
  try {
    await api("/api/quick-test", {method:"POST", body:JSON.stringify({
      duration_seconds:30, preset_number:$("#schedule-preset").value
    })});
    notify("30秒テストを予約しました。約2秒後に接続を開始します");
    await refreshState(true);
  } catch (error) { notify(error.message, "error"); }
});

$("#refresh").addEventListener("click", () => refreshState(true));

function renderPresets(presets) {
  const select = $("#schedule-preset");
  const selected = select.value;
  if (cameraCapabilities?.presets?.recall === false) {
    select.replaceChildren(new Option("指定なし（制御非対応）", ""));
    return;
  }
  const names = new Map(presets.map(item => [Number(item.number), item.name]));
  const commonPresets = Array.from({length: 11}, (_, number) =>
    new Option(`#${number}${names.has(number) ? ` ${names.get(number)}` : ""}`, number));
  select.replaceChildren(new Option("指定なし", ""), ...commonPresets);
  if ([...select.options].some(option => option.value === selected)) select.value = selected;

  const list = $("#preset-list");
  if (!presets.length) {
    list.innerHTML = '<p class="empty">プリセットはまだありません</p>';
    return;
  }
  list.replaceChildren(...presets.map(item => {
    const row = document.createElement("div");
    row.className = "preset-row";
    row.innerHTML = '<div><strong></strong><small></small></div><div class="preset-actions"><button type="button" class="recall secondary">呼び出す</button><button type="button" class="remove ghost">一覧から削除</button></div>';
    row.querySelector("strong").textContent = `#${item.number} ${item.name}`;
    row.querySelector("small").textContent = item.saved_at ? `保存 ${formatDate(item.saved_at)}` : "";
    row.querySelector(".recall").addEventListener("click", () => recallPreset(item));
    row.querySelector(".remove").addEventListener("click", () => removePreset(item));
    return row;
  }));
}

$("#preset-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const number = Number($("#preset-number").value);
  const name = $("#preset-name").value.trim();
  if (!window.confirm(`現在のパン・チルト・ズーム位置をプリセット #${number}「${name}」へ保存します。既存位置があれば上書きします。よろしいですか？`)) return;
  const button = $("#save-preset");
  button.disabled = true;
  try {
    await api("/api/presets", {method:"POST", body:JSON.stringify({number, name})});
    notify(`プリセット #${number} をカメラへ保存しました`);
    await refreshState(true);
  } catch (error) { notify(`プリセット保存: ${error.message}`, "error"); }
  finally { button.disabled = false; }
});

async function recallPreset(item) {
  if (!window.confirm(`カメラをプリセット #${item.number}「${item.name}」へ移動しますか？`)) return;
  try {
    await api(`/api/presets/${item.number}/recall`, {method:"POST", body:"{}"});
    notify(`プリセット #${item.number} を呼び出しました`);
  } catch (error) { notify(`プリセット呼出: ${error.message}`, "error"); }
}

async function removePreset(item) {
  if (!window.confirm(`#${item.number}「${item.name}」を録画コンソールの一覧から削除しますか？カメラ本体の位置記憶は残ります。`)) return;
  try {
    await api(`/api/presets/${item.number}/remove`, {method:"POST", body:"{}"});
    notify("プリセットを一覧から削除しました");
    await refreshState(true);
  } catch (error) { notify(`プリセット削除: ${error.message}`, "error"); }
}

function renderExposure(exposure) {
  const mode = $("#mode");
  const iris = $("#iris");
  const shutter = $("#shutter");
  mode.replaceChildren(...exposure.mode.options.map(option => new Option(option.label, option.value, false, option.value === exposure.mode.value)));
  iris.replaceChildren(...exposure.iris.options.map(option => new Option(option.label, option.value, false, option.value === exposure.iris.value)));
  const currentBank60Hz = exposure.shutter.value >= 17;
  const shutterOptions = exposure.shutter.options.filter(option => (option.value >= 17) === currentBank60Hz);
  shutter.replaceChildren(...shutterOptions.map(option => new Option(option.label, option.value, false, option.value === exposure.shutter.value)));
  $("#gain-limit").value = exposure.gain_limit;
  for (const element of [mode, $("#gain-limit"), $("#apply-exposure")]) element.disabled = false;
  syncIrisAvailability();
  const irisLabel = exposure.iris.actual ? "実絞り" : "絞り設定記憶";
  $("#exposure-current").innerHTML = `
    <div><dt>モード</dt><dd>${exposure.mode.label}</dd></div>
    <div><dt>${irisLabel}</dt><dd>${exposure.iris.label}</dd></div>
    <div><dt>シャッター</dt><dd>${exposure.shutter.label}</dd></div>
    <div><dt>Gain / 上限</dt><dd>${exposure.gain} / ${exposure.gain_limit}</dd></div>
    <div><dt>DRC</dt><dd>${exposure.drc}</dd></div>`;
}

function syncIrisAvailability() {
  const fixedIrisMode = [3, 11].includes(Number($("#mode").value));
  const fixedShutterMode = [3, 10].includes(Number($("#mode").value));
  $("#iris").disabled = !fixedIrisMode;
  $("#iris").title = fixedIrisMode ? "" : "固定絞りはManualまたはAAEでだけ反映されます";
  $("#shutter").disabled = !fixedShutterMode;
  $("#shutter").title = fixedShutterMode ? "" : "固定SSはManualまたはSAEでだけ反映されます";
}

async function loadExposure(showNotice = false) {
  const button = $("#load-exposure");
  button.disabled = true;
  try {
    const result = await api("/api/exposure");
    renderExposure(result.exposure);
    if (showNotice) notify("カメラから露出設定を取得しました");
  } catch (error) { notify(`露出取得: ${error.message}`, "error"); }
  finally { button.disabled = false; }
}

$("#load-exposure").addEventListener("click", () => loadExposure(true));
$("#mode").addEventListener("change", syncIrisAvailability);

$("#exposure-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const modeText = $("#mode").selectedOptions[0].textContent;
  const irisText = $("#iris").selectedOptions[0].textContent;
  const shutterText = $("#shutter").selectedOptions[0].textContent;
  if (!window.confirm(`カメラの露出を「${modeText} / 絞り ${irisText} / SS ${shutterText}」へ変更します。反映しますか？`)) return;
  const button = $("#apply-exposure");
  button.disabled = true;
  try {
    const result = await api("/api/exposure", {method:"POST", body:JSON.stringify({
      mode:Number($("#mode").value), iris:Number($("#iris").value), shutter:Number($("#shutter").value), gain_limit:Number($("#gain-limit").value)
    })});
    renderExposure(result.exposure);
    notify("露出設定を反映し、実機から再取得して確認しました");
  } catch (error) { notify(`露出変更: ${error.message}`, "error"); button.disabled = false; }
});

function renderGuard(guard, forceFormSync = false) {
  const unavailable = guard.available === false;
  const form = $("#guard-form");
  const editing = !forceFormSync && (guardFormDirty || guardFormSubmitting || form.contains(document.activeElement));
  if (!editing) {
    $("#guard-enabled").checked = guard.enabled;
    const irisLimit = $("#guard-iris-limit");
    const selectedIris = String(guard.manual_iris);
    irisLimit.replaceChildren(...(guard.iris_options || []).map(option => new Option(option.label, option.value, false, String(option.value) === selectedIris)));
    $("#guard-night-policy").value = guard.night_policy || "natural";
    $("#guard-exposure-compensation").value = Number(guard.exposure_compensation_ev ?? 0).toFixed(1);
    $("#guard-slowest-shutter").value = String(guard.slowest_denominator ?? 60);
    $("#guard-quality-gain").value = Number(guard.quality_gain_limit ?? 3);
  }
  $("#guard-iris-limit").disabled = unavailable || guardFormSubmitting;
  $("#guard-night-policy").disabled = unavailable || guardFormSubmitting;
  $("#guard-exposure-compensation").disabled = unavailable || guardFormSubmitting;
  $("#guard-slowest-shutter").disabled = unavailable || guardFormSubmitting;
  $("#guard-quality-gain").disabled = unavailable || guardFormSubmitting;
  $("#guard-enabled").disabled = unavailable || guardFormSubmitting;
  $("#save-guard").disabled = unavailable || guardFormSubmitting;
  const metrics = guard.metrics;
  $("#calibrate-guard").disabled = unavailable || !metrics || metrics.valid === false;
  const status = unavailable ? "安全停止中" : (guard.enabled ? (guard.last_error ? "解析・設定保留" : "動作中") : guard.phase_label);
  const detail = guard.unavailable_reason || guard.last_error || guard.last_action || "操作履歴なし";
  const container = $("#guard-current");
  const metricText = metrics
    ? `測光平均 ${Number(metrics.metered_yavg ?? metrics.y_avg).toFixed(1)} / P90 ${Number(metrics.metered_yp90 ?? metrics.y_p90).toFixed(0)} / 白飛び ${(Number(metrics.metered_white_fraction ?? metrics.white_fraction) * 100).toFixed(2)}%`
    : "解析フレーム待ち";
  const effectiveAverage = Number(guard.effective_target_yavg ?? guard.target_yavg);
  const effectiveHigh = Number(guard.effective_target_yhigh ?? guard.target_yhigh);
  const targetScale = Number(guard.target_scale ?? 1);
  const compensationEv = Number(guard.exposure_compensation_ev ?? 0);
  const compensationText = `${compensationEv > 0 ? "+" : ""}${compensationEv.toFixed(1)} EV`;
  const targetText = targetScale < 0.995
    ? `現在 平均 ${effectiveAverage.toFixed(1)} / P90 ${effectiveHigh.toFixed(0)}（${compensationText}、夜間${Math.round(targetScale * 100)}%）`
    : `現在 平均 ${effectiveAverage.toFixed(1)} / P90 ${effectiveHigh.toFixed(0)}（${compensationText}）`;
  const policyLabels = {natural:"自然・低ノイズ", balanced:"バランス", visibility:"視認性優先"};
  container.innerHTML = "<div class='guard-status'><dt>状態</dt><dd></dd></div><div class='guard-phase'><dt>制御モード</dt><dd></dd></div><div class='guard-metrics'><dt>現在の解析値</dt><dd></dd></div><div class='guard-target'><dt>目標値</dt><dd></dd></div><div class='guard-last'><dt>最後の調整</dt><dd></dd></div>";
  const values = container.querySelectorAll("dd");
  values[0].textContent = status;
  values[1].textContent = `${guard.phase_label} / 最遅1/${guard.slowest_denominator} / ${compensationText} / ${policyLabels[guard.night_policy] || guard.night_policy} / Gain≤${guard.quality_gain_limit}`;
  values[2].textContent = metricText;
  values[3].textContent = targetText;
  values[4].textContent = detail;
}

$("#guard-form").addEventListener("input", () => { guardFormDirty = true; });
$("#guard-form").addEventListener("change", () => { guardFormDirty = true; });

$("#guard-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const enabled = $("#guard-enabled").checked;
  const irisText = $("#guard-iris-limit").selectedOptions[0]?.textContent || "F5.6";
  if (enabled && !window.confirm(`カメラをManualへ切り替え、回折上限を${irisText}としてSS・絞り・Gainの画像AEを開始します。実行しますか？`)) return;
  const button = $("#save-guard");
  button.disabled = true;
  guardFormSubmitting = true;
  try {
    const result = await api("/api/exposure-guard", {method:"POST", body:JSON.stringify({
      enabled,
      manual_iris:Number($("#guard-iris-limit").value),
      night_policy:$("#guard-night-policy").value,
      exposure_compensation_ev:Number($("#guard-exposure-compensation").value),
      slowest_denominator:Number($("#guard-slowest-shutter").value),
      quality_gain_limit:Number($("#guard-quality-gain").value)
    })});
    guardFormDirty = false;
    guardFormSubmitting = false;
    renderGuard(result.guard, true);
    notify(enabled ? "夕景AEを有効にしました" : "夕景AEを停止しました");
  } catch (error) { notify(`夕景AE: ${error.message}`, "error"); }
  finally {
    guardFormSubmitting = false;
    button.disabled = false;
  }
});

$("#calibrate-guard").addEventListener("click", async () => {
  if (!window.confirm("現在の映像輝度を夕景AEの基準として保存しますか？露出設定自体は変更しません。")) return;
  const button = $("#calibrate-guard");
  button.disabled = true;
  try {
    const result = await api("/api/exposure-guard/calibrate", {method:"POST", body:"{}"});
    renderGuard(result.guard);
    notify("現在映像を輝度基準に保存しました");
  } catch (error) { notify(`輝度基準: ${error.message}`, "error"); }
  finally { button.disabled = false; }
});

setDefaultTimes();
refreshState(true);
$("#acknowledge-control").addEventListener("click", async () => {
  if (!window.confirm("カメラの移動が停止し、画角を確認できましたか？未確認のまま解除しないでください。")) return;
  try { await api("/api/control/acknowledge", {method:"POST", body:JSON.stringify({confirmed:true})}); await refreshState(); }
  catch(error) { notify(error.message, "error"); }
});
$("#input-check").addEventListener("click", async () => {
  try { await api("/api/input-check", {method:"POST", body:"{}"}); await refreshState(); }
  catch(error) { notify(error.message, "error"); }
});
stateTimer = window.setInterval(() => refreshState(false), 2000);
