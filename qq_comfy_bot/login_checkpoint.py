"""Small local login-lifecycle receipts. Never record tickets, QR URLs or passwords."""
import hashlib
import json
from pathlib import Path
from datetime import datetime,timezone
import psutil


def record(root,config,event,state,**fields):
    try:
        base=Path(config['qq_data_dir']) if config.get('qq_data_dir') else None
        entry={'at':datetime.now(timezone.utc).isoformat(),'boot':psutil.boot_time(),
               'event':event,'state':state,'pinned_data_directory':str(base) if base else None}
        entry.update({k:v for k,v in fields.items() if k in ('previous_state','login_phase','is_login','qr_accepted','failure_category','recovery_action','error_type','worker_exit_code')})
        if base:
            global_path=base/'nt_qq'/'global'
            registry=global_path/'nt_data'/'msf'/'Registry20'
            entry['device_registry_present']=registry.is_file()
            if registry.is_file():entry['device_registry_sha256']=hashlib.sha256(registry.read_bytes()).hexdigest()
            entry['login_files']={p.name:{'bytes':p.stat().st_size,'mtime':p.stat().st_mtime}
                                  for p in (global_path/'nt_db').glob('login.db*')}
        folder=Path(root)/'data';folder.mkdir(exist_ok=True)
        with (folder/'login_lifecycle.jsonl').open('a',encoding='utf-8') as archive:
            archive.write(json.dumps(entry,ensure_ascii=False)+'\n')
    except (OSError,ValueError):
        pass
