-- Per-app content a Steam uninstall leaves behind (Proton prefix, shader
-- cache, subscribed Workshop items).  Byte counts are nullable: NULL means
-- not measured, 0 means looked for and absent.  residual_state is NULL on
-- rows recorded before this migration.
ALTER TABLE installed_observations ADD COLUMN residual_state TEXT
    CHECK (residual_state IS NULL OR residual_state IN ('measured', 'partial', 'unknown'));
ALTER TABLE installed_observations ADD COLUMN residual_compatdata_bytes INTEGER
    CHECK (residual_compatdata_bytes IS NULL OR residual_compatdata_bytes >= 0);
ALTER TABLE installed_observations ADD COLUMN residual_shadercache_bytes INTEGER
    CHECK (residual_shadercache_bytes IS NULL OR residual_shadercache_bytes >= 0);
ALTER TABLE installed_observations ADD COLUMN residual_workshop_bytes INTEGER
    CHECK (residual_workshop_bytes IS NULL OR residual_workshop_bytes >= 0);

ALTER TABLE installed_current ADD COLUMN residual_state TEXT
    CHECK (residual_state IS NULL OR residual_state IN ('measured', 'partial', 'unknown'));
ALTER TABLE installed_current ADD COLUMN residual_compatdata_bytes INTEGER
    CHECK (residual_compatdata_bytes IS NULL OR residual_compatdata_bytes >= 0);
ALTER TABLE installed_current ADD COLUMN residual_shadercache_bytes INTEGER
    CHECK (residual_shadercache_bytes IS NULL OR residual_shadercache_bytes >= 0);
ALTER TABLE installed_current ADD COLUMN residual_workshop_bytes INTEGER
    CHECK (residual_workshop_bytes IS NULL OR residual_workshop_bytes >= 0);
