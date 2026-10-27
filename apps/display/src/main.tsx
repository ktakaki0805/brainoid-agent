import { SetupRequired } from "@brainoid-internal/ui/react";
import { createRoot } from "react-dom/client";
import "@brainoid-internal/ui/styles.css";

const element = document.getElementById("root");
if (!element) throw new Error("Missing app root");
const root = createRoot(element);
if (import.meta.env.MODE === "fake") {
  const { Preview } = await import("@brainoid-internal/ui/preview");
  root.render(<Preview surface="display" />);
} else {
  root.render(<SetupRequired surface="display" />);
}
