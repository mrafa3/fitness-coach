-- Fitness Coach schema. Raw facts only; everything else is derived in metrics.py.
-- Data arrives from Apple Health through the Health Auto Export app (see health.py).

CREATE TABLE IF NOT EXISTS weigh_ins (
    day             TEXT PRIMARY KEY,   -- YYYY-MM-DD, one row per day (Health Auto Export aggregates by day)
    weight_lb       REAL,
    lean_mass_lb    REAL,               -- Eufy lean body mass, via Apple Health
    body_fat_pct    REAL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    id              TEXT PRIMARY KEY,     -- db.workout_id(type, start, app): stable across re-sends
    started_at      TEXT NOT NULL,        -- local wall-clock time, ISO
    sport_type      TEXT NOT NULL,        -- Apple Health workout type, e.g. "Running", "High Intensity Interval Training"
    kind            TEXT NOT NULL,        -- run | strength | recovery | other (metrics.classify)
    distance_m      REAL,
    moving_time_s   INTEGER,              -- active time (excludes pauses)
    elapsed_time_s  INTEGER,              -- start to end; NTC sessions pause a lot, so strength uses this
    source          TEXT,                 -- app that recorded it: iFIT, Nike Training, ...
    avg_speed_mph   REAL,                 -- runs: average speed as the app reports it (iFIT does)
    trainer         INTEGER,              -- 1 = indoor, when the app says so (iFIT reports treadmill runs as outdoor)
    energy_kcal     REAL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    source          TEXT PRIMARY KEY,
    last_synced_at  TEXT
);

-- Coaching. One plan per week; sessions are the prescriptions adherence is scored against.
-- the weekly summary as first sent to Google Calendar, so later data doesn't rewrite it
CREATE TABLE IF NOT EXISTS plan_summaries (
    week_start  TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coach_plans (
    week_start      TEXT PRIMARY KEY,     -- Monday
    created_at      TEXT NOT NULL,
    focus           TEXT,
    guardrail_notes TEXT,                 -- what the code clamped, if anything
    model           TEXT,
    raw_json        TEXT
);

CREATE TABLE IF NOT EXISTS planned_sessions (
    week_start      TEXT NOT NULL REFERENCES coach_plans(week_start),
    day             TEXT NOT NULL,        -- ISO date
    kind            TEXT NOT NULL,        -- run | strength | recovery
    title           TEXT,
    run_type        TEXT,                 -- easy | long | tempo | intervals | recovery
    distance_mi     REAL,
    target_pace     REAL,                 -- min/mile
    body_focus      TEXT,                 -- strength: lower | upper | core | full_body
    minutes         INTEGER,              -- strength and recovery length
    details         TEXT,
    PRIMARY KEY (week_start, day, kind)
);
