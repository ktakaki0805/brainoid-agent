import { createRoot } from "react-dom/client";
import { SessionController } from "@brainoid-internal/ui/session";
import { authenticationConfiguration } from "../../mobile/src/auth-config.ts";
import { createBrowserAuthentication } from "../../mobile/src/firebase-browser.ts";
import { AccountRecovery } from "./recovery.ts";
import { WebAccountDeletion } from "./view.tsx";
import "@brainoid-internal/ui/styles.css";
import "./styles.css";

const element = document.getElementById("root");
if (!element) throw new Error("Missing app root");
const root = createRoot(element);
try {
  const config = authenticationConfiguration(import.meta.env, import.meta.env.MODE);
  if (!config) throw new Error("Explicit connection configuration required");
  const session = new SessionController({
    auth: createBrowserAuthentication(config.firebase, config.emulator),
    baseUrl: config.apiUrl,
    accountOnly: true,
  });
  root.render(<WebAccountDeletion recovery={new AccountRecovery(session, () => sessionStorage)} />);
} catch {
  root.render(
    <main className="account-page">
      <section className="card" role="status">
        <h1>アカウント削除ページの準備中です</h1>
        <p>現在このページからは申請できません。運営元の公開案内を確認してください。</p>
      </section>
    </main>,
  );
}
