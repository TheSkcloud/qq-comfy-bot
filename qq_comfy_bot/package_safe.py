"""Create a source-only release. Private runtime state is never traversed."""
import argparse, re, zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent
JSON_FILES={'character_card.json','preferences.json','render_profiles.json','timetable.json','workflow.json','workflow_h_api.json','workflow_h_before_detail_api.json'}

def sources(root=ROOT):
    for path in root.iterdir():
        if path.is_file() and (path.suffix=='.py' or path.name in JSON_FILES or path.name in {'README.md','HANDOFF.md','persona.md','requirements.txt','.env.example','.gitignore','configure.bat','一键启动.bat','start.bat','start_existing_comfy.bat'}):
            yield path
    for folder in ('plugins','web','assets'):
        base=root/folder
        if base.exists():
            for path in base.rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix in ('.py','.ps1','.html','.css','.js','.png','.ico','.svg'):
                    yield path

def package(destination):
    destination=Path(destination).resolve()
    if destination==ROOT or destination.is_relative_to(ROOT/'data'):
        raise ValueError('Unsafe output path')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(sources()):
            content=path.read_bytes()
            if path.name=='README.md':
                content=re.sub(r'(?<![0-9])[0-9]{7,12}(?![0-9])','[本机账号]',content.decode('utf-8')).encode('utf-8')
            archive.writestr('qq_comfy_bot/'+path.relative_to(ROOT).as_posix(),content)
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert not any('/data/' in name or name.endswith(('.env','.log','.db','.bak')) for name in archive.namelist())
    return destination

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('output');print(package(parser.parse_args().output))

