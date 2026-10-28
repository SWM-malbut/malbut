from pathlib import Path
import hashlib,json,shutil,tarfile,difflib
root=Path('/home/shin/ros2_ws/src/malbut');work=Path(__file__).resolve().parent
before=work/'before';paths=[];diff=[]
for pkg in ['malbut_stt','malbut_agent_server']:
 for p in (root/pkg).rglob('*'):
  if not p.is_file() or any(part in ('__pycache__','.pytest_cache') for part in p.parts):continue
  if p.suffix not in ('.py','.md','.yaml','.json','.txt'):continue
  rel=p.relative_to(root);b=before/rel
  if not b.exists() or p.read_bytes()!=b.read_bytes():
   paths.append(str(rel));dest=root/'malbut_test'/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest)
   diff.extend(difflib.unified_diff(b.read_text().splitlines(True) if b.exists() else [],p.read_text().splitlines(True),fromfile='before/'+str(rel),tofile='after/'+str(rel)))
(work/'changes.json').write_text(json.dumps(paths,indent=2));(work/'changes.patch').write_text(''.join(diff))
for pkg in ['malbut_stt','malbut_agent_server']:
 shutil.copytree(root/pkg/pkg,work/'release/code'/pkg,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
with tarfile.open(work/'release_code.tar.gz','w:gz') as a:a.add(work/'release/code',arcname='code',filter=lambda t:None if '__pycache__' in t.name or t.name.endswith('.pyc') else t)
print(len(paths),'owned changes; mirrors and candidate code synchronized')
