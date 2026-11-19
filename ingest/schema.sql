PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=OFF;

CREATE TABLE IF NOT EXISTS source (
  id          INTEGER PRIMARY KEY,
  domain      TEXT NOT NULL,
  url         TEXT NOT NULL,
  title       TEXT,
  publisher   TEXT,
  license     TEXT,
  retrieved_at TEXT,
  bytes       INTEGER,
  sha256      TEXT,
  UNIQUE(domain, url)
);

CREATE TABLE IF NOT EXISTS geo_state (
  fips        TEXT PRIMARY KEY,
  abbr        TEXT,
  name        TEXT,
  pop         INTEGER,
  lat         REAL,
  lon         REAL,
  land_sqmi   REAL
);

-- Continuum of Care (HUD homeless planning bodies)
CREATE TABLE IF NOT EXISTS geo_coc (
  coc_num     TEXT PRIMARY KEY,
  name        TEXT,
  state       TEXT,
  cities      TEXT,
  pop         INTEGER,
  lat         REAL,
  lon         REAL,
  geometry    TEXT,            -- geojson ring set (simplified), nullable
  source_id   INTEGER
);

CREATE TABLE IF NOT EXISTS geo_city (
  geoid       TEXT PRIMARY KEY,   -- 7-digit place FIPS, or county 5-digit
  kind        TEXT,               -- place | county | city
  name        TEXT,
  state       TEXT,
  pop         INTEGER,
  lat         REAL,
  lon         REAL,
  coc_num     TEXT,
  source_id   INTEGER
);

CREATE TABLE IF NOT EXISTS org (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  name_norm   TEXT NOT NULL,
  ein         TEXT,
  uei         TEXT,
  org_type    TEXT,          -- nonprofit | government | forprofit | housing_authority | faith | unknown
  website     TEXT,
  email       TEXT,
  phone       TEXT,
  address1    TEXT,
  city        TEXT,
  state       TEXT,
  zip         TEXT,
  county      TEXT,
  lat         REAL,
  lon         REAL,
  coc_num     TEXT,
  ntee        TEXT,
  founded     INTEGER,
  status      TEXT,          -- active | inactive | unknown
  revenue     INTEGER,
  expenses    INTEGER,
  assets      INTEGER,
  employees   INTEGER,
  fiscal_year INTEGER,
  is_est      INTEGER DEFAULT 0,
  source_id   INTEGER,
  updated_at  TEXT,
  UNIQUE(name_norm, state)
);
CREATE INDEX IF NOT EXISTS ix_org_state ON org(state);
CREATE INDEX IF NOT EXISTS ix_org_ein ON org(ein);
CREATE INDEX IF NOT EXISTS ix_org_uei ON org(uei);

CREATE TABLE IF NOT EXISTS org_person (
  id          INTEGER PRIMARY KEY,
  org_id      INTEGER,
  org_name    TEXT,
  person      TEXT NOT NULL,
  person_norm TEXT,
  title       TEXT,
  comp        INTEGER,
  year        INTEGER,
  role_class  TEXT,          -- officer | director | key_employee | highest_paid | trustee
  source_id   INTEGER,
  UNIQUE(person_norm, org_name, year, title)
);
CREATE INDEX IF NOT EXISTS ix_person_norm ON org_person(person_norm);

