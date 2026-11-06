-- Applied to Supabase project project-hub on 2026-11-05 (migration ci_verifier_native_tables).
-- The verifier's reports, check results, settings and indexes as real tables (previously packed inside one snapshot row).
create table if not exists ci_verifier.spreadsheets (
  seq bigserial primary key,
  file_hash text not null unique,
  filename text not null,
  version integer not null,
  result_json text not null
);
create index if not exists spreadsheets_filename on ci_verifier.spreadsheets (filename);
create table if not exists ci_verifier.spreadsheet_checks (
  seq bigserial primary key,
  file_hash text not null,
  check_hash text not null unique,
  result_json text not null
);
create index if not exists spreadsheet_checks_file on ci_verifier.spreadsheet_checks (file_hash, seq desc);
create table if not exists ci_verifier.spreadsheet_sources (source_hash text primary key, source_json text not null);
create table if not exists ci_verifier.client_contacts (job text primary key, contacts_json text not null);
create table if not exists ci_verifier.settings (key text primary key, value_json text not null);
create table if not exists ci_verifier.vin_index (ident text not null, shown text not null, file_hash text not null, sheet text, "row" integer);
create index if not exists vin_index_ident on ci_verifier.vin_index (ident text_pattern_ops);
create index if not exists vin_index_file on ci_verifier.vin_index (file_hash);
create table if not exists ci_verifier.vin_indexed (file_hash text primary key);
create table if not exists ci_verifier.dashboards (file_hash text primary key, versions_json text not null, messages_json text not null, updated_at text not null);
create table if not exists ci_verifier.api_keys (
  id text primary key, name text not null, key_hash text not null unique, prefix text not null,
  can_send integer not null default 0, created_at text not null, last_used text, revoked_at text
);

create or replace view ci_verifier.reports_overview with (security_invoker = true) as
  select s.seq, s.filename, s.version, s.file_hash,
         (s.result_json::jsonb ->> 'received_at')::timestamptz as received_at,
         s.result_json::jsonb ->> 'internal_check_status' as check_status,
         (s.result_json::jsonb ->> 'finding_count')::int as findings_at_upload
  from ci_verifier.spreadsheets s;

grant usage on schema ci_verifier to ci_verifier;
grant select, insert, update, delete on all tables in schema ci_verifier to ci_verifier;
grant usage, select, update on all sequences in schema ci_verifier to ci_verifier;

do $$
declare t text;
begin
  foreach t in array array['ci_verifier_state','ci_verifier_files','ci_verifier_upload_parts','spreadsheets','spreadsheet_checks',
                           'spreadsheet_sources','client_contacts','settings','vin_index','vin_indexed','dashboards','api_keys'] loop
    execute format('alter table ci_verifier.%I enable row level security', t);
    execute format('drop policy if exists ci_verifier_only on ci_verifier.%I', t);
    execute format('create policy ci_verifier_only on ci_verifier.%I for all to ci_verifier using (true) with check (true)', t);
  end loop;
end $$;
