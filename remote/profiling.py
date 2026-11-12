"""Collect a declared profiler plan exclusively, then export its CSVs on CPU."""
import json
from pathlib import Path
import sys
import time


GROUPS = ('PipeUtilization', 'L2Cache', 'MemoryUB')


class ProfilerPipeline:
    def __init__(self, execution, groups):
        self.execution = execution
        self.groups = tuple(groups)
        self.results = {}
        self.commands = {}
        self.report = dict(enabled=True, status='not_run', groups=list(self.groups),
                           export_mode='database_then_parallel_summary',
                           measurement_changed=False)

    def matches(self, argv):
        e = self.execution
        if self.groups not in (GROUPS, GROUPS[:1]):
            return False
        expected = ['msprof', '--output='+str(e.work/'profile-PipeUtilization'),
                    '--application=bash '+str(e.root/'problems'/e.pack['id']/'profile.sh')+' '+e.job.name+' PipeUtilization',
                    '--task-time=l0', '--ascendcl=off', '--runtime-api=off',
                    '--ai-core=on', '--aic-metrics=PipeUtilization']
        script = Path(e.env.get('ASCEND_HOME_PATH', ''))/'tools/profiler/profiler_tool/analysis/msprof/msprof.py'
        return argv == expected and script.is_file()

    def run(self, argv, cwd, timeout, env):
        key = tuple(argv)
        if key in self.results:
            return self.results[key]
        if self.results or not self.matches(argv):
            raise ValueError('Profiler plan changed after collection began')
        e = self.execution
        plan = []
        self.report['status'] = 'collecting'
        for group in self.groups:
            command = list(argv)
            command[1] = '--output='+str(e.work/('profile-'+group))
            command[2] = argv[2].rsplit(' ', 1)[0]+' '+group
            command[-1] = '--aic-metrics='+group
            self.commands[group] = tuple(command)
            if e.device_lease is not None:
                e.device_lease.emit('status', status='running', message='采集性能指标 · '+group)
            result = self._run('profiler_collect_'+group, command+['--type=db'], cwd, timeout, env, False)
            self.results[tuple(command)] = result
            if result[0] or result[2]:
                continue
            directory = e.work/('profile-'+group)
            profiles = list(directory.glob('PROF_*'))
            if len(profiles) != 1 or not profiles[0].is_dir() or profiles[0].is_symlink():
                self.results[tuple(command)] = (1, result[1], 'Missing or ambiguous profiler database')
                continue
            plan.append(dict(group=group, directory=str(profiles[0])))
        # Every device command has returned and its process group was reaped.
        # All later adapter calls use results from THIS evaluation, not old runs.
        if e.device_lease is not None:
            e.device_lease.close()
            e.device_lease.emit('status', status='running', message='NPU 采集完成，CPU 并行导出性能报表')
        self.report['device_released_before_export'] = bool(e.device_lease and e.device_lease.released_at is not None)
        self.report['status'] = 'exporting'
        self.report['export_started_at'] = time.time()
        if plan:
            path = e.work/'profiler-export-plan.json'
            path.write_text(json.dumps(dict(work=str(e.work), exports=plan)))
            e.own(path)
            code, output, reason = self._run('profiler_summary_export',
                [sys.executable, str(Path(__file__).with_name('profile_export.py')), str(path)],
                cwd, 150, env, True)
            (e.job/'profiler-summary-export.log').write_text(output['stdout']+'\n'+output['stderr'])
            try:
                if code or reason:
                    raise ValueError(f'CPU export failed: exit={code}, reason={reason}')
                exports = json.loads(output['stdout'])['exports']
                if len(exports) != len(plan) or [item['group'] for item in exports] != [item['group'] for item in plan]:
                    raise ValueError('CPU export groups do not match collected groups')
                self.report['export_seconds_by_group'] = {item['group']:item['seconds'] for item in exports}
                for item in exports:
                    command = self.commands[item['group']]
                    _, collected, _ = self.results[command]
                    self.results[command] = (item['code'], dict(stdout=collected['stdout']+'\n'+item['output'],
                                                               stderr=collected['stderr']), item['reason'])
            except (ValueError, KeyError, TypeError) as error:
                for item in plan:
                    command = self.commands[item['group']]
                    self.results[command] = (1, output, str(error)[:1000])
        self.report['export_finished_at'] = time.time()
        self.report['status'] = 'complete' if all(not r[0] and not r[2] for r in self.results.values()) else 'partial'
        return self.results[key]

    def _run(self, stage, argv, cwd, timeout, env, cpu):
        e = self.execution
        start, started_at = time.monotonic(), time.time()
        try:
            return e.runner(argv, cwd, timeout, dict(env, OJ_CPU_PREPARATION='1' if cpu else '0'))
        finally:
            e.stages[stage] = round(time.monotonic()-start, 4)
            e.timeline.append(dict(stage=stage, started_at=started_at, finished_at=time.time()))
