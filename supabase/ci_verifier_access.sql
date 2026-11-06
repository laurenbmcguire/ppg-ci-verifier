-- Applied to Supabase project project-hub on 2026-11-05 (migration ci_verifier_access).
-- People who can use the verifier, the clients each can see, sign-in sessions and one-time sign-in links.
create table if not exists ci_verifier.users (
  id text primary key, email text not null unique, name text, role text not null check (role in ('admin','member')),
  clients_json text not null default '[]', created_at text not null, last_login text, disabled_at text
);
create table if not exists ci_verifier.sessions (token_hash text primary key, user_id text not null, created_at text not null, expires_at text not null);
create index if not exists sessions_user on ci_verifier.sessions (user_id);
create table if not exists ci_verifier.login_tokens (token_hash text primary key, user_id text not null, created_at text not null, expires_at text not null, used_at text);
grant select, insert, update, delete on ci_verifier.users, ci_verifier.sessions, ci_verifier.login_tokens to ci_verifier;
do $$
declare t text;
begin
  foreach t in array array['users','sessions','login_tokens'] loop
    execute format('alter table ci_verifier.%I enable row level security', t);
    execute format('drop policy if exists ci_verifier_only on ci_verifier.%I', t);
    execute format('create policy ci_verifier_only on ci_verifier.%I for all to ci_verifier using (true) with check (true)', t);
  end loop;
end $$;
