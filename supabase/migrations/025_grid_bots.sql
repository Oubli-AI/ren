-- Spreadsheet bots (agent/api/core/grid_bots.py), after AppSheet's bots.
-- A bot lives in a page's spreadsheet (its grid's `bots`). One that runs on a schedule is kept here for the user who
-- saved the page last, with when it is next due; the Ren API claims a due row by moving `next_run_at` forward with a
-- compare-and-set, as it does `agent_schedules`, and drops the row once the page no longer holds the bot.
-- A bot's run is an `agent_runs` row like a scheduled agent's: with Ren (no `agent_id`) or the user's saved agent,
-- `trigger` 'bot' when rows added or changed started it, and the bot's name in `bot`.
-- The Ren API writes with the service-role key and filters by owner_id; RLS lets a signed-in user read their own bots.
create table public.grid_bots (
 id uuid primary key default gen_random_uuid(),
 owner_id uuid not null references auth.users(id) on delete cascade,
 document_id uuid not null references public.project_documents(id) on delete cascade,
 sheet text not null check(length(sheet) between 1 and 31),
 name text not null check(length(name) between 1 and 60),
 cadence jsonb not null check(jsonb_typeof(cadence) = 'object'),
 next_run_at timestamptz,
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 unique(document_id, sheet, name)
);
create index grid_bots_due on public.grid_bots(next_run_at);
create index grid_bots_owner on public.grid_bots(owner_id);
alter table public.grid_bots enable row level security;
create policy "Own grid bots" on public.grid_bots for select to authenticated
 using (owner_id = (select auth.uid()));
revoke all on public.grid_bots from anon;
grant select on public.grid_bots to authenticated;

alter table public.agent_runs alter column agent_id drop not null;
alter table public.agent_runs add column bot text not null default '' check(length(bot) <= 60);
alter table public.agent_runs drop constraint agent_runs_trigger_check;
alter table public.agent_runs add constraint agent_runs_trigger_check check(trigger in ('schedule', 'manual', 'bot'));
