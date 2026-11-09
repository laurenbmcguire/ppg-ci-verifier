-- Offline schema proposal only. NOT applied; no PostgreSQL transport/repository adapter yet.
-- Match hosting/outlook_delivery.py states and blocking-scope semantics.
-- Apply only after review, backup and explicit staging authorization.
BEGIN;
CREATE TABLE IF NOT EXISTS public.outlook_delivery_intents (
    id text PRIMARY KEY,
    scope text NOT NULL,
    state text NOT NULL CHECK (state IN (
        'awaiting_confirmation', 'confirmed', 'dispatching', 'accepted',
        'rejected_before_send', 'unknown', 'cancelled_before_dispatch')),
    snapshot text NOT NULL CHECK (octet_length(snapshot) <= 100000),
    digest text NOT NULL CHECK (length(digest) = 64),
    attachment bytea NOT NULL CHECK (octet_length(attachment) BETWEEN 1 AND 3000000),
    draft text NOT NULL CHECK (octet_length(draft) <= 100000),
    expires double precision NOT NULL,
    created double precision NOT NULL,
    confirmed_actor text,
    confirmed_at double precision,
    dispatched_at double precision,
    provider_receipt text
);
CREATE UNIQUE INDEX IF NOT EXISTS outlook_delivery_active_scope
    ON public.outlook_delivery_intents(scope)
    WHERE state IN ('awaiting_confirmation','confirmed','dispatching','unknown','accepted');
-- No direct browser table access. No allow policy and no service credential provisioning.
ALTER TABLE public.outlook_delivery_intents ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.outlook_delivery_intents FROM PUBLIC, anon, authenticated;
COMMIT;
