-- Run ONCE on Neon as the database owner, AFTER the first `make migrate` (migration 0001 creates
-- the group role `hub_app`, revokes CREATE on `public` from PUBLIC and sets the default
-- privileges; operations §2 "Database roles", P10-07). Creates the login user of the app:
--   psql "$NEON_OWNER_URL" -v app_password="$(openssl rand -hex 24)" -f infra/deploy/neon-app-user.sql
-- Then put postgresql+psycopg://hub_api:<that password>@<pooled host>/<db>?sslmode=require into
-- the secret `database-url`. The password never goes into the repository.
\set ON_ERROR_STOP on
CREATE ROLE hub_api LOGIN PASSWORD :'app_password' IN ROLE hub_app;
