-- Migration 3: edit/delete logging defaults.
-- DMs log by default (opt-out); groups/channels are off unless whitelisted
-- (opt-in via chat_log_policy_set). Reset existing group/channel rows to off
-- so groups stop spamming the log channel.
UPDATE chats SET log_deletes = 0 WHERE kind IN ('group', 'channel');
