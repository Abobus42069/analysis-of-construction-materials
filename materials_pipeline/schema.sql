CREATE TABLE IF NOT EXISTS pipeline_schema_versions (
    version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO pipeline_schema_versions(version) VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS pipeline_documents (
    id text PRIMARY KEY, source_url text NOT NULL, sha256 text NOT NULL,
    kind text NOT NULL, record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(source_url, sha256, kind)
);
CREATE INDEX IF NOT EXISTS pipeline_documents_record_gin ON pipeline_documents USING gin(record);
CREATE TABLE IF NOT EXISTS pipeline_material_snapshots (
    id text PRIMARY KEY, material_id text NOT NULL, sha256 text NOT NULL,
    record jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(material_id, sha256)
);
CREATE INDEX IF NOT EXISTS pipeline_material_identity ON pipeline_material_snapshots(material_id);
CREATE TABLE IF NOT EXISTS pipeline_material_document_links (
    id text PRIMARY KEY, material_id text NOT NULL,
    document_id text NOT NULL REFERENCES pipeline_documents(id), record jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS pipeline_offer_observations (
    id text PRIMARY KEY, material_id text, retailer text NOT NULL, city text NOT NULL,
    observed_at timestamptz NOT NULL, record jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS pipeline_offers_search ON pipeline_offer_observations(material_id, city, observed_at);
CREATE TABLE IF NOT EXISTS pipeline_tasks (
    id text PRIMARY KEY, run_id text NOT NULL, payload jsonb NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','retry_wait','succeeded','failed','needs_review')),
    attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK(max_attempts>0),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    owner text, token text, lease_until timestamptz,
    document_id text REFERENCES pipeline_documents(id), error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS pipeline_task_claim ON pipeline_tasks(state, available_at, lease_until);

CREATE TABLE IF NOT EXISTS pipeline_registry_cards (
    id text PRIMARY KEY, designation text NOT NULL, source_url text NOT NULL,
    sha256 text NOT NULL, record jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS pipeline_registry_designation ON pipeline_registry_cards(designation);
