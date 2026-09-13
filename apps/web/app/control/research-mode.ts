import type { Message } from "./contracts";

export type ConversationMode = "chat" | "research";

export function explicitConversationMode(content: string): ConversationMode | null {
  const command = /^\s*\/(research|chat)(?=\s|$)/i.exec(content);
  return command ? command[1].toLowerCase() as ConversationMode : null;
}

export function persistedConversationMode(messages: Message[]): ConversationMode {
  let mode: ConversationMode = "chat";
  for (const message of messages) {
    if (message.role === "user") mode = explicitConversationMode(message.content) ?? mode;
  }
  return mode;
}

export function conversationContent(content: string, mode: ConversationMode, persistedMode: ConversationMode): string {
  if (explicitConversationMode(content) || mode === persistedMode) return content;
  return `/${mode} ${content}`;
}
