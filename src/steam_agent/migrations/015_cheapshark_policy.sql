ALTER TABLE provider_request_limits
    ADD COLUMN cooldown_until TEXT;

ALTER TABLE price_observations
    ADD COLUMN seller_id TEXT;

ALTER TABLE price_current
    ADD COLUMN seller_id TEXT;
