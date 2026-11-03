import { useCallback, useEffect, useState } from "react";

import { ApiError, api, errorMessage, type Me } from "./api";
import { Assistant } from "./screens/Assistant";
import { Knowledge } from "./screens/Knowledge";
import { Login } from "./screens/Login";

type Screen = "assistant" | "knowledge";

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [state, setState] = useState<"loading" | "anonymous" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [screen, setScreen] = useState<Screen>("assistant");

  const load = useCallback(async () => {
    setState("loading");
    try {
      const result = await api.me();
      setMe(result);
      setWorkspaceId((current) => current ?? result.workspaces[0]?.id ?? null);
      setState("ready");
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        setState("anonymous");
      } else {
        setError(errorMessage(caught));
        setState("error");
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function logout() {
    await api.logout().catch(() => undefined);
    setMe(null);
    setWorkspaceId(null);
    setState("anonymous");
  }

  if (state === "loading") return <p className="center muted">Loading…</p>;
  if (state === "anonymous") return <Login onLoggedIn={() => void load()} />;
  if (state === "error" || !me) {
    return (
      <div className="center">
        <p role="alert" className="error">{error}</p>
        <button type="button" onClick={() => void load()}>Try again</button>
      </div>
    );
  }

  const workspace = me.workspaces.find((item) => item.id === workspaceId);

  return (
    <div className="shell">
      <header className="topbar">
        <strong className="brand">OpsAssist</strong>
        <nav aria-label="Main">
          <button type="button" className={screen === "assistant" ? "tab active" : "tab"} aria-current={screen === "assistant" ? "page" : undefined} onClick={() => setScreen("assistant")}>
            Assistant
          </button>
          <button type="button" className={screen === "knowledge" ? "tab active" : "tab"} aria-current={screen === "knowledge" ? "page" : undefined} onClick={() => setScreen("knowledge")}>
            Knowledge
          </button>
        </nav>
        <div className="topbar-right">
          {me.workspaces.length > 0 && (
            <label>
              <span className="sr-only">Active workspace</span>
              <select value={workspaceId ?? ""} onChange={(event) => setWorkspaceId(event.target.value)}>
                {me.workspaces.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} ({item.role})
                  </option>
                ))}
              </select>
            </label>
          )}
          <span className="muted user-name">{me.user.display_name}</span>
          <button type="button" className="ghost" onClick={() => void logout()}>
            Sign out
          </button>
        </div>
      </header>
      <main className="content">
        {!workspace ? (
          <p className="center muted">You are not a member of any workspace. Ask an administrator to add you.</p>
        ) : screen === "assistant" ? (
          <Assistant key={workspace.id} workspaceId={workspace.id} />
        ) : (
          <Knowledge key={workspace.id} workspaceId={workspace.id} role={workspace.role} />
        )}
      </main>
    </div>
  );
}
