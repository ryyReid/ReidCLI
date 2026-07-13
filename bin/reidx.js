#!/usr/bin/env node
"use strict";

const { spawnSync } = require("node:child_process");
const { findPython } = require("../scripts/find-python");

function main() {
  const python = findPython();
  if (!python) {
    console.error(
      "reidx: no Python 3.12+ interpreter found on PATH.\n" +
        "Install Python (https://www.python.org/downloads/) and re-run `npm install -g reidx`."
    );
    process.exit(1);
  }

  // Inherit the caller's environment so PowerShell / Windows Terminal theme
  // vars (WT_SESSION, COLORTERM, NO_COLOR, etc.) reach Python. Only pin
  // UTF-8 for *this* child process — never rewrite the parent shell.
  const env = { ...process.env };
  if (!env.PYTHONIOENCODING) {
    env.PYTHONIOENCODING = "utf-8";
  }
  if (!env.PYTHONUTF8 && process.platform === "win32") {
    env.PYTHONUTF8 = "1";
  }

  const result = spawnSync(
    python.cmd,
    [...python.args, "-m", "reidx", ...process.argv.slice(2)],
    { stdio: "inherit", env }
  );

  if (result.error) {
    console.error(
      `reidx: failed to launch Python (${python.cmd}): ${result.error.message}`
    );
    process.exit(1);
  }

  process.exit(result.status === null ? 1 : result.status);
}

main();
