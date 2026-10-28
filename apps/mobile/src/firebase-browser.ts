import type { AuthenticationAdapter } from "@brainoid-internal/ui/auth";
import { type FirebaseOptions, initializeApp } from "firebase/app";
import {
  connectAuthEmulator,
  initializeAuth,
  inMemoryPersistence,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  signOut,
} from "firebase/auth";

/** Browser credentials live only in this page. This is not native protected storage. */
export function createBrowserAuthentication(
  config: FirebaseOptions,
  emulator = false,
): AuthenticationAdapter {
  if (emulator && config.projectId !== "demo-brainoid") throw new Error("Invalid emulator project");
  const app = initializeApp(config, `brainoid-${crypto.randomUUID()}`);
  const auth = initializeAuth(app, { persistence: inMemoryPersistence });
  if (emulator) connectAuthEmulator(auth, "http://127.0.0.1:9099", { disableWarnings: true });
  return {
    storage: "browser-memory",
    current: async () => {
      await auth.authStateReady();
      return auth.currentUser ? { uid: auth.currentUser.uid } : null;
    },
    subscribe: (listener) =>
      onAuthStateChanged(auth, (user) => listener(user ? { uid: user.uid } : null)),
    signIn: async (email, password) => {
      await signInWithEmailAndPassword(auth, email, password);
    },
    token: async (uid, forceRefresh) => {
      const user = auth.currentUser;
      if (!user || user.uid !== uid || user.tenantId !== null) throw new Error("Identity changed");
      const token = await user.getIdToken(forceRefresh);
      if (auth.currentUser !== user) throw new Error("Identity changed");
      return token;
    },
    signOut: () => signOut(auth),
  };
}
