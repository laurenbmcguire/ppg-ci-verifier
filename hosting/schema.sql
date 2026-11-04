-- Apply only to the dedicated verifier database after connection is authorized.
-- No existing PPG schemas are altered. Do not use shared application credentials.
CREATE TABLE IF NOT EXISTS ci_verifier_state (
    namespace TEXT PRIMARY KEY,
    revision BIGINT NOT NULL DEFAULT 0,
    payload BYTEA,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
