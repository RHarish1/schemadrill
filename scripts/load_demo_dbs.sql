CREATE SCHEMA IF NOT EXISTS chinook;

CREATE TABLE IF NOT EXISTS chinook.artist (
  artist_id integer PRIMARY KEY,
  name text NOT NULL
);

CREATE TABLE IF NOT EXISTS chinook.album (
  album_id integer PRIMARY KEY,
  title text NOT NULL,
  artist_id integer NOT NULL REFERENCES chinook.artist(artist_id)
);

CREATE TABLE IF NOT EXISTS chinook.track (
  track_id integer PRIMARY KEY,
  name text NOT NULL,
  album_id integer NOT NULL REFERENCES chinook.album(album_id),
  milliseconds integer NOT NULL,
  unit_price numeric(10, 2) NOT NULL
);

GRANT USAGE ON SCHEMA chinook TO schemadrill_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA chinook TO schemadrill_readonly;
