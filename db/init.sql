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

