import { execFileSync } from "node:child_process";
import { cpSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { createClient } from "@brainoid-internal/openapi-generator";
import config from "../openapi-ts.config.mjs";

const args = process.argv.slice(2);
if (args.length > 1 || (args.length === 1 && args[0] !== "--check")) {
  throw new Error("Usage: scripts/generate_api_client [--check]");
}
const temporary = mkdtempSync(join(tmpdir(), "brainoid-api-client-"));
const files = (directory) =>
  readdirSync(directory, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => relative(directory, join(entry.parentPath, entry.name)))
    .sort();
try {
  const source = join(temporary, "source");
  const output = join(temporary, "output");
  await createClient({ ...config, output: { ...config.output, path: source } });
  // The upstream runtime does not support exactOptionalPropertyTypes internally.
  // Compile its implementation with strict TS, then expose declarations to consumers
  // that retain all workspace checks, including exactOptionalPropertyTypes.
  execFileSync(
    process.execPath,
    [
      fileURLToPath(new URL("./bin/tsc", import.meta.resolve("typescript/package.json"))),
      "--ignoreConfig",
      "--declaration",
      "--strict",
      "--noUncheckedIndexedAccess",
      "--isolatedModules",
      "--verbatimModuleSyntax",
      "--target",
      "ES2022",
      "--module",
      "ESNext",
      "--moduleResolution",
      "Bundler",
      "--lib",
      "ES2022,DOM,DOM.Iterable",
      "--outDir",
      output,
      ...files(source).map((name) => join(source, name)),
    ],
    { stdio: "inherit" },
  );
  // Compiler-emitted declarations may retain spaces before documentation comments.
  for (const name of files(output)) {
    const path = join(output, name);
    writeFileSync(path, readFileSync(path, "utf8").replace(/[ \t]+$/gm, ""));
  }

  const destination = config.output.path;
  if (args[0] === "--check") {
    const expected = files(output);
    const actual = files(destination);
    if (
      JSON.stringify(expected) !== JSON.stringify(actual) ||
      expected.some(
        (name) => !readFileSync(join(output, name)).equals(readFileSync(join(destination, name))),
      )
    ) {
      throw new Error(
        "Generated client differs. Run mise run generate-client and review the changes.",
      );
    }
    console.log("Generated client is up to date.");
  } else {
    rmSync(destination, { recursive: true, force: true });
    cpSync(output, destination, { recursive: true });
    console.log(`Generated client written to ${destination}.`);
  }
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
