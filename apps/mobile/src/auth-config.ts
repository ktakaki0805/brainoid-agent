export interface AuthenticationConfiguration {
  apiUrl: string;
  firebase: { apiKey: string; authDomain: string; projectId: string; appId: string };
  emulator: boolean;
}
export function authenticationConfiguration(
  env: Record<string, string | undefined>,
  mode: string,
): AuthenticationConfiguration | undefined {
  const values = [
    env.VITE_API_URL,
    env.VITE_FIREBASE_API_KEY,
    env.VITE_FIREBASE_AUTH_DOMAIN,
    env.VITE_FIREBASE_PROJECT_ID,
    env.VITE_FIREBASE_APP_ID,
  ];
  if (values.some((value) => !value?.trim())) return undefined;
  const [apiUrl, apiKey, authDomain, projectId, appId] = values as [
    string,
    string,
    string,
    string,
    string,
  ];
  const url = new URL(apiUrl);
  const emulator = mode === "emulator";
  if (url.username || url.password || url.search || url.hash || url.pathname !== "/")
    throw new Error("Invalid API root");
  if (emulator) {
    if (
      apiUrl !== "http://127.0.0.1:8000" ||
      projectId !== "demo-brainoid" ||
      env.VITE_AUTH_EMULATOR !== "http://127.0.0.1:9099"
    )
      throw new Error("Invalid emulator configuration");
  } else if (
    url.protocol !== "https:" ||
    projectId.startsWith("demo-") ||
    env.VITE_AUTH_EMULATOR !== undefined
  ) {
    throw new Error("Invalid production authentication configuration");
  }
  return { apiUrl, firebase: { apiKey, authDomain, projectId, appId }, emulator };
}
