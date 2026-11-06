import { useCallback, useEffect, useState, type ChangeEvent, type FormEvent } from "react";

import { api, errorMessage, type DocumentInfo, type Role, type Source, type Visibility } from "../api";

const POLL_MS = 3000;

const DOCUMENT_STATUS: Record<DocumentInfo["latest_status"], { label: string; tone: string }> = {
  pending: { label: "Queued", tone: "neutral" },
  processing: { label: "Indexing", tone: "neutral" },
  ready: { label: "Indexed", tone: "good" },
  failed: { label: "Failed", tone: "bad" },
};

interface DocumentsProps {
  workspaceId: string;
  source: Source;
  canWrite: boolean;
  onChanged: () => void;
}

function Documents({ workspaceId, source, canWrite, onChanged }: DocumentsProps) {
  const [documents, setDocuments] = useState<DocumentInfo[] | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);

  const load = useCallback(async () => {
    try {
      setDocuments(await api.listDocuments(workspaceId, source.id));
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }, [workspaceId, source.id]);

  useEffect(() => {
    void load();
  }, [load]);

  const inProgress = documents?.some((d) => d.latest_status === "pending" || d.latest_status === "processing");
  useEffect(() => {
    if (!inProgress) return;
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [inProgress, load]);

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    setUploading(true);
    setError(null);
    setMessage(null);
    const outcomes: string[] = [];
    for (const file of files) {
      try {
        const result = await api.uploadDocument(workspaceId, source.id, file);
        outcomes.push(result.deduplicated ? `${file.name}: unchanged, not reprocessed` : `${file.name}: queued for indexing`);
      } catch (caught) {
        outcomes.push(`${file.name}: failed. ${errorMessage(caught)}`);
      }
    }
    setMessage(outcomes.join(" · "));
    setUploading(false);
    void load();
    onChanged();
  }

  async function remove(document: DocumentInfo) {
    if (!window.confirm(`Permanently delete "${document.title}" and all its versions? Answers that cite it will be withheld.`)) return;
    try {
      await api.deleteDocument(workspaceId, document.id);
      void load();
      onChanged();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  return (
    <div className="documents">
      {canWrite && (
        <label className="upload">
          <span>{uploading ? "Uploading…" : "Upload Markdown, text or PDF"}</span>
          <input type="file" accept=".md,.markdown,.txt,.pdf" multiple disabled={uploading} onChange={upload} />
        </label>
      )}
      {message && <p role="status" className="muted">{message}</p>}
      {error && <p role="alert" className="error">{error}</p>}
      {documents === null && !error && <p className="muted">Loading documents…</p>}
      {documents?.length === 0 && <p className="muted">No documents in this source yet.</p>}
      {documents && documents.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Document</th>
              <th>Status</th>
              <th>Active version</th>
              <th>Chunks</th>
              <th>Indexed</th>
              <th><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {documents.map((document) => {
              const status = DOCUMENT_STATUS[document.latest_status];
              return (
                <tr key={document.id}>
                  <td>{document.title}</td>
                  <td>
                    <span className={`badge badge-${status.tone}`}>{status.label}</span>
                    {document.latest_error && <div className="error small">{document.latest_error}</div>}
                    {document.latest_status !== "ready" && document.current_version_number !== null && (
                      <div className="muted small">Version {document.current_version_number} stays active until this finishes.</div>
                    )}
                  </td>
                  <td>{document.current_version_number ?? "None"}</td>
                  <td>{document.chunk_count}</td>
                  <td>{document.indexed_at ? new Date(document.indexed_at).toLocaleString() : "Not yet"}</td>
                  <td>
                    {canWrite && (
                      <button type="button" className="danger-ghost" onClick={() => void remove(document)}>
                        Delete
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function Knowledge({ workspaceId, role }: { workspaceId: string; role: Role }) {
  const [sources, setSources] = useState<Source[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [visibility, setVisibility] = useState<Visibility>("workspace");
  const [error, setError] = useState<string | null>(null);
  const canWrite = role !== "viewer";
  const isAdmin = role === "admin" || role === "owner";

  const load = useCallback(async () => {
    try {
      setSources(await api.listSources(workspaceId));
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }, [workspaceId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function create(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const source = await api.createSource(workspaceId, name.trim(), visibility);
      setName("");
      setSelected(source.id);
      void load();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  async function removeSource(source: Source) {
    if (!window.confirm(`Permanently delete source "${source.name}", all its documents and their index data?`)) return;
    try {
      await api.deleteSource(workspaceId, source.id);
      if (selected === source.id) setSelected(null);
      void load();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  return (
    <div className="knowledge">
      <h1>Knowledge</h1>
      <p className="page-intro">
        Documents the assistant can search when it answers, such as policies, runbooks and meeting notes. Live systems like email and
        GitHub belong on the Integrations page instead.
      </p>
      <ol className="steps">
        <li>
          <strong>Create a source</strong>: a folder that groups related documents and sets who can read them.
        </li>
        <li>
          <strong>Upload files</strong> to it: Markdown, text or PDF. Each is indexed in the background.
        </li>
        <li>
          <strong>Ask on the Assistant page.</strong> Answers cite the exact document and section.
        </li>
      </ol>
      {error && <p role="alert" className="error">{error}</p>}
      {canWrite && (
        <form className="source-form card" onSubmit={create}>
          <label>
            New source name
            <input required placeholder="For example: Engineering runbooks" maxLength={200} value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label>
            Who can read it
            <select value={visibility} onChange={(e) => setVisibility(e.target.value as Visibility)}>
              <option value="workspace">Everyone in this workspace</option>
              <option value="restricted">Only people granted access</option>
            </select>
          </label>
          <button type="submit">Create source</button>
        </form>
      )}
      {sources === null && !error && <p className="muted">Loading sources…</p>}
      {sources?.length === 0 && <p className="muted">No sources yet. Create one, then upload documents to it.</p>}
      <ul className="source-list">
        {sources?.map((source) => (
          <li key={source.id} className="card">
            <div className="source-row">
              <button
                type="button"
                className="source-name"
                aria-expanded={selected === source.id}
                disabled={!source.can_read}
                onClick={() => setSelected(selected === source.id ? null : source.id)}
              >
                {source.name}
              </button>
              <span className={`badge badge-${source.visibility === "restricted" ? "warn" : "neutral"}`}>
                {source.visibility === "restricted" ? "Restricted" : "Workspace"}
              </span>
              <span className="muted">{source.document_count} document(s)</span>
              {!source.can_read && <span className="muted">You manage this source but have no read access.</span>}
              {isAdmin && (
                <button type="button" className="danger-ghost push-right" onClick={() => void removeSource(source)}>
                  Delete source
                </button>
              )}
            </div>
            {selected === source.id && source.can_read && <Documents workspaceId={workspaceId} source={source} canWrite={canWrite} onChanged={() => void load()} />}
          </li>
        ))}
      </ul>
    </div>
  );
}
