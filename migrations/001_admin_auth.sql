-- Run once in the Supabase SQL editor as the project owner.
-- Membership is separate from auth.users: registration never grants admin access.
begin;
create table public.admin_memberships (
    user_id uuid primary key references auth.users(id) on delete cascade,
    display_name text not null check (char_length(display_name) between 1 and 120),
    role text not null default 'admin' check (role in ('super_admin', 'admin')),
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
alter table public.admin_memberships enable row level security;
revoke all on public.admin_memberships from anon, authenticated;
grant select on public.admin_memberships to authenticated;
grant all on public.admin_memberships to service_role;
create policy "Administrators can read their own active membership"
on public.admin_memberships for select to authenticated
using ((select auth.uid()) = user_id and is_active = true);
-- Deliberately no INSERT/UPDATE/DELETE policies for authenticated clients.
create function public.touch_admin_membership() returns trigger
language plpgsql set search_path = '' as $$
begin
    new.updated_at = now();
    return new;
end;
$$;
create trigger admin_membership_updated before update on public.admin_memberships
for each row execute function public.touch_admin_membership();
commit;
