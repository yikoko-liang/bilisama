// Module resolution hooks: `electron` becomes the recording stub next door.
//
// Registered through node:module's register() from probe.mjs. The href is
// built the same way probe.mjs imports the stub, so both land on ONE module
// instance — main.mjs and the probe have to share the same ledger.

const STUB = new URL("./electron_stub.mjs", import.meta.url).href;

export function resolve(specifier, context, next) {
  if (specifier === "electron") return { url: STUB, shortCircuit: true };
  return next(specifier, context);
}
