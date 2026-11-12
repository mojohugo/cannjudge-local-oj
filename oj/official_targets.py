"""Administrator-selected official targets and immutable public templates."""
import hashlib
import json
import re
from .config import ROOT

CONFIG_PATH = ROOT / 'config/official_submit.json'
CONTESTS = {'https://cannjudge.cn/ccpc/ladder_2026_changchun',
            'https://cannjudge.cn/public/2026_electronic',
            'https://cannjudge.cn/ccpc/ladder_2026_jingzhou',
            'https://cannjudge.cn/ccpc/ladder_2026_xiamen'}
PUBLIC_TARGETS = {'public_add': ('https://cannjudge.cn/public/vector', 'add'),
                  'erf': ('https://cannjudge.cn/icpc/competition', 'erf'),
                  'mish': ('https://cannjudge.cn/ccpc/onsite_2026_changchun', 'mish')}


def configuration():
    value = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    if value['contest_url'] not in CONTESTS:
        raise ValueError('官方赛事未在管理员允许列表中')
    if any(not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', key) or not re.fullmatch(r'[a-z0-9_]+', slug)
           for key, slug in value['problems'].items()):
        raise ValueError('Invalid official problem mapping')
    for key, url in value.get('contest_overrides', {}).items():
        if key not in value['problems'] or (url not in CONTESTS and
                PUBLIC_TARGETS.get(key) != (url, value['problems'][key])):
            raise ValueError('Invalid official contest override')
    return value


def problem_url(config, identity):
    return config.get('contest_overrides', {}).get(identity, config['contest_url']) + '/' + config['problems'][identity]


def project_template(identity):
    config = configuration()
    if identity not in config.get('project_templates', {}):
        raise ValueError('该题使用 kernel.asc；请调用 submit_official_kernel')
    return official_template(identity)


def official_template(identity):
    config = configuration()
    is_project = identity in config.get('project_templates', {})
    templates = config.get('project_templates' if is_project else 'kernel_templates', {})
    if identity not in templates:
        raise ValueError('本题尚未归档公开模板；可从官网或已有官方提交读取模板')
    path = ROOT / 'official_templates' / templates[identity]
    if path.resolve().parent != (ROOT / 'official_templates').resolve():
        raise ValueError('Invalid template path')
    value = json.loads(path.read_text(encoding='utf-8'))
    if value['problem_id'] != identity or value['official_url'] != problem_url(config, identity):
        raise ValueError('Template target mismatch')
    value['submission_kind'] = 'project' if is_project else 'kernel'
    value['submission_tool'] = 'submit_official_project' if is_project else 'submit_official_kernel'
    if not is_project and value['editable_files'] != ['kernel.asc']:
        raise ValueError('Kernel template must edit only kernel.asc')
    value['local_evaluation'] = local_evaluation(identity)
    return value


def local_evaluation(identity):
    from .problem import registry
    try:
        spec = registry.get(identity)
    except ValueError:
        return dict(available=False, reason='尚未启用本题本地评测；仅支持官方提交。测试数据规格以 data_specifications 的证据为准，未知数据不作推断。')
    return dict(available=True, tool='submit_project' if spec['contract'].get('submission_kind')=='project' else 'submit_kernel',
                problem_sha256=spec['pack_sha256'], dataset=spec.get('dataset'))


def public_problem(identity):
    config = configuration()
    if identity not in config['problems']:
        raise ValueError('题目未配置')
    path = ROOT/'official_problems'/(identity+'.json')
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding='utf-8'))
    template = official_template(identity)
    if value.get('problem_id')!=identity or value.get('official_id')!=template['official_problem_id'] or value.get('official_url')!=problem_url(config,identity):
        raise ValueError('公开题目元数据与官方模板不匹配')
    return value


def normalize_project(identity, files, max_bytes):
    template = project_template(identity)
    required = set(template['editable_files'])
    if not isinstance(files, list) or len(files) != len(required):
        raise ValueError('必须提供完整的可编辑文件集合：' + ', '.join(sorted(required)))
    output = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {'path', 'content'}:
            raise ValueError('每个工程文件必须只有 path 和 content')
        path, content = item['path'], item['content']
        if not isinstance(path, str) or path not in required or path in output:
            raise ValueError('文件重复、只读或不在当前官方模板中')
        if not isinstance(content, str) or not content.strip() or '\x00' in content:
            raise ValueError('工程源码不能为空或包含 NUL')
        output[path] = content.removeprefix('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    if sum(len(v.encode()) for v in output.values()) > max_bytes:
        raise ValueError('Source too large')
    rows = [{'path': p, 'content': c} for p, c in sorted(output.items())]
    hashes = [[r['path'], hashlib.sha256(r['content'].encode()).hexdigest()] for r in rows]
    digest = hashlib.sha256(json.dumps(hashes, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return rows, digest, template
