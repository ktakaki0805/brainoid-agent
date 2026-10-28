import type { AuthenticationAdapter, AuthIdentity } from "@brainoid-internal/ui/auth";
import type { FirebaseAuthenticationPlugin } from "@capacitor-firebase/authentication";

/** Native Firebase owns its credential store. No refresh credential crosses this adapter. */
export function createNativeAuthentication(
  plugin: FirebaseAuthenticationPlugin,
): AuthenticationAdapter {
  let epoch = 0;
  const current = async (): Promise<AuthIdentity | null> => {
    const result = await plugin.getCurrentUser();
    if ((await plugin.getTenantId()).tenantId !== null)
      throw new Error("Auth tenants are unsupported");
    return result.user ? { uid: result.user.uid } : null;
  };
  return {
    storage: "native-firebase",
    current,
    subscribe(listener) {
      let active = true;
      let events = 0;
      const handle = plugin.addListener("authStateChange", ({ user }) => {
        epoch++;
        events++;
        if (active) listener(user ? { uid: user.uid } : null);
      });
      void handle
        .then(async () => {
          const before = events;
          const user = await current();
          if (active && before === events) listener(user);
        })
        .catch(() => {
          if (active) listener(null);
        });
      return () => {
        active = false;
        void handle.then((subscription) => subscription.remove()).catch(() => {});
      };
    },
    signIn: async (email, password) => {
      await plugin.signInWithEmailAndPassword({ email, password });
    },
    token: async (uid, forceRefresh) => {
      const before = epoch;
      if ((await current())?.uid !== uid) throw new Error("Identity changed");
      const result = await plugin.getIdToken({ forceRefresh });
      if (before !== epoch || (await current())?.uid !== uid || !result.token)
        throw new Error("Identity changed");
      return result.token;
    },
    signOut: async () => {
      epoch++;
      await plugin.signOut();
    },
  };
}
