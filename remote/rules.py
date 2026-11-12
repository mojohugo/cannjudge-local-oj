"""Transparent local preflight, not a replica of the private official validator."""
import re


def inspect_source(source, contract=None):
    contract = contract or {'entrypoint': 'run_kernel'}
    if contract.get('submission_kind') == 'project':
        if __package__:
            from .project import inspect_project
        else:
            from project import inspect_project
        return inspect_project(source, contract, inspect_source)
    entrypoint = contract['entrypoint']
    # Preserve offsets/newlines while ignoring comments and ordinary literals.
    token = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
    masked = token.sub(lambda m: ''.join('\n' if c=='\n' else ' ' for c in m.group()), source)
    issues=[]
    def add(rule,match,message,suggestion,origin='local_service_policy',severity='error'):
        pos=match.start() if hasattr(match,'start') else int(match)
        if hasattr(match, 'group'):
            pos += len(match.group()) - len(match.group().lstrip())
        line=source.count('\n',0,pos)+1
        column=pos-(source.rfind('\n',0,pos)+1)+1
        issues.append(dict(rule_id=rule,severity=severity,origin=origin,stage='static',line=line,column=column,
                           message=message,evidence=source.splitlines()[line-1][:240] if source.splitlines() else '',suggestion=suggestion))
    entries=list(re.finditer(r'\bvoid\s+'+re.escape(entrypoint)+r'\s*\([^;{}]*\)\s*\{',masked))
    if contract.get('require_entrypoint', True) and len(entries)!=1:
        add('ENTRYPOINT',0,f'需要一个 void {entrypoint} 定义，检测到 {len(entries)} 个','保留题目模板入口的参数、返回类型和 C linkage。','template_contract')
    for match in re.finditer(r'\b(?:main|WinMain)\s*\(',masked):
        add('PROTECTED_RUNNER',match,'提交不能定义独立程序入口','仅提交 kernel.asc；main 由评测器提供。','template_contract')
    for match in re.finditer(r'\b(?:local_bench|oj_memory|OJLaunchCount|CUMSUM_KERNEL_SOURCE|COSINE_SIMILARITY_KERNEL_SOURCE|LOG_SIGMOID_KERNEL_SOURCE|__wrap_\w+|__real_\w+)\b',masked):
        add('JUDGE_INTEGRITY',match,'源码引用了评测器内部符号','删除评测器符号依赖，只使用题目公开接口。')
    banned=r'\b(?:system|popen|pclose|fork|vfork|execve|execv|execl|socket|connect|sendto|recvfrom|fopen|freopen|open|openat|unlink|remove|chmod|chown|dlopen|dlsym|syscall|getenv|putenv|setenv)\s*\('
    for match in re.finditer(banned,masked):
        add('HOST_SIDE_EFFECT',match,'kernel 源码包含进程、文件、网络或环境访问操作','将数据生成放入独立 generate.cpp；kernel 只计算输入张量。这是本服务策略，不代表已确认的完整官方禁令。')
    # Do not rescan a run of blank/comment-masked lines from every newline.
    # Leading whitespace before # stays on its own line; issue offsets after
    # lstrip are identical, including directives preceded by blank lines.
    for match in re.finditer(r'^[^\S\n]*#\s*(?:define|undef)\s+(?:main|run_kernel|TENSOR_GROUP_INFO_DEFINED|CUMSUM_\w+|COSINE_SIMILARITY_KERNEL_SOURCE|LOG_SIGMOID_KERNEL_SOURCE|aclrt\w+)\b',masked,re.M):
        add('PROTECTED_MACRO',match,'不能改写评测器入口、类型或 runtime API 宏','删除会影响评测器的宏定义。')
    # Include names require original string contents; first strip only comments.
    comments=re.sub(r'//[^\n]*|/\*[\s\S]*?\*/',lambda m:''.join('\n' if c=='\n' else ' ' for c in m.group()),source)
    for match in re.finditer(r'^[^\S\n]*#\s*include\s*[<"]([^>"\n]+)[>"]',comments,re.M):
        name=match.group(1)
        if name not in contract.get('allowed_includes', []) and ('..' in name or name.startswith(('/', '\\')) or ':' in name or name in {'main.asc','judge.asc','data_utils.h'}):
            add('INCLUDE_BOUNDARY',match,'非法或受保护的 include 路径：'+name,'使用 SDK / 标准头文件；不能读取主程序、评测器或绝对路径文件。')
    for match in re.finditer(r'\b(?:printf|fprintf|aclrtGetDeviceInfo|aclrtGetSocName|aclrtMemcpy|aclrtMallocHost)\s*\(',masked):
        add('REVIEW_HOST_API',match,'发现需人工复核的 host API；该 API 单项是否被官方接受尚未确认','检查是否仅用于必要的数据搬运或诊断；不将组合拒绝误解为此 API 单项禁令。',severity='warning')
    return dict(passed=not any(x['severity']=='error' for x in issues), issues=issues,
                coverage=['single source entrypoint','protected harness names/macros','direct host side effects','include paths'],
                limitations=['静态检查不是完整 C++ AST/宏展开或恶意源码沙箱。','单 kernel 规则由运行时逐调用检查与 profiler 交叉核对。','官方私有规则和全部异常场景尚未公开，本地通过不保证官方合规。'])
