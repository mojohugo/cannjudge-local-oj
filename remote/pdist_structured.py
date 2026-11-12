"""Whitelisted Pdist correctness suite, separate from canonical/performance data.

Only administrator code creates inputs and references; no user files, shape,
coefficient, reference, or work-limit override is accepted by this entry point.
"""
import hashlib
import json
from pathlib import Path
import subprocess

SUITE = 'pdist-wide-structured-v1'
KINDS = ('float32', 'float16', 'bfloat16')
# n, d, dtype_id, scale exponent, mask layout. Do not change a version in place.
CASES = ()  # Install a reviewed private recipe before enabling this suite.


def validate_suite(spec, suite, generator=None, indices=None):
    if suite is None:
        return None
    if not CASES:
        raise ValueError('Private structured regression recipe is not installed')
    if (spec['id'] != 'pdist' or type(suite) is not str or suite != SUITE
            or generator is not None or indices is not None):
        raise ValueError('regression_suite requires Pdist '+SUITE+' without generator or local_case_indices')
    return suite


def recipe_sha256():
    digest = hashlib.sha256()
    for path in (Path(__file__), Path(__file__).with_suffix('.cpp')):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def suite_info():
    return dict(name=SUITE, problem_id='pdist', correctness_only=True,
                scoring_eligible=False, recipe_sha256=recipe_sha256(),
                formula='sqrt(k1*(ai-aj)^2+k2*(bi-bj)^2)',
                bounds=dict(integer_coefficients=[-128,128], scale_exponents=[-10,-4,0],
                            maximum_columns=16385, integer_squared_sum_upper_bound=16385*256**2),
                checks=['full_output', 'dual_poison', 'input_immutability'],
                cases=[dict(index=i,shape=[n,d],dtype=KINDS[t],scale_exponent=e,mask_layout=m)
                       for i,(n,d,t,e,m) in enumerate(CASES,1)])


def dataset_error(dataset):
    info = suite_info()
    if (not isinstance(dataset, dict) or dataset.get('mode') != 'structured_regression'
            or dataset.get('version') != SUITE or dataset.get('regression_suite') != SUITE
            or dataset.get('profile_sha256') != info['recipe_sha256']
            or dataset.get('correctness_only') is not True
            or dataset.get('reference_crosscheck', {}).get('passed') is not True):
        return 'Structured regression identity/reference crosscheck mismatch'
    cases = dataset.get('cases', [])
    if len(cases) != len(CASES) or any(
            any(c.get(k) != expected[k] for k in ('index','shape','dtype','scale_exponent','mask_layout'))
            for c,expected in zip(cases,info['cases'])):
        return 'Structured regression case specification mismatch'
    return None


