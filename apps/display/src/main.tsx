import { createRoot } from "react-dom/client";
import { Recovery } from "./recovery/index.tsx";
import "@brainoid-internal/ui/styles.css";

const element = document.getElementById("root");
if (!element) throw new Error("Missing app root");
const root = createRoot(element);
if (import.meta.env.MODE === "fake") {
  const { SessionPreview } = await import("@brainoid-internal/ui/session-fake");
  root.render(<SessionPreview surface="display" />);
} else {
  root.render(<Recovery />);
}
