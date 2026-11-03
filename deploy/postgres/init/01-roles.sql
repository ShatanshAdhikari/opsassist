-- Runs once on first start of an empty data directory. Development credentials only.
-- opsassist_owner (bootstrap superuser here) runs migrations.
-- opsassist_app serves requests: not owner, not superuser, so RLS applies to it.
CREATE ROLE opsassist_app LOGIN PASSWORD 'dev_app_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

CREATE DATABASE opsassist_test OWNER opsassist_owner;
