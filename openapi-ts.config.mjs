// Tool versions live in package.json/pnpm-lock.yaml; no credentials enter generation.
export default {
  input: "./specs/openapi.yaml",
  output: {
    path: "packages/api-client/src/generated",
    module: { extension: ".js" },
    tsConfigPath: "packages/api-client/tsconfig.json",
  },
  plugins: [
    "@hey-api/typescript",
    { name: "@hey-api/client-fetch", baseUrl: false },
    "@hey-api/sdk",
  ],
};
