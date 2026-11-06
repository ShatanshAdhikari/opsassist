import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api, type Connection, type ToolAdmin, type ToolServer } from "../api";
import { GitHubConnections, isReadOnly, parseRepositories, slugFor } from "./GitHubConnections";

const KEY = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n";

const connection: Connection = {
  id: "c1",
  provider: "github",
  name: "GitHub",
  config: { app_id: "1", installation_id: 2, repositories: ["me/repo"] },
  last_verified_at: "2026-11-05T00:00:00Z",
  created_at: "2026-11-05T00:00:00Z",
};

const server: ToolServer = {
  id: "s1",
  slug: "github",
  name: "GitHub",
  kind: "github",
  connection_id: "c1",
  enabled: true,
  health: "healthy",
  last_error_code: null,
  last_checked_at: null,
  last_discovered_at: null,
};

function tool(overrides: Partial<ToolAdmin> = {}): ToolAdmin {
  return {
    id: "t1",
    server_id: "s1",
    tool_id: "github.issues_search",
    name: "issues_search",
    description: "Search issues.",
    review_status: "pending",
    enabled: false,
    available: true,
    access: "read",
    risk_level: 0,
    definition_hash: "h1",
    pending_hash: null,
    annotations: { read_only_hint: true, destructive_hint: false },
    ...overrides,
  };
}

function show({ connections = [connection], servers = [server], tools = [] as ToolAdmin[], isAdmin = true } = {}) {
  const spies = {
    listConnections: vi.spyOn(api, "listConnections").mockResolvedValue(connections),
    listToolServers: vi.spyOn(api, "listToolServers").mockResolvedValue(servers),
    listTools: vi.spyOn(api, "listTools").mockResolvedValue(tools),
  };
  render(<GitHubConnections workspaceId="w1" isAdmin={isAdmin} />);
  return spies;
}

beforeEach(() => vi.restoreAllMocks());

describe("helpers", () => {
  it("derives a valid slug from the name", () => {
    expect(slugFor("GitHub")).toBe("github");
    expect(slugFor("  My Org's GitHub!  ")).toBe("my-org-s-github");
    expect(slugFor("!!!")).toBe("github");
    expect(slugFor("a".repeat(80))).toHaveLength(64);
  });

  it("splits repositories on lines, spaces and commas", () => {
    expect(parseRepositories("me/a\n me/b, me/c\n\n")).toEqual(["me/a", "me/b", "me/c"]);
  });

  it("treats a tool as read-only only when the provider says so", () => {
    expect(isReadOnly(tool())).toBe(true);
    expect(isReadOnly(tool({ annotations: {} }))).toBe(false);
    expect(isReadOnly(tool({ annotations: { read_only_hint: true, destructive_hint: true } }))).toBe(false);
    // A changed tool is judged by the definition that approval would accept.
    expect(isReadOnly(tool({ pending_definition: { annotations: { read_only_hint: false } } }))).toBe(false);
  });
});

