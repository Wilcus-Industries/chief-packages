// Open chief's browser profile in a visible Chrome window so the owner can log
// in. Usage: node login.mjs <server_dir> <profile_dir> [url]
//
// Must be launched through Playwright, not plain Chrome: Playwright starts
// Chrome with --use-mock-keychain, so cookies saved by a normal Chrome launch
// would not decrypt for chief (and vice versa).
import { createRequire } from "node:module";
import path from "node:path";

const [serverDir, profileDir, url] = process.argv.slice(2);
if (!serverDir || !profileDir) {
  console.error("usage: node login.mjs <server_dir> <profile_dir> [url]");
  process.exit(2);
}
const require = createRequire(path.join(serverDir, "package.json"));
const { chromium } = require("playwright-core");

let context;
try {
  context = await chromium.launchPersistentContext(profileDir, {
    channel: "chrome",
    headless: false,
    viewport: null,
  });
} catch (err) {
  console.error(
    "could not open the profile — if chief's browser is running it holds the " +
      "profile lock: ask chief to close the browser (browser_close), or restart " +
      "chief, then retry.\n" + err.message,
  );
  process.exit(1);
}
const page = context.pages()[0] ?? (await context.newPage());
if (url) await page.goto(url);
console.log("Log in to whatever chief should use, then close the window to save.");
await new Promise((resolve) => context.on("close", resolve));
