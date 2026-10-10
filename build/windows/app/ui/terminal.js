// The Laintas terminal window: an emulator wired to the CLI through the host
// process. Every byte the CLI writes is drawn here, so colors, fonts and
// mouse support do not depend on the Windows version or its console.
(() => {
  const { invoke } = window.__TAURI__.core;
  const { listen } = window.__TAURI__.event;
  const appWindow = window.__TAURI__.window.getCurrentWindow();

  // The CLI asks the terminal for its background and picks its text palette
  // to match, so these two schemes are the only place the window sets color.
  const THEMES = {
    dark: {
      background: "#0d1117", foreground: "#e6edf3", cursor: "#e6edf3", cursorAccent: "#0d1117",
      selectionBackground: "#264f78",
      black: "#484f58", red: "#ff7b72", green: "#3fb950", yellow: "#d29922",
      blue: "#58a6ff", magenta: "#bc8cff", cyan: "#39c5cf", white: "#b1bac4",
      brightBlack: "#6e7681", brightRed: "#ffa198", brightGreen: "#56d364", brightYellow: "#e3b341",
      brightBlue: "#79c0ff", brightMagenta: "#d2a8ff", brightCyan: "#56d4dd", brightWhite: "#f0f6fc",
    },
    light: {
      background: "#ffffff", foreground: "#24292f", cursor: "#24292f", cursorAccent: "#ffffff",
      selectionBackground: "#b6d7ff",
      black: "#24292f", red: "#cf222e", green: "#116329", yellow: "#4d2d00",
      blue: "#0969da", magenta: "#8250df", cyan: "#1b7c83", white: "#6e7781",
      brightBlack: "#57606a", brightRed: "#a40e26", brightGreen: "#1a7f37", brightYellow: "#633c01",
      brightBlue: "#218bff", brightMagenta: "#a475f9", brightCyan: "#3192aa", brightWhite: "#8c959f",
    },
  };
  const scheme = window.matchMedia("(prefers-color-scheme: light)");
  const theme = () => (scheme.matches ? THEMES.light : THEMES.dark);

  const term = new Terminal({
    allowProposedApi: true,
    cursorBlink: true,
    fontFamily: '"Cascadia Mono", "Cascadia Code", Consolas, "Microsoft YaHei UI", "Microsoft YaHei", monospace',
    fontSize: 14,
    scrollback: 20000,
    theme: theme(),
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
  scheme.addEventListener("change", () => { term.options.theme = theme(); });

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
