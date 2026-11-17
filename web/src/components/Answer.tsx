import type { Citation, Grounding, GroundingStatus, Message } from "../api";

const STATUS: Record<GroundingStatus, { label: string; tone: "good" | "warn" | "bad" | "neutral"; help: string }> = {
  grounded: { label: "Cited answer", tone: "good", help: "Every statement cites a source. Check the sources for important decisions." },
  partially_grounded: { label: "Partly cited", tone: "warn", help: "Some statements have no citation. Verify them before relying on them." },
  unsupported: { label: "Not verifiable", tone: "bad", help: "The model's answer cited no sources, so it was discarded. These are the excerpts found." },
  extractive: { label: "Excerpts only", tone: "neutral", help: "No model is configured. These are retrieved excerpts, not a generated answer." },
  no_evidence: { label: "No source found", tone: "neutral", help: "Nothing in the sources you can access answers this." },
  model_unavailable: { label: "Model unavailable", tone: "bad", help: "No answer was generated. These are the excerpts found." },
  budget_exceeded: { label: "Limit reached", tone: "bad", help: "This question hit its time or token limit, so no answer was generated. These are the excerpts found." },
  withheld: { label: "Withheld", tone: "bad", help: "A source of this answer was deleted or your access to it ended." },
};

const TOOL_CALL_STATUS: Record<string, string> = {
  succeeded: "Returned data",
  tool_error: "Reported an error",
  failed: "Did not complete",
  denied: "Refused",
  skipped: "Not run",
};

/** A short name for where a citation came from, for labels and buttons. */
export function citationName(citation: Citation): string {
  return (citation.kind === "tool" ? citation.tool_id : citation.document_title) ?? "unavailable source";
}

function ToolCalls({ grounding }: { grounding: Grounding }) {
  if (grounding.tool_calls.length === 0) return null;
  return (
    <div className="tool-calls">
      <h3>Live systems checked</h3>
      <ul>
        {grounding.tool_calls.map((call, index) => (
          <li key={`${call.tool_id}-${index}`}>
            <span className={`badge badge-${call.status === "succeeded" ? "good" : "bad"}`}>
              {TOOL_CALL_STATUS[call.status] ?? call.status}
            </span>{" "}
            {call.tool_id}
            <span className="source-meta"> {JSON.stringify(call.arguments)}</span>
            {call.fetched_at && <span className="source-meta"> · fetched {new Date(call.fetched_at).toLocaleString()}</span>}
            {call.code && <span className="source-meta"> · {call.code}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

const MARKER_SPLIT = /(\[S\d+\])/;
const IS_MARKER = /^\[S\d+\]$/;

interface AnswerTextProps {
  text: string;
  citations: Citation[];
  onCite: (citation: Citation) => void;
}

/** Renders as text with citation buttons. Content is never parsed as HTML or Markdown (ADR-0007). */
export function AnswerText({ text, citations, onCite }: AnswerTextProps) {
  const byMarker = new Map(citations.map((citation) => [citation.marker, citation]));
  return (
    <p className="answer-text">
      {text.split(MARKER_SPLIT).map((part, index) => {
        const citation = IS_MARKER.test(part) ? byMarker.get(part.slice(1, -1)) : undefined;
        if (!citation) return <span key={index}>{part}</span>;
        const where = citationName(citation);
        return (
          <button
            key={index}
            type="button"
            className="cite-chip"
            aria-label={`Citation ${citation.marker}: ${where}`}
            onClick={() => onCite(citation)}
          >
            {citation.marker}
          </button>
        );
      })}
    </p>
  );
}

export function GroundingBadge({ grounding }: { grounding: Grounding }) {
  const status = STATUS[grounding.status];
  return (
    <div className="grounding">
      <span className={`badge badge-${status.tone}`}>{status.label}</span>
      <span className="grounding-help">{status.help}</span>
      {grounding.model && <span className="grounding-model">Model: {grounding.model}</span>}
      {grounding.notes.length > 0 && (
        <ul className="grounding-notes">
          {grounding.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function locationLabel(citation: {
  section_path: string[];
  page: number | null;
  line_start: number | null;
  line_end: number | null;
}): string {
  const parts: string[] = [];
  if (citation.section_path.length > 0) parts.push(citation.section_path.join(" › "));
  if (citation.page !== null) parts.push(`page ${citation.page}`);
  if (citation.line_start !== null) {
    parts.push(
      citation.line_end !== null && citation.line_end !== citation.line_start
        ? `lines ${citation.line_start}–${citation.line_end}`
        : `line ${citation.line_start}`,
    );
  }
  return parts.join(", ");
}

interface MessageViewProps {
  message: Message;
  onCite: (citation: Citation) => void;
}

export function MessageView({ message, onCite }: MessageViewProps) {
  if (message.role === "user") {
    return (
      <div className="message message-user">
        <p>{message.content}</p>
      </div>
    );
  }
  const cited = message.citations.filter((citation) => citation.used);
  return (
    <div className="message message-assistant">
      {message.grounding && <GroundingBadge grounding={message.grounding} />}
      <AnswerText text={message.content} citations={message.citations} onCite={onCite} />
      {message.grounding && <ToolCalls grounding={message.grounding} />}
      {cited.length > 0 && (
        <div className="sources">
          <h3>Sources</h3>
          <ul>
            {cited.map((citation) => (
              <li key={citation.marker}>
                <button type="button" className="source-link" onClick={() => onCite(citation)}>
                  <span className="cite-chip-static">{citation.marker}</span>{" "}
                  {citation.access_revoked ? (
                    "Source no longer available"
                  ) : citation.kind === "tool" ? (
                    <>
                      {citation.tool_id}
                      <span className="source-meta"> · live data, fetched {citation.fetched_at ? new Date(citation.fetched_at).toLocaleString() : "at an unknown time"}</span>
                      {citation.truncated && <span className="badge badge-warn"> Incomplete</span>}
                    </>
                  ) : (
                    <>
                      {citation.document_title}
                      <span className="source-meta"> · {citation.source_name} · v{citation.version_number}</span>
                      {locationLabel(citation) && <span className="source-meta"> · {locationLabel(citation)}</span>}
                      {citation.stale && <span className="badge badge-warn"> May be outdated</span>}
                    </>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
