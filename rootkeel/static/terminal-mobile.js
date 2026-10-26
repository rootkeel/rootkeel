// SPDX-License-Identifier: AGPL-3.0-only
"use strict";
// Copy and paste for Zellij 0.45.1's web client on a phone, added by the broker to the terminal page.
// That client takes every touch on the terminal (a drag scrolls, a long press is a right click), so no text can be
// selected, and it types through a hidden password input, which no paste menu reaches and which drops newlines.
// While its mobile UI is on, a long press on the terminal (or the copy button in the top bar) shows the visible screen
// as plain text the phone's own selection and Copy work on, with the pressed word already selected, kept within the
// part of the page the phone shows; it copies only what is selected and closes once it is copied or on a tap outside
// the selection. A Paste button in the top bar and a PASTE key in the key row send the clipboard through xterm's paste
// (bracketed, newlines kept). The phone's keyboard comes up only for a tap on the terminal: the web client would
// bring it up as any finger lands there, so a scroll or a long press raised it too. The desktop page is left as Zellij
// made it.
(() => {
  const CHROME = "zj-mobile-chrome";  // Zellij's own controls: taps there do not refocus its hidden input.
  const SETTLE_MS = 1000;
  const LONG_PRESS_MS = 450;  // Before Zellij's own long press (500 ms), which sends a right click.
  const MOVE_PX = 16;  // As far as Zellij lets a finger drift before a press becomes a scroll.
  const TAP_MS = 300, TAP_PX = 10;
  const QUIET_MS = 2000;  // How long after a copy the terminal may not overwrite the clipboard.
  const style = document.createElement("style");
  style.textContent = `
    .zj-mobile-topbar .rk-select, .zj-mobile-topbar .rk-paste { color: var(--zj-text); flex: 0 0 auto; min-width: 40px;
      padding: 0 8px; justify-content: center; }
    .rk-select svg, .rk-paste svg { width: 20px; height: 20px; }
    .zj-mobile-modbar .rk-paste { font-size: 11px; }
    .rk-screen { position: fixed; z-index: 650; overflow: auto; overscroll-behavior: contain; touch-action: pan-x pan-y;
      user-select: text; -webkit-user-select: text; -webkit-touch-callout: default; }
    .rk-screen pre { margin: 0; white-space: pre; user-select: text; -webkit-user-select: text; }
    .rk-bar, .rk-sheet { position: fixed; left: 0; right: 0; z-index: 660; display: flex; gap: 8px; padding: 10px 12px;
      background: var(--zj-bg); color: var(--zj-text);
      font-family: 'JetBrains Mono', 'Consolas', 'Monaco', 'Courier New', monospace; font-size: 14px; }
    .rk-bar { bottom: 0; flex-wrap: wrap; border-top: 1px solid var(--zj-green);
      padding-bottom: calc(10px + env(safe-area-inset-bottom, 0px)); }
    .rk-sheet { top: 44px; flex-direction: column; border-bottom: 1px solid var(--zj-green); }
    .rk-bar button, .rk-sheet button { flex: 1 1 0; min-height: 44px; background: transparent; cursor: pointer;
      border: 1px solid var(--zj-green); color: var(--zj-green); font: inherit; }
    .rk-bar { user-select: none; -webkit-user-select: none; -webkit-touch-callout: none; }
    .rk-sheet .rk-row { display: flex; gap: 8px; }
    .rk-sheet textarea { min-height: 6em; font: inherit; font-size: 16px; color: var(--zj-text);
      background: var(--zj-surface); border: 1px solid var(--zj-border); user-select: text; -webkit-user-select: text; }
    .rk-status { flex: 1 0 100%; color: var(--zj-text-dim); font-size: 13px; }
    .rk-status:empty { display: none; }
    .rk-toast { position: fixed; top: 52px; left: 50%; transform: translateX(-50%); z-index: 670; pointer-events: none;
      padding: 8px 14px; border: 1px solid var(--zj-green); background: var(--zj-surface); color: var(--zj-text);
      font-family: 'JetBrains Mono', 'Consolas', 'Monaco', 'Courier New', monospace; font-size: 14px; }
    .rk-toast:empty { display: none; }`;
  document.head.appendChild(style);
  const ICON = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round">';
  const COPY_ICON = ICON + '<rect x="9" y="9" width="12" height="12" rx="2"/>' +
    '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
  const PASTE_ICON = ICON + '<rect x="8" y="2" width="8" height="4" rx="1"/>' +
    '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/></svg>';

  let ui = null;  // The added controls, while the mobile UI is on.

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function button(className, text, label, onClick) {
    const node = element("button", className, text);
    node.type = "button";
    if (label) node.setAttribute("aria-label", label);
    node.addEventListener("click", onClick);
    return node;
  }
  // Like Zellij's own keys: pressing it keeps the focus, and so the phone's keyboard, and any selection where they are.
  function keepFocus(node) {
    for (const type of ["pointerdown", "mousedown"]) node.addEventListener(type, event => event.preventDefault());
    return node;
  }
  // Zellij's hidden input keeps the phone's keyboard up; it is a page global of the web client (soft-keyboard.js).
  function keyboardInput() { return window.__zjSoftKbdCapture && window.__zjSoftKbdCapture.element; }

  // Zellij focuses that input, which brings the keyboard up, as a finger lands on the terminal, and blurs and focuses
  // it again to keep the keyboard up. While a finger is on the terminal (ui.touch) both wait: the keyboard stays as it
  // was through a scroll or a long press. A tap lets them through as it lifts, when Zellij's own release focuses the
  // input, still within the tap, so the keyboard comes up. Zellij's two-finger tap still turns the keyboard on and
  // off: it changes __zjSoftKbdEnabled first, and the keyboard going down blurs the input as before.
  function guardKeyboard() {
    const input = keyboardInput();
    if (!input || input.rkGuarded) return;
    input.rkGuarded = true;
    input.focus = function (options) {
      const touch = ui && ui.touch, on = !!window.__zjSoftKbdEnabled;
      if (!touch || touch.tap || (on && !touch.enabled)) HTMLElement.prototype.focus.call(this, options);
    };
    input.blur = function () {
      const touch = ui && ui.touch;
      if (!touch || touch.tap || !window.__zjSoftKbdEnabled) HTMLElement.prototype.blur.call(this);
    };
  }
  function unguardKeyboard() {
    const input = keyboardInput();
    if (!input || !input.rkGuarded) return;
    for (const name of ["focus", "blur", "rkGuarded"]) delete input[name];
  }
  // The finger, or the last of two, has lifted: once Zellij has handled the release, its focus calls go through again.
  function endTouch() {
    const touch = ui.touch;
    setTimeout(() => { if (ui && ui.touch === touch) ui.touch = null; });
  }

  function toast(text) {
    ui.toast.textContent = text;
    clearTimeout(ui.toastTimer);
    ui.toastTimer = setTimeout(() => { if (ui) ui.toast.textContent = ""; }, 2000);
  }

  // The rows the terminal shows. Zellij keeps the scrollback itself (xterm's is 0), so the page holds only these.
  // Zellij paints blank cells as spaces, which a copy should not carry at the end of a line.
  function screenText(term) {
    const buffer = term.buffer.active, rows = [];
    for (let y = 0; y < term.rows; y++) {
      const line = buffer.getLine(buffer.viewportY + y);
      rows.push(line ? line.translateToString(true).replace(/ +$/, "") : "");
    }
    while (rows.length && !rows[rows.length - 1]) rows.pop();
    return rows.join("\n");
  }

  // The part of the page the phone shows, in the coordinates of fixed elements: the keyboard, and on some phones the
  // browser's own toolbar, cover the rest.
  function shown() {
    const view = window.visualViewport;
    return view ? {top: view.offsetTop, bottom: view.offsetTop + view.height} : {top: 0, bottom: innerHeight};
  }

  // Keeps the bar and the text within what the phone shows: the bar at its bottom, the text between the terminal's
  // top and the bar, scrolling to its last line where the bar covers the terminal's bottom rows.
  function place() {
    const select = ui && ui.select;
    if (!select) return;
    const seen = shown(), area = document.getElementById("terminal").getBoundingClientRect();
    select.bar.style.bottom = Math.max(0, innerHeight - seen.bottom) + "px";
    const top = Math.max(0, area.top, seen.top);
    const bottom = Math.min(area.bottom, seen.bottom, select.bar.getBoundingClientRect().top);
    Object.assign(select.layer.style, {left: area.left + "px", top: top + "px", width: area.width + "px",
      height: Math.max(0, bottom - top) + "px"});
  }

  // Lays the text over the terminal's own grid: rows at the cell height, and characters spaced to the cell width,
  // which xterm rounds to whole pixels while the font's own advance usually is not. The text itself is replaced only
  // when it changed, as that drops any selection in it. Says whether it did.
  function drawScreen() {
    const select = ui.select, term = window.term;
    place();
    const area = document.getElementById("terminal").getBoundingClientRect();
    const grid = term.element.querySelector(".xterm-screen").getBoundingClientRect();
    const cellWidth = grid.width / term.cols, cellHeight = grid.height / term.rows;
    const top = select.layer.getBoundingClientRect().top;
    Object.assign(select.layer.style, {background: (term.options.theme || {}).background || "#000",
      color: (term.options.theme || {}).foreground || "#fff"});
    const pre = select.pre;
    // A spare row below the last: it scrolls clear of the bar's edge.
    Object.assign(pre.style, {fontFamily: term.options.fontFamily, fontSize: term.options.fontSize + "px",
      lineHeight: cellHeight + "px", letterSpacing: "0px", paddingLeft: Math.max(0, grid.left - area.left) + "px",
      paddingTop: Math.max(0, grid.top - top) + "px", paddingBottom: cellHeight + "px"});
    const probe = element("span", "", "W".repeat(40));
    pre.appendChild(probe);
    pre.style.letterSpacing = (cellWidth - probe.getBoundingClientRect().width / 40) + "px";
    probe.remove();
    const text = screenText(term);
    if (pre.textContent === text) return false;
    pre.textContent = text;
    select.chosen = "";
    return true;
  }

  // The run of non-blank characters under a point of the screen (a word, a path, a URL), as {row, col, text}.
  function wordAt(x, y) {
    const term = window.term, grid = term.element.querySelector(".xterm-screen").getBoundingClientRect();
    const row = Math.floor((y - grid.top) / (grid.height / term.rows));
    const cell = Math.floor((x - grid.left) / (grid.width / term.cols));
    const line = screenText(term).split("\n")[row] || "";
    const cells = term.buffer.active.getLine(term.buffer.active.viewportY + row);
    let col = -1;
    for (let i = 0, at = 0; cells && i <= cell; i++) {
      const char = cells.getCell(i);
      if (!char) break;
      if (char.getWidth()) [col, at] = [at, at + (char.getChars() || " ").length];
    }
    if (col < 0 || !/\S/.test(line[col] || "")) return null;
    let start = col, end = col + 1;
    while (start > 0 && /\S/.test(line[start - 1])) start--;
    while (end < line.length && /\S/.test(line[end])) end++;
    return {row, col: start, text: line.slice(start, end)};
  }

  // Selects that word where the redrawn screen now has it: the keyboard coming up for the press, or going down for the
  // copy view, has Zellij redraw the screen at another size, and the word may be gone for a moment.
  function selectWord() {
    const select = ui.select, word = select.word, lines = select.pre.textContent.split("\n");
    let best = null;
    lines.forEach((line, row) => {
      for (let col = line.indexOf(word.text); col >= 0; col = line.indexOf(word.text, col + 1)) {
        if (/\S/.test(line[col - 1] || "") || /\S/.test(line[col + word.text.length] || "")) continue;
        const distance = Math.abs(row - word.row) * 1000 + Math.abs(col - word.col);
        if (!best || distance < best.distance) best = {row, col, distance};
      }
    });
    if (!best) return;
    Object.assign(word, {row: best.row, col: best.col});
    const start = lines.slice(0, best.row).reduce((sum, line) => sum + line.length + 1, 0) + best.col;
    const range = document.createRange();
    range.setStart(select.pre.firstChild, start);
    range.setEnd(select.pre.firstChild, start + word.text.length);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
  }

  // The screen as it is when the copy view opens, held still for selecting. Right after opening or a resize,
  // Zellij's redraw may still be arriving, so for a moment it follows the terminal, keeping a long press's word
  // selected, until a selection of the owner's own starts.
  function redraw(resized) {
    const select = ui && ui.select;
    if (!select) return;
    if (resized) select.settleUntil = performance.now() + SETTLE_MS;
    else if (performance.now() > select.settleUntil || select.touched) return;
    const chosen = selectedText();
    if (!resized && chosen && !(select.word && chosen === select.word.text)) return;
    if (drawScreen() || !chosen) {
      if (select.word) selectWord();
    }
  }

  function openSelect(word) {
    if (ui.select) return closeSelect();
    const input = keyboardInput();
    // The phone's keyboard would cover the text. Past the guard: a long press is still a finger on the terminal.
    if (input) HTMLElement.prototype.blur.call(input);
    const select = {layer: element("div", "rk-screen " + CHROME), pre: element("pre"), bar: element("div", "rk-bar " + CHROME),
      status: element("span", "rk-status"), word: null, tap: null, touched: false, chosen: ""};
    select.layer.setAttribute("aria-label", "Terminal text");
    select.status.setAttribute("role", "status");
    select.layer.appendChild(select.pre);
    // Pressing these leaves the selection as it is, for Copy to take.
    select.bar.append(select.status, keepFocus(button("", "Select all", null, selectAll)),
      keepFocus(button("", "Copy", null, copySelection)), keepFocus(button("", "Done", null, closeSelect)));
    document.body.append(select.layer, select.bar);
    ui.select = select;
    ui.selectButton.setAttribute("aria-pressed", "true");
    const term = window.term;
    select.onResize = () => redraw(true);
    select.onCopy = event => copied(select, event);
    select.onSelectionChange = () => {
      const text = selectedText();
      if (text) select.chosen = text;
    };
    select.disposables = [term.onResize(select.onResize), term.onWriteParsed(() => redraw(false))];
    addEventListener("resize", select.onResize);
    // The keyboard or the browser's toolbar coming or going moves what the phone shows.
    if (window.visualViewport) {
      for (const type of ["resize", "scroll"]) visualViewport.addEventListener(type, place);
    }
    // The phone's own Copy, or ours: either way the text is on the clipboard, and the terminal can come back.
    document.addEventListener("copy", select.onCopy);
    // What was selected last: tapping a button can clear the phone's selection before the tap reaches the page.
    document.addEventListener("selectionchange", select.onSelectionChange);
    // A tap outside the selection is done with it; a press, a drag or a tap on the selection is still selecting.
    select.layer.addEventListener("pointerdown", event => {
      // From here the text holds still under the owner's own selecting.
      select.word = null;
      select.touched = true;
      select.tap = {x: event.clientX, y: event.clientY, t: performance.now(), on: onSelection(event.clientX, event.clientY)};
    });
    select.layer.addEventListener("pointercancel", () => { select.tap = null; });
    select.layer.addEventListener("pointerup", event => {
      const tap = select.tap;
      select.tap = null;
      if (tap && !tap.on && performance.now() - tap.t < TAP_MS &&
          Math.hypot(event.clientX - tap.x, event.clientY - tap.y) < TAP_PX) closeSelect();
    });
    select.word = word;
    redraw(true);
  }

  function closeSelect() {
    const select = ui && ui.select;
    if (!select) return;
    for (const disposable of select.disposables) disposable.dispose();
    removeEventListener("resize", select.onResize);
    if (window.visualViewport) {
      for (const type of ["resize", "scroll"]) visualViewport.removeEventListener(type, place);
    }
    document.removeEventListener("copy", select.onCopy);
    document.removeEventListener("selectionchange", select.onSelectionChange);
    select.layer.remove();
    select.bar.remove();
    ui.select = null;
    ui.selectButton.setAttribute("aria-pressed", "false");
  }

  function selectAll() {
    const range = document.createRange();
    range.selectNodeContents(ui.select.pre);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
  }

  function selectedText() {
    const selection = getSelection();
    return selection.rangeCount && ui.select.layer.contains(selection.anchorNode) ? selection.toString() : "";
  }

  function onSelection(x, y) {
    const selection = getSelection();
    if (!selectedText()) return false;
    const slop = 12;  // Room for a fingertip, and for the handles at either end.
    return [...selection.getRangeAt(0).getClientRects()].some(box =>
      x >= box.left - slop && x <= box.right + slop && y >= box.top - slop && y <= box.bottom + slop);
  }

  // What Copy takes: the selection, or the one last made if a tap has cleared it since. Never the whole screen unless
  // Select all chose it.
  function chosenText() {
    return selectedText() || ui.select.chosen;
  }

  // Called while the copy goes ahead: the clipboard gets exactly the chosen text, whatever the selection is now.
  function copied(select, event) {
    const text = chosenText();
    if (!text) return;
    event.clipboardData.setData("text/plain", text);
    event.preventDefault();
    ui.copiedAt = performance.now();
    setTimeout(() => {
      if (!ui || ui.select !== select) return;
      closeSelect();
      toast("Copied");
    });
  }

  // A line in the bar, which grows the bar: the text above it is placed again.
  function say(select, text) {
    select.status.textContent = text;
    place();
  }

  // Runs inside the tap, which every phone browser requires before it lets a page write the clipboard.
  async function copySelection() {
    const select = ui.select, text = chosenText();
    if (!text) {
      say(select, "Select some text first: press on a word and drag the handles.");
      return;
    }
    let done = false;
    ui.copiedAt = performance.now();
    try {
      await navigator.clipboard.writeText(text);
      done = true;
    } catch {
      try { done = document.execCommand("copy"); } catch {}
    }
    if (!ui || ui.select !== select) return;
    if (!done) {
      say(select, "Copying was blocked: press the selected text and choose Copy.");
      return;
    }
    closeSelect();
    toast("Copied");
  }

  // A long press on the terminal opens the copy view on the pressed word, in place of Zellij's right click. The word
  // is the one under the finger as it lands, before Zellij's own pointerdown would focus its input and so redraw.
  // From here until the finger lifts, Zellij's focus calls wait (guardKeyboard); a press anywhere else ends that.
  function onPointerDown(event) {
    if (!ui) return;
    if (event.pointerType !== "touch" || !document.getElementById("terminal").contains(event.target)) {
      ui.touch = null;
      return;
    }
    guardKeyboard();
    ui.touch = {tap: false, enabled: !!window.__zjSoftKbdEnabled};
    ui.landed = {x: event.clientX, y: event.clientY, word: wordAt(event.clientX, event.clientY)};
  }
  function onTouchStart(event) {
    cancelPress();
    const landed = ui && ui.landed;
    if (ui) ui.landed = null;
    if (!ui || event.touches.length !== 1) return;
    const touch = event.touches[0], x = touch.clientX, y = touch.clientY;
    const word = landed && Math.hypot(landed.x - x, landed.y - y) < 1 ? landed.word : wordAt(x, y);
    ui.press = {x, y, word, fired: false, timer: setTimeout(longPress, LONG_PRESS_MS)};
  }
  function onTouchMove(event) {
    const press = ui && ui.press;
    if (!press || press.fired) return;
    const touch = event.touches[0];
    if (event.touches.length !== 1 || Math.hypot(touch.clientX - press.x, touch.clientY - press.y) > MOVE_PX) cancelPress();
  }
  function onTouchEnd(event) {
    const press = ui && ui.press;
    // Zellij would bring the keyboard back on release, which on an iPhone also clears the selection.
    if (press && press.fired) event.stopPropagation();
    // One finger that lifts where it landed, before the long press: a tap, for which Zellij may bring the keyboard up.
    else if (press && ui.touch && !event.touches.length) ui.touch.tap = true;
    if (ui && !event.touches.length) endTouch();
    cancelPress();
  }
  function cancelPress() {
    const press = ui && ui.press;
    if (!press) return;
    clearTimeout(press.timer);
    ui.press = null;
    if (press.fired && ui.select) ui.select.layer.style.pointerEvents = "";
  }
  function longPress() {
    const press = ui.press;
    press.fired = true;
    // Ends Zellij's own handling of this touch: its long press would send a right click, its release a click.
    document.getElementById("terminal").dispatchEvent(new Event("touchcancel"));
    if (typeof navigator.vibrate === "function") navigator.vibrate(10);
    if (ui.select) return;
    openSelect(press.word);
    // The finger is still down where the text now is: until it lifts, the phone's own long press must not select
    // there too, nor stretch the word's selection to wherever the finger ends up.
    ui.select.layer.style.pointerEvents = "none";
  }

  // The terminal's own copy (OSC 52, as Zellij's copy on select sends) would replace what the copy view has just put
  // on the clipboard: while the view is open and for a moment after a copy from it, the clipboard is left as it is.
  function onTerminalCopy() {
    return !!ui && (!!ui.select || performance.now() - ui.copiedAt < QUIET_MS);
  }

  function paste(text) { if (text) window.term.paste(text); }

  // The clipboard first: Safari and Firefox show their own Paste button for it, Chrome asks once. Where that is
  // refused, a text box the phone's own paste menu works in.
  async function pasteClipboard() {
    closeMenu();
    closeSelect();
    let text = "";
    try { text = await navigator.clipboard.readText(); } catch {}
    if (!ui) return;
    if (text) paste(text);
    else openSheet();
  }

  function openSheet() {
    if (ui.sheet) return ui.sheet.querySelector("textarea").focus();
    const sheet = element("div", "rk-sheet " + CHROME), box = element("textarea"), row = element("div", "rk-row");
    box.setAttribute("aria-label", "Text to paste");
    box.placeholder = "Press here and choose Paste, then Send";
    box.autocapitalize = "off";
    box.spellcheck = false;
    box.setAttribute("autocorrect", "off");
    row.append(button("", "Send", null, () => {
      const text = box.value;
      closeSheet();
      paste(text);
      const input = keyboardInput();
      if (input && window.__zjSoftKbdEnabled) input.focus({preventScroll: true});
    }), button("", "Cancel", null, closeSheet));
    sheet.append(box, row);
    document.body.appendChild(sheet);
    ui.sheet = sheet;
    box.focus();
  }

  function closeSheet() {
    if (!ui || !ui.sheet) return;
    ui.sheet.remove();
    ui.sheet = null;
  }

  function closeMenu() {
    const menu = document.querySelector(".zj-mobile-menu");
    if (menu) menu.classList.remove("zj-open");
  }

  function install() {
    const topbar = document.querySelector(".zj-mobile-topbar"), modbar = document.querySelector(".zj-mobile-modbar"),
      terminal = document.getElementById("terminal");
    if (!topbar || !modbar || !terminal) return;
    ui = {select: null, sheet: null, press: null, landed: null, touch: null, toast: element("div", "rk-toast " + CHROME), toastTimer: 0,
      listening: new AbortController(), copiedAt: -Infinity};
    // Before xterm's clipboard addon, which handles OSC 52 only when this one passes it on.
    ui.terminalCopy = window.term.parser.registerOscHandler(52, onTerminalCopy);
    ui.toast.setAttribute("role", "status");
    ui.selectButton = button("rk-select", "", "Select text to copy", () => openSelect(null));
    ui.selectButton.setAttribute("aria-pressed", "false");
    ui.selectButton.innerHTML = COPY_ICON;
    ui.pasteButton = keepFocus(button("rk-paste", "", "Paste", pasteClipboard));
    ui.pasteButton.innerHTML = PASTE_ICON;
    const hamburger = topbar.querySelector(".zj-hamburger");
    topbar.insertBefore(ui.selectButton, hamburger);
    topbar.insertBefore(ui.pasteButton, hamburger);
    ui.pasteKey = keepFocus(button("rk-paste", "PASTE", "Paste", pasteClipboard));
    modbar.appendChild(ui.pasteKey);
    document.body.appendChild(ui.toast);
    // Capturing, so these see each touch before Zellij's own handlers on the terminal and on the window do.
    const options = {capture: true, passive: true, signal: ui.listening.signal};
    terminal.addEventListener("touchstart", onTouchStart, options);
    terminal.addEventListener("touchmove", onTouchMove, options);
    terminal.addEventListener("touchend", onTouchEnd, options);
    terminal.addEventListener("touchcancel", event => {
      if (!event.isTrusted) return;
      cancelPress();
      endTouch();
    }, options);
    guardKeyboard();
  }

  function uninstall() {
    closeSelect();
    closeSheet();
    cancelPress();
    unguardKeyboard();
    ui.listening.abort();
    ui.terminalCopy.dispose();
    clearTimeout(ui.toastTimer);
    for (const node of [ui.selectButton, ui.pasteButton, ui.pasteKey, ui.toast]) node.remove();
    ui = null;
  }

  // Zellij builds its mobile UI after the terminal and turns it on and off with a class on <body> (the phone's
  // Switch to Desktop, or a resize past its mobile width).
  function sync() {
    const on = document.body.classList.contains("zj-mobile-active") && !!window.term;
    if (on && !ui) install();
    else if (!on && ui) uninstall();
  }
  // Registered before the web client's own scripts run, so this sees each touch before its pointerdown handler.
  addEventListener("pointerdown", onPointerDown, {capture: true, passive: true});
  new MutationObserver(sync).observe(document.body, {childList: true, attributes: true, attributeFilter: ["class"]});
  sync();
})();
