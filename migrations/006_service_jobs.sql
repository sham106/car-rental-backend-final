-- Apply before deploying the service job workflow.
begin;
create table if not exists public.fleet_service_jobs (
 id text primary key,
 data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id),
 vehicle_id text generated always as (data->>'vehicleId') stored not null references public.fleet_vehicles(id)
);
alter table public.fleet_service_jobs enable row level security;
revoke all on public.fleet_service_jobs from public, anon, authenticated;
grant all on public.fleet_service_jobs to service_role;
create or replace function public.fleet_snapshot() returns jsonb language plpgsql stable security invoker set search_path='' as $$
declare result jsonb := '{}'::jsonb; r text; items jsonb;
begin
 foreach r in array array['owners','vehicles','customers','bookings','assignments','maintenance','service_jobs','compliance','files','documents','settings','categories','locations','audit','notifications','outbox','idempotency'] loop
  execute format('select coalesce(jsonb_agg(data order by id), ''[]''::jsonb) from public.%I', 'fleet_' || r) into items;
  result := result || jsonb_build_object(r, items);
 end loop;
 return jsonb_build_object('revision', (select revision from public.fleet_revision where singleton), 'records', result);
end; $$;
create or replace function public.fleet_commit(expected_revision bigint, changes jsonb, allocations jsonb) returns boolean
language plpgsql security invoker set search_path='' as $$
declare actual_revision bigint; item jsonb; r text;
begin
 select revision into actual_revision from public.fleet_revision where singleton for update;
 if actual_revision <> expected_revision then return false; end if;
 if jsonb_array_length(changes)>20000 then raise exception 'Transaction too large'; end if;
 for item in select value from jsonb_array_elements(changes) loop
  r := item->>'resource';
  if not r = any(array['owners','vehicles','customers','bookings','assignments','maintenance','service_jobs','compliance','files','documents','settings','categories','locations','audit','notifications','outbox','idempotency']) then raise exception 'Unknown resource'; end if;
  if item->'data' = 'null'::jsonb then
   if r='audit' then raise exception 'Audit records are append only'; end if;
   execute format('delete from public.%I where id=$1', 'fleet_' || r) using item->>'id';
  else
   if r='audit' then
    execute 'insert into public.fleet_audit(id,data) values ($1,$2)' using item->>'id', item->'data';
   else
    execute format('insert into public.%I(id,data) values ($1,$2) on conflict(id) do update set data=excluded.data', 'fleet_' || r) using item->>'id', item->'data';
   end if;
  end if;
 end loop;
 delete from public.fleet_allocations where true;
 insert into public.fleet_allocations(id,vehicle_id,occupied,kind)
 select x->>'id', x->>'vehicleId', daterange((x->>'startDate')::date,(x->>'endDate')::date,'[]'), x->>'kind'
 from jsonb_array_elements(allocations) x;
 update public.fleet_revision set revision=revision+1 where singleton;
 return true;
end; $$;
revoke all on function public.fleet_snapshot() from public, anon, authenticated;
revoke all on function public.fleet_commit(bigint,jsonb,jsonb) from public, anon, authenticated;
grant execute on function public.fleet_snapshot() to service_role;
grant execute on function public.fleet_commit(bigint,jsonb,jsonb) to service_role;
notify pgrst, 'reload schema';
commit;
