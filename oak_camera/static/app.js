"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {status: null, selected: new Set(), configs: new Map(), cards: new Map(), drafts: new Map(), formBaseline: {}, active: "", dirty: false, busy: false, polling: false, captureKey: "", streamEpoch: 0, seenSockets: "", noticeTimer: null, controlTab: "exposure", selectedView: false, initialSetupOpened: false};
  const resolutions = {"1080p": "1920 × 1080", "4k": "3840 × 2160", "12mp": "12 MP sensor mode"};
  const resolutionOrder = ["1080p", "4k", "12mp"];
  const resolutionWarning = "CAM_A and CAM_D need matching sensor resolutions with this DepthAI version. Choose the same mode before starting.";
  const defaults = {exposure_mode: "auto", exposure_us: 10000, iso: 400, exposure_lock: false, auto_exposure_limit_us: 0, white_balance_mode: "auto", white_balance_kelvin: 4500, white_balance_lock: false, focus_mode: "continuous", focus: 128, exposure_compensation: 0, brightness: 0, contrast: 0, saturation: 0, sharpness: 1, luma_denoise: 1, chroma_denoise: 1, anti_banding: "auto", effect_mode: "off"};
  const numericControls = new Set(["exposure_us", "iso", "white_balance_kelvin", "focus", "exposure_compensation", "brightness", "contrast", "saturation", "sharpness", "luma_denoise", "chroma_denoise", "auto_exposure_limit_us"]);

  function node(tag, className, text) { const el = document.createElement(tag); if (className) el.className = className; if (text !== undefined) el.textContent = text; return el; }
  function showError(error) { $("error-text").textContent = error instanceof Error ? error.message : String(error); $("error-banner").hidden = false; }
  function notice(message) { clearTimeout(state.noticeTimer); $("notice").textContent = message; $("notice").hidden = false; state.noticeTimer = setTimeout(() => { $("notice").hidden = true; }, 6500); }
  function socketLabel(camera) { return (camera?.socket === "CAM_AA" ? "CAM_A" : camera?.socket) || camera?.label || "Camera"; }
  function shortLabel(camera) { return camera?.socket ? socketLabel(camera) : "—"; }
  function cameraOrder(camera) { const order = {CAM_A: 0, CAM_AA: 0, CAM_D: 1, CAM_B: 2, CAM_C: 3}; return order[camera.socket] ?? 9; }
  function cameras() { return (state.status?.cameras || []).slice().sort((a, b) => cameraOrder(a) - cameraOrder(b)); }
  function activeCameras() {
    if (!state.status?.running) return [];
    return cameras().filter(camera => typeof camera.active === "boolean" ? camera.active : Array.isArray(state.status.active_sockets) ? state.status.active_sockets.includes(camera.socket) : state.selected.has(camera.socket));
  }
  function selectedCamera() { return cameras().find(camera => camera.socket === state.active); }
  function rawAvailable() { return Boolean(state.status?.running && state.status?.raw_enabled); }
  function setConnection(label, kind) { $("connection-text").textContent = label; $("connection-pill").className = `connection-pill ${kind || ""}`; }

  async function api(path, body) {
    let response;
    try { response = await fetch(path, body === undefined ? {cache: "no-store"} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)}); }
    catch (_) { throw new Error("Cannot reach OAK FFC TEST. Check that the application is running and the host computer is reachable."); }
    let data;
    try { data = await response.json(); }
    catch (_) { throw new Error(`The server returned an unreadable response (HTTP ${response.status}).`); }
    if (!response.ok) throw new Error(data.error || `The request failed (HTTP ${response.status}).`);
    return data;
  }

  async function action(work) {
    if (state.busy) return;
    $("error-banner").hidden = true;
    state.busy = true; document.body.classList.add("busy"); updateEnabled();
    try { await work(); }
    catch (error) { showError(error); }
    finally { state.busy = false; document.body.classList.remove("busy"); updateEnabled(); }
  }

  function configFor(camera) {
    if (!state.configs.has(camera.socket)) state.configs.set(camera.socket, {resolution: camera.resolution || "1080p", fps: camera.requested_fps || 10});
    return state.configs.get(camera.socket);
  }

  function selectedConfigurations() {
    return cameras().filter(camera => state.selected.has(camera.socket)).map(camera => configFor(camera));
  }

  function hasResolutionConflict() {
    const socketA = state.selected.has("CAM_A") ? "CAM_A" : state.selected.has("CAM_AA") ? "CAM_AA" : null;
    return Boolean(socketA && state.selected.has("CAM_D") && state.configs.get(socketA)?.resolution !== state.configs.get("CAM_D")?.resolution);
  }

  function renderConfigurations(force = false) {
    const list = cameras();
    const key = list.map(camera => `${camera.socket}:${camera.sensor}:${camera.autofocus}:${camera.label}`).join("|");
    if (key === state.seenSockets && !force) { updateSelections(); return; }
    state.seenSockets = key;
    const host = $("camera-configs"); host.replaceChildren();
    if (!list.length) {
      const empty = node("div", "discovery-empty");
      const icon = node("span", "empty-icon", "◎"); icon.setAttribute("aria-hidden", "true");
      empty.append(icon, node("p", "", "No cameras discovered. Check power and USB, then scan again.")); host.append(empty); return;
    }
    for (const camera of list) {
      const card = $("camera-config-template").content.firstElementChild.cloneNode(true); card.dataset.socket = camera.socket;
      card.querySelector(".config-label").textContent = shortLabel(camera);
      card.querySelector(".config-label").title = socketLabel(camera);
      card.querySelector(".config-sensor").textContent = `${camera.sensor || "Unknown sensor"} · ${camera.autofocus === true ? "Autofocus" : camera.autofocus === false ? "Fixed focus" : "Focus unknown"}`;
      card.querySelector(".lane-badge").textContent = ["CAM_A", "CAM_AA", "CAM_D"].includes(camera.socket) ? "4 LANE" : "2 LANE";
      const check = card.querySelector(".camera-select"); check.setAttribute("aria-label", `Select ${socketLabel(camera)}`);
      check.addEventListener("change", () => {
        if (check.checked && state.selected.size >= 3) { check.checked = false; showError("Select a maximum of three cameras per session."); return; }
        if (check.checked) state.selected.add(camera.socket); else state.selected.delete(camera.socket);
        updateSelections(); updateEnabled();
      });
      const config = configFor(camera);
      const resolution = card.querySelector(".resolution-select"); resolution.value = config.resolution; resolution.setAttribute("aria-label", `${shortLabel(camera)} resolution`);
      resolution.addEventListener("change", () => { config.resolution = resolution.value; renderWarnings(); updateEnabled(); });
      const fps = card.querySelector(".fps-input"); fps.value = config.fps; fps.setAttribute("aria-label", `${shortLabel(camera)} frame rate`);
      fps.addEventListener("input", () => { config.fps = Number(fps.value); });
      host.append(card);
    }
    updateSelections();
  }

  function updateSelections() {
    document.querySelectorAll(".camera-config").forEach(card => { const selected = state.selected.has(card.dataset.socket); card.classList.toggle("selected", selected); card.querySelector(".camera-select").checked = selected; });
    $("selection-count").textContent = `${state.selected.size} selected`;
    document.querySelectorAll("[data-preset]").forEach(button => { button.classList.toggle("active", Number(button.dataset.preset) === state.selected.size); button.setAttribute("aria-pressed", String(Number(button.dataset.preset) === state.selected.size)); });
    renderWarnings();
  }

  function renderWarnings() {
    const warnings = Array.isArray(state.status?.warnings) ? state.status.warnings.slice() : [];
    if (hasResolutionConflict()) warnings.push(resolutionWarning);
    if (state.selected.has("CAM_B") && state.selected.has("CAM_C")) warnings.push("CAM_B and CAM_C may share I²C camera controls for identical sensors. Settings may affect both cameras; use CAM_A + CAM_D + CAM_B or CAM_C for three-camera testing.");
    $("warnings").hidden = !warnings.length;
    $("warnings").replaceChildren(...warnings.map(warning => node("p", "", String(warning))));
    const actionable = warnings.filter(warning => !String(warning).toLowerCase().startsWith("demo mode:"));
    $("workspace-warning").hidden = !actionable.length;
    $("workspace-warning").textContent = hasResolutionConflict() ? "Match resolutions in Setup" : `Setup: ${actionable.length} ${actionable.length === 1 ? "notice" : "notices"}`;
    $("workspace-warning").title = actionable.join("\n");
  }

  function updateEnabled() {
    const running = Boolean(state.status?.running), locked = state.busy || running;
    $("scan-button").disabled = locked;
    $("start-button").disabled = locked || !state.selected.size || hasResolutionConflict();
    $("setup-start-button").disabled = $("start-button").disabled;
    $("match-resolutions").disabled = locked || new Set(selectedConfigurations().map(config => config.resolution)).size < 2;
    $("stop-button").disabled = state.busy || !running;
    $("setup-stop-button").disabled = $("stop-button").disabled;
    $("setup-start-button").hidden = running;
    $("setup-stop-button").hidden = !running;
    $("setup-state-help").textContent = running ? "Stop streams to edit the configuration." : "Changes take effect when streams start.";
    $("raw-enabled").disabled = locked;
    document.querySelectorAll("[data-preset]").forEach(button => { button.disabled = locked || cameras().length < Number(button.dataset.preset); });
    document.querySelectorAll(".camera-config").forEach(card => {
      card.querySelector(".camera-select").disabled = locked;
      card.querySelectorAll(".resolution-select, .fps-input").forEach(input => { input.disabled = locked || !state.selected.has(card.dataset.socket); });
    });
    const streams = activeCameras();
    $("capture-all").disabled = state.busy || !streams.length;
    $("capture-selected").disabled = state.busy || !streams.some(camera => camera.socket === state.active);
    $("capture-format").disabled = state.busy || !running;
    document.querySelectorAll("#capture-format, .camera-format").forEach(select => { const option = select.querySelector('option[value="raw"]'); if (option) option.disabled = !rawAvailable(); if (select.value === "raw" && !rawAvailable()) select.value = "jpeg"; });
    document.querySelectorAll(".capture-button, .camera-format").forEach(button => { button.disabled = state.busy || !running; });
    $("control-camera").disabled = state.busy || !streams.length;
    $("control-fields").disabled = state.busy || !streams.length;
    $("apply-controls").disabled = state.busy || !streams.length;
    $("reset-controls").disabled = state.busy || !streams.length || !state.dirty;
    document.querySelectorAll("[data-camera-socket], .tune-button").forEach(button => { button.disabled = state.busy; });
    updateControlModes();
  }

  function updateControlModes() {
    const camera = selectedCamera(), stopped = !state.status?.running || state.busy || !camera;
    const exposureAuto = $("exposure-mode").value !== "manual";
    ["exposure-us", "exposure-number", "iso", "iso-number"].forEach(id => { $(id).disabled = stopped || exposureAuto; });
    const wbAuto = $("white-balance-mode").value !== "manual";
    ["white-balance-kelvin", "white-balance-number"].forEach(id => { $(id).disabled = stopped || wbAuto; });
    const hasFocus = camera?.autofocus === true;
    $("focus-mode").disabled = stopped || !hasFocus;
    $("trigger-autofocus").hidden = $("focus-mode").value !== "auto" || !hasFocus;
    $("trigger-autofocus").disabled = stopped || !hasFocus;
    ["focus", "focus-number"].forEach(id => { $(id).disabled = stopped || !hasFocus || $("focus-mode").value !== "manual"; });
    $("focus-help").textContent = camera && camera.autofocus === false ? "This module has a fixed-focus lens. Focus control is unavailable." : camera && !hasFocus ? "Autofocus capability has not been reported by this module." : "Low values focus farther away; high values focus nearer.";
    $("exposure-compensation").disabled = stopped || !exposureAuto;
    $("exposure-lock").disabled = stopped || !exposureAuto;
    $("auto-exposure-limit").disabled = stopped || !exposureAuto;
    $("white-balance-lock").disabled = stopped || $("white-balance-mode").value !== "auto";
    if (!exposureAuto) $("exposure-lock").checked = false;
    if ($("white-balance-mode").value !== "auto") $("white-balance-lock").checked = false;
  }

  function setDirty(dirty) { state.dirty = dirty; $("control-dirty").textContent = dirty ? "Unapplied changes · saved in this tab" : "Settings apply to this camera only."; $("control-dirty").classList.toggle("dirty", dirty); $("reset-controls").disabled = !dirty || state.busy || !state.status?.running; }

  function saveDraft() {
    if (!state.active || !state.dirty) return;
    const values = {}, pairs = {};
    $("controls-form").querySelectorAll("[name]").forEach(input => { values[input.name] = input.type === "checkbox" ? input.checked : input.value; });
    $("controls-form").querySelectorAll("input[data-pair]").forEach(input => { pairs[input.id] = input.value; });
    state.drafts.set(state.active, {values, pairs, baseline: {...state.formBaseline}});
  }

  function hydrateControls(restoreDraft = true) {
    const camera = selectedCamera();
    const draft = restoreDraft ? state.drafts.get(state.active) : null;
    state.formBaseline = {...defaults, ...(draft?.baseline || camera?.controls || {})};
    const values = {...defaults, ...(camera?.controls || {}), ...(draft?.values || {})};
    const fps = camera?.requested_fps || state.configs.get(camera?.socket)?.fps || 10;
    const maxExposure = Math.max(100, Math.floor(1000000 / fps / 100) * 100);
    for (const id of ["exposure-us", "exposure-number"]) $(id).max = maxExposure;
    $("auto-exposure-limit").max = Math.floor(1000000 / fps);
    $("exposure-limit").textContent = `Up to ${(maxExposure / 1000).toLocaleString(undefined, {maximumFractionDigits: 1})} ms at ${fps} fps.`;
    values.exposure_us = Math.min(values.exposure_us, maxExposure);
    $("controls-form").querySelectorAll("[name]").forEach(input => { if (values[input.name] !== undefined) { if (input.type === "checkbox") input.checked = values[input.name] === true; else input.value = values[input.name]; if (input.dataset.pair) $(input.dataset.pair).value = input.value; } });
    if (draft?.pairs) Object.entries(draft.pairs).forEach(([id, value]) => { if ($(id)) $(id).value = value; });
    $("control-camera-badge").textContent = camera ? shortLabel(camera) : "—";
    $("control-help").textContent = camera && state.status?.running ? `${camera.sensor || "Camera"} · Preview updates after Apply.` : "Start streams to adjust the selected camera.";
    setDirty(Boolean(draft)); updateControlModes();
  }

  function selectControl(socket) {
    if (socket === state.active) return;
    saveDraft();
    state.active = socket; $("control-camera").value = socket; hydrateControls();
    renderCameraSelection(); updateEnabled();
  }

  function renderCameraSelection() {
    state.cards.forEach((card, socket) => { const active = socket === state.active; card.classList.toggle("active", active); card.querySelector(".tune-button").setAttribute("aria-pressed", String(active)); card.querySelector(".tune-button").textContent = active ? "Selected" : "Select"; });
    document.querySelectorAll("[data-camera-socket]").forEach(button => { const active = button.dataset.cameraSocket === state.active; button.setAttribute("aria-pressed", String(active)); const draft = state.drafts.has(button.dataset.cameraSocket) || (active && state.dirty); button.querySelector(".draft-dot").hidden = !draft; button.title = draft ? "This camera has unapplied changes" : `Select ${button.dataset.cameraSocket}`; });
    $("camera-gallery").classList.toggle("selected-view", state.selectedView);
    $("view-all").setAttribute("aria-pressed", String(!state.selectedView));
    $("view-selected").setAttribute("aria-pressed", String(state.selectedView));
    $("viewer-selection").textContent = state.active ? `Editing ${shortLabel(selectedCamera())}` : "Select a camera to adjust";
    $("capture-hint").textContent = state.active ? `${shortLabel(selectedCamera())} selected` : "Choose a running camera";
    $("capture-selected").setAttribute("aria-label", state.active ? `Capture selected camera ${shortLabel(selectedCamera())}` : "Capture selected camera");
  }

  function setControlTab(name, focus = false) {
    if (!$("panel-" + name)) return;
    state.controlTab = name;
    document.querySelectorAll("[data-control-tab]").forEach(button => { const selected = button.dataset.controlTab === name; button.setAttribute("aria-selected", String(selected)); button.tabIndex = selected ? 0 : -1; if (selected && focus) button.focus(); });
    document.querySelectorAll(".control-tab-panel").forEach(panel => { panel.hidden = panel.id !== "panel-" + name; });
    document.querySelector(".controls-scroll").scrollTop = 0;
  }

  function openDialog(id) {
    const target = $(id);
    if (target.open) return;
    document.querySelectorAll("dialog[open]").forEach(dialog => dialog.close());
    target.showModal();
    syncFeedbackHost();
  }

  function syncFeedbackHost() { (document.querySelector("dialog[open]") || document.body).append(document.querySelector(".feedback-stack")); }

  function renderControls(streams) {
    const select = $("control-camera");
    const key = streams.map(camera => camera.socket).join(",");
    if (select.dataset.sockets !== key) {
      select.dataset.sockets = key; select.replaceChildren();
      if (!streams.length) { const option = node("option", "", "Start a camera to adjust"); option.value = ""; select.append(option); }
      streams.forEach(camera => { const option = node("option", "", socketLabel(camera)); option.value = camera.socket; select.append(option); });
      const tabs = $("camera-tabs"); tabs.replaceChildren();
      if (!streams.length) tabs.append(node("span", "empty-tab-label", "No active cameras"));
      streams.forEach(camera => { const button = node("button", "", shortLabel(camera)); button.type = "button"; button.dataset.cameraSocket = camera.socket; button.setAttribute("aria-pressed", "false"); const marker = node("span", "draft-dot"); marker.hidden = true; marker.setAttribute("aria-hidden", "true"); button.append(marker); button.addEventListener("click", () => selectControl(camera.socket)); tabs.append(button); });
    }
    if (!streams.some(camera => camera.socket === state.active)) selectControl(streams[0]?.socket || "");
    else {
      select.value = state.active;
      const reported = {...defaults, ...(selectedCamera()?.controls || {})};
      if (!state.dirty && Object.keys(reported).some(key => reported[key] !== state.formBaseline[key])) hydrateControls(false);
    }
    renderCameraSelection();
  }

  function metadataText(camera) {
    const metadata = camera.metadata || {}, parts = [];
    const exposure = metadata.exposure_us ?? metadata.exposure_time_us;
    if (Number.isFinite(exposure)) parts.push(`${(exposure / 1000).toLocaleString(undefined, {maximumFractionDigits: 2})} ms`);
    if (Number.isFinite(metadata.iso)) parts.push(`ISO ${metadata.iso}`);
    const wb = metadata.white_balance_kelvin ?? metadata.color_temperature;
    if (Number.isFinite(wb)) parts.push(`${wb} K`);
    const focus = metadata.focus ?? metadata.lens_position;
    if (Number.isFinite(focus)) parts.push(`Lens ${focus}`);
    return parts;
  }

  function renderStreams() {
    const streams = activeCameras(), host = $("camera-gallery");
    host.dataset.count = streams.length;
    const sockets = new Set(streams.map(camera => camera.socket));
    for (const [socket, card] of state.cards) { if (!sockets.has(socket)) { card.querySelector("img").removeAttribute("src"); card.remove(); state.cards.delete(socket); } }
    host.classList.toggle("single", streams.length <= 1);
    let empty = $("workspace-empty");
    if (!streams.length) {
      if (!empty) { empty = node("div", "workspace-empty"); empty.id = "workspace-empty"; const lens = node("div", "lens-illustration", "◎"); lens.setAttribute("aria-hidden", "true"); const button = node("button", "button button-primary", "Configure cameras"); button.type = "button"; button.dataset.openSetup = ""; empty.append(lens, node("h2", "", "Ready when you are."), node("p", "", "Choose up to three cameras in Setup, then start the streams."), button); host.append(empty); }
    } else if (empty) empty.remove();
    for (const camera of streams) {
      let card = state.cards.get(camera.socket);
      if (!card) {
        card = $("camera-card-template").content.firstElementChild.cloneNode(true); card.dataset.socket = camera.socket;
        card.querySelector(".camera-label").textContent = shortLabel(camera);
        card.querySelector(".camera-label").title = socketLabel(camera);
        card.querySelector(".camera-sensor").textContent = camera.sensor || "";
        card.querySelector(".tune-button").setAttribute("aria-label", `Adjust ${shortLabel(camera)} settings`);
        card.querySelector(".tune-button").addEventListener("click", () => selectControl(camera.socket, true));
        card.querySelector(".preview").addEventListener("click", event => { if (!event.target.closest("button") && !state.busy) selectControl(camera.socket); });
        const img = card.querySelector(".camera-image"); img.alt = `${socketLabel(camera)} live preview`; img.src = `/stream/${encodeURIComponent(camera.socket)}?session=${state.streamEpoch}`;
        img.addEventListener("error", () => { if (state.status?.running) { card.dataset.imageError = "true"; card.querySelector(".preview-message").textContent = "Preview disconnected. Waiting to reconnect…"; card.querySelector(".preview").classList.remove("has-frame"); } });
        img.addEventListener("load", () => { delete card.dataset.imageError; });
        card.querySelector(".fullscreen-button").setAttribute("aria-label", `View ${shortLabel(camera)} fullscreen`);
        card.querySelector(".fullscreen-button").addEventListener("click", () => { const preview = card.querySelector(".preview"); if (!preview.requestFullscreen) { showError("Fullscreen is not supported by this browser."); return; } preview.requestFullscreen().catch(error => showError(`Could not open fullscreen: ${error.message}`)); });
        const format = card.querySelector(".camera-format"); format.value = $("capture-format").value; format.setAttribute("aria-label", `${shortLabel(camera)} capture format`);
        card.querySelector(".capture-button").setAttribute("aria-label", `Capture image from ${shortLabel(camera)}`);
        card.querySelector(".capture-button").addEventListener("click", () => action(() => capture([camera.socket], format.value)));
        host.append(card); state.cards.set(camera.socket, card);
      }
      const frames = Number(camera.frames) || 0;
      const age = camera.last_frame_age;
      const stale = frames > 0 && (age === null || (Number.isFinite(age) && age > 3));
      const healthy = frames > 0 && !stale;
      card.querySelector(".camera-dot").className = `status-dot camera-dot ${healthy ? "live" : stale ? "error" : ""}`;
      const frameState = card.querySelector(".frame-state"); frameState.textContent = !frames ? "Waiting for frames" : stale ? `Stale${Number.isFinite(age) ? ` · ${age.toFixed(1)}s ago` : ""}` : "Receiving frames"; frameState.classList.toggle("stale", stale);
      card.querySelector(".camera-fps").textContent = Number.isFinite(camera.fps) ? camera.fps.toFixed(1) : "—";
      card.querySelector(".camera-frames").textContent = frames.toLocaleString();
      const resolution = camera.resolution || state.configs.get(camera.socket)?.resolution;
      card.querySelector(".preview-resolution").textContent = resolutions[resolution] || resolution || "";
      card.querySelector(".preview").classList.toggle("has-frame", frames > 0 && !card.dataset.imageError);
      const metaHost = card.querySelector(".camera-metadata"); metaHost.replaceChildren(...metadataText(camera).map(text => node("span", "", text)));
      if (card.dataset.imageError && healthy && !card.dataset.retrying) { card.dataset.retrying = "true"; setTimeout(() => { if (state.cards.get(camera.socket) !== card || !state.status?.running) return; delete card.dataset.retrying; delete card.dataset.imageError; card.querySelector(".camera-image").src = `/stream/${encodeURIComponent(camera.socket)}?retry=${Date.now()}`; }, 2500); }
    }
    renderControls(streams);
    renderCameraSelection();
  }

  function safeDownloadURL(url) { try { const parsed = new URL(url, location.href); return parsed.origin === location.origin && ["http:", "https:"].includes(parsed.protocol) ? parsed.href : null; } catch (_) { return null; } }
  function fileSize(size) { if (!Number.isFinite(size)) return ""; return size >= 1048576 ? `${(size / 1048576).toFixed(1)} MB` : size >= 1024 ? `${Math.round(size / 1024)} KB` : `${size} B`; }
  function renderCaptures(captures) {
    if (!Array.isArray(captures)) return;
    const key = JSON.stringify(captures); if (key === state.captureKey) return; state.captureKey = key;
    $("capture-count").textContent = captures.length;
    const host = $("capture-list"); host.replaceChildren();
    if (!captures.length) { host.append(node("p", "captures-empty", "Your saved images and their metadata will appear here.")); return; }
    const sorted = captures.slice().sort((a,b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
    for (const capture of sorted.slice(0, 18)) {
      const item = node("div", "capture-item"); const icon = node("div", "capture-file-icon", "▧"); icon.setAttribute("aria-hidden", "true");
      const description = node("div"); const format = (capture.format || "image").toUpperCase();
      description.append(node("div", "capture-item-title", `${socketLabel({socket: capture.socket})} · ${format} capture`));
      const date = new Date(capture.created_at); const dateText = Number.isNaN(date.getTime()) ? capture.created_at || "" : date.toLocaleString(undefined, {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit"});
      description.append(node("div", "capture-item-details", dateText));
      const links = node("div", "capture-links");
      for (const file of capture.files || []) { const url = safeDownloadURL(file.url); if (!url) continue; const link = node("a", "", file.name || "Download"); link.href = url; link.download = file.name || ""; if (fileSize(file.size)) link.append(node("small", "", fileSize(file.size))); links.append(link); }
      item.append(icon, description, links); host.append(item);
    }
    if (captures.length > 18) host.append(node("p", "captures-empty", `Showing the 18 most recent captures of ${captures.length}. All files remain saved on the host computer.`));
  }

  function renderStatus(status) {
    const previous = state.status;
    state.status = status;
    const list = cameras(), detected = new Set(list.map(camera => camera.socket));
    for (const socket of state.selected) if (!detected.has(socket)) state.selected.delete(socket);
    if (!previous || (!previous.cameras?.length && list.length)) {
      const runningCameras = list.filter(camera => camera.active === true || status.active_sockets?.includes(camera.socket));
      for (const camera of (status.running && runningCameras.length ? runningCameras : list.slice(0, Math.min(2, list.length)))) state.selected.add(camera.socket);
    }
    if (status.running && !previous?.running) { state.streamEpoch += 1; $("raw-enabled").checked = Boolean(status.raw_enabled); }
    $("demo-banner").hidden = !status.demo;
    const device = status.device || {};
    $("device-name").textContent = device.name || (device.id ? "OAK device" : "No device connected");
    $("device-id").textContent = device.id ? `ID ${device.id}` : "Connect the OAK device over USB";
    const usbLabels = {SUPER: "USB 3 · SuperSpeed", SUPER_PLUS: "USB 3 · SuperSpeed+", HIGH: "USB 2 · High Speed", FULL: "USB · Full Speed", LOW: "USB · Low Speed", DEMO: "Simulated"};
    $("usb-speed").textContent = usbLabels[device.usb_speed] || device.usb_speed || "—";
    $("capture-directory").hidden = !status.capture_directory;
    $("capture-directory").textContent = status.capture_directory ? `Save folder: ${status.capture_directory}` : "";
    $("camera-count").textContent = `${list.length} detected`;
    const active = activeCameras().length;
    $("stream-count").textContent = status.running ? `${active} ${active === 1 ? "stream" : "streams"} active` : "Streams stopped";
    $("pipeline-state").textContent = status.running ? "Running" : "Stopped";
    $("pipeline-dot").className = `status-dot ${status.running ? "live" : ""}`;
    $("pipeline-detail").textContent = status.running ? (status.raw_enabled ? "Preview + RAW enabled" : "Live preview enabled") : "Configure cameras to begin";
    $("live-tag").hidden = !status.running;
    setConnection(status.demo ? "Demo device" : device.id || list.length ? "Device connected" : "No device", device.id || list.length ? "live" : "");
    if (status.error) showError(status.error);
    renderConfigurations(); renderStreams(); renderCaptures(status.captures); updateEnabled();
  }

  async function refresh() { const result = await api("/api/status"); renderStatus(result); return result; }
  async function scan() { const result = await api("/api/scan", {}); if (result.cameras) renderStatus(result); else await refresh(); notice(`${cameras().length} ${cameras().length === 1 ? "camera" : "cameras"} discovered.`); }
  async function capture(sockets, format) {
    const result = await api("/api/capture", {sockets, format});
    const count = result.captures?.length ?? sockets.length;
    notice(`${count} ${format.toUpperCase()} ${count === 1 ? "capture" : "captures"} saved. Open Captures to download.`);
    const saved = await api("/api/captures"); renderCaptures(saved.captures); await refresh();
  }

  $("dismiss-error").addEventListener("click", () => { $("error-banner").hidden = true; });
  $("open-setup").addEventListener("click", () => openDialog("setup-dialog"));
  $("workspace-warning").addEventListener("click", () => openDialog("setup-dialog"));
  $("open-captures").addEventListener("click", () => openDialog("captures-dialog"));
  document.addEventListener("click", event => { if (event.target.closest("[data-open-setup]")) openDialog("setup-dialog"); if (event.target.closest("[data-close-dialog]")) event.target.closest("dialog").close(); });
  document.querySelectorAll("dialog").forEach(dialog => { dialog.addEventListener("close", syncFeedbackHost); });
  $("view-all").addEventListener("click", () => { state.selectedView = false; renderCameraSelection(); });
  $("view-selected").addEventListener("click", () => { state.selectedView = true; renderCameraSelection(); });
  document.querySelectorAll("[data-control-tab]").forEach(button => {
    button.addEventListener("click", () => setControlTab(button.dataset.controlTab));
    button.addEventListener("keydown", event => {
      const tabs = [...document.querySelectorAll("[data-control-tab]")], current = tabs.indexOf(button);
      const next = event.key === "ArrowRight" ? (current + 1) % tabs.length : event.key === "ArrowLeft" ? (current + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
      if (next !== null) { event.preventDefault(); setControlTab(tabs[next].dataset.controlTab, true); }
    });
  });
  $("scan-button").addEventListener("click", () => action(scan));
  $("match-resolutions").addEventListener("click", () => {
    if (state.busy || state.status?.running) return;
    const configs = selectedConfigurations();
    const highest = resolutionOrder.slice().reverse().find(mode => configs.some(config => config.resolution === mode));
    if (!highest || configs.length < 2) return;
    configs.forEach(config => { config.resolution = highest; });
    document.querySelectorAll(".camera-config").forEach(card => {
      if (state.selected.has(card.dataset.socket)) card.querySelector(".resolution-select").value = highest;
    });
    renderWarnings(); updateEnabled();
    notice(`Selected cameras now use ${highest === "12mp" ? "12 MP" : highest === "4k" ? "4K" : "1080p"}, the highest selected mode.`);
  });
  document.querySelectorAll("[data-preset]").forEach(button => button.addEventListener("click", () => { state.selected = new Set(cameras().slice(0, Number(button.dataset.preset)).map(camera => camera.socket)); updateSelections(); updateEnabled(); }));
  async function startStreams() {
    if (hasResolutionConflict()) throw new Error(resolutionWarning);
    const selected = cameras().filter(camera => state.selected.has(camera.socket)).map(camera => ({socket: camera.socket, ...configFor(camera)}));
    for (const camera of selected) { if (!Number.isInteger(camera.fps) || camera.fps < 2 || camera.fps > 30) throw new Error(`${camera.socket}: enter a whole-number frame rate between 2 and 30 fps.`); }
    $("start-button").textContent = "Starting…";
    $("setup-start-button").textContent = "Starting…";
    try { const result = await api("/api/start", {cameras: selected, raw_enabled: $("raw-enabled").checked}); if (result.cameras) renderStatus(result); else await refresh(); if ($("setup-dialog").open) $("setup-dialog").close(); notice("Streams started. Select a camera to adjust its image."); }
    finally { $("start-button").replaceChildren(node("span", "", "▶"), document.createTextNode(" Start")); $("setup-start-button").textContent = "Start streams"; }
  }
  async function stopStreams() { const result = await api("/api/stop", {}); if (result.cameras) renderStatus(result); else await refresh(); notice("Streams stopped. Open Setup to change cameras and resolution."); }
  $("start-button").addEventListener("click", () => action(startStreams));
  $("setup-start-button").addEventListener("click", () => action(startStreams));
  $("stop-button").addEventListener("click", () => action(stopStreams));
  $("setup-stop-button").addEventListener("click", () => action(stopStreams));
  $("capture-all").addEventListener("click", () => action(() => capture(activeCameras().map(camera => camera.socket), $("capture-format").value)));
  $("capture-selected").addEventListener("click", () => action(() => capture([state.active], $("capture-format").value)));
  $("capture-format").addEventListener("change", () => { document.querySelectorAll(".camera-format").forEach(select => { select.value = $("capture-format").value; }); });
  $("control-camera").addEventListener("change", () => selectControl($("control-camera").value));
  $("reset-controls").addEventListener("click", () => { state.drafts.delete(state.active); hydrateControls(false); renderCameraSelection(); });
  $("trigger-autofocus").addEventListener("click", () => action(async () => {
    if (!state.active) return;
    await api(`/api/controls/${encodeURIComponent(state.active)}`, {focus_mode: "auto"});
    state.formBaseline.focus_mode = "auto";
    const draft = state.drafts.get(state.active);
    if (draft) draft.baseline.focus_mode = "auto";
    await refresh();
    notice(`Autofocus requested for ${shortLabel(selectedCamera())}.`);
  }));
  $("controls-form").querySelectorAll('input[type="number"]').forEach(input => { input.required = true; });
  $("controls-form").addEventListener("input", event => { const input = event.target; if (input.dataset.pair) { const paired = $(input.dataset.pair); if (input.type === "number" && (input.value === "" || !input.validity.valid)) { setDirty(true); saveDraft(); renderCameraSelection(); return; } paired.value = input.value; } setDirty(true); updateControlModes(); saveDraft(); renderCameraSelection(); });
  $("controls-form").addEventListener("change", () => { setDirty(true); updateControlModes(); saveDraft(); renderCameraSelection(); });
  $("controls-form").addEventListener("submit", event => {
    event.preventDefault();
    const invalid = $("controls-form").querySelector("input:invalid, select:invalid");
    if (invalid) { const panel = invalid.closest(".control-tab-panel"); if (panel) setControlTab(panel.id.replace("panel-", "")); $("controls-form").reportValidity(); return; }
    const values = {};
    const baseline = state.formBaseline;
    $("controls-form").querySelectorAll("[name]").forEach(input => { if (!input.disabled) { const value = input.type === "checkbox" ? input.checked : numericControls.has(input.name) ? Number(input.value) : input.value; if (value !== baseline[input.name]) values[input.name] = value; } });
    action(async () => {
      if (!state.active) throw new Error("Start a camera before applying settings.");
      const socket = state.active;
      if (Object.keys(values).length) await api(`/api/controls/${encodeURIComponent(socket)}`, values);
      state.drafts.delete(socket); setDirty(false); await refresh(); hydrateControls(false); renderCameraSelection(); notice(`Settings applied to ${shortLabel(selectedCamera())}.`);
    });
  });

  async function poll() {
    if (!state.busy && !state.polling && !document.hidden) {
      state.polling = true;
      try { await refresh(); }
      catch (error) { setConnection("Server unavailable", "error"); showError(error); }
      finally { state.polling = false; }
    }
    setTimeout(poll, 1500);
  }

  async function initialize() {
    await action(async () => { const status = await refresh(); if (!status.running && !status.cameras?.length) await scan(); });
    if (!state.status?.running && !state.initialSetupOpened) { state.initialSetupOpened = true; openDialog("setup-dialog"); }
    poll();
  }
  initialize().catch(showError);
})();
