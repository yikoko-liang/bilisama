// The JavaScript half of the gate. Ledger #52: about 2700 lines ship — the pet
// page, the Electron shell and the shell's test harness — and until this file
// none of it went through anything, while the Python beside it goes through
// black + ruff + mypy --strict.
//
// Scope note: this only catches the class of mistake a parser and a scope
// analyser can see (undeclared names, unused bindings, unreachable code). It is
// not a type checker, and generating .d.ts for the page modules was ruled out.
// tests/ui/test_js_gate.py stays: it pins that every static import resolves to a
// file that exists, which no lint rule here checks.
//
// Run: `npm run lint`, or `scripts/gate.sh`, which runs it when node_modules is
// present and says out loud when it is not.

import js from "@eslint/js";
import globals from "globals";

export default [
  {
    // node_modules is ignored by default; these three are not, and each one
    // buries the ~2700 lines that matter under tens of thousands of findings.
    // .venv carries playwright's bundled browser tooling (measured: 29,577
    // errors from there alone); desktop/resources/vendor holds downloaded
    // runtime files — same rule as ruff's extend-exclude for
    // src/bilisama/ingest/bilibili/_vendor, where vendored code stays
    // byte-identical to its source and our gates skip it.
    ignores: [".venv/**", "desktop/resources/vendor/**", "**/node_modules/**"],
  },
  js.configs.recommended,
  {
    // The pet page and the panel: ES modules in a browser tab.
    files: ["src/bilisama/ui/web/js/**/*.js"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: globals.browser,
    },
  },
  {
    // The capture worklet runs on the audio thread, where `window` does not
    // exist and `sampleRate`, `currentTime` and AudioWorkletProcessor do.
    files: ["src/bilisama/ui/web/js/capture-worklet.js"],
    languageOptions: {
      globals: { ...globals.worker, ...globals.audioWorklet },
    },
  },
  {
    // The Electron main process and the shell's test harness: node globals,
    // ES modules.
    files: ["desktop/preview/**/*.mjs", "tests/ui/shell/**/*.mjs", "*.mjs"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: globals.node,
    },
  },
  {
    // Sandboxed preloads cannot be ES modules (desktop/preview/preload.cjs:3).
    files: ["**/*.cjs"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "commonjs",
      globals: globals.node,
    },
  },
  {
    rules: {
      // Catching a rejection and doing nothing with it is how the audio page's
      // device errors used to disappear — same rule as CLAUDE.md's "no bare
      // except, never swallow silently" on the Python side. `catch {}` with a
      // comment inside still passes; a truly empty block does not.
      "no-empty": ["error", { allowEmptyCatch: false }],
      // An argument that stopped being used is usually half of an unfinished
      // rename. Leading underscore is the opt-out, matching Python convention.
      "no-unused-vars": [
        "error",
        { args: "after-used", argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      eqeqeq: ["error", "smart"],
      "no-var": "error",
      "prefer-const": "error",
      // Off, and not because it never bites: it flags `let granted = null;`
      // before a try that assigns it (ui/web/js/audio.js:131), where the null
      // is a deliberate "not held yet" for the catch path to read. Turning the
      // first JavaScript gate red on a defensive initializer would teach people
      // to reach for --no-verify. Worth revisiting once the page's error paths
      // settle.
      "no-useless-assignment": "off",
    },
  },
];
