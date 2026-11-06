import { useCallback, useEffect, useState, type ChangeEvent, type FormEvent } from "react";

import { api, errorMessage, type Connection, type ToolAdmin, type ToolServer } from "../api";

/** Fixed text per verification code, so nothing from GitHub is shown as a message. */
const CHECK_TEXT: Record<string, string> = {
  credential_rejected:
    "GitHub refused the app ID, installation ID or key, or the app is not installed on one of these repositories.",
  reauthorization_required: "GitHub no longer accepts this key. Replace the key to continue.",
  rate_limited: "GitHub is limiting requests right now. Try again in a few minutes.",
};

function checkText(code: string): string {
  return CHECK_TEXT[code] ?? `The GitHub check failed (${code}). Nothing was read.`;
}

/** The slug prefixes tool names, such as "github.issues_search". */
export function slugFor(name: string): string {
  const slug = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64)
    .replace(/-+$/, "");
  return slug || "github";
}

export function parseRepositories(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

/** Only tools the provider marks read-only and non-destructive can be approved from this screen. */
export function isReadOnly(tool: ToolAdmin): boolean {
  // Approving a changed tool approves its pending definition, so judge that one.
  const hints = (tool.pending_definition ? tool.pending_definition.annotations : tool.annotations) ?? {};
  return hints.read_only_hint === true && hints.destructive_hint !== true;
}

function repositoriesOf(connection: Connection): string[] {
  const value = connection.config.repositories;
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function readText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === "string" ? reader.result : "");
    reader.onerror = () => reject(new Error("That file could not be read."));
    reader.readAsText(file);
  });
}

async function readKeyFile(event: ChangeEvent<HTMLInputElement>): Promise<{ name: string; text: string } | null> {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return null;
  if (file.size > 16_000) throw new Error("That file is too large to be a GitHub App key.");
  const text = await readText(file);
  if (!text.includes("PRIVATE KEY")) throw new Error("That file is not a PEM private key. Choose the .pem file GitHub gave you.");
  return { name: file.name, text };
}

interface FormProps {
  workspaceId: string;
  onDone: (message: string) => void;
  onCancel: () => void;
}

