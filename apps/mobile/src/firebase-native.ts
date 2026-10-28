import type { AuthenticationAdapter, AuthIdentity } from "@brainoid-internal/ui/auth";
import type { FirebaseAuthenticationPlugin } from "@capacitor-firebase/authentication";

/** Native Firebase owns its credential store. No refresh credential crosses this adapter. */
export function createNativeAuthentication(
  plugin: FirebaseAuthenticationPlugin,
): AuthenticationAdapter {
  let epoch = 0;
  let operationEpoch = 0;
  let reauthentication: { uid: string; invalid: boolean } | undefined;
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
        if (reauthentication && user?.uid !== reauthentication.uid) reauthentication.invalid = true;
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
      operationEpoch++;
      await plugin.signInWithEmailAndPassword({ email, password });
    },
    reauthenticate: async (uid, email, password) => {
      const before = epoch;
      const original = (await plugin.getCurrentUser()).user;
      if (
        !original ||
        original.uid !== uid ||
        !original.email ||
        original.email.toLowerCase() !== email.trim().toLowerCase() ||
        (await plugin.getTenantId()).tenantId !== null ||
        before !== epoch
      )
        throw new Error("Identity changed");
      // This plugin has no reauthenticate method. A credential sign-in refreshes auth_time.
      // Constrain it to the current email and verify the resulting global identity.
      const operation = ++operationEpoch;
      const guard = { uid, invalid: false };
      reauthentication = guard;
      try {
        const result = await plugin.signInWithEmailAndPassword({ email: original.email, password });
        const identity = await current();
        if (
          operation !== operationEpoch ||
          guard.invalid ||
          result.user?.uid !== uid ||
          identity?.uid !== uid
        ) {
          epoch++;
          await plugin.signOut();
          throw new Error("Identity changed");
        }
      } finally {
        if (reauthentication === guard) reauthentication = undefined;
      }
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
      operationEpoch++;
      epoch++;
      await plugin.signOut();
    },
  };
}
