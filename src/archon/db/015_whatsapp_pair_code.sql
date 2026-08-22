-- Migration 15: WhatsApp phone-number pairing (code, not QR).
--
-- WHY THE QR PATH IS NOT ENOUGH. Migration 013 assumed the tenant scans a QR
-- shown by the app. That fails in the actual product case: the user links their
-- OWN WhatsApp from the SAME phone that is displaying the app, and a phone
-- cannot scan a code on its own screen. WhatsApp's answer is "Link with phone
-- number instead" (Linked Devices -> Link a Device), which shows an 8-character
-- code the user types into WhatsApp. whatsmeow exposes it as PairPhone, surfaced
-- by neonize 0.4.3.post0 as Client.PairPhone(phone, ...) -> code.
--
-- Three columns, all nullable so existing rows upgrade untouched:
--
--   phone_e164           the number the user ASKED to pair, recorded when the
--                        link starts. Distinct from `phone_jid`, which is the
--                        JID WhatsApp confirms only after pairing succeeds —
--                        keeping them apart means a failed attempt still says
--                        which number was tried, which is most of the useful
--                        diagnostic when a user mistypes their own number.
--   pair_code            the 8-char code, held only while it is being shown.
--                        Cleared on pair/fail. NOT encrypted, deliberately: it
--                        is not a credential an attacker can spend. It is typed
--                        into WhatsApp by the user to authorise THIS pending
--                        session, so holding a copy grants nothing without also
--                        being that session — unlike `session_envelope`, which
--                        IS the account and is AEAD-wrapped.
--   pair_code_expires_at when we stop treating the code as current and require
--                        a fresh request. OUR bookkeeping horizon, not a value
--                        WhatsApp reports back, and named so the app renders a
--                        "request a new code" prompt rather than pretending to
--                        count down WhatsApp's own timer.
--
-- `status` also gains 'awaiting_code' (code issued, waiting for the user to
-- enter it). It is a free-text column with no CHECK constraint, so this is a
-- documentation change rather than a schema one — but the app drives its UI off
-- these strings, so the set is written down where the table is defined:
--   not_linked | pending | awaiting_code | paired | failed | logged_out | banned

ALTER TABLE whatsapp_links ADD COLUMN phone_e164 TEXT;
ALTER TABLE whatsapp_links ADD COLUMN pair_code TEXT;
ALTER TABLE whatsapp_links ADD COLUMN pair_code_expires_at TEXT;
