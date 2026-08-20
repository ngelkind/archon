-- Migration 2: view-once / self-destruct media capture.
-- Separate from the whitelist: a chat can have capture on without being
-- whitelisted for the LLM pipeline, and vice versa.
ALTER TABLE chats ADD COLUMN capture_media INTEGER NOT NULL DEFAULT 0;
