import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { api, errorMessage, type Citation, type Conversation, type Message } from "../api";
import { MessageView } from "../components/Answer";
import { CitationPanel } from "../components/CitationPanel";

// How often to check for an answer to a question asked before this page was (re)loaded, and when to
// stop. The server's whole-question limit is at most this in any configuration used so far.
const WAIT_POLL_MS = 4000;
const WAIT_GIVE_UP_MS = 20 * 60 * 1000;

/** The last question in a conversation, if no answer follows it yet. */
export function unansweredQuestion(messages: Message[]): Message | null {
  const last = messages[messages.length - 1];
  return last && last.role === "user" ? last : null;
}

const SUGGESTIONS = [
  "Summarize the emails I received this week that need a reply.",
  "Which issues are open in our GitHub repository, and which look most urgent?",
  "What do our uploaded documents say about the release process?",
];

export function Assistant({ workspaceId }: { workspaceId: string }) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  // The conversation the pending question belongs to; null while it is being created.
  const [pendingIn, setPendingIn] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [citation, setCitation] = useState<Citation | null>(null);
  const [waiting, setWaiting] = useState<"answering" | "gave_up" | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // The open conversation, read when an answer arrives, after the person may have moved on.
  const activeRef = useRef<string | null>(null);
  activeRef.current = activeId;

  const refresh = useCallback(async () => {
    try {
      setConversations(await api.listConversations(workspaceId));
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }, [workspaceId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: "smooth" });
  }, [messages, pending, waiting]);

  // A question asked before a reload, or in another tab, is answered on the server regardless.
  // Show that it is still being answered, and pick the answer up when it is stored.
  const pendingHere = Boolean(pending) && pendingIn === activeId;
  const unanswered = pendingHere ? null : unansweredQuestion(messages);
  useEffect(() => {
    if (!activeId || !unanswered) {
      setWaiting(null);
      return;
    }
    const conversationId = activeId;
    const askedAt = Date.parse(unanswered.created_at);
    let cancelled = false;
    const check = async () => {
      if (Date.now() - askedAt > WAIT_GIVE_UP_MS) {
        setWaiting("gave_up");
        return;
      }
      setWaiting("answering");
      try {
        const latest = await api.listMessages(workspaceId, conversationId);
        if (!cancelled && !unansweredQuestion(latest)) setMessages(latest);
      } catch {
        // A failed check is retried on the next tick. It never ends the wait by itself.
      }
    };
    void check();
    const timer = window.setInterval(() => void check(), WAIT_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeId, unanswered, workspaceId]);

  async function open(id: string) {
    setActiveId(id);
    setCitation(null);
    setError(null);
    try {
      const loaded = await api.listMessages(workspaceId, id);
      // The question this page is waiting on is already stored. It shows as pending, not twice.
      const own = id === pendingIn && pending ? unansweredQuestion(loaded) : null;
      setMessages(own ? loaded.slice(0, -1) : loaded);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }

  function startNew() {
    setActiveId(null);
    setMessages([]);
    setCitation(null);
    setError(null);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const text = question.trim();
    if (!text || pending || (waiting === "answering" && unanswered)) return;
    setPending(text);
    setPendingIn(activeId);
    setQuestion("");
    setError(null);
    try {
      const id = activeId ?? (await api.createConversation(workspaceId, text.slice(0, 80))).id;
      setActiveId(id);
      setPendingIn(id);
      activeRef.current = id;
      const result = await api.ask(workspaceId, id, text);
      // If another conversation is open now, the answer stays stored and shows when this one is opened.
      if (activeRef.current === id) setMessages((current) => [...current, result.user_message, result.answer]);
      void refresh();
    } catch (caught) {
      // Restore the question, so a failure never loses what the user typed.
      setQuestion(text);
      setError(errorMessage(caught));
    } finally {
      setPending(null);
      setPendingIn(null);
    }
  }

  return (
    <div className={`assistant ${citation ? "with-panel" : ""}`}>
      <nav className="conversation-list" aria-label="Conversations">
        <button type="button" className="secondary full" onClick={startNew}>
          New question
        </button>
        {conversations.length === 0 && <p className="muted">No conversations yet.</p>}
        <ul>
          {conversations.map((conversation) => (
            <li key={conversation.id}>
              <button
                type="button"
                className={conversation.id === activeId ? "conversation active" : "conversation"}
                aria-current={conversation.id === activeId ? "true" : undefined}
                onClick={() => void open(conversation.id)}
              >
                {conversation.title}
              </button>
            </li>
          ))}
        </ul>
      </nav>

      <section className="chat">
        <div className="messages" aria-live="polite">
          {messages.length === 0 && !pendingHere && (
            <div className="empty">
              <h2>Ask a question</h2>
              <p>The assistant answers from two places:</p>
              <ul className="where-answers-come-from">
                <li>
                  <strong>Documents</strong> uploaded on the Knowledge page, such as policies, runbooks and notes.
                </li>
                <li>
                  <strong>Live data</strong> from systems connected on the Integrations page, such as your Gmail or GitHub.
                </li>
              </ul>
              <p className="muted">
                Each fact links to where it came from, so you can check it. It only reads: it never sends, changes or deletes anything.
                Follow-up questions in the same conversation can refer to earlier ones.
              </p>
              <div className="suggestions">
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    className="ghost"
                    onClick={() => {
                      setQuestion(suggestion);
                      // Focus the box, so Enter sends the question rather than pressing this button again.
                      inputRef.current?.focus();
                    }}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          )}
          {messages.map((message) => (
            <MessageView key={message.id} message={message} onCite={setCitation} />
          ))}
          {pendingHere && (
            <>
              <div className="message message-user">
                <p>{pending}</p>
              </div>
              <div className="message message-assistant" role="status">
                <p className="muted">Searching sources and preparing an answer…</p>
              </div>
            </>
          )}
          {waiting === "answering" && unanswered && (
            <div className="message message-assistant" role="status">
              <p className="muted">This question is still being answered. The answer will appear here when it is ready.</p>
            </div>
          )}
          {waiting === "gave_up" && unanswered && (
            <div className="message message-assistant" role="status">
              <p className="muted">No answer was recorded for this question. It may have been interrupted. Ask it again to retry.</p>
            </div>
          )}
          <div ref={endRef} />
        </div>
        {error && (
          <p role="alert" className="error chat-error">
            {error}
          </p>
        )}
        <form className="ask" onSubmit={submit}>
          <label htmlFor="question" className="sr-only">
            Your question
          </label>
          <textarea
            id="question"
            ref={inputRef}
            rows={2}
            maxLength={4000}
            value={question}
            placeholder="Ask a question…"
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
          />
          <button type="submit" disabled={Boolean(pending) || (waiting === "answering" && Boolean(unanswered)) || !question.trim()}>
            Ask
          </button>
        </form>
      </section>

      {citation && <CitationPanel workspaceId={workspaceId} citation={citation} onClose={() => setCitation(null)} />}
    </div>
  );
}
