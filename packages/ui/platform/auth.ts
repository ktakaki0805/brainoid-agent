/** Auth proves a global identity only. The API validates tenant membership on every call. */
export interface AuthIdentity {
  readonly uid: string;
}
export interface AuthenticationAdapter {
  readonly storage: "browser-memory" | "native-firebase" | "fake";
  current(): Promise<AuthIdentity | null>;
  subscribe(listener: (identity: AuthIdentity | null) => void): () => void;
  signIn(email: string, password: string): Promise<void>;
  token(uid: string, forceRefresh: boolean): Promise<string>;
  signOut(): Promise<void>;
}
