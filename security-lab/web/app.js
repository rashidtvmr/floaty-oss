"use strict";
(() => {
  const el = (id) => document.getElementById(id);
  const state = { stream: null, rows: [], recorder: null, chunks: [], timer: null };
  const status = (message) => { el("status").textContent = message; };
  const live = () => !!state.stream && state.stream.getVideoTracks().some((t) => t.readyState === "live");
  const surface = () => state.stream?.getVideoTracks()[0]?.getSettings().displaySurface || "Unreported";
  function update() {
    el("start").disabled = live();
    el("stop").disabled = !live();
    el("snapshot").disabled = !live();
    el("record").disabled = !live() || !!state.recorder;
    el("finish-record").disabled = !state.recorder;
    el("surface").textContent = live() ? surface() : "Not selected";
    el("stream-state").textContent = live() ? "Active, user-approved" : "Inactive";
    el("placeholder").hidden = live();
  }
  function finishRecording() {
    if (state.timer) clearTimeout(state.timer);
    state.timer = null;
    if (state.recorder && state.recorder.state !== "inactive") state.recorder.stop();
  }
  function stop() {
    finishRecording();
    for (const track of state.stream?.getTracks() || []) track.stop();
    state.stream = null;
    el("preview").srcObject = null;
    status("Capture stopped. No recording or upload.");
    update();
  }
  async function start() {
    if (!navigator.mediaDevices?.getDisplayMedia) {
      status("Your browser does not provide getDisplayMedia.");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: { ideal: 10, max: 15 } }, audio: false
      });
      stop();
      state.stream = stream;
      el("preview").srcObject = stream;
      stream.getVideoTracks()[0].addEventListener("ended", stop, { once: true });
      update();
      status("Authorized stream active: " + surface());
    } catch (error) {
      status(error?.name === "NotAllowedError" ? "Sharing denied or cancelled." : "Capture unavailable; check browser/portal permissions.");
    }
  }
  function interpretation(row) {
    if (row.seen === "yes" && row.inCapture === "no") return "Outside selected capture";
    if (row.seen === "yes" && row.inCapture === "yes") return "Included in capture";
    return "Insufficient evidence";
  }
  function drawRows() {
    const body = el("observations");
    body.replaceChildren();
    if (!state.rows.length) {
      const cell = body.insertRow().insertCell();
      cell.colSpan = 6;
      cell.textContent = "No observations yet.";
      return;
    }
    state.rows.forEach((row, index) => {
      const tr = body.insertRow();
      [index+1, row.testCase, row.seen, row.inCapture, interpretation(row), row.notes || "-"]
        .forEach((value) => { tr.insertCell().textContent = String(value); });
    });
  }
  function add() {
    state.rows.push({
      at: new Date().toISOString(),
      testCase: el("case").value,
      actualSurface: live() ? surface() : "none",
      seen: el("participant").value,
      inCapture: el("captured").value,
      notes: el("notes").value.slice(0, 240)
    });
    drawRows();
    status("Recorded " + state.rows.length + " observation(s) locally.");
  }
  function download(blob, name) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function exportReport() {
    const report = {
      title: "ContextLens browser screen capture test",
      createdAt: new Date().toISOString(),
      limitation: "Self-reported selected capture surfaces, not proof of universal detection.",
      observations: state.rows.map((row) => Object.assign({}, row, { interpretation: interpretation(row) }))
    };
    download(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }), "capture-test-report.json");
  }
  function snapshot() {
    const video = el("preview");
    if (!live() || !video.videoWidth) {
      status("Wait for a frame from the shared stream.");
      return;
    }
    const canvas = document.createElement("canvas");
    const scale = Math.min(1, 1920 / video.videoWidth);
    canvas.width = Math.round(video.videoWidth * scale);
    canvas.height = Math.round(video.videoHeight * scale);
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob((blob) => { if (blob) download(blob, "capture-preview.png"); }, "image/png");
  }
  function record() {
    if (!live() || state.recorder || !window.MediaRecorder) return;
    try {
      const mime = ["video/webm;codecs=vp8", "video/webm"].find((x) => MediaRecorder.isTypeSupported(x));
      const recorder = new MediaRecorder(state.stream, mime ? { mimeType: mime } : {});
      state.recorder = recorder;
      state.chunks = [];
      recorder.ondataavailable = (event) => { if (event.data?.size) state.chunks.push(event.data); };
      recorder.onstop = () => {
        const chunks = state.chunks.splice(0);
        state.recorder = null;
        if (chunks.length) download(new Blob(chunks, { type: mime || "video/webm" }), "capture-test.webm");
        status("Recording stopped and downloaded locally.");
        update();
      };
      recorder.start(250);
      state.timer = setTimeout(finishRecording, 30000);
      update();
      status("Recording approved capture for up to 30 seconds.");
    } catch (_error) {
      state.recorder = null;
      status("MediaRecorder unavailable.");
      update();
    }
  }
  el("start").addEventListener("click", start);
  el("stop").addEventListener("click", stop);
  el("add").addEventListener("click", add);
  el("export").addEventListener("click", exportReport);
  el("snapshot").addEventListener("click", snapshot);
  el("record").addEventListener("click", record);
  el("finish-record").addEventListener("click", finishRecording);
  el("clear").addEventListener("click", () => {
    if (window.confirm("Delete local observations?")) { state.rows = []; drawRows(); }
  });
  window.addEventListener("beforeunload", stop);
  update();
})();
