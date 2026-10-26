// SPDX-License-Identifier: AGPL-3.0-only
"use strict";
// A full-screen button for Zellij 0.45.1's web client, added by the broker to the terminal page on every device.
// It puts the whole page in full screen (the Fullscreen API) and takes it out again, and shows which it is: the
// browser's own way out (Esc on a computer, its own exit on a phone) changes it too. The web client re-fits the
// terminal to the window's new size itself. On a computer or tablet the button floats in the top right corner, faint
// until the pointer is on it; on a phone it sits in the web client's top bar, beside the copy and paste buttons, and
// a tap on it leaves the keyboard as it was. While the page is in full screen it is always on screen, to leave by:
// where a phone's top bar is covered (a menu page of the web client), it floats above that. A browser without full
// screen for a page (Safari on an iPhone) gets no button.
(() => {
  const style = document.createElement("style");
  style.textContent = `
    .rk-fullscreen { display: flex; align-items: center; justify-content: center; cursor: pointer; padding: 0;
      color: var(--zj-text, #ccc); background: transparent; border: 0; }
    .rk-fullscreen svg { width: 20px; height: 20px; }
    .rk-fullscreen.rk-float { position: fixed; top: 6px; right: 6px; z-index: 720; width: 32px; height: 32px;
      border: 1px solid var(--zj-border, #555); border-radius: 4px; background: var(--zj-bg, #000); opacity: 0.45; }
    .rk-fullscreen.rk-float:hover, .rk-fullscreen.rk-float:focus-visible { opacity: 1; }
    .zj-mobile-topbar .rk-fullscreen { flex: 0 0 auto; min-width: 40px; padding: 0 8px; }`;
  document.head.appendChild(style);
  const ICON = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round"><path d="';
  const ENTER_ICON = ICON + 'M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/></svg>';
  const EXIT_ICON = ICON + 'M9 4v5H4M20 9h-5V4M15 20v-5h5M4 15h5v5"/></svg>';

  // Safari before 16.4 (and iPads on it) has only the prefixed API.
  const page = document.documentElement;
  const supported = () => !!(document.fullscreenEnabled || document.webkitFullscreenEnabled);
  const full = () => !!(document.fullscreenElement || document.webkitFullscreenElement);

  const control = document.createElement("button");
  control.type = "button";
  // Like Zellij's own controls: the web client does not bring the phone's keyboard up for a tap on it, and pressing
  // it leaves the focus, and so the keyboard, where it is.
  control.className = "rk-fullscreen zj-mobile-chrome";
  for (const type of ["pointerdown", "mousedown"]) control.addEventListener(type, event => event.preventDefault());
  control.addEventListener("click", () => {
    try {
      const done = full() ? (document.exitFullscreen || document.webkitExitFullscreen).call(document)
        : page.requestFullscreen ? page.requestFullscreen({navigationUI: "hide"}) : page.webkitRequestFullscreen();
      if (done && done.catch) done.catch(() => {});  // Refused: the page stays as it is, and so does the button.
    } catch {}
  });

  // The web client turns its phone UI on and off with a class on <body>, builds it in #zj-mobile-root after the page
  // loads, and opens its menu pages over the top bar with a class on each. The terminal itself is not watched.
  let root = null;
  const watcher = new MutationObserver(render);
  watcher.observe(document.body, {childList: true, attributes: true, attributeFilter: ["class"]});

  // Where the button goes now: the phone's top bar while it shows, otherwise floating; floating on a phone only in
  // full screen, as the way out.
  function render() {
    const body = document.body, topbar = document.querySelector(".zj-mobile-topbar");
    const built = document.getElementById("zj-mobile-root");
    if (built && built !== root) {
      root = built;
      watcher.observe(root, {subtree: true, childList: true, attributes: true, attributeFilter: ["class"]});
    }
    const phone = body.classList.contains("zj-mobile-active"), on = full();
    const barShown = phone && topbar && !body.classList.contains("zj-mobile-standalone") &&
      !document.querySelector(".zj-mobile-overlay.zj-open");
    const home = !supported() ? null : barShown ? topbar : (!phone || on) ? body : null;
    if (!home) {
      control.remove();
      return;
    }
    control.classList.toggle("rk-float", home === body);
    const before = home === topbar ? topbar.querySelector(".zj-hamburger") : null;
    if (control.parentNode !== home || (before && control.nextSibling !== before)) home.insertBefore(control, before);
    if (control.getAttribute("aria-pressed") !== String(on)) {
      control.setAttribute("aria-pressed", String(on));
      control.setAttribute("aria-label", on ? "Exit full screen" : "Full screen");
      control.title = on ? "Exit full screen" : "Full screen";
      control.innerHTML = on ? EXIT_ICON : ENTER_ICON;
    }
  }

  for (const type of ["fullscreenchange", "webkitfullscreenchange"]) document.addEventListener(type, render);
  addEventListener("resize", render);
  render();
})();
