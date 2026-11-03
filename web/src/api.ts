/** Typed API client. Mirrors backend/src/opsassist/api/schemas.py. */

export type Role = "owner" | "admin" | "member" | "viewer";
export type Visibility = "workspace" | "restricted";
export type GroundingStatus =
  | "grounded"
  | "partially_grounded"
  | "unsupported"
  | "extractive"
  | "no_evidence"
  | "model_unavailable"
  | "withheld";

export interface User {
  id: string;
  email: string;
  display_name: string;
}
export interface Workspace {
  id: string;
  name: string;
  slug: string;
  role: Role;
}
export interface Me {
  user: User;
  workspaces: Workspace[];
}
export interface Source {
  id: string;
  name: string;
  description: string;
  visibility: Visibility;
  document_count: number;
  can_read: boolean;
}
export interface DocumentInfo {
  id: string;
  source_id: string;
  title: string;
  content_type: string;
  updated_at: string;
  current_version_number: number | null;
  latest_version_number: number;
  latest_status: "pending" | "processing" | "ready" | "failed";
  latest_error: string | null;
  chunk_count: number;
  indexed_at: string | null;
}
export interface Citation {
  marker: string;
  chunk_id: string;
  document_id: string;
  source_id: string;
  document_title: string | null;
  source_name: string | null;
  version_number: number | null;
  excerpt: string | null;
  section_path: string[];
  page: number | null;
  line_start: number | null;
  line_end: number | null;
  indexed_at: string | null;
  stale: boolean;
  used: boolean;
  access_revoked: boolean;
}
export interface Grounding {
  status: GroundingStatus;
  provider: string | null;
  model: string | null;
  dense_available: boolean;
  uncited_sentences: number;
  removed_citations: string[];
  stale_sources: number;
  notes: string[];
}
export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  grounding: Grounding | null;
  created_at: string;
}
export interface Conversation {
  id: string;
  title: string;
  updated_at: string;
}
export interface Chunk {
  id: string;
  document_title: string;
  source_name: string;
  version_number: number;
  content: string;
  section_path: string[];
  page: number | null;
  line_start: number | null;
  line_end: number | null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId: string | null,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, { credentials: "same-origin", ...init });
  } catch {
    throw new ApiError(0, "network_error", "The server could not be reached.", null);
  }
  if (response.status === 204) return undefined as T;
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const error = (body as { error?: { code?: string; message?: string; request_id?: string } } | null)?.error;
    throw new ApiError(
      response.status,
      error?.code ?? "unknown_error",
      error?.message ?? `Request failed with status ${response.status}.`,
      error?.request_id ?? null,
    );
  }
  return body as T;
}

function json(method: string, body: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

export const api = {
  me: () => request<Me>("/me"),
  login: (email: string, password: string) => request<User>("/auth/login", json("POST", { email, password })),
  logout: () => request<void>("/auth/logout", { method: "POST" }),

  listSources: (ws: string) => request<Source[]>(`/workspaces/${ws}/sources`),
  createSource: (ws: string, name: string, visibility: Visibility) =>
    request<Source>(`/workspaces/${ws}/sources`, json("POST", { name, visibility })),
  deleteSource: (ws: string, id: string) => request<void>(`/workspaces/${ws}/sources/${id}`, { method: "DELETE" }),

  listDocuments: (ws: string, source: string) => request<DocumentInfo[]>(`/workspaces/${ws}/sources/${source}/documents`),
  uploadDocument: (ws: string, source: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ document: DocumentInfo; deduplicated: boolean }>(`/workspaces/${ws}/sources/${source}/documents`, {
      method: "POST",
      body: form,
    });
  },
  deleteDocument: (ws: string, id: string) => request<void>(`/workspaces/${ws}/documents/${id}`, { method: "DELETE" }),
  getChunk: (ws: string, id: string) => request<Chunk>(`/workspaces/${ws}/chunks/${id}`),

  listConversations: (ws: string) => request<Conversation[]>(`/workspaces/${ws}/conversations`),
  createConversation: (ws: string, title: string) => request<Conversation>(`/workspaces/${ws}/conversations`, json("POST", { title })),
  listMessages: (ws: string, id: string) => request<Message[]>(`/workspaces/${ws}/conversations/${id}/messages`),
  ask: (ws: string, id: string, question: string) =>
    request<{ user_message: Message; answer: Message }>(`/workspaces/${ws}/conversations/${id}/messages`, json("POST", { question })),
};

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.requestId ? `${error.message} (reference ${error.requestId})` : error.message;
  }
  return "Something went wrong.";
}
