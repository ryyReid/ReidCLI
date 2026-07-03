"use strict";

const { spawnSync } = require("node:child_process");

const CANDIDATES =
  process.platform === "win32"
    ? [["py", ["-3"]], ["python", []], ["python3", []]]
    : [["python3", []], ["python", []]];

function commandWorks(cmd, args) {
  const result = spawnSync(cmd, [...args, "--version"], { stdio: "ignore" });
  return !result.error && result.status === 0;
}

function findPython() {
  for (const [cmd, args] of CANDIDATES) {
    if (commandWorks(cmd, args)) {
      return { cmd, args };
    }
  }
  return null;
}

module.exports = { findPython };
