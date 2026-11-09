CREATE TABLE IF NOT EXISTS referrals (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    patient_code VARCHAR(64) NOT NULL CHECK (BTRIM(patient_code) <> ''),
    modality VARCHAR(3) NOT NULL CHECK (modality IN ('CT', 'MRI', 'XR', 'US', 'PET')),
    priority SMALLINT NOT NULL CHECK (priority BETWEEN 1 AND 5),
    doctor VARCHAR(120) NOT NULL CHECK (BTRIM(doctor) <> ''),
    issued_at DATE NOT NULL,
    status VARCHAR(20) NOT NULL CHECK (
        status IN ('created', 'scheduled', 'completed', 'cancelled')
    )
);

CREATE INDEX IF NOT EXISTS idx_referrals_status ON referrals (status);

CREATE TABLE IF NOT EXISTS tasks (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    record_id BIGINT NOT NULL REFERENCES referrals(id) ON DELETE CASCADE,
    status VARCHAR(16) NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'running', 'done', 'failed')
    ),
    priority SMALLINT NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
    seed BIGINT NOT NULL CHECK (seed BETWEEN 0 AND 2147483647),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    token BIGINT NOT NULL DEFAULT 0 CHECK (token >= 0),
    lease_until TIMESTAMPTZ,
    worker_id VARCHAR(253),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tasks_claim
    ON tasks (status, priority DESC, created_at, id);
CREATE INDEX IF NOT EXISTS idx_tasks_record_id ON tasks (record_id);

CREATE TABLE IF NOT EXISTS task_results (
    task_id BIGINT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    token BIGINT NOT NULL CHECK (token >= 1),
    worker_id VARCHAR(253) NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