describe("GitHubConnections", () => {
  it("tells a member an admin connects GitHub, without loading admin data", async () => {
    const spies = show({ isAdmin: false });
    expect(await screen.findByText(/administrator connects GitHub/)).toBeInTheDocument();
    expect(spies.listConnections).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Connect GitHub" })).toBeNull();
  });

  it("offers to connect when nothing is connected", async () => {
    show({ connections: [], servers: [] });
    expect(await screen.findByText("Not connected.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect GitHub" })).toBeInTheDocument();
  });

  it("ignores connections from other providers", async () => {
    show({ connections: [{ ...connection, id: "g1", provider: "gmail", name: "Gmail (me)" }], servers: [] });
    expect(await screen.findByText("Not connected.")).toBeInTheDocument();
    expect(screen.queryByText("Gmail (me)")).toBeNull();
  });

  it("connects, finds tools and checks the connection, then reports each step", async () => {
    show({ connections: [], servers: [] });
    const create = vi.spyOn(api, "createGitHub").mockResolvedValue({ connection, tool_server: server });
    const discover = vi.spyOn(api, "discoverTools").mockResolvedValue({ added: ["a", "b"], changed: [], removed: [], unchanged: [], rejected: [] });
    const verify = vi.spyOn(api, "verifyConnection").mockResolvedValue({ verified: true, code: null });

    await userEvent.click(await screen.findByRole("button", { name: "Connect GitHub" }));
    const form = screen.getByRole("form", { name: "Connect GitHub" });
    await userEvent.type(within(form).getByLabelText("App ID"), "5613304");
    await userEvent.type(within(form).getByLabelText("Installation ID"), "178596510");
    await userEvent.type(within(form).getByLabelText(/Repositories the assistant may read/), "me/repo\nme/other");
    const file = new File([KEY], "app.pem", { type: "application/x-pem-file" });
    await userEvent.upload(within(form).getByLabelText(/private key file/), file);
    expect(await within(form).findByText("Key file chosen: app.pem")).toBeInTheDocument();
    await userEvent.click(within(form).getByRole("button", { name: "Connect GitHub" }));

    await waitFor(() => expect(verify).toHaveBeenCalledWith("w1", "c1"));
    expect(create).toHaveBeenCalledWith("w1", {
      slug: "github",
      name: "GitHub",
      app_id: "5613304",
      installation_id: 178596510,
      private_key: KEY,
      repositories: ["me/repo", "me/other"],
    });
    expect(discover).toHaveBeenCalledWith("w1", "s1");
    expect(await screen.findByText(/saved\. Found 2 tools\. GitHub accepted/)).toBeInTheDocument();
  });

  it("refuses a file that is not a private key, and never sends it", async () => {
    show({ connections: [], servers: [] });
    const create = vi.spyOn(api, "createGitHub");
    await userEvent.click(await screen.findByRole("button", { name: "Connect GitHub" }));
    const form = screen.getByRole("form", { name: "Connect GitHub" });
    await userEvent.upload(within(form).getByLabelText(/private key file/), new File(["hello"], "notes.pem"));
    expect(await within(form).findByRole("alert")).toHaveTextContent(/not a PEM private key/);
    expect(create).not.toHaveBeenCalled();
  });

  it("does not report success when the check fails", async () => {
    show({ connections: [], servers: [] });
    vi.spyOn(api, "createGitHub").mockResolvedValue({ connection, tool_server: server });
    vi.spyOn(api, "discoverTools").mockResolvedValue({ added: [], changed: [], removed: [], unchanged: [], rejected: [] });
    vi.spyOn(api, "verifyConnection").mockResolvedValue({ verified: false, code: "credential_rejected" });
    await userEvent.click(await screen.findByRole("button", { name: "Connect GitHub" }));
    const form = screen.getByRole("form", { name: "Connect GitHub" });
    await userEvent.type(within(form).getByLabelText("App ID"), "1");
    await userEvent.type(within(form).getByLabelText("Installation ID"), "2");
    await userEvent.type(within(form).getByLabelText(/Repositories the assistant may read/), "me/repo");
    await userEvent.upload(within(form).getByLabelText(/private key file/), new File([KEY], "app.pem"));
    await within(form).findByText("Key file chosen: app.pem");
    await userEvent.click(within(form).getByRole("button", { name: "Connect GitHub" }));
    expect(await screen.findByText(/GitHub refused the app ID/)).toBeInTheDocument();
    expect(screen.queryByText(/GitHub accepted/)).toBeNull();
  });

  it("shows a connection that has never been checked as not checked", async () => {
    show({ connections: [{ ...connection, last_verified_at: null }], servers: [{ ...server, health: "unknown" }] });
    expect(await screen.findByText("Not checked yet")).toBeInTheDocument();
    expect(screen.queryByText("Connected")).toBeNull();
  });

  it("says the status is unknown, not unchecked, when its tool server cannot be matched", async () => {
    // An older server sends no connection_id, so nothing pairs with the connection.
    show({ servers: [{ ...server, connection_id: null }] });
    expect(await screen.findByText("Status unknown")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/could not load the connection's health/);
    expect(screen.queryByText("Not checked yet")).toBeNull();
    expect(screen.queryByText("Connected")).toBeNull();
  });

  it("shows a failing connection as needing attention", async () => {
    show({ servers: [{ ...server, health: "unavailable", last_error_code: "credential_rejected" }] });
    expect(await screen.findByText("Needs attention")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/GitHub refused/);
  });

  it("approves every read-only tool at once, but never a tool not marked read-only", async () => {
    const readA = tool();
    const readB = tool({ id: "t2", name: "issue_get", tool_id: "github.issue_get" });
    const writer = tool({ id: "t3", name: "issue_close", tool_id: "github.issue_close", annotations: { destructive_hint: true } });
    const other = tool({ id: "t4", server_id: "elsewhere", name: "threads_search" });
    show({ tools: [readA, readB, writer, other] });
    const approve = vi.spyOn(api, "approveReadTool").mockResolvedValue(readA);

    expect(await screen.findByText(/stays off until approval steps exist/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve issue_close/ })).toBeNull();
    expect(screen.queryByText("threads_search")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Approve all 2 read-only tools" }));
    await waitFor(() => expect(approve).toHaveBeenCalledTimes(2));
    expect(approve).toHaveBeenCalledWith("w1", readA);
    expect(approve).toHaveBeenCalledWith("w1", readB);
  });

  it("asks before removing a connection", async () => {
    show();
    const remove = vi.spyOn(api, "deleteConnection").mockResolvedValue(undefined);
    await userEvent.click(await screen.findByRole("button", { name: "Remove" }));
    expect(remove).not.toHaveBeenCalled();
    expect(screen.getByText(/uninstall it there too/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Remove connection" }));
    expect(remove).toHaveBeenCalledWith("w1", "c1");
  });
});
