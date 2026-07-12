ALTER TABLE declared_app_sync_demand
ADD COLUMN explicit INTEGER NOT NULL DEFAULT 0 CHECK (explicit IN (0, 1));

CREATE INDEX declared_app_explicit_context_idx
    ON declared_app_sync_demand(
        account_id, machine_id, country, language, explicit, appid
    );
