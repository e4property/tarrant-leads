# Tarrant County Motivated Seller Lead Scraper

## Project Overview
Automated daily scraper for Tarrant County (Fort Worth/DFW) Foreclosure
Notices. Built 2026-10-01 directly on the mechanism proven working on
bexar-leads the same night after a multi-hour live investigation — see
that repo's commit history for the full root-cause story.

## Data Source
tarrant.tx.publicsearch.us — same PublicSearch.us vendor platform as
Bexar/Nueces/Dallas/Wilson, **but a different table layout**. Confirmed
live before building: Tarrant's FC listing has only 4 real columns
(Grantor, Sale Date [month/year only], Filed Date, Property Address) and
no document number or link anywhere in the row. Don't assume one county's
column shape applies to another — verify live first, every time.

## Mechanism (do not revert to a narrowed date range)
- `searchType=quickSearch` + `instrumentDateRange` spanning the site's
  full index (`20000404` to ~180 days in the future), sorted desc.
  Narrowing this date range is dead on this platform family — confirmed
  independently on both bexar-leads and nueces-leads the same night.
  Pagination with early-stop against `known_docs` does the real bounding.
- `about:blank` reset before every navigation, "confirmed empty" requires
  both "No Results Found" and "Suggestions:" text. Both fixes for the
  same React-SPA client-side-route race condition found on nueces-leads
  and bexar-leads.

## v1.0 scope — what's NOT built yet
- No owner mailing address, loan amount, lender, or appraised value
  enrichment. Needs Tarrant Appraisal District's own ArcGIS endpoint,
  which has not been researched or verified live — do not guess at a URL
  for it the way old bexar-leads code once did for BCAD.
- `doc_number` is a synthetic id (filed date + hash of grantor+address),
  not a real county document number — the listing exposes none. Revisit
  if a per-doc detail page turns out to be click-reachable after all.
- Pre-foreclosure (Appointment of Substitute Trustee equivalent) not
  built — FC/Foreclosure Notice discovery only so far.

## GitHub Secrets Required
None yet — this source is public/unauthenticated as tested.
