-- Apply after 001_admin_auth.sql. No existing admin data is removed.
begin;
create extension if not exists btree_gist with schema extensions;
create table if not exists public.fleet_revision (singleton boolean primary key default true check(singleton), revision bigint not null default 0);
insert into public.fleet_revision values (true, 0) on conflict (singleton) do nothing;
alter table public.fleet_revision enable row level security;
revoke all on public.fleet_revision from public, anon, authenticated;
grant all on public.fleet_revision to service_role;
create table if not exists public.fleet_owners (id text primary key, data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id));
alter table public.fleet_owners enable row level security;
revoke all on public.fleet_owners from public, anon, authenticated;
grant all on public.fleet_owners to service_role;
create table if not exists public.fleet_vehicles (id text primary key, data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id), owner_id text generated always as (data->>'ownerId') stored not null references public.fleet_owners(id));
alter table public.fleet_vehicles enable row level security;
revoke all on public.fleet_vehicles from public, anon, authenticated;
grant all on public.fleet_vehicles to service_role;
create table if not exists public.fleet_customers (id text primary key, data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id));
alter table public.fleet_customers enable row level security;
revoke all on public.fleet_customers from public, anon, authenticated;
grant all on public.fleet_customers to service_role;
create table if not exists public.fleet_bookings (id text primary key, data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id), vehicle_id text generated always as (data->>'vehicleId') stored not null references public.fleet_vehicles(id), customer_id text generated always as (data->>'customerId') stored not null references public.fleet_customers(id));
alter table public.fleet_bookings enable row level security;
revoke all on public.fleet_bookings from public, anon, authenticated;
grant all on public.fleet_bookings to service_role;
create table if not exists public.fleet_assignments (id text primary key, data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id), vehicle_id text generated always as (data->>'vehicleId') stored not null references public.fleet_vehicles(id));
alter table public.fleet_assignments enable row level security;
revoke all on public.fleet_assignments from public, anon, authenticated;
grant all on public.fleet_assignments to service_role;
create table if not exists public.fleet_maintenance (id text primary key, data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id), vehicle_id text generated always as (data->>'vehicleId') stored not null references public.fleet_vehicles(id));
alter table public.fleet_maintenance enable row level security;
revoke all on public.fleet_maintenance from public, anon, authenticated;
grant all on public.fleet_maintenance to service_role;
create table if not exists public.fleet_compliance (id text primary key, data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id), vehicle_id text generated always as (data->>'vehicleId') stored not null references public.fleet_vehicles(id));
alter table public.fleet_compliance enable row level security;
revoke all on public.fleet_compliance from public, anon, authenticated;
grant all on public.fleet_compliance to service_role;
create table if not exists public.fleet_files (id text primary key, data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id));
alter table public.fleet_files enable row level security;
revoke all on public.fleet_files from public, anon, authenticated;
grant all on public.fleet_files to service_role;
create table if not exists public.fleet_documents (id text primary key, data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id), vehicle_id text generated always as (data->>'vehicleId') stored not null references public.fleet_vehicles(id), file_id text generated always as (data->>'fileId') stored not null references public.fleet_files(id));
alter table public.fleet_documents enable row level security;
revoke all on public.fleet_documents from public, anon, authenticated;
grant all on public.fleet_documents to service_role;
create table if not exists public.fleet_settings (id text primary key, data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id));
alter table public.fleet_settings enable row level security;
revoke all on public.fleet_settings from public, anon, authenticated;
grant all on public.fleet_settings to service_role;
create table if not exists public.fleet_categories (id text primary key, data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id));
alter table public.fleet_categories enable row level security;
revoke all on public.fleet_categories from public, anon, authenticated;
grant all on public.fleet_categories to service_role;
create table if not exists public.fleet_locations (id text primary key, data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id));
alter table public.fleet_locations enable row level security;
revoke all on public.fleet_locations from public, anon, authenticated;
grant all on public.fleet_locations to service_role;
create table if not exists public.fleet_audit (id text primary key, data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id));
alter table public.fleet_audit enable row level security;
revoke all on public.fleet_audit from public, anon, authenticated;
grant all on public.fleet_audit to service_role;
create table if not exists public.fleet_notifications (id text primary key, data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id));
alter table public.fleet_notifications enable row level security;
revoke all on public.fleet_notifications from public, anon, authenticated;
grant all on public.fleet_notifications to service_role;
create table if not exists public.fleet_outbox (id text primary key, data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id));
alter table public.fleet_outbox enable row level security;
revoke all on public.fleet_outbox from public, anon, authenticated;
grant all on public.fleet_outbox to service_role;
create table if not exists public.fleet_idempotency (id text primary key, data jsonb not null check (jsonb_typeof(data)='object' and data->>'id'=id));
alter table public.fleet_idempotency enable row level security;
revoke all on public.fleet_idempotency from public, anon, authenticated;
grant all on public.fleet_idempotency to service_role;
create unique index if not exists fleet_vehicle_registration_unique on public.fleet_vehicles (upper(regexp_replace(data->>'registrationNumber', '\s', '', 'g')));
create unique index if not exists fleet_vehicle_slug_unique on public.fleet_vehicles ((data->>'slug'));
create unique index if not exists fleet_booking_reference_unique on public.fleet_bookings ((data->>'reference'));
create table if not exists public.fleet_allocations (
 id text primary key, vehicle_id text not null references public.fleet_vehicles(id),
 occupied daterange not null, kind text not null check (kind in ('booking','assignment')),
 exclude using gist (vehicle_id with =, occupied with &&)
);
alter table public.fleet_allocations enable row level security;
revoke all on public.fleet_allocations from public, anon, authenticated;
grant all on public.fleet_allocations to service_role;
create or replace function public.fleet_snapshot() returns jsonb language plpgsql stable security invoker set search_path='' as $$
declare result jsonb := '{}'::jsonb; r text; items jsonb;
begin
 foreach r in array array['owners','vehicles','customers','bookings','assignments','maintenance','compliance','files','documents','settings','categories','locations','audit','notifications','outbox','idempotency'] loop
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
  if not r = any(array['owners','vehicles','customers','bookings','assignments','maintenance','compliance','files','documents','settings','categories','locations','audit','notifications','outbox','idempotency']) then raise exception 'Unknown resource'; end if;
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
insert into storage.buckets(id,name,public,file_size_limit,allowed_mime_types)
values ('fleet-files','fleet-files',false,10485760,array['image/jpeg','image/png','image/webp','application/pdf'])
on conflict(id) do nothing;
-- Storage stays private. FastAPI checks authorization and serves files through /api.
notify pgrst, 'reload schema';
commit;
