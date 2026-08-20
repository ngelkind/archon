-- Let a queued auto-reply remember which message it answers, so the send can
-- quote/tag it (Telegram reply_to).
ALTER TABLE pending_replies ADD COLUMN reply_to TEXT;
