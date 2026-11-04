import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, type GmailStatus, type ToolAdmin } from "../api";
import { Integrations } from "./Integrations";

const connected: GmailStatus = {
  configured: true,
  connected: true,
  connection_id: "c1",
  address: "me@example.test",
  health: "healthy",
  last_error_code: null,
  last_verified_at: "2026-11-04T00:00:00Z",
};

const pendingTool: ToolAdmin = {
  id: "t1",
  tool_id: "gmail.threads_search",
  name: "threads_search",
  description: "Search this person's mailbox.",
  review_status: "pending",
  enabled: false,
  available: true,
  access: "write",
  risk_level: 3,
  definition_hash: "abc",
  pending_hash: null,
};

function show(status: Partial<GmailStatus> = {}, tools: ToolAdmin[] = [], signInResult: string | null = null, role: "owner" | "member" = "owner") {
  vi.spyOn(api, "gmailStatus").mockResolvedValue({ ...connected, ...status });
  vi.spyOn(api, "listTools").mockResolvedValue(tools);
  render(<Integrations workspaceId="w1" role={role} signInResult={signInResult} />);
}

beforeEach(() => vi.restoreAllMocks());

describe("Integrations", () => {
  it("offers to connect when nothing is connected", async () => {
    show({ connected: false, connection_id: null, address: null, health: null });
    expect(await screen.findByRole("button", { name: "Connect Gmail" })).toBeInTheDocument();
    expect(screen.getByText("Not connected")).toBeInTheDocument();
  });

  it("says when the server is not set up, without offering to connect", async () => {
    show({ configured: false, connected: false });
    expect(await screen.findByText(/not set up on this server/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect Gmail" })).toBeNull();
  });

  it("never shows an expired sign-in as connected", async () => {
    show({ health: "unavailable", last_error_code: "reauthorization_required" });
    expect(await screen.findByText("Needs attention")).toBeInTheDocument();
    expect(screen.queryByText("Connected")).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent(/expired or was revoked/);
    expect(screen.getByRole("button", { name: "Connect again" })).toBeInTheDocument();
  });

  it("shows a fixed message for a sign-in result and ignores unknown text", async () => {
    show({}, [], "<img src=x onerror=alert(1)>");
    expect(await screen.findByText("Google sign-in did not complete.")).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
  });

  it("explains a refused grant", async () => {
    show({ connected: false }, [], "wrong_scope");
    expect(await screen.findByText(/grant was given back/)).toBeInTheDocument();
  });

  it("asks before disconnecting and reports what Google confirmed", async () => {
    show();
    const disconnect = vi.spyOn(api, "disconnectGmail").mockResolvedValue({ revoked_at_provider: true });
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect" }));
    expect(disconnect).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Revoke and disconnect" }));
    expect(disconnect).toHaveBeenCalledWith("w1", false);
    expect(await screen.findByText(/Google confirmed access is revoked/)).toBeInTheDocument();
  });

  it("does not claim revocation when Google could not confirm it", async () => {
    show();
    const disconnect = vi
      .spyOn(api, "disconnectGmail")
      .mockRejectedValueOnce(new ApiError(503, "tool_unavailable", "Google could not confirm the revocation.", null))
      .mockResolvedValueOnce({ revoked_at_provider: false });
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect" }));
    await userEvent.click(screen.getByRole("button", { name: "Revoke and disconnect" }));
    await userEvent.click(await screen.findByRole("button", { name: /Remove anyway/ }));
    await waitFor(() => expect(disconnect).toHaveBeenLastCalledWith("w1", true));
    expect(await screen.findByText(/did not confirm the revocation/)).toBeInTheDocument();
    expect(screen.queryByText(/Google confirmed/)).toBeNull();
  });

  it("lets an admin approve a tool by its exact definition", async () => {
    show({}, [pendingTool]);
    const approve = vi.spyOn(api, "approveReadTool").mockResolvedValue(pendingTool);
    await userEvent.click(await screen.findByRole("button", { name: "Approve threads_search as read-only" }));
    expect(approve).toHaveBeenCalledWith("w1", pendingTool);
  });

  it("tells a member an admin must approve, with no approve button", async () => {
    show({}, [pendingTool], null, "member");
    expect(await screen.findByText(/administrator must approve/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Approve/ })).toBeNull();
  });
});
