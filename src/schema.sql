CREATE TABLE public.hidden_gems (
    appid             BIGINT                      NOT NULL,
    nombre            TEXT,
    generos           TEXT,
    precio            NUMERIC(8,2),
    puntuacion        NUMERIC(5,4),
    total_resenas     INTEGER,
    resenas_positivas INTEGER,
    hidden_gem_score  NUMERIC(5,4),
    tier              CHARACTER(1),
    updated_at        TIMESTAMP WITHOUT TIME ZONE DEFAULT now(),

    CONSTRAINT hidden_gems_pkey
        PRIMARY KEY (appid)
);
