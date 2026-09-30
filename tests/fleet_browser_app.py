"""Local browser fixture: test auth, in-memory persistence and fake external storage."""
from contextlib import asynccontextmanager

import httpx

from app.config import Settings
from app.main import create_app
from tests.test_auth import Provider
from tests.test_fleet import MemoryStore

provider=Provider()
files={}


def upstream(request):
    if request.url.path.startswith('/storage/v1/object/'):
        key=request.url.path
        if request.method=='POST':
            files[key]=request.content
            return httpx.Response(200,json={'Key':key})
        if request.method=='GET':
            return httpx.Response(200 if key in files else 404,content=files.get(key,b''))
        return httpx.Response(200,json={})
    return provider(request)


settings=Settings(_env_file=None,app_env='test',supabase_url='https://test.example.com',
    supabase_publishable_key='test-publishable',supabase_secret_key='test-secret',
    frontend_origin='http://localhost:3002',allowed_origins=['http://localhost:3002', 'http://localhost:3009'],
    allowed_hosts=['localhost','127.0.0.1'],auth_rate_limit=100)
app=create_app(settings,transport=httpx.MockTransport(upstream))
original=app.router.lifespan_context


@asynccontextmanager
async def lifespan(app):
    async with original(app):
        store=MemoryStore()
        store.settings=settings
        store.client=app.state.fleet.client
        app.state.fleet=store
        yield


app.router.lifespan_context=lifespan
