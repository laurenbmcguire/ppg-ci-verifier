-- Apply only to the dedicated verifier database/schema after connection is authorized.
-- No existing schemas are altered. Do not use shared application credentials.
-- The app role needs: SELECT/INSERT/UPDATE on ci_verifier_state;
--   SELECT/INSERT on ci_verifier_files (write-once: never grant UPDATE/DELETE);
--   SELECT/INSERT/UPDATE/DELETE on ci_verifier_upload_parts.
CREATE TABLE IF NOT EXISTS ci_verifier_state (
    namespace TEXT PRIMARY KEY,
    revision BIGINT NOT NULL DEFAULT 0,
    payload BYTEA,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Uploaded originals, content-addressed by path (uploads/<sha256>.<ext>); immutable.
CREATE TABLE IF NOT EXISTS ci_verifier_files (
    path TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    size BIGINT NOT NULL,
    content BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- In-progress chunked uploads; rows are deleted when the upload is assembled.
CREATE TABLE IF NOT EXISTS ci_verifier_upload_parts (
    upload_id TEXT NOT NULL,
    part_index INTEGER NOT NULL,
    content BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (upload_id, part_index)
);
