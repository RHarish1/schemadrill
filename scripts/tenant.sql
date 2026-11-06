CREATE TABLE IF NOT EXISTS public.users (
  user_id text PRIMARY KEY,
  email text,
  display_name text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.user_datasets (
  dataset_id text PRIMARY KEY,
  user_id text NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
  label text NOT NULL,
  schema_name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.user_datasets
  DROP CONSTRAINT IF EXISTS user_datasets_pkey;
ALTER TABLE public.user_datasets
  ADD CONSTRAINT user_datasets_pkey PRIMARY KEY (dataset_id, user_id);

CREATE INDEX IF NOT EXISTS user_datasets_user_id_idx
  ON public.user_datasets(user_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.users, public.user_datasets
  TO schemadrill_readonly;
