# Wiki: Connect Your Neon Database

> Status: describes the **planned** connection feature (see [roadmap](../roadmap.md)).
> Neon's dashboard labels may change; if a button has moved, look for "Connect" or
> "Connection details" on your project page.

## What you are giving SchemaDrill

A Neon connection string looks like this:

```text
postgresql://USER:PASSWORD@ep-xxxx.region.aws.neon.tech/DBNAME?sslmode=require
```

It is **not** a share link like a Google Drive link. It is a **username and password** for a
database role. Access depends on **which role is in the string**, not on the link:

| Role in the string | Can do |
| --- | --- |
| The default owner role (often `neondb_owner`) | Everything: read, write, create and drop tables |
| A role you granted only `SELECT` | Read only |
| A role with `INSERT` and `UPDATE` on chosen tables | Read plus limited writes |

Treat the string like a password. Anyone who has it has that role's access.

## Step 1: Copy the connection string

1. Sign in to [neon.com](https://neon.com). No account yet? Create a free one, then come back.
2. Open your **Project** and pick the **branch** and **database** you want to query.
3. Click **Connect** on the project dashboard.
4. Choose the **role** and **database**. Prefer a read-only role if you have one (see below).
5. Copy the connection string. Use the **direct** (non-pooled) one unless told otherwise.

## Step 2 (recommended): Create a read-only role

Run this once in the Neon SQL Editor, replacing the password and schema names:

```sql
CREATE ROLE schemadrill_reader LOGIN PASSWORD 'choose-a-strong-password';
GRANT USAGE ON SCHEMA public TO schemadrill_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO schemadrill_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO schemadrill_reader;
```

Then build a string with that user and password and paste it into SchemaDrill. You can add
more schemas the same way. If you later want CSV uploads, SchemaDrill will ask you to add a
separate **writer** role, so reads and writes never share one credential.

## Step 3: Paste it in SchemaDrill

1. Go to **Connections > Add database** and paste the string.
2. SchemaDrill checks the connection and shows what access the role has (read-only,
   or write/owner).
3. If the role is an **owner**, SchemaDrill warns you and recommends the read-only role.
   An owner string is used only for the **one-time** setup of safe roles if you choose that,
   and is not kept (planned behavior).
4. SchemaDrill reads your table structure and builds searchable schema embeddings so the AI
   can find the right tables. **Table contents are not embedded.**
5. Your database appears in the dropdown.

## How long the connection is kept

- Once saved, the connection stays **encrypted at rest** until you remove it. You don't
  paste it again each visit.
- A saved connection stops working if you **reset the role password**, delete the role,
  project, branch or database, or Neon suspends the project.
- SchemaDrill checks on use. If it fails, the connection is marked `needs reconnect`, you
  get a clear message, and the other databases are unaffected. Paste a fresh string to fix it.
- Neon free-tier databases sleep when idle. The first query may take a few seconds while it
  wakes up.
- You can **delete** a connection at any time. The stored string and its embeddings are removed.

## If you don't have a Neon account

1. Choose **Create a free Neon account** in SchemaDrill. It opens neon.com in a new tab.
2. Sign up, create a project, then return to SchemaDrill and follow Step 1.

SchemaDrill can't redirect you back automatically, because that needs Neon OAuth. See
[why-no-neon-oauth.md](../why-no-neon-oauth.md).

## Rotate or revoke access

Run `ALTER ROLE schemadrill_reader PASSWORD 'new-password';` or `DROP ROLE schemadrill_reader;`
in Neon. The old string stops working immediately.
