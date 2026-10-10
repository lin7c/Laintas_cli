// The Laintas terminal window: an emulator wired to the CLI through the host
// process. Every byte the CLI writes is drawn here, so colors, fonts and
// mouse support do not depend on the Windows version or its console.
(() => {
  const { invoke } = window.__TAURI__.core;
  const { listen } = window.__TAURI__.event;
  const appWindow = window.__TAURI__.window.getCurrentWindow();

  // The window is always a soft dark gray. A white background washed out
  // several of the CLI's colors, and the CLI asks the terminal for its
  // background and picks its text palette to match, so a gray one gets the
  // dark palette whatever the Windows theme is.
  const THEME = {
    background: "#22272e", foreground: "#d6dee6", cursor: "#d6dee6", cursorAccent: "#22272e",
    selectionBackground: "#3b5070",
    black: "#545d68", red: "#f47067", green: "#57ab5a", yellow: "#c69026",
    blue: "#539bf5", magenta: "#b083f0", cyan: "#39c5cf", white: "#909dab",
    brightBlack: "#636e7b", brightRed: "#ff938a", brightGreen: "#6bc46d", brightYellow: "#daaa3f",
    brightBlue: "#6cb6ff", brightMagenta: "#dcbdfb", brightCyan: "#56d4dd", brightWhite: "#cdd9e5",
  };

  const term = new Terminal({
    allowProposedApi: true,
    cursorBlink: true,
    fontFamily: '"Cascadia Mono", "Cascadia Code", Consolas, "Microsoft YaHei UI", "Microsoft YaHei", monospace',
    fontSize: 14,
    scrollback: 20000,
    theme: THEME,
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  // Width tables that match what the CLI computes for CJK text and emoji.
  term.loadAddon(new Unicode11Addon.Unicode11Addon());
  term.unicode.activeVersion = "11";
  // Ctrl+click opens a link in the default browser; a plain click stays
  // with the program on screen, which may be using the mouse itself.
  term.loadAddon(new WebLinksAddon.WebLinksAddon((event, uri) => {
    if (event.ctrlKey) invoke("open_url", { url: uri }).catch(() => {});
  }));
  term.open(document.getElementById("terminal"));
  try {
    const webgl = new WebglAddon.WebglAddon();
    webgl.onContextLoss(() => webgl.dispose());
    term.loadAddon(webgl);
  } catch (_) {
    // The default renderer stays in place where WebGL is unavailable.
  }

  const encoder = new TextEncoder();
  const send = (bytes) => invoke("term_input", { data: Array.from(bytes) }).catch(() => {});
  let running = false;

  term.onData((text) => { if (running) send(encoder.encode(text)); });
  term.onBinary((text) => { if (running) send(Uint8Array.from(text, (c) => c.charCodeAt(0))); });
  term.onResize(({ rows, cols }) => { if (running) invoke("term_resize", { rows, cols }).catch(() => {}); });
  term.onTitleChange((title) => { appWindow.setTitle(title || "Laintas").catch(() => {}); });

  // Ctrl+C copies when text is selected and interrupts otherwise. Returning
  // false leaves the key to the webview, whose copy and paste events the
  // emulator already handles (paste keeps bracketed-paste framing).
  term.attachCustomKeyEventHandler((event) => {
    if (event.type !== "keydown" || !event.ctrlKey || event.altKey) return true;
    const key = event.key.toLowerCase();
    if (key === "c" && (event.shiftKey || term.hasSelection())) {
      document.execCommand("copy");
      term.clearSelection();
      return false;
    }
    if (key === "v") return false;
    return true;
  });
  document.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    if (term.hasSelection()) {
      document.execCommand("copy");
      term.clearSelection();
    } else if (running) {
      navigator.clipboard.readText().then((text) => term.paste(text)).catch(() => {});
    }
  });

  const decode = (text) => Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
  let pending = Promise.resolve();
  const start = () => {
    fit.fit();
    pending = invoke("term_start", { rows: term.rows, cols: term.cols })
      .then(() => { running = true; })
      .catch((error) => { term.writeln(`\r\n\x1b[31mLaintas could not start: ${error}\x1b[0m`); });
  };

  listen("term:output", (event) => term.write(decode(event.payload)));
  listen("term:exit", (event) => {
    running = false;
    if (event.payload === 0) {
      appWindow.close();
      return;
    }
    term.writeln(`\r\n\x1b[2m[Laintas exited with status ${event.payload}. Press Enter to start again.]\x1b[0m`);
    const restart = term.onKey(({ domEvent }) => {
      if (domEvent.key !== "Enter") return;
      restart.dispose();
      term.reset();
      start();
    });
  });

  let resizeTimer = 0;
  new ResizeObserver(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => fit.fit(), 30);
  }).observe(document.getElementById("terminal"));

  // Fonts change the cell size, so measure only after they have loaded.
  document.fonts.ready.then(() => { start(); term.focus(); });
  window.addEventListener("focus", () => term.focus());
  // Used by the automated window test to read the screen back.
  window.__laintasTerminal = term;
})();
