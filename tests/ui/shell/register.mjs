// `node --import ./register.mjs` installs the hooks before probe.mjs runs, so
// main.mjs's own `import ... from "electron"` is already redirected by the
// time it is loaded.

import { register, registerHooks } from "node:module";

import { resolve } from "./hooks.mjs";

// registerHooks (node 22.15+) runs in-thread and is what register() is being
// deprecated in favour of; the older call stays as the fallback so this
// harness does not pin a node newer than the shell itself needs.
if (typeof registerHooks === "function") registerHooks({ resolve });
else register("./hooks.mjs", import.meta.url);
