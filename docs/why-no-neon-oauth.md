# Why SchemaDrill Does Not Use Neon OAuth

**Decision: SchemaDrill will not pursue Neon OAuth.** This is a free, non-commercial project,
and the "Connect Neon" experience is built on connection strings instead.

## What Neon OAuth would have given us

A "Connect Neon" button that sends the user to Neon's consent screen, then back to
SchemaDrill with a token. With that token we could list their projects and branches, and
create roles for them, with a smooth sign-up redirect for people without Neon accounts.

## Why we are not doing it

Based on Neon's published policy as found in research (confirm on their partner page, as
terms can change):

1. **Partners only.** Neon states it offers OAuth integrations to partners with an **active
   commercial relationship**. There is no self-serve client registration.
2. **Commercial terms.** Access means negotiating a partnership agreement with Neon's
   business team. The terms are not public.
3. **Wrong fit.** SchemaDrill is free, non-commercial, and has no revenue to bring to such an
   agreement. Applying would likely fail, and pursuing it would stall the roadmap.
4. **No dependency risk.** Building core features on an approval we can't control would block
   everything behind it.

## What we do instead

| Need | Approach |
| --- | --- |
| Link a user's database | User pastes their own connection string |
| Safe access | We recommend a dedicated read-only role (and a separate writer role for uploads) |
| User has no Neon account | We link to neon.com sign-up; they return and paste the string |
| Database dropdown | One entry per saved connection |

Trade-off: a few more onboarding steps and no automatic return redirect. Everything else in
the product works the same.

## How a user does it (short version)

1. Open your Neon project and click **Connect**.
2. Pick the role and database, copy the connection string.
3. Paste it into **Connections > Add database** in SchemaDrill.

Full guide, including creating a read-only role: [wiki/connect-neon.md](wiki/connect-neon.md).

## Possible future option (not committed)

Users can create a personal **Neon API key** in their Neon account settings. If the Neon API
allows listing projects and branches with one, we may offer it as an optional shortcut for
auto-populating the dropdown. This is unverified and secondary; the connection string stays
the main path.

If the project ever becomes a commercial product, revisit this decision.
