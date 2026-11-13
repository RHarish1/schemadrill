CREATE TABLE IF NOT EXISTS public.tenant_connections (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id text NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
  label text NOT NULL,
  host text NOT NULL,
  database text NOT NULL,
  role_name text NOT NULL,
  schemas text[] NOT NULL,
  ingest_status text NOT NULL CHECK (ingest_status IN ('running', 'ready', 'failed')),
  table_count int NOT NULL DEFAULT 0,
  ingested_at timestamptz,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tenant_connections_user_id_idx
  ON public.tenant_connections(user_id);

CREATE TABLE IF NOT EXISTS public.tenant_schema_embeddings (
  connection_id uuid NOT NULL REFERENCES public.tenant_connections(id) ON DELETE CASCADE,
  user_id text NOT NULL,
  schema_name text NOT NULL,
  table_name text NOT NULL,
  ddl text NOT NULL,
  embedding vector(384) NOT NULL,
  PRIMARY KEY (connection_id, schema_name, table_name)
);

CREATE INDEX IF NOT EXISTS tenant_schema_embeddings_user_conn_idx
  ON public.tenant_schema_embeddings(user_id, connection_id);

REVOKE ALL ON public.tenant_connections, public.tenant_schema_embeddings
  FROM schemadrill_readonly;
