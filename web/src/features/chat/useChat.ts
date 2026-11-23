"use client";

import { useReducer, useRef, useState } from "react";
import { useApi } from "@/lib/api/context";
import { isFailure } from "@/lib/api/errors";
import { createSession, postMessage, stopReply } from "./queries";
import { newReply, type Reply, reduceReply } from "./reply";
import { useReplyStream } from "./useReplyStream";

export type ChatMessage = { id: string; role: "user"; text: string } | { id: string; role: "assistant"; reply: Reply };

type Action =
  | { type: "add"; messages: ChatMessage[] }
  | { type: "event"; id: string; event: string; data: Record<string, unknown> };

function reduce(messages: ChatMessage[], action: Action): ChatMessage[] {
  if (action.type === "add") return [...messages, ...action.messages];
  return messages.map((message) =>
    message.role === "assistant" && message.id === action.id ? { ...message, reply: reduceReply(message.reply, action.event, action.data) } : message,
  );
}

/**
 * The conversation of one panel: a session made on the first message, the messages so far, and one open stream per running
 * reply. It lives above the dialog so a reply keeps arriving while the panel is closed.
 */
export function useChat() {
  const api = useApi();
  const [messages, dispatch] = useReducer(reduce, []);
  const [sendError, setSendError] = useState<unknown>(null);
  const [sending, setSending] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const session = useRef<string | null>(null);
  const texts = useRef(new Map<string, string>());
  const sessionOf = useRef(new Map<string, string>()); // the session each reply belongs to: a reply found running may be in another
  const followed = useRef(new Set<string>()); // replies picked up after a 409, not sent from this panel

  const open = useReplyStream((id, event, data) => {
    if (event === "text.delta" && typeof data.text === "string") texts.current.set(id, (texts.current.get(id) ?? "") + data.text);
    if (event === "message.snapshot" && typeof data.text === "string") texts.current.set(id, data.text);
    dispatch({ type: "event", id, event, data });
    // The reply is announced once, when it is whole, not word by word (FRONTEND_UI 7).
    if (event === "message.end") {
      setAnnouncement(texts.current.get(id) ?? "");
      if (followed.current.delete(id)) setSendError(null); // the reply that blocked the message is over: say no more about it
    }
  });

  const running = messages.find((message) => message.role === "assistant" && !message.reply.done);

  async function send(text: string, contextJobId: number | null): Promise<boolean> {
    setSendError(null);
    setSending(true);
    try {
      session.current ??= (await createSession(api)).id;
      const accepted = await postMessage(api, session.current, text, contextJobId);
      sessionOf.current.set(accepted.message_id, session.current);
      setAnnouncement("");
      dispatch({
        type: "add",
        messages: [
          { id: accepted.user_message_id, role: "user", text },
          { id: accepted.message_id, role: "assistant", reply: newReply() },
        ],
      });
      open(accepted.message_id, accepted.stream_url);
      return true;
    } catch (error) {
      setSendError(error);
      follow(error);
      return false;
    } finally {
      setSending(false);
    }
  }

  /**
   * A 409 `reply_running` names the reply that is still being written, one this panel does not know after a reload: show it
   * as the running reply, so Stop is offered for it, and open its stream so waiting shows it arrive (the stream starts with a
   * snapshot of the text so far).
   */
  function follow(error: unknown) {
    if (!isFailure(error, "conflict") || error.details.state !== "reply_running") return;
    const { message_id: id, session_id: where } = error.details;
    if (typeof id !== "string" || typeof where !== "string") return;
    if (messages.some((message) => message.id === id)) return; // already shown and streaming
    sessionOf.current.set(id, where);
    followed.current.add(id);
    dispatch({ type: "add", messages: [{ id, role: "assistant", reply: newReply() }] });
    open(id, `/v1/chat/sessions/${where}/messages/${id}/stream`);
  }

  async function stop() {
    const where = running === undefined ? undefined : sessionOf.current.get(running.id);
    if (running === undefined || where === undefined) return;
    try {
      await stopReply(api, where, running.id);
    } catch (error) {
      setSendError(error);
    }
  }

  return { messages, send, stop, sending, running: running !== undefined, sendError, announcement };
}
