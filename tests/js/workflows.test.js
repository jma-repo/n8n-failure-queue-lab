const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const dir = path.join(__dirname, "..", "..", "n8n", "workflows");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

// What n8n hands a Code node when it runs; only the names matter for compiling.
const GLOBALS = ["$input", "$", "$env", "$execution", "$json", "$node", "$items", "$workflow", "$now", "$today", "$itemIndex"];

for (const file of fs.readdirSync(dir).filter((f) => f.endsWith(".json"))) {
  const workflow = JSON.parse(fs.readFileSync(path.join(dir, file), "utf8"));
  for (const node of workflow.nodes.filter((n) => n.type === "n8n-nodes-base.code")) {
    test(`${workflow.name}: Code node "${node.name}" compiles`, () => {
      assert.doesNotThrow(() => new AsyncFunction(...GLOBALS, node.parameters.jsCode));
    });
  }
}
