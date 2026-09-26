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

INSERT INTO chinook.artist (artist_id, name) VALUES
  (1, 'AC/DC'),
  (2, 'Accept'),
  (3, 'Aerosmith')
ON CONFLICT (artist_id) DO NOTHING;

INSERT INTO chinook.album (album_id, title, artist_id) VALUES
  (1, 'For Those About To Rock We Salute You', 1),
  (2, 'Balls to the Wall', 2),
  (3, 'Restless and Wild', 2),
  (4, 'Big Ones', 3)
ON CONFLICT (album_id) DO NOTHING;

INSERT INTO chinook.track (track_id, name, album_id, milliseconds, unit_price) VALUES
  (1, 'For Those About To Rock', 1, 343719, 0.99),
  (2, 'Put The Finger On You', 1, 205662, 0.99),
  (3, 'Balls to the Wall', 2, 342562, 0.99),
  (4, 'Fast As a Shark', 3, 230619, 0.99),
  (5, 'Walk On Water', 4, 263497, 0.99),
  (6, 'Love In An Elevator', 4, 321828, 0.99)
ON CONFLICT (track_id) DO NOTHING;

GRANT USAGE ON SCHEMA chinook TO schemadrill_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA chinook TO schemadrill_readonly;
