// Copy the terminal emulator's browser builds into ui/vendor. The window
// loads plain files, so there is no bundler step.
import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const out = join(root, "ui", "vendor");
mkdirSync(out, { recursive: true });
for (const file of [
  "@xterm/xterm/lib/xterm.js",
  "@xterm/xterm/css/xterm.css",
  "@xterm/addon-fit/lib/addon-fit.js",
  "@xterm/addon-unicode11/lib/addon-unicode11.js",
  "@xterm/addon-web-links/lib/addon-web-links.js",
  "@xterm/addon-webgl/lib/addon-webgl.js",
]) {
  copyFileSync(join(root, "node_modules", file), join(out, file.split("/").pop()));
}
