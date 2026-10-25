import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

test("PR document check detects local path and heading regressions, then recovers", () => {
  // New, hidden documents must be checked before git add; ignored dependencies must not.
  const directory = mkdtempSync(".pr-link-test-");
  const document = join(directory, "links.md");
  const valid = "# 日本語の見出し\n\n[同じ文書](#日本語の見出し)\n[別文書](target.md#見出し)\n";
  function check() {
    const result = spawnSync("mise", ["run", "check:docs"], {
      encoding: "utf8",
      timeout: 30_000,
    });
    assert.equal(result.error, undefined);
    return { status: result.status, output: result.stdout + result.stderr };
  }
  try {
    writeFileSync(join(directory, "target.md"), "# 見出し\n");
    mkdirSync(join(directory, "node_modules"));
    writeFileSync(join(directory, "node_modules", "ignored.md"), "[ignored](missing.md)\n");
    // Offline mode excludes external links, including an unreachable loopback service.
    writeFileSync(document, `${valid}\n[external](http://127.0.0.1:1/unreachable)\n`);
    let result = check();
    assert.equal(result.status, 0, result.output);

    writeFileSync(document, `${valid}\n[missing file](missing.md)\n`);
    result = check();
    assert.notEqual(result.status, 0, result.output);
    assert.match(result.output, /File not found/);
    assert.match(result.output, /missing\.md/);

    writeFileSync(document, `${valid}\n[missing heading](target.md#missing-heading)\n`);
    result = check();
    assert.notEqual(result.status, 0, result.output);
    assert.match(result.output, /Cannot find fragment/);
    assert.match(result.output, /missing-heading/);

    writeFileSync(document, `${valid}\n[missing local heading](#missing-local-heading)\n`);
    result = check();
    assert.notEqual(result.status, 0, result.output);
    assert.match(result.output, /Cannot find fragment/);

    writeFileSync(document, valid);
    result = check();
    assert.equal(result.status, 0, result.output);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
