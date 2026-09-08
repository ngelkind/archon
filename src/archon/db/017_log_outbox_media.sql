-- Re-attach cached media to deletion/edit cards.
--
-- A deleted PHOTO used to log as a text-only card ("[photo]"), so the whole
-- point of message auditing — seeing what vanished — was lost for media. The
-- outbox now carries the cached file path; the worker re-sends the media with
-- the card as its caption when the file still exists, and falls back to text
-- when it does not.
ALTER TABLE log_outbox ADD COLUMN media_path TEXT;
