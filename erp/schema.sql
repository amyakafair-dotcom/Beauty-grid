-- Beauty-Grid ERP schema for Supabase (Postgres).
-- Run once in Supabase: SQL Editor -> New query -> paste -> Run.
-- Then make yourself admin (see the last block).

create extension if not exists pgcrypto;

-- Producers (Beauty-Grid's customers)
create table if not exists producers (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  phone text, city text,
  currency text not null default 'SDG',
  notes text,
  created_at timestamptz not null default now()
);

-- Who can log in: admin (Beauty-Grid staff) or producer (sees own data only)
create table if not exists profiles (
  id uuid primary key references auth.users on delete cascade,
  full_name text,
  role text not null default 'producer' check (role in ('admin', 'producer')),
  producer_id uuid references producers on delete set null
);

create table if not exists materials (
  id uuid primary key default gen_random_uuid(),
  producer_id uuid not null references producers on delete cascade,
  name text not null,
  kind text not null default 'raw' check (kind in ('raw', 'pack')),
  unit text not null default 'g',
  cost numeric not null default 0,          -- cost per unit
  stock numeric not null default 0,
  reorder_level numeric not null default 0,
  supplier text,
  created_at timestamptz not null default now()
);

create table if not exists products (
  id uuid primary key default gen_random_uuid(),
  producer_id uuid not null references producers on delete cascade,
  name text not null,
  sku text, size text,
  price numeric not null default 0,
  stock numeric not null default 0,         -- QC-passed units available to sell
  reorder_level numeric not null default 0,
  created_at timestamptz not null default now()
);

-- Formula / bill of materials: material quantity per one unit of product
create table if not exists formulas (
  id uuid primary key default gen_random_uuid(),
  producer_id uuid not null references producers on delete cascade,
  product_id uuid not null references products on delete cascade,
  material_id uuid not null references materials on delete cascade,
  qty_per_unit numeric not null
);

-- Production batches with QC
create table if not exists batches (
  id uuid primary key default gen_random_uuid(),
  producer_id uuid not null references producers on delete cascade,
  product_id uuid not null references products on delete cascade,
  code text not null,
  qty numeric not null,
  remaining numeric not null default 0,
  made_on date not null default current_date,
  expires_on date,
  qc text not null default 'pending' check (qc in ('pending', 'pass', 'fail')),
  qc_notes text,
  created_at timestamptz not null default now()
);

-- Every stock change, for audit
create table if not exists movements (
  id uuid primary key default gen_random_uuid(),
  producer_id uuid not null references producers on delete cascade,
  item_type text not null check (item_type in ('material', 'product')),
  item_id uuid not null,
  qty numeric not null,                     -- + in, - out
  reason text not null,
  ref text,
  created_at timestamptz not null default now()
);

create table if not exists customers (
  id uuid primary key default gen_random_uuid(),
  producer_id uuid not null references producers on delete cascade,
  name text not null,
  phone text, city text,
  channel text default 'instagram',
  notes text,
  created_at timestamptz not null default now()
);

create table if not exists orders (
  id uuid primary key default gen_random_uuid(),
  producer_id uuid not null references producers on delete cascade,
  customer_id uuid references customers on delete set null,
  channel text not null default 'instagram',
  status text not null default 'new' check (status in ('new', 'confirmed', 'delivering', 'delivered', 'cancelled')),
  paid boolean not null default false,
  delivery_fee numeric not null default 0,
  total numeric not null default 0,
  ordered_on date not null default current_date,
  notes text,
  created_at timestamptz not null default now()
);

create table if not exists order_items (
  id uuid primary key default gen_random_uuid(),
  producer_id uuid not null references producers on delete cascade,
  order_id uuid not null references orders on delete cascade,
  product_id uuid references products on delete set null,
  qty numeric not null,
  price numeric not null
);

create table if not exists expenses (
  id uuid primary key default gen_random_uuid(),
  producer_id uuid not null references producers on delete cascade,
  category text not null,
  amount numeric not null,
  spent_on date not null default current_date,
  notes text,
  created_at timestamptz not null default now()
);

-- ---------- Row-level security ----------
create or replace function is_admin() returns boolean
language sql stable security definer set search_path = public as
$$ select exists (select 1 from profiles where id = auth.uid() and role = 'admin') $$;

create or replace function my_producer() returns uuid
language sql stable security definer set search_path = public as
$$ select producer_id from profiles where id = auth.uid() $$;

alter table producers enable row level security;
alter table profiles enable row level security;
drop policy if exists admin_all on producers;
create policy admin_all on producers for all using (is_admin()) with check (is_admin());
drop policy if exists own_read on producers;
create policy own_read on producers for select using (id = my_producer());
drop policy if exists admin_all on profiles;
create policy admin_all on profiles for all using (is_admin()) with check (is_admin());
drop policy if exists self_read on profiles;
create policy self_read on profiles for select using (id = auth.uid());

do $$
declare t text;
begin
  foreach t in array array['materials','products','formulas','batches','movements','customers','orders','order_items','expenses'] loop
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists admin_all on %I', t);
    execute format('create policy admin_all on %I for all using (is_admin()) with check (is_admin())', t);
    execute format('drop policy if exists own_read on %I', t);
    execute format('create policy own_read on %I for select using (producer_id = my_producer())', t);
  end loop;
end $$;

-- New sign-ups get a producer profile with no producer yet (admin links them).
create or replace function handle_new_user() returns trigger
language plpgsql security definer set search_path = public as
$$ begin
  insert into profiles (id, full_name) values (new.id, new.raw_user_meta_data->>'full_name');
  return new;
end $$;
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users
  for each row execute function handle_new_user();

-- ---------- Make yourself admin ----------
-- 1. Authentication -> Users -> Add user (your email + password).
-- 2. Run (replace the email):
-- update profiles set role = 'admin' where id = (select id from auth.users where email = 'you@example.com');
