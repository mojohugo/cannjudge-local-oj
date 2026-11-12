from contextlib import asynccontextmanager
from pathlib import Path
import hashlib
import hmac
import json
import mimetypes
import secrets
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from .config import ROOT, load_settings, password_hash
from .problem import problem, registry, generator_example as example_for, check_source, DEFAULT_PROBLEM
from .store import Store, SubmissionBusy
from .worker import QueueWorker
from .scoring import comparison, select_baseline
from .mcp_server import build_mcp
from .leaderboard import Leaderboards
from .validation import validation_summary
from .contest_scoring import scoring_rules
from .official import OfficialJudge
from .official_login import OfficialLogin
from .official_targets import official_template
from .optimization_skill import read_skill, skill_hint


def create_app(settings=None):
    # Windows file-association MIME entries can incorrectly classify .js as text/plain.
    mimetypes.add_type('text/javascript', '.js')
    mimetypes.add_type('text/css', '.css')
    settings = settings or load_settings()
    store = Store(settings.state_dir)
    worker = QueueWorker(settings, store)
    leaderboards = Leaderboards(settings.state_dir, store)
    official = OfficialJudge(settings,store)
    official_login = OfficialLogin(official.accounts)
    attempts = {}
    attempts_lock = threading.Lock()
    mcp, oauth = build_mcp(settings, store, leaderboards, official)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        lock = None
        if settings.worker_enabled or settings.official_submit_enabled:
            lock = (settings.state_dir / 'service.lock').open('a+b')
            lock.seek(0)
            lock.write(b'1')
            lock.flush()
            lock.seek(0)
            try:
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except ImportError:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if settings.worker_enabled:worker.start()
            official.start()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            official_login.close()
            official.stop()
            worker.stop()
            if lock:
                lock.close()

    app = FastAPI(title='CannJudge Local OJ', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.settings = store, settings
    app.state.worker = worker
    app.state.official = official
    app.state.official_login = official_login
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    from .web_views import BrowserCompression
    app.add_middleware(BrowserCompression)

    @app.middleware('http')
    async def response_policy(request, call_next):
        # The SDK validates PKCE/client/redirect. Bind the token request's
        # resource explicitly too, including refresh requests.
        if request.url.path == '/token' and request.method == 'POST':
            size = request.headers.get('content-length')
            if not size or not size.isdigit() or int(size) > 16384:
                return JSONResponse({'error':'invalid_request'}, status_code=400)
            body = await request.body()
            values = parse_qs(body.decode('utf-8', errors='replace'))
            if values.get('resource') != [oauth.resource]:
                return JSONResponse({'error':'invalid_target','error_description':'resource must match the OJ MCP endpoint'}, status_code=400)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers['Cache-Control'] = ('public, max-age=31536000, immutable'
            if request.url.path.startswith('/static/releases/') else
            'public, max-age=0, must-revalidate' if request.url.path.startswith('/static/') else 'no-store')
        return response

    def same_origin(request):
        origin = request.headers.get('origin')
        expected = request_origin(request)
        if origin and origin != expected:
            raise HTTPException(403, '不接受跨站请求')

    def request_origin(request):
        # TLS terminates at the tunnel. Trust only the configured public host,
        # never forwarded headers supplied by a caller; keep loopback usable.
        host = request.headers.get('host', '')
        if settings.public_origin and host == urlparse(settings.public_origin).netloc:
            return settings.public_origin.rstrip('/')
        return f'{request.url.scheme}://{host}'

    def authenticated(request, mutation=False):
        authorization = request.headers.get('authorization', '')
        if authorization.startswith('Bearer '):
            digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
            if settings.api_token_digest and hmac.compare_digest(digest, settings.api_token_digest):
                return dict(csrf=None, api=True)
        token = request.cookies.get('oj_session', '')
        digest = hashlib.sha256(token.encode()).hexdigest()
        with store.db() as db:
            row = db.execute('SELECT * FROM sessions WHERE digest=? AND expires>?', (digest, time.time())).fetchone()
        if not row:
            raise HTTPException(401, '请先登录')
        if mutation:
            same_origin(request)
            if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), row['csrf']):
                raise HTTPException(403, 'CSRF token 无效，请重新登录')
        return dict(csrf=row['csrf'], api=False)

    async def json_body(request, limit):
        if request.headers.get('content-type', '').split(';')[0] != 'application/json':
            raise HTTPException(415, '请使用 application/json')
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, '请求超过大小限制')
            chunks.append(chunk)
        try:
            value = json.loads(b''.join(chunks))
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, 'JSON 格式错误')
        if not isinstance(value, dict):
            raise HTTPException(400, 'JSON 必须是对象')
        return value

    def submission(identity, *, queue_info=False):
        try:
            if uuid.UUID(identity).hex != identity:
                raise ValueError()
        except ValueError:
            raise HTTPException(404, '提交不存在')
        found = store.get(identity, queue_info=queue_info)
        if found is None:
            raise HTTPException(404, '提交不存在')
        return found

    def selected_problem(identity):
        try:
            return problem(identity)
        except ValueError as error:
            raise HTTPException(404, str(error))

    @app.get('/api/health')
    def health():
        result = {'status': 'ok'}
        if settings.worker_enabled:
            alive = sum(thread.is_alive() for thread in worker.threads)
            result['evaluation_workers'] = {'alive': alive, 'expected': worker.worker_count}
            if alive != worker.worker_count:result['status'] = 'degraded'
        if settings.official_submit_enabled:
            result['official_worker'] = official.worker_status()
            if (result['official_worker']['alive'] != result['official_worker']['expected']
                    or not result['official_worker']['supervisor_alive']):result['status'] = 'degraded'
        return JSONResponse(result,status_code=200 if result['status']=='ok' else 503)

    @app.get('/.well-known/oauth-authorization-server')
    def oauth_metadata():
        # MCP SDK 1.27 advertises secret-based methods even for a provider
        # accepting only public PKCE clients. Match our actual contract.
        return JSONResponse({
            'issuer': oauth.origin + '/',
            'authorization_endpoint': oauth.origin + '/authorize',
            'token_endpoint': oauth.origin + '/token',
            'registration_endpoint': oauth.origin + '/register',
            'revocation_endpoint': oauth.origin + '/revoke',
            'scopes_supported': ['oj:access'],
            'response_types_supported': ['code'],
            'grant_types_supported': ['authorization_code', 'refresh_token'],
            'token_endpoint_auth_methods_supported': ['none'],
            'revocation_endpoint_auth_methods_supported': ['none'],
            'code_challenge_methods_supported': ['S256'],
        }, headers={'Access-Control-Allow-Origin': '*'})

    @app.post('/revoke')
    async def oauth_revoke(request: Request):
        # SDK 1.27's revocation model requires a client_secret field even for
        # public clients. RFC 7009 permits public clients to omit that field.
        size = request.headers.get('content-length', '')
        if (request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded'
                or not size.isdigit() or int(size) > 16384):
            return JSONResponse({'error': 'invalid_request'}, status_code=400)
        values = parse_qs((await request.body()).decode('utf-8', errors='replace'))
        if len(values.get('client_id', [])) != 1 or len(values.get('token', [])) != 1:
            return JSONResponse({'error': 'invalid_request'}, status_code=400)
        client = await oauth.get_client(values['client_id'][0])
        if not client or client.token_endpoint_auth_method != 'none':
            return JSONResponse({'error': 'invalid_client'}, status_code=401)
        token = await oauth.load_access_token(values['token'][0])
        if token is None:
            token = await oauth.load_refresh_token(client, values['token'][0])
        if token and token.client_id == client.client_id:
            await oauth.revoke_token(token)
        return Response(status_code=200)

    @app.get('/oauth/consent')
    def oauth_consent():
        return FileResponse(ROOT / 'web' / 'consent.html')

    @app.get('/api/oauth/pending')
    def oauth_pending(request: str):
        pending = oauth.load('pending', request)
        if not pending:
            raise HTTPException(404, '授权请求已过期')
        return {'client_name': pending['client_name']}

    @app.post('/api/oauth/approve')
    async def oauth_approve(request: Request):
        same_origin(request)
        if request.headers.get('x-oj-consent') != '1':
            raise HTTPException(403, '缺少授权确认')
        body = await json_body(request, 2048)
        if not isinstance(body.get('request'), str) or not isinstance(body.get('api_key'), str):
            raise HTTPException(400, '参数无效')
        with attempts_lock:
            key = 'oauth:' + (request.client.host if request.client else 'unknown')
            now = time.time()
            recent = [t for t in attempts.get(key, []) if t > now - 300]
            if len(recent) >= 5:
                raise HTTPException(429, '授权尝试过多，请稍后再试')
            attempts[key] = recent + [now]
        try:
            redirect = oauth.approve(body['request'], body['api_key'])
        except ValueError as error:
            raise HTTPException(400, str(error))
        return {'redirect': redirect}

    @app.post('/api/login')
    async def login(request: Request):
        same_origin(request)
        body = await json_body(request, 2048)
        password = body.get('password')
        if not isinstance(password, str) or len(password) > 512:
            raise HTTPException(400, '密码格式无效')
        key = request.client.host if request.client else 'unknown'
        now = time.time()
        with attempts_lock:
            for stale in [k for k, times in attempts.items() if not times or max(times) < now - 300]:
                attempts.pop(stale, None)
            recent = [t for t in attempts.get(key, []) if t > now - 300]
            if len(recent) >= 5:
                raise HTTPException(429, '登录尝试过多，请 5 分钟后再试')
            attempts[key] = recent + [now]
        valid = hmac.compare_digest(password_hash(password, settings.password_salt), settings.password_digest)
        if not valid:
            raise HTTPException(401, '密码错误')
        with attempts_lock:
            attempts.pop(key, None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with store.db() as db:
            db.execute('DELETE FROM sessions WHERE expires<=?', (now,))
            db.execute('INSERT INTO sessions VALUES(?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), csrf, now + settings.session_seconds))
        response = JSONResponse({'csrf': csrf})
        response.set_cookie('oj_session', token, max_age=settings.session_seconds, httponly=True,
                            secure=settings.secure_cookies or request_origin(request).startswith('https://'),
                            samesite='strict', path='/')
        return response

    @app.get('/api/session')
    def session(request: Request):
        return authenticated(request)

    @app.post('/api/logout')
    def logout(request: Request):
        authenticated(request, True)
        token = request.cookies.get('oj_session', '')
        with store.db() as db:
            db.execute('DELETE FROM sessions WHERE digest=?', (hashlib.sha256(token.encode()).hexdigest(),))
        response = JSONResponse({'ok': True})
        response.delete_cookie('oj_session', path='/')
        return response

    @app.get('/api/problem')
    def get_problem(request: Request, problem_id: str = DEFAULT_PROBLEM):
        authenticated(request)
        return dict(selected_problem(problem_id), scoring=scoring_rules(problem_id))

    @app.get('/api/problems')
    def list_problems(request: Request):
        authenticated(request)
        return {'problems': [dict(p,scoring=scoring_rules(p['id'])) for p in registry.listing()], 'default_problem': DEFAULT_PROBLEM,
                'optimization_skill': skill_hint(),
                'backend': {'ssh_host': settings.ssh_host, 'remote_root': settings.remote_root,
                            'ssh_reconnect_seconds':settings.ssh_reconnect_seconds,
                            'ssh_job_attempts':settings.ssh_job_attempts,
                            'evaluation_workers':worker.worker_count, 'npu_exclusive':True,
                            'connection':worker.connector.status}}

    @app.get('/api/optimization-skill')
    def optimization_skill(request: Request, section: str='overview', problem_id: str | None=None,
                           reference: str | None=None, offset: int=0, limit: int=12000,
                           revision: int | None=None, view: str='current'):
        authenticated(request)
        try:
            return read_skill(section, problem_id, reference, offset, limit,
                              state_dir=settings.state_dir, revision=revision, view=view)
        except ValueError as error:
            raise HTTPException(400, str(error))

    @app.get('/api/problems/{problem_id}')
    def problem_detail(problem_id: str, request: Request):
        authenticated(request)
        return dict(selected_problem(problem_id), scoring=scoring_rules(problem_id))

    @app.get('/api/generator-example')
    def generator_example(request: Request, problem_id: str = DEFAULT_PROBLEM):
        authenticated(request)
        selected_problem(problem_id)
        return example_for(problem_id)

    @app.get('/api/leaderboard')
    def leaderboard(request: Request, problem_id: str = DEFAULT_PROBLEM):
        authenticated(request)
        selected_problem(problem_id)
        return leaderboards.get(problem_id)

    @app.get('/api/scoring')
    def scoring(request: Request, problem_id: str = DEFAULT_PROBLEM):
        authenticated(request)
        selected_problem(problem_id)
        return scoring_rules(problem_id)

    @app.post('/api/submissions/{identity}/baseline')
    def set_baseline(identity: str, request: Request):
        authenticated(request, True)
        try:
            return select_baseline(settings.state_dir, submission(identity))
        except ValueError as error:
            raise HTTPException(409, str(error))

    @app.get('/api/submissions')
    def list_submissions(request: Request, problem_id: str | None = None, summary_only: bool = False):
        authenticated(request)
        if problem_id is not None:
            selected_problem(problem_id)
        rows = store.listing(problem_id=problem_id, queue_info=True)
        if summary_only:
            from .web_views import local_summary
            specs = {key: problem(key) for key in {row['problem_id'] for row in rows}}
            for row in rows:
                row['validation'] = validation_summary(row, specs[row['problem_id']])
            return {'submissions': [local_summary(row) for row in rows]}
        boards = {p:leaderboards.get(p) for p in {row['problem_id'] for row in rows}}
        specs = {p:problem(p) for p in boards}
        for row in rows:
            row['validation'] = validation_summary(row, specs[row['problem_id']])
            row['comparison'] = comparison(settings.state_dir, row['result'])
            row['score'] = leaderboards.score(row['result'],boards[row['problem_id']])
        return {'submissions': rows}

    @app.post('/api/diagnostics/local-ab', status_code=202)
    async def submit_local_ab(request: Request):
        authenticated(request, mutation=True)
        body = await json_body(request, 4096)
        from .local_ab import enqueue_ab
        from .store import QueueFull
        try:
            return await run_in_threadpool(enqueue_ab, settings, store, body.get('submission_a'), body.get('submission_b'),
                              body.get('local_case_indices'), body.get('cycles'), body.get('budget_seconds'),
                              custom_case_indices=body.get('custom_case_indices'))
        except SubmissionBusy as error:
            raise HTTPException(503, error.detail, headers={"Retry-After": "1"})
        except QueueFull as error:
            raise HTTPException(429, str(error))
        except (ValueError, OSError) as error:
            raise HTTPException(400, str(error))

    @app.post('/api/submissions', status_code=202)
    async def submit(request: Request):
        authenticated(request, True)
        body = await json_body(request, settings.max_source_bytes * 6 + 8192)
        source = body.get('source')
        label = body.get('label', 'kernel.asc')
        generator = body.get('generator')
        problem_id = body.get('problem_id', DEFAULT_PROBLEM)
        profile_mode = body.get('profile_mode', 'full')
        if profile_mode not in ('full','timing'):
            raise HTTPException(400, 'profile_mode 必须是 full 或 timing')
        selected_problem(problem_id)
        if generator is not None and (not isinstance(generator, str) or not generator.strip() or
                                       len(generator.encode('utf-8')) > 128 * 1024 or '\x00' in generator):
            raise HTTPException(400, '数据生成器必须是非空 UTF-8 C++ 源码，最大 128 KiB')
        spec = selected_problem(problem_id)
        is_project = spec['contract'].get('submission_kind') == 'project'
        if is_project:
            from remote.project import normalize_files
            try:
                _, digest, encoded = normalize_files(body.get('files'), spec['contract']['editable_files'], settings.max_source_bytes)
                source = encoded.decode('utf-8')
            except (ValueError, UnicodeError) as error:
                raise HTTPException(400, str(error))
        elif body.get('files') is not None:
            raise HTTPException(400, '此题只接受单文件 source')
        if not isinstance(source, str) or not source.strip() or '\x00' in source:
            raise HTTPException(400, '源码不能为空或包含 NUL')
        if not isinstance(label, str) or len(label) > 120:
            raise HTTPException(400, '备注最长 120 个字符')
        try:
            encoded = source.encode('utf-8')
        except UnicodeEncodeError:
            raise HTTPException(400, '源码必须是合法 UTF-8')
        if not is_project and len(encoded) > settings.max_source_bytes:
            raise HTTPException(413, 'kernel.asc 最大 2 MiB')
        from remote.local_cases import validate_local_case_indices
        try:
            local_case_indices = validate_local_case_indices(spec, body.get('local_case_indices'), generator)
            from remote.pdist_structured import validate_suite
            regression_suite = validate_suite(spec, body.get('regression_suite'), generator, local_case_indices)
        except ValueError as error:
            raise HTTPException(400, str(error))
        identity = uuid.uuid4().hex
        try:
            enqueued = await run_in_threadpool(store.enqueue, identity, label.strip() or 'kernel.asc', digest if is_project else hashlib.sha256(encoded).hexdigest(), encoded, settings.queue_limit, generator, problem_id, 'timing' if regression_suite else profile_mode, local_case_indices=local_case_indices, regression_suite=regression_suite, **({'request_id': body['request_id']} if body.get('request_id') is not None else {}))
            if body.get('request_id') is not None:
                identity = enqueued
        except SubmissionBusy as error:
            raise HTTPException(503, error.detail, headers={"Retry-After": "1"})
        except ValueError as error:
            from .local_requests import RequestConflict
            if isinstance(error, RequestConflict):
                raise HTTPException(409, str(error))
            if body.get('request_id') is not None and 'request_id must' in str(error):
                raise HTTPException(400, str(error))
            raise HTTPException(429, str(error))
        row = store.get(identity, queue_info=True)
        if body.get('summary_only') is True:
            from .web_views import local_summary
            return local_summary(row)
        return row

    @app.post('/api/submissions/{identity}/rerun', status_code=202)
    async def rerun_archived(identity: str, request: Request):
        authenticated(request, True)
        body = await json_body(request, 128 * 1024 * 6 + 8192)
        from .local_archive import ArchivedKernelRequest, submit_archived
        from .local_requests import RequestConflict
        from .store import QueueFull
        if not isinstance(body, dict) or 'submission_id' in body:
            raise HTTPException(400, 'Use the local submission ID in the URL')
        try:
            arguments = ArchivedKernelRequest(submission_id=identity, **body)
            return await run_in_threadpool(submit_archived, settings, store, arguments)
        except SubmissionBusy as error:
            raise HTTPException(503, error.detail, headers={'Retry-After': '1'})
        except RequestConflict as error:
            raise HTTPException(409, str(error))
        except QueueFull as error:
            raise HTTPException(429, str(error))
        except ValueError as error:
            raise HTTPException(400, str(error))

    @app.post('/api/check')
    async def preflight(request: Request):
        authenticated(request, True)
        body = await json_body(request, settings.max_source_bytes * 6 + 8192)
        source = body.get('source')
        problem_id = body.get('problem_id', DEFAULT_PROBLEM)
        spec = selected_problem(problem_id)
        if spec['contract'].get('submission_kind') == 'project':
            from remote.project import normalize_files
            try:
                source = normalize_files(body.get('files'), spec['contract']['editable_files'], settings.max_source_bytes)[2].decode('utf-8')
            except (ValueError, UnicodeError) as error:
                raise HTTPException(400, str(error))
        if not isinstance(source, str) or (spec['contract'].get('submission_kind') != 'project' and len(source.encode('utf-8')) > settings.max_source_bytes):
            raise HTTPException(400, '源码格式或大小无效')
        problem_id = body.get('problem_id', DEFAULT_PROBLEM)
        selected_problem(problem_id)
        return check_source(source, problem_id)

    @app.get('/api/submission-requests/{request_id}')
    def submission_request(request_id: str, request: Request):
        authenticated(request)
        try:
            row = store.get_by_request_id(request_id)
        except ValueError as error:
            raise HTTPException(400, str(error))
        if row is None:
            raise HTTPException(404, 'request_id_not_found')
        from .local_requests import compact_receipt
        return compact_receipt(row)

    @app.get('/api/submissions/{identity}')
    def detail(identity: str, request: Request, binary_layout: bool=False, timings_only: bool=False, include_device_samples: bool=False):
        authenticated(request)
        row = submission(identity, queue_info=True)
        if timings_only:
            from .submission_timings import timing_view
            return timing_view(row, include_device_samples=include_device_samples, state_dir=settings.state_dir)
        row['validation'] = validation_summary(row)
        row['comparison'] = comparison(settings.state_dir, row['result'])
        row['score'] = leaderboards.score(row['result'])
        from .launch_metrics import with_launch_metrics
        from .pdist_diagnostics import with_pdist_diagnostics
        row = with_pdist_diagnostics(row, settings.state_dir, store, binary_layout=binary_layout)
        return with_launch_metrics(row, settings.state_dir)

    @app.get('/api/submissions/{identity}/source')
    def source(identity: str, request: Request):
        authenticated(request)
        row = submission(identity)
        is_project = row['result'].get('submission_kind') == 'project'
        filename = 'project.json' if is_project else 'kernel.asc'
        return FileResponse(settings.state_dir / 'submissions' / identity / filename,
                            filename=filename, media_type='application/json' if is_project else 'text/plain')

    @app.get('/api/submissions/{identity}/logs')
    def logs(identity: str, request: Request):
        authenticated(request)
        submission(identity)
        directory = settings.state_dir / 'submissions' / identity
        files = ['compile.log', 'generator.log', 'ssh.stderr.log', 'connection.log'] + [f'case-{i:02d}.stderr.log' for i in range(1, 31)]
        files += [f'attempts/{i}/{name}' for i in range(1,6) for name in ('compile.log','generator.log','ssh.stderr.log')]
        return {'logs': {name: (directory / name).read_text(encoding='utf-8', errors='replace')[-100000:]
                         for name in files if (directory / name).is_file()}}

    @app.get('/api/submissions/{identity}/generator')
    def generator_source(identity: str, request: Request):
        authenticated(request)
        submission(identity)
        path = settings.state_dir / 'submissions' / identity / 'generate.cpp'
        if not path.is_file():
            raise HTTPException(404, '该提交使用题目默认数据')
        return FileResponse(path, filename='generate.cpp', media_type='text/plain')

    @app.post('/api/submissions/{identity}/cancel')
    def cancel(identity: str, request: Request):
        authenticated(request, True)
        submission(identity)
        if not store.cancel_queued(identity):
            raise HTTPException(409, '仅能取消尚未开始的排队任务')
        return {'ok': True}

    def official_call(callback):
        try:
            return callback()
        except (ValueError,TypeError) as error:
            raise HTTPException(400,str(error))

    @app.get('/api/official/accounts')
    def official_accounts(request: Request, refresh_quotas: bool=False):
        authenticated(request)
        return official.account_settings(refresh_quotas)

    @app.post('/api/official/accounts',status_code=201)
    async def official_account_create(request: Request):
        authenticated(request,True)
        body=await json_body(request,1024)
        return official_call(lambda:official.accounts.create(body.get('label')))

    @app.post('/api/official/accounts/{identity}/activate')
    def official_account_switch(identity: str,request: Request):
        authenticated(request,True)
        return official_call(lambda:official.accounts.switch(identity))

    @app.post('/api/official/settings')
    async def official_settings(request: Request):
        authenticated(request,True)
        body=await json_body(request,1024)
        return official_call(lambda:official.accounts.set_validation(body.get('local_validation_required')))

    @app.post('/api/official/accounts/{identity}/login')
    def official_login_start(identity: str,request: Request):
        authenticated(request,True)
        return official_call(lambda:official_login.start(identity))

    @app.get('/api/official/login/{identity}')
    def official_login_frame(identity: str,request: Request):
        authenticated(request)
        return official_call(lambda:official_login.get(identity).rpc('frame'))

    @app.post('/api/official/login/{identity}/action')
    async def official_login_action(identity: str,request: Request):
        authenticated(request,True)
        body=await json_body(request,25000)
        action=body.get('action')
        fields={'click':('x','y'),'text':('text',),'key':('key',),'scroll':('delta',)}.get(action)
        if fields is None:raise HTTPException(400,'不支持的登录操作')
        return await run_in_threadpool(official_call,lambda:official_login.get(identity).rpc(action,**{k:body.get(k) for k in fields}))

    @app.post('/api/official/login/{identity}/finish')
    def official_login_finish(identity: str,request: Request):
        authenticated(request,True)
        return official_call(lambda:official_login.get(identity).finish())

    @app.post('/api/official/login/{identity}/close')
    def official_login_close(identity: str,request: Request):
        authenticated(request,True)
        # Closing an expired window is idempotent.
        try:official_login.get(identity).close()
        except ValueError:pass
        return {'closed':True}

    @app.get('/api/official/connection')
    def official_connection(request: Request, check_login: bool=False, account_id: str|None=None):
        authenticated(request)
        return official_call(lambda:official.connection(check_login,account_id))

    @app.get('/api/official/problems')
    def official_problems(request: Request, account_id: str|None=None):
        authenticated(request)
        return official_call(lambda:official.read_archive(account_id).catalog())

    @app.get('/api/official/problems/{problem_id}')
    def official_problem(request: Request, problem_id: str, account_id: str|None=None):
        authenticated(request)
        return official_call(lambda:official.read_archive(account_id).problem(problem_id))

    @app.get('/api/official/problems/{problem_id}/template')
    def official_project_template(request: Request, problem_id: str):
        authenticated(request)
        return official_call(lambda:official_template(problem_id))

    @app.get('/api/official/history')
    def official_history(request: Request, problem_id: str|None=None, limit: int=30, offset: int=0, order: str='newest', account_id: str|None=None, summary_only: bool=False):
        authenticated(request)
        value = official_call(lambda:official.read_archive(account_id).listing(problem_id,limit,offset,order))
        from .web_views import browser_record
        return browser_record(value) if summary_only else value

    @app.get('/api/official/case-bests')
    def official_case_bests(request: Request, problem_id: str, case_index: int|None=None, account_id: str|None=None, summary_only: bool=False):
        authenticated(request)
        value = official_call(lambda:official.read_archive(account_id).bests(problem_id,case_index))
        from .web_views import browser_record
        return browser_record(value) if summary_only else value

    @app.get('/api/official/history/{identity}')
    def official_record(request: Request, identity: str, account_id: str|None=None, summary_only: bool=False):
        authenticated(request)
        value = official_call(lambda:official.read_archive(account_id).get(identity))
        from .web_views import browser_record
        return browser_record(value) if summary_only else value

    @app.get('/api/official/history/{identity}/source')
    def official_source(request: Request, identity: str, file_path: str='kernel.asc', account_id: str|None=None):
        authenticated(request)
        record,content,sha=official_call(lambda:official.read_archive(account_id).source_content(identity,file_path))
        return Response(content,media_type='text/plain; charset=utf-8',headers={'X-Source-SHA256':sha})

    @app.get('/api/official/history/{identity}/project')
    def official_project_source(request: Request, identity: str, account_id: str|None=None):
        authenticated(request)
        value=official_call(lambda:official.read_archive(account_id).project_source(identity))
        return JSONResponse(value,headers={'Content-Disposition':'attachment; filename="'+value['problem_id']+'-project.json"'})

    @app.post('/api/official/sync',status_code=202)
    async def official_sync(request: Request):
        authenticated(request,True)
        body=await json_body(request,2048)
        return official_call(lambda:official.sync(body.get('problem_id'),body.get('account_id')))

    @app.get('/api/official/sync')
    def official_sync_status(request: Request, identity: str|None=None, account_id: str|None=None):
        authenticated(request)
        return official_call(lambda:official.archive_for(account_id).sync_get(identity))

    @app.get('/api/official/jobs')
    def official_jobs(request: Request, problem_id: str|None=None, limit: int=20, account_id: str|None=None):
        authenticated(request)
        return official_call(lambda:{'jobs':official.listing(problem_id,limit,account_id),'queues':official.queue_status(account_id)})

    @app.post('/api/official/jobs',status_code=202)
    async def official_submit(request: Request):
        authenticated(request,True)
        body=await json_body(request,settings.max_source_bytes*6+4096)
        return official_call(lambda:official.submit(body.get('problem_id'),body.get('request_id'),
            source=body.get('source'),local_submission_id=body.get('local_submission_id'),label=body.get('label','kernel.asc'),
            account_id=body.get('account_id'),expected_local_validation=body.get('expected_local_validation'),files=body.get('files'),purpose=body.get('purpose')))

    @app.get('/api/official/jobs/{identity}')
    def official_job(request: Request,identity: str):
        authenticated(request)
        return official_call(lambda:official.get(identity))

    @app.post('/api/official/jobs/{identity}/refresh')
    def official_job_refresh(request: Request,identity: str):
        authenticated(request,True)
        return official_call(lambda:official.get(identity,refresh=True))

    @app.get('/official')
    def official_page():
        return FileResponse(ROOT/'web/official.html')

    app.mount('/static', StaticFiles(directory=ROOT / 'web'), name='static')

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'web' / 'index.html')

    app.mount('/', mcp_app)

    return app
