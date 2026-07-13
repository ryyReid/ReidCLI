"use strict";

/**
 * npm postinstall:
 *  1. pip install the Python package (so `python -m reidx` works)
 *  2. seed ~/.reidcli/settings.json so global `npm i -g` installs are usable
 *     without copying files out of node_modules
 */

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { findPython } = require("./find-python");

const PACKAGE_ROOT = path.join(__dirname, "..");
const STORAGE_DIRNAME = ".reidcli";
const SETTINGS_NAME = "settings.json";

function defaultSettings() {
  return {
    _comment:
      "ReidX user settings. Edit this file or use /connect and /model in the TUI. " +
      "Location: ~/.reidcli/settings.json  |  empty env values are ignored.",
    env: {
      ANTHROPIC_API_KEY: "",
      ANTHROPIC_BASE_URL: "",
      ANTHROPIC_MODEL: "",
      OPENAI_API_KEY: "",
      OPENAI_BASE_URL: "",
      OPENAI_MODEL: "",
    },
    theme: "dark",
    effortLevel: "medium",
    reidx: {
      default_provider: "stub",
      log_level: "WARNING",
      policy: {
        default_mode: "balanced",
        shell_timeout_seconds: 60,
      },
      providers: {},
    },
  };
}

function userSettingsPath() {
  const override = (process.env.REIDX_STORAGE || "").trim();
  const root = override
    ? path.resolve(override.replace(/^~(?=$|[/\\])/, os.homedir()))
    : path.join(os.homedir(), STORAGE_DIRNAME);
  return { root, file: path.join(root, SETTINGS_NAME) };
}

function seedSettings() {
  const { root, file } = userSettingsPath();
  try {
    fs.mkdirSync(root, { recursive: true });
  } catch (err) {
    console.warn("reidx: could not create settings directory:", root, err.message);
    return null;
  }
  if (fs.existsSync(file)) {
    try {
      const raw = fs.readFileSync(file, "utf8").trim();
      if (raw) {
        JSON.parse(raw);
        return file; // already valid
      }
    } catch {
      // corrupt → rewrite below
    }
  }
  try {
    fs.writeFileSync(file, JSON.stringify(defaultSettings(), null, 2) + "\n", "utf8");
    console.log("reidx: created user settings →", file);
    return file;
  } catch (err) {
    console.warn("reidx: could not write settings:", file, err.message);
    return null;
  }
}

function inVirtualEnv() {
  return Boolean(process.env.VIRTUAL_ENV || process.env.CONDA_PREFIX);
}

function pipInstall(python) {
  // `--user` fails inside an active venv; install into the active env instead.
  const args = [...python.args, "-m", "pip", "install", "--quiet"];
  if (!inVirtualEnv()) {
    args.push("--user");
  }
  args.push(PACKAGE_ROOT);
  const result = spawnSync(python.cmd, args, { stdio: "inherit" });
  return !result.error && result.status === 0;
}

function main() {
  // Always seed settings first — works even if Python/pip is missing.
  const settings = seedSettings();

  const python = findPython();
  if (!python) {
    console.warn(
      "reidx: no Python 3.12+ interpreter found on PATH — skipping pip install.\n" +
        "Install Python, then run: pip install --user " +
        JSON.stringify(PACKAGE_ROOT) +
        (settings ? `\nSettings ready at: ${settings}` : "")
    );
    return;
  }

  console.log("reidx: installing Python package (reidx) via pip...");
  if (!pipInstall(python)) {
    console.warn(
      "reidx: automatic `pip install` failed.\n" +
        "Run it manually: " +
        `${python.cmd} ${python.args.join(" ")} -m pip install --user ${JSON.stringify(PACKAGE_ROOT)}`
    );
  } else if (settings) {
    console.log("reidx: ready. Edit settings if needed:", settings);
    console.log('reidx: then run `reid` (or `npx reid`). Use /connect to add a provider.');
  }
}

main();