-- Federal awards: contracts, grants, cooperative agreements, IDVs
CREATE TABLE IF NOT EXISTS award (
  id            INTEGER PRIMARY KEY,
  award_key     TEXT UNIQUE,   -- generated_internal_id
  award_id      TEXT,
  recipient_name TEXT,
  recipient_norm TEXT,
  recipient_uei TEXT,
  org_id        INTEGER,
  award_type    TEXT,          -- contract | grant | idv | other
  agency        TEXT,
  subtier       TEXT,
  office        TEXT,
  cfda          TEXT,
  psc           TEXT,
  naics         TEXT,
  program       TEXT,
  description   TEXT,
  amount_obligated REAL,
  amount_total  REAL,          -- potential / base+options or award amount
  pop_start     TEXT,
  pop_end       TEXT,
  fy            TEXT,
  place_state   TEXT,
  place_city    TEXT,
  place_county  TEXT,
  source_id     INTEGER,
  is_est        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_award_org ON award(org_id);
CREATE INDEX IF NOT EXISTS ix_award_state ON award(place_state);
CREATE INDEX IF NOT EXISTS ix_award_cfda ON award(cfda);
CREATE INDEX IF NOT EXISTS ix_award_norm ON award(recipient_norm);

-- HUD Point-in-Time counts per CoC per year, incl. subpopulations
CREATE TABLE IF NOT EXISTS pit (
  coc_num     TEXT NOT NULL,
  year        INTEGER NOT NULL,
  total       INTEGER,
  sheltered   INTEGER,
  unsheltered INTEGER,
  households  INTEGER,
  chronic     INTEGER,
  smi         INTEGER,   -- severely mentally ill (adults)
  sud         INTEGER,   -- substance use disorder (adults)
  veterans    INTEGER,
  youth       INTEGER,   -- under 25
  children    INTEGER,
  families    INTEGER,
  hiv         INTEGER,
  dv          INTEGER,
  is_est      INTEGER DEFAULT 0,
  source_id   INTEGER,
  PRIMARY KEY(coc_num, year)
);

CREATE TABLE IF NOT EXISTS facility (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  name_norm   TEXT,
  kind        TEXT,   -- state_psychiatric | psych_unit | prison | jail | shelter | detox | other
  status      TEXT,   -- open | closed | partial | unknown
  opened      INTEGER,
  closed      INTEGER,
  beds        INTEGER,
  capacity_est INTEGER,
  operator    TEXT,
  owner       TEXT,
  state       TEXT,
  city        TEXT,
  address     TEXT,
  lat         REAL,
  lon         REAL,
  budget      INTEGER,       -- annual operating budget if published
  staff       INTEGER,
  annual_cost_per_bed INTEGER,
  population_note TEXT,
  source_id   INTEGER,
  is_est      INTEGER DEFAULT 0,
  UNIQUE(name_norm, state, city)
);
CREATE INDEX IF NOT EXISTS ix_fac_state ON facility(state);
CREATE INDEX IF NOT EXISTS ix_fac_kind ON facility(kind);

CREATE TABLE IF NOT EXISTS law (
  id          INTEGER PRIMARY KEY,
  jurisdiction TEXT,   -- US | state abbr | city
  topic       TEXT,    -- camping_ban | sit_lie | property | drug | mental_health_hold | encampment_clearance | funding | housing_first | other
  citation    TEXT,
  title       TEXT,
  summary     TEXT,
  status      TEXT,    -- enacted | enjoined | repealed | proposed
  year        INTEGER,
  url         TEXT,
  source_id   INTEGER,
  UNIQUE(jurisdiction, citation, title)
);

CREATE TABLE IF NOT EXISTS media (
  id          INTEGER PRIMARY KEY,
  kind        TEXT,   -- video | image | dataset
  title       TEXT,
  url         TEXT UNIQUE,
  thumbnail   TEXT,
  publisher   TEXT,
  author      TEXT,
  published   TEXT,
  place       TEXT,
  state       TEXT,
  license     TEXT,
  duration    INTEGER,
  summary     TEXT,
  source_id   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_media_state ON media(state);

CREATE TABLE IF NOT EXISTS doc (
  id          INTEGER PRIMARY KEY,
  kind        TEXT,   -- report | audit | study | article | dataset | court
  title       TEXT,
  url         TEXT UNIQUE,
  publisher   TEXT,
  author      TEXT,
  published   TEXT,
  place       TEXT,
  state       TEXT,
  summary     TEXT,
  source_id   INTEGER
);

-- Generic numeric metrics so any domain can add series without schema churn
CREATE TABLE IF NOT EXISTS metric (
  id          INTEGER PRIMARY KEY,
  entity_type TEXT,   -- state | coc | city | org | facility | us
  entity_id   TEXT,
  metric      TEXT,   -- overdose_deaths | crime_rate | drug_share | spend_per_homeless | beds_per_100k ...
  value       REAL,
  unit        TEXT,
  year        INTEGER,
  is_est      INTEGER DEFAULT 0,
  note        TEXT,
  source_id   INTEGER,
  UNIQUE(entity_type, entity_id, metric, year)
);
CREATE INDEX IF NOT EXISTS ix_metric ON metric(entity_type, metric);

CREATE TABLE IF NOT EXISTS ingest_run (
  id        INTEGER PRIMARY KEY,
  domain    TEXT, ts TEXT, rows INTEGER, notes TEXT, ok INTEGER
);