def _run(argv, timeout=120):
    # Only trusted admin sources/arguments. No submitted code runs here. Files
    # remain coordinator-owned; generator environment has no user/SSH secrets.
    result = subprocess.run([str(a) for a in argv], check=True, capture_output=True,
                            text=True, timeout=timeout,
                            env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','OMP_NUM_THREADS':'1'})
    return result.stdout


def compile_reference(pack_root, target):
    _run(['g++','-std=c++17','-O3','-fopenmp','-fno-fast-math','-ffp-contract=off',
          '-I'+str(pack_root),Path(__file__).with_suffix('.cpp'),'-o',target], 60)
    _run(['g++','-std=c++17','-O3','-fopenmp','-fno-fast-math','-ffp-contract=off',
          pack_root/'cpu_data.cpp','-o',str(target)+'-coordinate'],60)


def generate(binary, prefix, n, d, dtype, exponent, layout):
    return json.loads(_run([binary,'structured',prefix,dtype,n,d,exponent,layout]))


def crosscheck(binary, directory):
    """Before wide generation, compare every output byte to the original FP64 loop.

    27 small cases cover all dtype/scale/mask combinations; small row counts with
    wide d also cover mask counts at the actual column tails and split boundaries.
    """
    directory.mkdir(mode=0o700)
    shapes = [(35,65,t,e,m) for t in range(3) for e in (-10,-4,0) for m in range(3)]
    shapes += [(9,d,t,e,m) for _,d,t,e,m in CASES]
    comparisons = 0
    for i,(n,d,t,e,m) in enumerate(shapes):
        prefix=directory/f'check_{i}'
        generate(binary,prefix,n,d,t,e,m)
        output=Path(str(prefix)+'_y.bin')
        expected=output.read_bytes()
        _run([str(binary)+'-coordinate','reference',prefix,t,n,d,1,1,0,'uniform'])
        if output.read_bytes() != expected:
            raise ValueError(f'Structured reference differs from coordinate FP64 at {n,d,t,e,m}')
        comparisons += n*(n-1)//2
        Path(str(prefix)+'_x.bin').unlink();output.unlink()
    directory.rmdir()
    return dict(passed=True, cases=len(shapes), output_elements=comparisons,
                comparison='all_output_bytes_after_original_dtype_rounding')


def sha256(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def prepare(adapter, spec, suite, job):
    validate_suite(spec,suite)
    binary=job/'structured-reference'
    compile_reference(adapter.ROOT,binary)
    checked=crosscheck(binary,job/'structured-crosscheck')
    view=job/'structured-data';view.mkdir(mode=0o755)
    records=[];total=0
    for i,(n,d,t,e,m) in enumerate(CASES,1):
        width=4 if t==0 else 2; out=n*(n-1)//2
        total += (n*d+out)*width
        if max(n*d*width,out*width)>256*1024**2 or total>2*1024**3:
            raise ValueError('Structured suite exceeds memory budget')
        label=f'structured_{i:02d}';prefix=view/label
        counts=generate(binary,prefix,n,d,t,e,m)
        paths={s:view/(label+'_'+s+'.bin') for s in ('x','y')}
        if paths['x'].stat().st_size != n*d*width or paths['y'].stat().st_size != out*width:
            raise ValueError('Structured binary size mismatch')
        dtype=KINDS[t]
        line='\t'.join([label,str(t),'0',f'{n},{d}',str(spec['checker']['rtol'][dtype]),
                        str(spec['checker']['atol'][dtype]),'0','1','1'])
        (view/f'case-{i:02d}.tsv').write_text(line+'\n')
        records.append(dict(index=i,label=label,name=label,shape=[n,d],dtype=t,
                            output_elements=out,tsv=line,scale_exponent=e,mask_layout=m,
                            **counts,sha256={s:sha256(p) for s,p in paths.items()},
                            specification_origin='structured_regression',official_case_index=None))
    manifest=dict(problem_id='pdist',dataset_version=SUITE,profile_sha256=recipe_sha256(),
                  reference_crosscheck=checked,records=records)
    (view/'all.tsv').write_text(''.join(r['tsv']+'\n' for r in records))
    (view/'manifest.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
    return view,manifest


def evaluate(adapter, spec, suite, job, work, generator, env, run, emit, identity, started, source_sha256):
    validate_suite(spec,suite,generator)
    emit('status',status='generating',message='生成宽矩阵结构化正确性回归；先逐项核对 FP64 参考')
    view,manifest=prepare(adapter,spec,suite,job)

    def correctness_run(argv,cwd,timeout,run_env):
        if argv[0]=='msprof':
            return 125,dict(stdout='',stderr='Correctness-only regression; profiling disabled'),'profile_skipped'
        if Path(argv[0]).name=='pdist_bench':
            argv=list(argv)
            # Original harness: two prevalidation poisons plus one further checked
            # invocation. profile mode itself has no events/timers or msprof.
            for key,value in (('--mode','profile'),('--warmups','0'),('--repeats','1'),('--rounds','1')):
                argv[argv.index(key)+1]=value
        return run(argv,cwd,timeout,run_env)

    def publish(kind,**fields):
        if kind=='status' and fields.get('message','').startswith('采集性能指标'):
            return
        if kind=='dataset':
            dataset=fields['dataset']
            dataset.update(mode='structured_regression',regression_suite=SUITE,correctness_only=True,
                           reference_crosscheck=manifest['reference_crosscheck'])
            for case,record in zip(dataset['cases'],manifest['records']):
                case.update({k:record[k] for k in ('scale_exponent','mask_layout','k1','k2','sha256')})
        elif kind=='case':
            case=fields['case']
            case.update(time_us=None,min_us=None,max_us=None,correctness_only=True)
        elif kind=='metrics':
            fields['metrics']=dict(group=fields['metrics']['group'],status='skipped',
                                   reason='Structured correctness regression; no performance measurements')
        elif kind=='finished':
            fields['message']=fields.get('message','')+' · '+SUITE+'；仅正确性，不计排名、校准或官方上传门禁'
        emit(kind,**fields)

    factory=adapter.fixed_data_dir
    try:
        adapter.fixed_data_dir=lambda:view
        return adapter.evaluate(job,work,None,env,correctness_run,publish,identity,started,source_sha256)
    finally:
        adapter.fixed_data_dir=factory
