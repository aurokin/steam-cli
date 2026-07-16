# Pricing evidence strategy

Status: M3 fallback semantics accepted; provider/outreach plan working and last
verified 2026-07-11.

Historical pricing must work for a new installation. Local observations are
supplemental evidence for outages, personalized prices, and recent changes; they
are not the primary historical answer.

There is no single universal “historical price.” Queries must specify whether
they mean an exact Steam app/package/bundle, the lowest Steam offer for a game,
an authorized Steam-key retailer, any store/DRM, a region/currency, or a
subscription/giveaway.

## Provider candidates

| Provider | Machine-readable history | Contract posture | Intended role |
| --- | --- | --- | --- |
| [IsThereAnyDeal](https://docs.isthereanydeal.com/) | Timestamped price changes via `GET /games/history/v2`; all-time/annual/three-month lows; offers and bundles | Private applications must contact `api@isthereanydeal.com`; attribution, supplied URLs/data, and non-competition terms apply | Preferred primary provider after written permission |
| [GG.deals API](https://forum.gg.deals/d/1778/3) | Current retail/keyshop prices and historical lows; AppID/SubID/BundleID endpoints; not a full public graph series | Hobby access with attribution; premium/commercial route; scraping prohibited | Exact-product low and second-provider fallback after terms are agreed |
| [Sensor Tower / VGI](https://app.sensortower.com/vgi/api-information/) | Commercial Steam price-change/history data across supported currencies | Enterprise contract/custom dataset | Dependable licensed escalation path; request price-history-only/OEM quote |
| [CheapShark](https://apidocs.cheapshark.com/) | Current offers and cheapest-ever summary, not full history | Free API; user-driven requests, descriptive User-Agent, redirect links; USD only | Zero-onboarding degraded fallback |
| [pepe.deals](https://pepe.deals/api) | Per-store minimum, current offers, bundle/free/subscription history | Public API invitation but unclear license, quotas, and redistribution rights | Experimental after written clarification |
| Steam current price | Current regional price only | Provisional storefront endpoint | Current Steam observation, never historical backfill |
| SteamDB | Excellent human pages only | No API; automated scraping/crawling prohibited | Manual reference links only |

## Published cost and test onboarding

Verified 2026-07-10. “No published charge” means the provider does not list a
price for ordinary API access; it does not promise perpetual free service.

| Provider | Published cost | Minimum test setup | Budget implication |
| --- | --- | --- | --- |
| ITAD | No API fee or usage-credit price is published | Register an app and verify email; public apps receive credentials, while private apps must request approval. This app dashboard currently grants 100 requests per 5 minutes. | Expect `$0` for an approved development test; cache to the account-specific limit and do not assume the documented default |
| GG.deals Free | `$0` for personal, hobby, and open-source projects, with attribution and preserved affiliate/referral links | Free account/API key | Best first authenticated E2E provider; no funds required |
| GG.deals Premium | Custom quote; not self-serve | Contact from a commercial email with use case | Needed only for commercial use, higher limits, faster refresh, store-granular prices, or top-ten offers |
| CheapShark | `$0`, no API key | Descriptive `User-Agent`; on-demand requests; CheapShark redirect links | Immediate zero-cost fallback test |
| pepe.deals | No API charge or key is published | Direct API request; separately request permission/limits/retention terms | Technically testable for `$0`, but remain experimental until terms are clarified |
| Sensor Tower/VGI | Custom Business pricing based on licenses/options | Demo/quote; API is a Business feature | Do not expect a small prepaid sandbox; use only if the quote justifies licensed enterprise history |
| SteamGridDB | No API fee is published; account-generated key and personal/non-commercial terms | Account/API key; optional Patreon is support, not documented API billing | Likely `$0` for personal development; clarify distributed/commercial use before making it a default |

The current testing budget is therefore **zero dollars** for the useful first
round. There is no need to preload balances. The gating work is account creation,
attribution, and provider approval—not buying API credits.

Credential preconfiguration is implemented for ITAD, GG.deals, and SteamGridDB
behind the accepted OS credential boundary. This is not M3 activation. It stores
only API keys, not ITAD OAuth client secrets. Live ITAD use remains blocked until
the resulting application has a canonical public URL or private-use approval.

Two unauthenticated smoke probes were successful during research:

- CheapShark resolved Steam AppID `1091500` to a current game/deal record.
- pepe.deals returned current offers and per-store historical minimums for the
  same AppID.

The pepe.deals response also demonstrated why it stays experimental: its
“current” list included an expired offer and mixed an Ultimate Edition offer into
the base game's result. The adapter must filter expiry and preserve product/
edition identity rather than accepting the response as a single canonical price.

Cost references:

- [ITAD access, terms, and limits](https://docs.isthereanydeal.com/)
- [GG.deals API pricing](https://gg.deals/api/)
- [GG.deals Premium announcement](https://gg.deals/announcement/ggdeals-api-update-new-endpoints-premium-api-and-user-built-extensions/)
- [CheapShark API terms](https://apidocs.cheapshark.com/)
- [Sensor Tower/VGI pricing](https://app.sensortower.com/vgi/upgrade)
- [pepe.deals API](https://pepe.deals/api)
- [SteamGridDB API](https://www.steamgriddb.com/api/v2)

A licensed [CC BY 4.0 research dataset](https://data.mendeley.com/datasets/ycy3sy3vj2/1)
contains daily USD prices for roughly 2,000 popular Steam apps from April 2019
through August 2020. It can seed fixtures and model validation, but it is neither
current nor broad enough for production.

Affiliate/catalog feeds from retailers can improve current-offer resilience but
do not supply historical backfill. They belong in later current-offer adapters.

## Recommended provider program

Run these in parallel before promising historical pricing:

1. Request ITAD approval for a locally installed, open-source agent CLI.
2. Request GG.deals premium/commercial terms and exact App/Sub/Bundle semantics.
3. Request a narrow VGI quote excluding sales-estimate products.
4. Ask CheapShark and pepe.deals whether attributed local caching and derived
   JSON recommendations are permitted.

For the first E2E pass, use GG.deals Free plus CheapShark. Add ITAD immediately
after private-use approval. Probe pepe.deals behind an experimental flag. A VGI
quote is a procurement comparison, not a dependency for starting development.

Ask every provider about:

- public distribution and personal versus commercial use;
- local retention duration and deletion;
- returning normalized facts and derived deal scores;
- redistribution of lows or history versus links only;
- required attribution and preservation of affiliate URLs;
- rate limits and on-demand/bulk behavior;
- region, currency, tax, voucher, and membership semantics;
- AppID, package/SubID, bundle, edition, and DLC identity;
- whether exact price events or only precomputed lows are available.

The concrete description should be consistent: a user-triggered, locally
installed CLI, invoked by an AI agent, that retrieves only relevant games,
caches provider data locally, preserves attribution/outbound URLs, and is not a
hosted price-comparison site.

## Runtime fallback ladder

Provider selection is capability-based rather than a single fixed priority:

1. Licensed exact-product history when the query requires it.
2. Licensed normalized-game/store history for general “buy now?” questions.
3. Historical-low summary when full history is unavailable.
4. Official/provisional current Steam price plus an explicit lack-of-history
   warning.
5. A manual-only reference URL.
6. Local observations as corroboration, never disguised as backfill.

The output should state which rung answered the query and which interpretation of
“historical low” it supports.

## Manual links are not browser-ingestion permission

Stable reference shapes include:

```text
https://steamdb.info/app/{appid}/
https://steamdb.info/sub/{packageid}/
https://steamdb.info/bundle/{bundleid}/
https://gg.deals/steam/app/{appid}/
https://gg.deals/steam/sub/{packageid}/
https://gg.deals/steam/bundle/{bundleid}/
```

Returning a URL, opening it for a human, having an agent read it, and bulk
ingestion are distinct capabilities. Visible Chrome automation is still
automated access. SteamDB and GG.deals do not publish a one-off agent-reading
exception to their anti-scraping rules.

```json
{
  "url": "https://steamdb.info/sub/12345/",
  "purpose": "manual historical-price inspection",
  "access_mode": "manual_only",
  "automation_supported": false,
  "reason": "provider disallows automated extraction"
}
```

The CLI may return or, where policy permits, open such a link for the user. It
must not ingest or summarize the page without provider permission. API URLs
returned by an approved provider remain the preferred agent-readable path.

## Price identity model

Price observations attach to purchasable products, not merely games:

- product kind and provider product ID: app, package, bundle, external SKU;
- included base game/DLC and edition;
- permanent license, key, gift, subscription, rental, or giveaway;
- store class, DRM/activation platform, and activation region;
- country, currency, tax, voucher, membership, and personalized-bundle state;
- regular/final integer minor units and effective interval;
- provider, observation time, source URL, and attribution;
- exact observation versus store/game/all-time/annual/three-month low;
- support level and mapping uncertainty.

This prevents an agent from comparing a base app with an Ultimate Edition key,
a personalized complete-the-set bundle, or a non-Steam activation key as though
they were the same offer.
