import { useCallback, useEffect, useState } from "react";

import { ApiError, api, errorMessage, type GmailStatus, type Role, type ToolAdmin } from "../api";
import { GitHubConnections } from "./GitHubConnections";

/** What came back from Google's sign-in page. Fixed codes, so nothing from the URL is displayed. */
const SIGN_IN_RESULT: Record<string, { text: string; good: boolean }> = {
  connected: { text: "Gmail is connected.", good: true },
  denied: { text: "Google sign-in was cancelled, so nothing changed.", good: false },
  invalid_state: { text: "That sign-in link was old or not yours. Please try connecting again.", good: false },
  not_permitted: { text: "Your role in this workspace cannot connect a mailbox.", good: false },
  exchange_failed: { text: "Google did not complete the sign-in. Please try again.", good: false },
  wrong_scope: { text: "Google granted different access than read-only mail, so the grant was given back and nothing was connected.", good: false },
  no_refresh_token: { text: "Google did not allow lasting access. Remove this app in your Google account, then connect again.", good: false },
  google_unavailable: { text: "Google could not be reached, so nothing was connected.", good: false },
  already_connected: { text: "Someone in this workspace has already connected this mailbox.", good: false },
};

const ERROR_TEXT: Record<string, string> = {
  reauthorization_required: "Your Google sign-in expired or was revoked. Connect again to continue. Nothing reads your mail until you do.",
  credential_rejected: "Google refused this connection. Connect again to continue.",
};

interface Props {
  workspaceId: string;
  role: Role;
  signInResult: string | null;
}

export function Integrations({ workspaceId, role, signInResult }: Props) {
  const [gmail, setGmail] = useState<GmailStatus | null>(null);
  const [tools, setTools] = useState<ToolAdmin[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [offerForce, setOfferForce] = useState(false);
  const isAdmin = role === "owner" || role === "admin";
  const result = signInResult ? (SIGN_IN_RESULT[signInResult] ?? { text: "Google sign-in did not complete.", good: false }) : null;

  const load = useCallback(async () => {
    try {
      setGmail(await api.gmailStatus(workspaceId));
      setTools(await api.listTools(workspaceId));
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }, [workspaceId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await action();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  const connect = () =>
    run(async () => {
      const { authorization_url: url } = await api.authorizeGmail(workspaceId);
      window.location.assign(url);
    });

  const verify = () =>
    run(async () => {
      const outcome = await api.verifyGmail(workspaceId);
      setMessage(outcome.verified ? "The connection works." : "The connection does not work. See below.");
      await load();
    });

  const disconnect = (force: boolean) =>
    run(async () => {
      try {
        const outcome = await api.disconnectGmail(workspaceId, force);
        setMessage(
          outcome.revoked_at_provider
            ? "Gmail is disconnected, and Google confirmed access is revoked."
            : "Gmail is removed here, but Google did not confirm the revocation. Remove this app in your Google account too.",
        );
        setConfirming(false);
        setOfferForce(false);
        await load();
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 503) setOfferForce(true);
        throw caught;
      }
    });

  const approve = (tool: ToolAdmin) =>
    run(async () => {
      await api.approveReadTool(workspaceId, tool);
      await load();
    });

  const problem = gmail?.last_error_code ?? null;
  const healthy = gmail?.connected && !problem;
  const mailTools = (tools ?? []).filter((tool) => /^gmail[-.]/.test(tool.tool_id));
  const needsReview = mailTools.filter((tool) => tool.review_status !== "approved" || !tool.enabled);

  return (
    <section className="integrations" aria-label="Integrations">
      <h1>Integrations</h1>
      <p className="page-intro">
        Connect the systems the assistant reads live, such as your mailbox and GitHub. Every connection is read-only, and the assistant
        can use a tool only after an administrator approves it.
      </p>
      {result && (
        <p role="status" className={result.good ? "notice-good" : "error"}>
          {result.text}
        </p>
      )}
      {message && <p role="status">{message}</p>}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}

      {!gmail ? (
        !error && <p className="muted">Loading…</p>
      ) : (
        <article className="integration-card">
          <header>
            <h2>Gmail</h2>
            <span className={`badge badge-${healthy ? "good" : gmail.connected ? "bad" : "neutral"}`}>
              {healthy ? "Connected" : gmail.connected ? "Needs attention" : "Not connected"}
            </span>
          </header>
          <p>Read-only access to your own mailbox. It cannot send, delete or label mail, and nobody else in this workspace can use it, including admins.</p>

          {!gmail.configured ? (
            <p role="status">Gmail is not set up on this server yet. See docs/GMAIL_SETUP.md.</p>
          ) : !gmail.connected ? (
            <button type="button" disabled={busy} onClick={() => void connect()}>
              Connect Gmail
            </button>
          ) : (
            <>
              <dl>
                <dt>Mailbox</dt>
                <dd>{gmail.address}</dd>
                <dt>Last verified</dt>
                <dd>{gmail.last_verified_at ? new Date(gmail.last_verified_at).toLocaleString() : "Never"}</dd>
              </dl>
              {problem && (
                <p role="alert" className="error">
                  {ERROR_TEXT[problem] ?? `The last attempt failed (${problem}). No mail was read.`}
                </p>
              )}
              <div className="row">
                <button type="button" className="secondary" disabled={busy} onClick={() => void connect()}>
                  Connect again
                </button>
                <button type="button" className="secondary" disabled={busy} onClick={() => void verify()}>
                  Check connection
                </button>
                {!confirming && (
                  <button type="button" className="ghost" disabled={busy} onClick={() => setConfirming(true)}>
                    Disconnect
                  </button>
                )}
              </div>
              {confirming && (
                <div role="group" aria-label="Confirm disconnecting Gmail" className="confirm">
                  <p>This revokes access at Google. Answers that used this mailbox will be withheld.</p>
                  <div className="row">
                    <button type="button" className="danger" disabled={busy} onClick={() => void disconnect(false)}>
                      Revoke and disconnect
                    </button>
                    {offerForce && (
                      <button type="button" className="danger" disabled={busy} onClick={() => void disconnect(true)}>
                        Remove anyway, without confirmation from Google
                      </button>
                    )}
                    <button
                      type="button"
                      className="ghost"
                      disabled={busy}
                      onClick={() => {
                        setConfirming(false);
                        setOfferForce(false);
                      }}
                    >
                      Keep connected
                    </button>
                  </div>
                </div>
              )}

              <h3>Tools</h3>
              {needsReview.length > 0 && !isAdmin && (
                <p role="status">An administrator must approve these tools before the assistant can read this mailbox.</p>
              )}
              {needsReview.length > 0 && isAdmin && (
                <p role="status">The assistant cannot use these until you approve them as read-only. Approving does not let you read anyone else's mail.</p>
              )}
              <ul className="tool-list">
                {mailTools.map((tool) => {
                  const approved = tool.review_status === "approved" && tool.enabled;
                  return (
                    <li key={tool.id}>
                      <span className={`badge badge-${approved ? "good" : "neutral"}`}>{approved ? "Approved" : "Awaiting approval"}</span>{" "}
                      <strong>{tool.name}</strong>
                      <p className="source-meta">{tool.description}</p>
                      {!approved && isAdmin && (
                        <button type="button" className="secondary" disabled={busy} onClick={() => void approve(tool)}>
                          Approve {tool.name} as read-only
                        </button>
                      )}
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </article>
      )}

      <GitHubConnections workspaceId={workspaceId} isAdmin={isAdmin} />
    </section>
  );
}
