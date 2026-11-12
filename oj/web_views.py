"""Small read models for the browser; archived evidence remains untouched."""
from starlette.middleware.gzip import GZipMiddleware


def local_summary(row):
    result = row.get('result') or {}
    cases = result.get('cases') or []
    value = {k: row.get(k) for k in ('id', 'label', 'problem_id', 'created', 'updated',
                                     'status', 'sha256', 'queue_stage', 'ahead_count', 'validation')}
    value.update(_summary=True, completed_cases=len(cases),
                 passed_cases=sum(c.get('status') == 'accepted' for c in cases),
                 result={k: result.get(k) for k in ('message', 'suite', 'profile_mode', 'local_case_indices')})
    return value


def browser_record(value):
    """Omit duplicated raw evidence only; keep score provenance and unknowns."""
    if isinstance(value, list):
        return [browser_record(item) for item in value]
    if isinstance(value, dict):
        result = {key: browser_record(item) for key, item in value.items()
                  if key not in {'evidence', 'input_statistics', 'distribution_samples'}}
        evidence = value.get('evidence')
        if isinstance(evidence, dict):
            brief = {key: item for key, item in evidence.items()
                     if key in {'label', 'source', 'status', 'confidence'}
                     and isinstance(item, (str, int, float, bool))}
            if brief:
                result['evidence'] = brief
        return result
    return value


class BrowserCompression:
    """Never buffer/compress MCP or OAuth streams."""
    def __init__(self, app):
        self.app = app
        self.compressed = GZipMiddleware(app, minimum_size=1200, compresslevel=3)

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        target = self.compressed if (scope['type'] == 'http' and
                    (path.startswith('/api/') or path.startswith('/static/'))) else self.app
        await target(scope, receive, send)