function ConnectForm({ workspaceId, onDone, onCancel }: FormProps) {
  const [name, setName] = useState("GitHub");
  const [appId, setAppId] = useState("");
  const [installationId, setInstallationId] = useState("");
  const [repositories, setRepositories] = useState("");
  const [key, setKey] = useState<{ name: string; text: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function chooseKey(event: ChangeEvent<HTMLInputElement>) {
    setError(null);
    try {
      setKey(await readKeyFile(event));
    } catch (caught) {
      setKey(null);
      setError(caught instanceof Error ? caught.message : "That file could not be read.");
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!key) {
      setError("Choose the app's private key file (.pem).");
      return;
    }
    setBusy(true);
    setError(null);
    let created: Awaited<ReturnType<typeof api.createGitHub>>;
    try {
      created = await api.createGitHub(workspaceId, {
        slug: slugFor(name),
        name: name.trim(),
        app_id: appId.trim(),
        installation_id: Number(installationId),
        private_key: key.text,
        repositories: parseRepositories(repositories),
      });
    } catch (caught) {
      setError(errorMessage(caught));
      setBusy(false);
      return;
    } finally {
      // The key is stored encrypted on the server. Do not keep a copy in the page.
      setKey(null);
    }
    const steps = ["GitHub connection saved."];
    try {
      const found = await api.discoverTools(workspaceId, created.tool_server.id);
      steps.push(`Found ${found.added.length + found.changed.length + found.unchanged.length} tools.`);
    } catch (caught) {
      steps.push(`Finding its tools failed: ${errorMessage(caught)} Use "Find tools" to try again.`);
    }
    try {
      const check = await api.verifyConnection(workspaceId, created.connection.id);
      steps.push(check.verified ? "GitHub accepted the app and the repositories." : checkText(check.code ?? "unknown"));
    } catch (caught) {
      steps.push(`The check did not run: ${errorMessage(caught)}`);
    }
    setBusy(false);
    onDone(steps.join(" "));
  }

  return (
    <form className="connect-form" onSubmit={submit} aria-label="Connect GitHub">
      <p className="muted">
        Uses a GitHub App you created. Its settings page shows the App ID. The installation ID is the number at the end of the
        app's installation page address. docs/GITHUB_SETUP.md has the steps.
      </p>
      <label>
        Name shown in OpsAssist
        <input required maxLength={200} value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label>
        App ID
        <input required inputMode="numeric" pattern="[A-Za-z0-9.\-]{1,64}" value={appId} onChange={(e) => setAppId(e.target.value)} />
      </label>
      <label>
        Installation ID
        <input required inputMode="numeric" pattern="[0-9]{1,20}" value={installationId} onChange={(e) => setInstallationId(e.target.value)} />
      </label>
      <label>
        Repositories the assistant may read, one per line, as owner/name
        <textarea required rows={3} placeholder="your-name/your-repo" value={repositories} onChange={(e) => setRepositories(e.target.value)} />
      </label>
      <label className="upload">
        <span>{key ? `Key file chosen: ${key.name}` : "Choose the private key file (.pem)"}</span>
        <input type="file" accept=".pem" onChange={(e) => void chooseKey(e)} />
      </label>
      <p className="muted small">The key is encrypted before it is stored and is never shown again.</p>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <div className="row">
        <button type="submit" disabled={busy}>
          {busy ? "Connecting…" : "Connect GitHub"}
        </button>
        <button type="button" className="ghost" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

interface CardProps {
  workspaceId: string;
  connection: Connection;
  server: ToolServer | undefined;
  tools: ToolAdmin[];
  onChanged: () => Promise<void>;
}

function ConnectionCard({ workspaceId, connection, server, tools, onChanged }: CardProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [editingRepos, setEditingRepos] = useState(false);
  const [repos, setRepos] = useState(repositoriesOf(connection).join("\n"));
  const [confirming, setConfirming] = useState(false);

  async function run(action: () => Promise<string | null>) {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const text = await action();
      if (text) setMessage(text);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  const check = () =>
    run(async () => {
      const outcome = await api.verifyConnection(workspaceId, connection.id);
      await onChanged();
      if (!outcome.verified) throw new Error(checkText(outcome.code ?? "unknown"));
      return "The connection works.";
    });

  const findTools = () =>
    run(async () => {
      if (!server) throw new Error("This connection has no tool server. Remove it and connect again.");
      const found = await api.discoverTools(workspaceId, server.id);
      await onChanged();
      const changed = found.changed.length > 0 ? ` ${found.changed.length} changed and need approval again.` : "";
      return `Found ${found.added.length + found.changed.length + found.unchanged.length} tools.${changed}`;
    });

  const saveRepos = () =>
    run(async () => {
      await api.setRepositories(workspaceId, connection.id, parseRepositories(repos));
      setEditingRepos(false);
      await onChanged();
      return "Repositories saved. Use Check connection to confirm the app can see them.";
    });

  const replaceKey = (event: ChangeEvent<HTMLInputElement>) =>
    run(async () => {
      const key = await readKeyFile(event);
      if (!key) return null;
      await api.replaceKey(workspaceId, connection.id, key.text);
      await onChanged();
      return "Key replaced. Use Check connection to confirm it works.";
    });

  const remove = () =>
    run(async () => {
      await api.deleteConnection(workspaceId, connection.id);
      await onChanged();
      return null;
    });

  const approve = (list: ToolAdmin[]) =>
    run(async () => {
      for (const tool of list) await api.approveReadTool(workspaceId, tool);
      await onChanged();
      return list.length === 1 ? `${list[0]?.name ?? "Tool"} approved.` : `${list.length} tools approved.`;
    });

  const problem = server?.last_error_code ?? null;
  // Without its tool server the health is unknown, which is not the same as never checked.
  const state = !server ? "neutral" : problem ? "bad" : connection.last_verified_at && server.health === "healthy" ? "good" : "neutral";
  const label = !server ? "Status unknown" : state === "bad" ? "Needs attention" : state === "good" ? "Connected" : "Not checked yet";
  const isApproved = (tool: ToolAdmin) => tool.review_status === "approved" && tool.enabled;
  const approvable = tools.filter((tool) => !isApproved(tool) && isReadOnly(tool));

  return (
    <article className="integration-card">
      <header>
        <h3>{connection.name}</h3>
        <span className={`badge badge-${state}`}>{label}</span>
      </header>
      {message && <p role="status">{message}</p>}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {!server && (
        <p role="alert" className="error">
          This page could not load the connection&apos;s health and tools. Reload the page. If this stays, the server may be running
          older code and needs a restart.
        </p>
      )}
      {problem && !error && (
        <p role="alert" className="error">
          {checkText(problem)}
        </p>
      )}
      <dl>
        <dt>Repositories</dt>
        <dd>{repositoriesOf(connection).join(", ") || "None"}</dd>
        <dt>Last verified</dt>
        <dd>{connection.last_verified_at ? new Date(connection.last_verified_at).toLocaleString() : "Never"}</dd>
      </dl>

      {editingRepos ? (
        <div className="connect-form">
          <label>
            Repositories, one per line, as owner/name
            <textarea rows={3} value={repos} onChange={(e) => setRepos(e.target.value)} />
          </label>
          <div className="row">
            <button type="button" disabled={busy} onClick={() => void saveRepos()}>
              Save repositories
            </button>
            <button type="button" className="ghost" disabled={busy} onClick={() => setEditingRepos(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="row">
          <button type="button" className="secondary" disabled={busy} onClick={() => void check()}>
            Check connection
          </button>
          <button type="button" className="secondary" disabled={busy} onClick={() => void findTools()}>
            Find tools
          </button>
          <button type="button" className="secondary" disabled={busy} onClick={() => setEditingRepos(true)}>
            Change repositories
          </button>
          <label className="button-like secondary">
            Replace key
            <input type="file" accept=".pem" className="sr-only" disabled={busy} onChange={(e) => void replaceKey(e)} />
          </label>
          {!confirming && (
            <button type="button" className="ghost" disabled={busy} onClick={() => setConfirming(true)}>
              Remove
            </button>
          )}
        </div>
      )}
      {confirming && (
        <div role="group" aria-label={`Confirm removing ${connection.name}`} className="confirm">
          <p>This deletes the stored key and these tools here. The app stays installed on GitHub, so uninstall it there too if you no longer need it.</p>
          <div className="row">
            <button type="button" className="danger" disabled={busy} onClick={() => void remove()}>
              Remove connection
            </button>
            <button type="button" className="ghost" disabled={busy} onClick={() => setConfirming(false)}>
              Keep it
            </button>
          </div>
        </div>
      )}

      <h4>Tools</h4>
      {tools.length === 0 ? (
        <p className="muted">No tools found yet. Use Find tools.</p>
      ) : (
        <>
          {approvable.length > 0 && (
            <div className="row">
              <p role="status">The assistant cannot use a tool until you approve it as read-only.</p>
              {approvable.length > 1 && (
                <button type="button" disabled={busy} onClick={() => void approve(approvable)}>
                  Approve all {approvable.length} read-only tools
                </button>
              )}
            </div>
          )}
          <ul className="tool-list">
            {tools.map((tool) => {
              const approved = isApproved(tool);
              return (
                <li key={tool.id}>
                  <span className={`badge badge-${approved ? "good" : "neutral"}`}>{approved ? "Approved" : "Awaiting approval"}</span>{" "}
                  <strong>{tool.name}</strong>
                  <p className="source-meta">{tool.description}</p>
                  {!approved &&
                    (isReadOnly(tool) ? (
                      <button type="button" className="secondary" disabled={busy} onClick={() => void approve([tool])}>
                        Approve {tool.name} as read-only
                      </button>
                    ) : (
                      <p className="muted small">Not marked read-only, so it stays off until approval steps exist.</p>
                    ))}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </article>
  );
}

export function GitHubConnections({ workspaceId, isAdmin }: { workspaceId: string; isAdmin: boolean }) {
  const [connections, setConnections] = useState<Connection[] | null>(null);
  const [servers, setServers] = useState<ToolServer[]>([]);
  const [tools, setTools] = useState<ToolAdmin[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const load = useCallback(async () => {
    if (!isAdmin) return;
    try {
      const [allConnections, allServers, allTools] = await Promise.all([
        api.listConnections(workspaceId),
        api.listToolServers(workspaceId),
        api.listTools(workspaceId),
      ]);
      setConnections(allConnections.filter((item) => item.provider === "github"));
      setServers(allServers);
      setTools(allTools);
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }, [workspaceId, isAdmin]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section className="integration-card" aria-label="GitHub">
      <header>
        <h2>GitHub</h2>
      </header>
      <p>Read-only access to issues, pull requests and code in the repositories you list. It cannot comment, merge or push.</p>
      {!isAdmin ? (
        <p role="status">An administrator connects GitHub for the whole workspace.</p>
      ) : (
        <>
          {message && <p role="status">{message}</p>}
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          {connections === null && !error && <p className="muted">Loading…</p>}
          {connections?.length === 0 && !adding && <p className="muted">Not connected.</p>}
          {connections?.map((connection) => {
            const server = servers.find((item) => item.connection_id === connection.id);
            return (
              <ConnectionCard
                key={connection.id}
                workspaceId={workspaceId}
                connection={connection}
                server={server}
                tools={server ? tools.filter((tool) => tool.server_id === server.id) : []}
                onChanged={load}
              />
            );
          })}
          {adding ? (
            <ConnectForm
              workspaceId={workspaceId}
              onCancel={() => setAdding(false)}
              onDone={(text) => {
                setAdding(false);
                setMessage(text);
                void load();
              }}
            />
          ) : (
            connections !== null && (
              <button type="button" onClick={() => setAdding(true)}>
                {connections.length === 0 ? "Connect GitHub" : "Connect another GitHub account"}
              </button>
            )
          )}
        </>
      )}
    </section>
  );
}
