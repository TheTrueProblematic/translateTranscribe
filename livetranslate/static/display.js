/* LiveTranslate display client (spec section 9).
 *
 * Renders strictly in sequence order, streams text as it generates, and keeps
 * the current line's starting position fixed so nothing jumps when a new line
 * arrives. All state is server-driven except font scale, which is local and
 * persisted.
 */
(() => {
  "use strict";

  const el = {
    stage: document.getElementById("stage"),
    prev: document.getElementById("prev"),
    cur: document.getElementById("cur"),
    dot: document.getElementById("dot"),
    glyph: document.getElementById("glyph"),
    level: document.getElementById("level"),
    toast: document.getElementById("toast"),
    enText: document.getElementById("enText"),
    enPartial: document.getElementById("enPartial"),
    enNote: document.getElementById("enNote"),
    backlog: document.getElementById("backlog"),
  };
  let backlogFull = 8;

  // ---------- font scale (persisted) ----------
  const SCALE_KEY = "livetranslate.fontScale";
  const SCALE_MIN = 0.45, SCALE_MAX = 2.6, SCALE_STEP = 0.08;
  let scale = parseFloat(localStorage.getItem(SCALE_KEY) || "");
  if (!isFinite(scale) || scale <= 0) scale = 1;

  function applyScale(showToast) {
    scale = Math.min(SCALE_MAX, Math.max(SCALE_MIN, scale));
    document.documentElement.style.setProperty("--scale", String(scale));
    localStorage.setItem(SCALE_KEY, String(scale));
    refit();
    if (showToast) {
      el.toast.textContent = Math.round(scale * 100) + "%";
      el.toast.classList.add("show");
      clearTimeout(applyScale._t);
      applyScale._t = setTimeout(() => el.toast.classList.remove("show"), 900);
    }
  }

  // ---------- shrink-only autofit ----------
  // Guarantees the spec's "nothing overflows or clips" without letting the
  // type oscillate while a line streams in: fit only ever decreases within a
  // line and resets to 1 when a new line starts.
  let fit = 1;
  function refit() {
    for (let i = 0; i < 24; i++) {
      if (el.cur.scrollHeight <= el.cur.clientHeight + 1) break;
      fit -= 0.04;
      if (fit < 0.5) { fit = 0.5; el.cur.style.setProperty("--fit", fit); break; }
      el.cur.style.setProperty("--fit", String(fit));
    }
  }
  function resetFit() {
    fit = 1;
    el.cur.style.setProperty("--fit", "1");
  }

  // ---------- rendering, strictly in sequence order ----------
  let renderedSeq = 0;   // highest sequence number shown
  let curSeq = 0;

  function setLine(seq, text, final) {
    if (seq < curSeq) return;            // stale response: never scramble
    if (seq > curSeq) {
      // A new line begins: promote current to previous. The previous band has
      // a fixed height, so this does not move the current line at all.
      if (el.cur.textContent) el.prev.textContent = el.cur.textContent;
      curSeq = seq;
      resetFit();
    }
    el.cur.textContent = text;
    refit();
    if (final) renderedSeq = Math.max(renderedSeq, seq);
  }

  // ---------- websocket ----------
  let ws = null, retry = 0;

  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws`);

    ws.onopen = () => { retry = 0; document.body.classList.remove("offline"); };
    ws.onclose = () => {
      document.body.classList.add("offline");
      retry = Math.min(retry + 1, 20);
      setTimeout(connect, Math.min(250 * retry, 3000));
    };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };

    ws.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      switch (m.type) {
        case "line":
          setLine(m.seq | 0, m.text || "", !!m.final);
          break;
        case "settings":
          document.body.classList.toggle("no-monitor", !m.show_english_monitor);
          if (m.backlog_bar_full > 0) backlogFull = m.backlog_bar_full;
          break;
        case "backlog": {
          const n = Math.max(0, m.pending | 0);
          const frac = Math.min(1, n / backlogFull);
          el.backlog.style.width = (frac * 100) + "%";
          el.backlog.classList.toggle("deep", frac >= 0.5 && frac < 0.9);
          el.backlog.classList.toggle("full", frac >= 0.9);
          break;
        }
        case "english": {
          // Speaker's monitor only. Trimmed to the last few words so it stays
          // one quiet line and never turns into a second thing to read.
          const words = (m.text || "").split(/\s+/).filter(Boolean);
          el.enText.textContent = words.slice(-14).join(" ");
          el.enPartial.textContent = m.partial ? " " + m.partial : "";
          el.enNote.textContent = m.note ? "  ⚠ " + m.note : "";
          break;
        }
        case "status":
          document.body.classList.toggle("paused", !!m.paused);
          document.body.classList.toggle("translating", !!m.translating);
          el.glyph.textContent = m.paused ? "⏸" : (m.translating ? "⋯" : "");
          break;
        case "level":
          el.level.style.width = Math.round(Math.min(1, m.rms || 0) * 100) + "%";
          break;
        case "clear":
          el.prev.textContent = "";
          el.cur.textContent = "";
          curSeq = 0; renderedSeq = 0; resetFit();
          break;
      }
    };
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  // ---------- keyboard ----------
  document.addEventListener("keydown", (e) => {
    if (e.key === "+" || e.key === "=") { scale += SCALE_STEP; applyScale(true); e.preventDefault(); }
    else if (e.key === "-" || e.key === "_") { scale -= SCALE_STEP; applyScale(true); e.preventDefault(); }
    else if (e.key === "0") { scale = 1; applyScale(true); e.preventDefault(); }
    else if (e.code === "Space" || e.key === "p" || e.key === "P") {
      // Manual hold. A bare SPACE once paused a whole session unnoticed, so
      // pausing now also raises a full-width banner (see display.css) and the
      // server logs it loudly. Resuming is the same key.
      send({ type: "toggle_pause" }); e.preventDefault();
    }
    else if (e.key === "e" || e.key === "E") {
      document.body.classList.toggle("no-monitor");   // hide the English strip
      e.preventDefault();
    }
    else if (e.key === "f" || e.key === "F") {
      if (!document.fullscreenElement) document.documentElement.requestFullscreen?.();
      else document.exitFullscreen?.();
      e.preventDefault();
    }
  });

  window.addEventListener("resize", () => { resetFit(); refit(); });

  // Exposed for the screenshot harness so section 12 test 7 can render
  // representative content without a live microphone.
  window.__lt_setLine = setLine;
  window.__lt_applyScale = (s) => { scale = s; applyScale(false); };

  resetFit();
  applyScale(false);
  connect();
})();
