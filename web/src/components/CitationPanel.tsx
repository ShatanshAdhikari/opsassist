import { useState } from "react";

import { ApiError, api, errorMessage, type Chunk, type Citation } from "../api";
import { locationLabel } from "./Answer";

interface Props {
  workspaceId: string;
  citation: Citation;
  onClose: () => void;
}

/** Evidence viewer. "Open current source" re-checks access on the server. */
export function CitationPanel({ workspaceId, citation, onClose }: Props) {
  const [chunk, setChunk] = useState<Chunk | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function openSource() {
    setError(null);
    try {
      setChunk(await api.getChunk(workspaceId, citation.chunk_id));
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 404
          ? "This excerpt is no longer in the current version of the document, or your access ended."
          : errorMessage(caught),
      );
    }
  }

  return (
    <aside className="citation-panel" aria-label={`Source ${citation.marker}`}>
      <header>
        <h2>Source {citation.marker}</h2>
        <button type="button" className="ghost" onClick={onClose}>
          Close
        </button>
      </header>
      {citation.access_revoked ? (
        <p role="status">This source was deleted or your access to it ended.</p>
      ) : (
        <>
          <p className="panel-title">{citation.document_title}</p>
          <dl>
            <dt>Knowledge source</dt>
            <dd>{citation.source_name}</dd>
            <dt>Version</dt>
            <dd>{citation.version_number}</dd>
            {locationLabel(citation) && (
              <>
                <dt>Location</dt>
                <dd>{locationLabel(citation)}</dd>
              </>
            )}
            <dt>Indexed</dt>
            <dd>{citation.indexed_at ? new Date(citation.indexed_at).toLocaleString() : "Unknown"}</dd>
          </dl>
          {citation.stale && <p className="badge badge-warn">This document has not been updated recently and may be outdated.</p>}
          <h3>Excerpt used for this answer</h3>
          <pre className="excerpt">{citation.excerpt}</pre>
          <button type="button" className="secondary" onClick={openSource}>
            Open current source
          </button>
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          {chunk && (
            <>
              <h3>Current source text (version {chunk.version_number})</h3>
              <pre className="excerpt">{chunk.content}</pre>
            </>
          )}
        </>
      )}
    </aside>
  );
}
