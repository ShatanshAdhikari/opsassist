import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { api, errorMessage, type Citation, type Conversation, type Message } from "../api";
import { MessageView } from "../components/Answer";
import { CitationPanel } from "../components/CitationPanel";

const SUGGESTIONS = [
  "Find the owner of this service and locate its runbook.",
  "What does our policy say about this?",
  "What was decided about this and why?",
];

export function Assistant({ workspaceId }: { workspaceId: string }) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [citation, setCitation] = useState<Citation | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

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
  }, [messages, pending]);

  async function open(id: string) {
    setActiveId(id);
    setCitation(null);
    setError(null);
    try {
      setMessages(await api.listMessages(workspaceId, id));
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
    if (!text || pending) return;
    setPending(text);
    setQuestion("");
    setError(null);
    try {
      const id = activeId ?? (await api.createConversation(workspaceId, text.slice(0, 80))).id;
      setActiveId(id);
      const result = await api.ask(workspaceId, id, text);
      setMessages((current) => [...current, result.user_message, result.answer]);
      void refresh();
    } catch (caught) {
      // Restore the question, so a failure never loses what the user typed.
      setQuestion(text);
      setError(errorMessage(caught));
    } finally {
      setPending(null);
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
          {messages.length === 0 && !pending && (
            <div className="empty">
              <h2>Ask about your organization's documents</h2>
              <p className="muted">Answers come only from sources you can access, with citations you can inspect. Questions are answered independently, so include the full context each time.</p>
              <div className="suggestions">
                {SUGGESTIONS.map((suggestion) => (
                  <button key={suggestion} type="button" className="ghost" onClick={() => setQuestion(suggestion)}>
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          )}
          {messages.map((message) => (
            <MessageView key={message.id} message={message} onCite={setCitation} />
          ))}
          {pending && (
            <>
              <div className="message message-user">
                <p>{pending}</p>
              </div>
              <div className="message message-assistant" role="status">
                <p className="muted">Searching sources and preparing an answer…</p>
              </div>
            </>
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
          <button type="submit" disabled={Boolean(pending) || !question.trim()}>
            Ask
          </button>
        </form>
      </section>

      {citation && <CitationPanel workspaceId={workspaceId} citation={citation} onClose={() => setCitation(null)} />}
    </div>
  );
}
