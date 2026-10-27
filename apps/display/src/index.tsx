import { AssistantHome, Frame } from "@brainoid-internal/ui/react";
import type { UiDependencies } from "@brainoid-internal/ui";
import { createRoot } from "react-dom/client";
import "@brainoid-internal/ui/styles.css";

/** Future native/device composition supplies authenticated API + platform adapters here. */
export function mountDisplay(element: HTMLElement, dependencies: UiDependencies) {
  if (dependencies.platform.surface !== "display") throw new Error("Incorrect platform surface");
  const root = createRoot(element);
  root.render(
    <Frame surface="display">
      <AssistantHome dependencies={dependencies} />
    </Frame>,
  );
  return () => root.unmount();
}
