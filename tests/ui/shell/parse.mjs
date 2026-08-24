// Parses every JS file it is handed and prints, per file, whether it is
// syntactically valid and what it imports. No file is EXECUTED: page modules
// reach for document and location at import time, and the shell's main.mjs
// would open a window.
//
// vm.SourceTextModule rather than `node --check`, which is not a check at all
// for these files: given module syntax it exits 0 without parsing, so a file
// with a plain syntax error passes (measured on node 26.0.0, 2026-08-25).
// Green for the wrong reason is the one outcome a gate must not produce.
//
// Run as: node --experimental-vm-modules parse.mjs <file>...

import { readFileSync } from "node:fs";
import process from "node:process";
import vm from "node:vm";

const report = process.argv.slice(2).map((file) => {
  const source = readFileSync(file, "utf-8");
  try {
    if (file.endsWith(".cjs")) {
      // Sandboxed preloads cannot be ES modules; parse that one as a script.
      new vm.Script(source, { filename: file });
      return { file, ok: true, deps: [] };
    }
    const parsed = new vm.SourceTextModule(source, { identifier: file });
    return { file, ok: true, deps: [...parsed.dependencySpecifiers] };
  } catch (err) {
    return { file, ok: false, error: `${err.name}: ${err.message}`, deps: [] };
  }
});

console.log(JSON.stringify(report));
