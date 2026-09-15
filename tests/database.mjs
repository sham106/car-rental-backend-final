// Real PostgreSQL compiled to WASM. No live Supabase credentials or data are used.
import { PGlite } from '../../Car-rental1/.review-tools/node_modules/@electric-sql/pglite/dist/index.js';
import { btree_gist } from '../../Car-rental1/.review-tools/node_modules/@electric-sql/pglite/dist/contrib/btree_gist.js';
import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
const db = new PGlite({extensions:{btree_gist}});
await db.exec(`create schema extensions; create schema storage;
create role anon; create role authenticated; create role service_role bypassrls;
create table storage.buckets (id text primary key,name text,public boolean,file_size_limit bigint,allowed_mime_types text[]);`);
await db.exec(await readFile(new URL('../migrations/002_fleet_backend.sql',import.meta.url),'utf8'));
console.log('PASS migration executes in PostgreSQL');
await db.exec('set role anon');
await assert.rejects(db.query('select public.fleet_snapshot()'), /permission denied/);
await assert.rejects(db.query('select * from public.fleet_vehicles'), /permission denied/);
await db.exec('set role authenticated');
await assert.rejects(db.query('select public.fleet_commit(0,\'[]\',\'[]\')'), /permission denied/);
console.log('PASS anonymous and authenticated clients cannot bypass FastAPI');
await db.exec('set role service_role');
const changes = [
 {resource:'owners',id:'owner',data:{id:'owner',name:'Owner'}},
 {resource:'vehicles',id:'car',data:{id:'car',ownerId:'owner',registrationNumber:'AB 123',slug:'test-car'}},
];
const commit = async (revision,rows,allocations=[]) => (await db.query('select public.fleet_commit($1,$2::jsonb,$3::jsonb) as ok',[revision,JSON.stringify(rows),JSON.stringify(allocations)])).rows[0].ok;
assert.equal(await commit(0,changes),true);
assert.equal(await commit(0,[]),false);
console.log('PASS stale revisions fail without modifying data');
await assert.rejects(commit(1,[{resource:'vehicles',id:'duplicate',data:{id:'duplicate',ownerId:'owner',registrationNumber:'ab123',slug:'other'}}]),/unique constraint/);
await assert.rejects(commit(1,[{resource:'vehicles',id:'missing-owner',data:{id:'missing-owner',ownerId:'missing',registrationNumber:'other',slug:'missing'}}]),/foreign key/);
console.log('PASS database enforces normalized registration uniqueness and owner references');
const a={id:'a',vehicleId:'car',startDate:'2030-01-01',endDate:'2030-01-03',kind:'booking'};
assert.equal(await commit(1,[],[a]),true);
await assert.rejects(commit(2,[{resource:'audit',id:'rolled-back',data:{id:'rolled-back'}}],[a,{...a,id:'b',kind:'assignment',startDate:'2030-01-03'}]),/exclusion constraint/);
let snapshot=(await db.query('select public.fleet_snapshot() as state')).rows[0].state;
assert.equal(snapshot.revision,2); assert.equal(snapshot.records.audit.length,0);
assert.equal((await db.query('select count(*)::int as n from public.fleet_allocations')).rows[0].n,1);
console.log('PASS overlapping bookings and assignments roll back the entire transaction, including audit');
assert.equal(await commit(2,[],[a,{...a,id:'b',kind:'assignment',startDate:'2030-01-04',endDate:'2030-01-05'}]),true);
console.log('PASS non-overlapping allocations commit');
assert.equal(await commit(3,[{resource:'audit',id:'audit',data:{id:'audit'}}]),true);
await assert.rejects(commit(4,[{resource:'audit',id:'audit',data:null}]),/append only/);
console.log('PASS audit records cannot be deleted through the transaction API');
await db.close();
