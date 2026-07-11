ALTER TABLE price_sync_demand
    ADD COLUMN targeted INTEGER NOT NULL DEFAULT 0
    CHECK (targeted IN (0, 1));

UPDATE price_sync_demand
SET targeted = evaluated;
