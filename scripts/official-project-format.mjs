import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
export const sha256 = value => createHash('sha256').update(value).digest('hex');
const config=JSON.parse(readFileSync(new URL('../config/official_submit.json',import.meta.url),'utf8'));
const projects=new Map(Object.entries(config.project_templates||{}).map(([id,file])=>{
  if(!/^[a-z][a-z0-9_]*\.json$/.test(file))throw new Error('Invalid trusted template path');
  const template=JSON.parse(readFileSync(new URL('../official_templates/'+file,import.meta.url),'utf8'));
  const url=(config.contest_overrides?.[id]||config.contest_url)+'/'+config.problems[id];
  if(template.problem_id!==id||template.official_url!==url)throw new Error('Template target mismatch');
  return [id,template];
}));
export const EIGH_EDITABLE_FILES=projects.get('eigh')?.editable_files||[];
export function editableProjectFiles(problemId,officialId){
  const template=projects.get(problemId);
  return template?.official_problem_id===officialId?template.editable_files:[];
}
export const normalizeContent = text => text.replace(/^\uFEFF/,'').replaceAll('\r\n','\n').replaceAll('\r','\n');
export const validProblemUrl = url => Object.entries(config.problems).some(([id,slug])=>url===(config.contest_overrides?.[id]||config.contest_url)+'/'+slug);
export function bundleHash(files) {
  const rows=files.map(f=>[f.path,sha256(normalizeContent(f.content))]).sort((a,b)=>a[0]<b[0]?-1:a[0]>b[0]?1:0);
  return sha256(JSON.stringify(rows));
}
export function validateProject(files,template) {
  const required=template.filter(f=>f.editable!==false).map(f=>f.path).sort();
  if(!required.length||!Array.isArray(files)||files.length!==required.length||new Set(files.map(f=>f.path)).size!==files.length||
     JSON.stringify(files.map(f=>f.path).sort())!==JSON.stringify(required))throw new Error('工程必须包含官方模板全部可编辑文件，不得增删或修改只读文件');
  if(files.some(f=>Object.keys(f).sort().join(',')!=='content,path'||typeof f.content!=='string'||!f.content.trim()||f.content.includes('\0')))throw new Error('Invalid project content');
  return files.map(f=>({path:f.path,content:normalizeContent(f.content)})).sort((a,b)=>a.path<b.path?-1:a.path>b.path?1:0);
}
