// Drive the built terminal window on a real Windows machine and check what
// it draws. The window is opened with the WebView2 debugging port enabled;
// this script reads the emulator's screen back and types through the same
// path a keyboard does (page -> native host -> terminal host -> pty).
//
//   node scripts/window_test.mjs <laintas-terminal.exe> <payload-root> <out-dir>
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const [exe, root, outDir] = process.argv.slice(2);
if (!exe || !root || !outDir) {
  console.error("usage: window_test.mjs <exe> <payload-root> <out-dir>");
  process.exit(2);
}
mkdirSync(outDir, { recursive: true });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const SCREEN = `(() => {
  const t = window.__laintasTerminal;
  if (!t) return null;
  const b = t.buffer.active, lines = [];
  for (let i = 0; i < b.length; i++) lines.push(b.getLine(i).translateToString(true));
  return { rows: t.rows, cols: t.cols, text: lines.join("\\n").trimEnd() };
})()`;

async function openWindow(port, extraEnv) {
  const child = spawn(exe, [], {
    env: {
      ...process.env, ...extraEnv,
      LAINTAS_TERMINAL_ROOT: root,
      LAINTAS_TERMINAL_DEBUG_PORT: String(port),
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${port}`,
    },
    stdio: "ignore",
  });
  let exited = null;
  let lastError = "";
  child.on("exit", (code, signal) => { exited = `code ${code}, signal ${signal}`; });
  child.on("error", (error) => { exited = `spawn error: ${error.message}`; });
  let target = null;
  for (let i = 0; i < 120 && !target; i++) {
    await sleep(500);
    try {
      const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      target = pages.find((p) => p.type === "page" && p.webSocketDebuggerUrl);
    } catch (error) { lastError = String(error.cause?.code || error.message); }
    if (exited) break;
  }
  if (!target) {
    console.log(`window process: ${exited ? `exited (${exited})` : "still running"}; last probe error: ${lastError}`);
    if (process.platform === "win32") {
      const report = spawnSync("powershell", ["-NoProfile", "-Command",
        "Get-Process laintas-terminal,msedgewebview2,bash,python3* -ErrorAction SilentlyContinue | Format-Table Id,ProcessName,MainWindowTitle -AutoSize | Out-String -Width 200; " +
        `Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -ge 9000 -and $_.LocalPort -le 9999 } | Format-Table LocalAddress,LocalPort,OwningProcess -AutoSize | Out-String; ` +
        "Get-WinEvent -FilterHashtable @{LogName='Application'; Level=2; StartTime=(Get-Date).AddMinutes(-3)} -MaxEvents 3 -ErrorAction SilentlyContinue | Format-List TimeCreated,ProviderName,Message | Out-String -Width 300"],
        { encoding: "utf8" });
      console.log(report.stdout || report.stderr);
    }
    if (!exited) spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
    throw new Error("the window never exposed a debuggable page");
  }
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  let nextId = 0;
  const waiting = new Map();
  socket.onmessage = (message) => {
    const data = JSON.parse(message.data);
    if (data.id && waiting.has(data.id)) {
      const { resolve, reject } = waiting.get(data.id);
      waiting.delete(data.id);
      data.error ? reject(new Error(data.error.message)) : resolve(data.result);
    }
  };
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++nextId;
    waiting.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expression) => {
    const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || "page error");
    return result.result.value;
  };
  return {
    url: target.url,
    evaluate,
    screen: () => evaluate(SCREEN),
    type: (text) => evaluate(`window.__laintasTerminal.input(${JSON.stringify(text)}, true)`),
    async waitFor(predicate, label, timeoutMs = 60000) {
      const deadline = Date.now() + timeoutMs;
      let screen = null;
      while (Date.now() < deadline) {
        screen = await this.screen();
        if (screen && predicate(screen)) return screen;
        await sleep(300);
      }
      throw new Error(`timed out waiting for ${label}\n--- screen ---\n${screen ? screen.text : "(no terminal)"}`);
    },
    async screenshot(name) {
      const shot = await send("Page.captureScreenshot", { format: "png" });
      writeFileSync(join(outDir, name), Buffer.from(shot.data, "base64"));
    },
    close() {
      socket.close();
      spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
    },
  };
}

const failures = [];
const check = (ok, label, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? `  (${detail})` : ""}`);
  if (!ok) failures.push(label);
};

async function shellPass() {
  console.log("== window + host + runtime, with a bare shell");
  const win = await openWindow(9333, { LAINTAS_TERMINAL_SHELL: "1" });
  try {
    console.log(`page ${win.url}`);
    let screen = await win.waitFor((s) => /[$#] ?$/.test(s.text), "a shell prompt");
    check(true, "the shell prompt is drawn", `${screen.rows}x${screen.cols}`);
    await win.type("stty size; uname -s; printf '\\033[38;2;63;185;80mcolor\\033[0m \\344\\270\\255\\346\\226\\207|\\n'; echo ready-$((6*7))\r");
    screen = await win.waitFor((s) => s.text.includes("ready-42"), "command output");
    check(screen.text.includes(`${screen.rows} ${screen.cols}`), "the pty has the window's size", `${screen.rows} ${screen.cols}`);
    check(/MSYS|CYGWIN/.test(screen.text), "commands run in the bundled runtime");
    const cell = await win.evaluate(`(() => {
      const t = window.__laintasTerminal, b = t.buffer.active;
      for (let y = 0; y < b.length; y++) {
        const line = b.getLine(y), text = line.translateToString(true);
        const x = text.indexOf("color ");
        if (x === 0) {
          const c = line.getCell(0), wide = line.getCell(6);
          return { rgb: c.isFgRGB(), fg: c.getFgColor(), wideChars: wide.getChars(), wideWidth: wide.getWidth() };
        }
      }
      return null;
    })()`);
    check(!!cell && cell.rgb && cell.fg === 0x3fb950, "24-bit color arrives unchanged", JSON.stringify(cell));
    check(!!cell && cell.wideChars === "中" && cell.wideWidth === 2, "CJK text is decoded and double-width");
    await win.evaluate("window.__laintasTerminal.resize(90, 28)");
    await win.type("clear; stty size; echo resized-$((1+1))\r");
    screen = await win.waitFor((s) => s.text.includes("resized-2"), "output after a resize");
    check(screen.text.includes("28 90"), "a resize reaches the pty");
    await win.type("\x03");
    await win.type("echo alive-$((2+3))\r");
    await win.waitFor((s) => s.text.includes("alive-5"), "a prompt after Ctrl+C");
    check(true, "Ctrl+C leaves the shell usable");
    await win.screenshot("shell.png");
  } finally {
    win.close();
  }
}

async function cliPass() {
  console.log("== the CLI itself, first run with no account");
  const win = await openWindow(9334, {});
  try {
    const screen = await win.waitFor((s) => s.text.trim().length > 0, "the CLI to draw anything", 120000);
    await sleep(8000);
    const settled = await win.screen();
    // The sign-in link carries a one-time secret; keep it out of the log.
    const shown = settled.text.replace(/https?:\/\/\S+(\n\S*(secret|device_id)\S*)*/g, "<url>");
    console.log(`--- screen ${settled.rows}x${settled.cols} ---\n${shown}\n---`);
    check(!/Traceback|Error:|could not start/.test(settled.text), "the CLI starts without an error on screen");
    check(/laintas/i.test(settled.text) || screen.text.length > 0, "the CLI draws its first screen");
    await win.screenshot("cli.png");
  } finally {
    win.close();
  }
}

try {
  await shellPass();
  await cliPass();
} catch (error) {
  console.error(String(error.stack || error));
  failures.push("exception");
}
console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
