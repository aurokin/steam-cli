# ADR 0004: provisional Steam wishlist reader

Status: accepted for M3 on 2026-07-11

## Decision

Support read-only `IWishlistService/GetWishlist` behind an independently
disableable provisional adapter. A complete promotion requires a sequential
`GetWishlistItemCount` and `GetWishlist` pair whose integer count and unique
AppIDs agree. Only AppID, priority, and added time are normalized. No response
body is retained.

An empty wishlist is confirmed only when the count endpoint explicitly returns
zero and the item list is valid and empty. Missing or invalid keys produced an
empty response envelope during the live probe, so an empty-looking envelope is
inaccessible or ambiguous, never a confirmed empty snapshot. Failed or changed
provider behavior preserves last-known-good data.

## Evidence

The primary-account live probe returned 238 items with integer `appid`,
`priority`, and `date_added` fields and an equal integer item count. Requests
without a usable key returned an empty response envelope with HTTP 200. Valve's
supported reference omits this interface, so the support level remains
provisional even though the request uses Valve's official API host.

## Consequences

Wishlist writes, Steam-session authentication, share-token claims, and private
wishlist diagnoses remain unsupported. A provider-shape change can disable this
adapter without affecting accepted owned-library behavior.
