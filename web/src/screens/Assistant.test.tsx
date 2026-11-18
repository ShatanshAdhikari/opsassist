import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api, type Message } from "../api";
import { App } from "../App";
import { Assistant, unansweredQuestion } from "./Assistant";

const SUGGESTED = "Summarize the emails I received this week that need a reply.";

function message(id: string, role: Message["role"], content: string, created_at = new Date().toISOString()): Message {
  return { id, role, content, citations: [], grounding: null, created_at };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => (resolve = done));
  return { promise, resolve };
}

beforeEach(() => vi.restoreAllMocks());

describe("unansweredQuestion", () => {
  it("is the last message only when it is a question", () => {
    const question = message("q", "user", "Why?");
    expect(unansweredQuestion([])).toBeNull();
    expect(unansweredQuestion([question])).toBe(question);
    expect(unansweredQuestion([question, message("a", "assistant", "Because.")])).toBeNull();
  });
});

describe("Assistant", () => {
  it("sends a suggested question with Enter, because clicking it moves focus to the box", async () => {
    vi.spyOn(api, "listConversations").mockResolvedValue([]);
    vi.spyOn(api, "createConversation").mockResolvedValue({ id: "c1", title: SUGGESTED, updated_at: "" });
    const ask = vi
      .spyOn(api, "ask")
      .mockResolvedValue({ user_message: message("q", "user", SUGGESTED), answer: message("a", "assistant", "Two need a reply.") });
    render(<Assistant workspaceId="w1" />);

    await userEvent.click(await screen.findByRole("button", { name: SUGGESTED }));
    expect(screen.getByLabelText("Your question")).toHaveFocus();
    await userEvent.keyboard("{Enter}");

    expect(await screen.findByText("Two need a reply.")).toBeInTheDocument();
    expect(ask).toHaveBeenCalledWith("w1", "c1", SUGGESTED);
  });

  it("adds a line with Shift+Enter instead of sending", async () => {
    vi.spyOn(api, "listConversations").mockResolvedValue([]);
    const ask = vi.spyOn(api, "ask");
    render(<Assistant workspaceId="w1" />);

    await userEvent.type(await screen.findByLabelText("Your question"), "first{Shift>}{Enter}{/Shift}second");
    expect(screen.getByLabelText("Your question")).toHaveValue("first\nsecond");
    expect(ask).not.toHaveBeenCalled();
  });

  it("shows a question asked before a reload as still being answered, then shows its answer", async () => {
    const question = message("q", "user", "Anything urgent?");
    vi.spyOn(api, "listConversations").mockResolvedValue([{ id: "c1", title: "Anything urgent?", updated_at: "" }]);
    const later = deferred<Message[]>();
    vi.spyOn(api, "listMessages")
      .mockResolvedValueOnce([question])
      .mockReturnValueOnce(later.promise);
    render(<Assistant workspaceId="w1" />);

    await userEvent.click(await screen.findByRole("button", { name: "Anything urgent?" }));
    expect(await screen.findByText(/still being answered/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();

    later.resolve([question, message("a", "assistant", "Nothing urgent.")]);
    expect(await screen.findByText("Nothing urgent.")).toBeInTheDocument();
    expect(screen.queryByText(/still being answered/)).toBeNull();
  });

  it("says no answer was recorded when a question is far older than any answer could take", async () => {
    const old = message("q", "user", "Anything urgent?", "2026-11-01T00:00:00Z");
    vi.spyOn(api, "listConversations").mockResolvedValue([{ id: "c1", title: "Anything urgent?", updated_at: "" }]);
    const list = vi.spyOn(api, "listMessages").mockResolvedValue([old]);
    render(<Assistant workspaceId="w1" />);

    await userEvent.click(await screen.findByRole("button", { name: "Anything urgent?" }));
    expect(await screen.findByText(/No answer was recorded/)).toBeInTheDocument();
    expect(list).toHaveBeenCalledTimes(1);
  });

  it("keeps an answer out of a different conversation opened while it was being prepared", async () => {
    vi.spyOn(api, "listConversations").mockResolvedValue([{ id: "c2", title: "Older question", updated_at: "" }]);
    vi.spyOn(api, "createConversation").mockResolvedValue({ id: "c1", title: "New", updated_at: "" });
    const answer = deferred<{ user_message: Message; answer: Message }>();
    vi.spyOn(api, "ask").mockReturnValue(answer.promise);
    vi.spyOn(api, "listMessages").mockResolvedValue([message("o1", "user", "Old?"), message("o2", "assistant", "Old answer.")]);
    render(<Assistant workspaceId="w1" />);

    await userEvent.type(await screen.findByLabelText("Your question"), "New question{Enter}");
    expect(await screen.findByText(/preparing an answer/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Older question" }));
    expect(await screen.findByText("Old answer.")).toBeInTheDocument();
    expect(screen.queryByText(/preparing an answer/)).toBeNull();

    answer.resolve({ user_message: message("q", "user", "New question"), answer: message("a", "assistant", "New answer.") });
    await waitFor(() => expect(api.ask).toHaveBeenCalled());
    await new Promise((done) => setTimeout(done, 0));
    expect(screen.queryByText("New answer.")).toBeNull();
  });
});

describe("App", () => {
  it("keeps a question being answered when the person visits another tab and comes back", async () => {
    vi.spyOn(api, "me").mockResolvedValue({
      user: { id: "u1", email: "me@example.test", display_name: "Me" },
      workspaces: [{ id: "w1", name: "Main", slug: "main", role: "member" }],
    });
    vi.spyOn(api, "listConversations").mockResolvedValue([]);
    vi.spyOn(api, "listSources").mockResolvedValue([]);
    vi.spyOn(api, "listDocuments").mockResolvedValue([]);
    vi.spyOn(api, "createConversation").mockResolvedValue({ id: "c1", title: "Q", updated_at: "" });
    const answer = deferred<{ user_message: Message; answer: Message }>();
    vi.spyOn(api, "ask").mockReturnValue(answer.promise);
    render(<App />);

    await userEvent.type(await screen.findByLabelText("Your question"), "Anything urgent?{Enter}");
    expect(await screen.findByText(/preparing an answer/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Knowledge" }));
    answer.resolve({ user_message: message("q", "user", "Anything urgent?"), answer: message("a", "assistant", "Nothing urgent.") });
    await userEvent.click(screen.getByRole("button", { name: "Assistant" }));

    expect(await screen.findByText("Nothing urgent.")).toBeVisible();
  });
});
