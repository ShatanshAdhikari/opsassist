import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Citation, GroundingStatus, Message } from "../api";
import { MessageView, locationLabel } from "./Answer";

const citation: Citation = {
  marker: "S1",
  kind: "document",
  chunk_id: "c1",
  document_id: "d1",
  source_id: "s1",
  tool_id: null,
  invocation_id: null,
  fetched_at: null,
  arguments: null,
  truncated: false,
  document_title: "Refund Policy",
  source_name: "Support",
  version_number: 2,
  excerpt: "Refunds need approval.",
  section_path: ["Policy", "Limits"],
  page: null,
  line_start: 4,
  line_end: 6,
  indexed_at: "2026-01-01T00:00:00Z",
  stale: false,
  used: true,
  access_revoked: false,
};

function message(status: GroundingStatus, content: string, citations: Citation[] = [citation]): Message {
  return {
    id: "m1",
    role: "assistant",
    content,
    citations,
    created_at: "2026-01-01T00:00:00Z",
    grounding: {
      status,
      provider: "ollama",
      model: "llama3",
      dense_available: true,
      uncited_sentences: 0,
      removed_citations: [],
      stale_sources: 0,
      notes: [],
      planner: "not_run",
      search_query: null,
      tool_calls: [],
    },
  };
}

const toolCitation: Citation = {
  ...citation,
  kind: "tool",
  chunk_id: null,
  document_id: null,
  source_id: null,
  tool_id: "github.pull_get",
  invocation_id: "i1",
  fetched_at: "2026-01-01T00:00:00Z",
  arguments: { number: 7 },
  document_title: null,
  source_name: null,
  version_number: null,
};

describe("MessageView", () => {
  it("turns markers into citation buttons that report the citation", async () => {
    const onCite = vi.fn();
    render(<MessageView message={message("grounded", "Refunds need approval [S1].")} onCite={onCite} />);
    await userEvent.click(screen.getByRole("button", { name: "Citation S1: Refund Policy" }));
    expect(onCite).toHaveBeenCalledWith(citation);
  });

  it("renders hostile output as inert text", () => {
    const hostile = '<img src=x onerror=alert(1)> <a href="http://evil.example">click</a> ![x](http://evil.example/a.png)';
    const { container } = render(<MessageView message={message("grounded", hostile, [])} onCite={vi.fn()} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
    expect(screen.getByText(/onerror/)).toBeInTheDocument();
  });

  it("leaves an unknown marker as plain text and not as a button", () => {
    render(<MessageView message={message("grounded", "Claim [S9].", [citation])} onCite={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /S9/ })).toBeNull();
  });

  it.each<[GroundingStatus, string]>([
    ["grounded", "Cited answer"],
    ["partially_grounded", "Partly cited"],
    ["unsupported", "Not verifiable"],
    ["extractive", "Excerpts only"],
    ["no_evidence", "No source found"],
    ["model_unavailable", "Model unavailable"],
    ["budget_exceeded", "Limit reached"],
    ["withheld", "Withheld"],
  ])("labels %s distinctly", (status, label) => {
    render(<MessageView message={message(status, "text", [])} onCite={vi.fn()} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("does not present a failure with a success label", () => {
    render(<MessageView message={message("model_unavailable", "text", [])} onCite={vi.fn()} />);
    expect(screen.queryByText("Cited answer")).toBeNull();
    expect(screen.getByText(/No answer was generated/)).toBeInTheDocument();
  });

  it("shows revoked citations without their metadata", () => {
    const revoked: Citation = { ...citation, document_title: null, excerpt: null, access_revoked: true };
    render(<MessageView message={message("withheld", "This answer is withheld.", [revoked])} onCite={vi.fn()} />);
    expect(screen.getByText(/Source no longer available/)).toBeInTheDocument();
    expect(screen.queryByText("Refund Policy")).toBeNull();
  });

  it("flags stale sources next to the citation", () => {
    render(<MessageView message={message("grounded", "Claim [S1].", [{ ...citation, stale: true }])} onCite={vi.fn()} />);
    expect(screen.getByText(/May be outdated/)).toBeInTheDocument();
  });

  it("surfaces notes such as removed citations", () => {
    const base = message("grounded", "Claim [S1].");
    const withNotes: Message = { ...base, grounding: { ...base.grounding!, notes: ["The model cited sources that do not exist."] } };
    render(<MessageView message={withNotes} onCite={vi.fn()} />);
    expect(screen.getByText(/do not exist/)).toBeInTheDocument();
  });
});

describe("live tool results", () => {
  it("names the tool and marks the source as live data", async () => {
    const onCite = vi.fn();
    render(<MessageView message={message("grounded", "PR 7 is open [S1].", [toolCitation])} onCite={onCite} />);
    expect(screen.getByText(/live data, fetched/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Citation S1: github.pull_get" }));
    expect(onCite).toHaveBeenCalledWith(toolCitation);
  });

  it("flags a cut-off result next to the citation", () => {
    render(<MessageView message={message("grounded", "Claim [S1].", [{ ...toolCitation, truncated: true }])} onCite={vi.fn()} />);
    expect(screen.getByText(/Incomplete/)).toBeInTheDocument();
  });

  it("never presents a failed call as having returned data", () => {
    const base = message("grounded", "Claim.", []);
    const failed: Message = {
      ...base,
      grounding: {
        ...base.grounding!,
        tool_calls: [{ tool_id: "github.pull_get", arguments: { number: 7 }, status: "failed", code: "timeout", invocation_id: null, fetched_at: null }],
      },
    };
    render(<MessageView message={failed} onCite={vi.fn()} />);
    expect(screen.getByText("Did not complete")).toBeInTheDocument();
    expect(screen.queryByText("Returned data")).toBeNull();
    expect(screen.getByText(/timeout/)).toBeInTheDocument();
  });

  it("shows nothing about live systems when none were checked", () => {
    render(<MessageView message={message("grounded", "Claim.", [])} onCite={vi.fn()} />);
    expect(screen.queryByText("Live systems checked")).toBeNull();
  });
});

describe("locationLabel", () => {
  it("combines section, page and lines", () => {
    expect(locationLabel(citation)).toBe("Policy › Limits, lines 4–6");
    expect(locationLabel({ section_path: [], page: 3, line_start: null, line_end: null })).toBe("page 3");
    expect(locationLabel({ section_path: [], page: null, line_start: 2, line_end: 2 })).toBe("line 2");
  });
});
