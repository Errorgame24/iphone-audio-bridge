const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const htmlPath = path.join(__dirname, "..", "web", "index.html");
const html = fs.readFileSync(htmlPath, "utf8");
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)];

test("inline player script parses successfully", () => {
    assert.ok(scripts.length > 0, "expected an inline player script");
    for (const [, source] of scripts) {
        new vm.Script(source, { filename: htmlPath });
    }
});