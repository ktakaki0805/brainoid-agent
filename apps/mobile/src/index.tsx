import { AssistantHome, Frame } from "@brainoid-internal/ui/react";
import type { UiDependencies } from "@brainoid-internal/ui";
import { createRoot } from "react-dom/client";
import "@brainoid-internal/ui/styles.css";

/** Future native/device composition supplies authenticated API + platform adapters here. */
export function mountMobile(element: HTMLElement, dependencies: UiDependencies) {
  if (dependencies.platform.surface !== "mobile") throw new Error("Incorrect platform surface");
  const root = createRoot(element);
  root.render(
    <Frame surface="mobile">
      <AssistantHome dependencies={dependencies} />
    </Frame>,
  );
  return () => root.unmount();
}
