@echo off
cd /d "%~dp0"
"runtime\python.exe" -c "import json, re, pathlib; p = pathlib.Path('update.json'); d = json.loads(p.read_text('utf-8')); nums = [int(x) for x in re.findall(r'\d+', d.get('version', '1.0.0'))] or [1, 0, 0]; nums[-1] += 1; d['version'] = '.'.join(str(x) for x in nums); p.write_text(json.dumps(d, indent=2) + '\n', 'utf-8'); print('Bumped version to v' + d['version'])"
git add -A
git commit -m "Update Trading Bot"
git push -u origin main
pause
