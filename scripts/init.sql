CREATE EXTENSION IF NOT EXISTS vector;

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'schemadrill_readonly') THEN
    CREATE ROLE schemadrill_readonly LOGIN PASSWORD 'schemadrill';
  END IF;
END
$$;

CREATE TABLE IF NOT EXISTS public.schema_embeddings (
  schema_name text NOT NULL,
  table_name text NOT NULL,
  ddl_text text NOT NULL,
  embedding vector(384) NOT NULL,
  PRIMARY KEY (schema_name, table_name)
);

CREATE INDEX IF NOT EXISTS schema_embeddings_embedding_hnsw
  ON public.schema_embeddings USING hnsw (embedding vector_cosine_ops);

GRANT USAGE ON SCHEMA public TO schemadrill_readonly;
GRANT SELECT ON public.schema_embeddings TO schemadrill_readonly;
