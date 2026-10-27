/** Device-only HTTP: no cloud credentials, persistent browser state or remote fallback. */
export interface Diagnostics {
  service: string;
  storage: string;
  reset: string;
  network: string;
  clock: string;
  observed_at: string;
  free_bytes: number | null;
  volume_pct: number | null;
  microphone_muted: boolean | null;
}

export class LocalDevice {
  private csrf = "";
  private confirmation = "";

  private async request(path: string, method: string, extra: Record<string, string> = {}) {
    const response = await fetch(path, {
      method,
      credentials: "same-origin",
      cache: "no-store",
      headers: { "X-CSRF-Token": this.csrf, ...extra },
      signal: AbortSignal.timeout(15_000),
    });
    if (!response.ok) throw new Error("local_request_failed");
    return response.json();
  }

  async status(): Promise<Diagnostics> {
    const session = await this.request("/local/session", "POST", { "X-Brainoid-Local": "1" });
    this.csrf = session.csrf_token;
    return this.request("/local/diagnostics", "GET");
  }

  async prepare(): Promise<void> {
    const result = await this.request("/local/reset/prepare", "POST");
    this.confirmation = result.reset_token;
  }

  cancel(): void {
    this.confirmation = "";
  }

  async reset(): Promise<void> {
    try {
      await this.request("/local/reset", "POST", {
        "X-Brainoid-Reset-Token": this.confirmation,
        "X-Brainoid-Confirm": "local-administrator",
      });
    } finally {
      this.confirmation = "";
      this.csrf = "";
    }
  }
}
