"""Versioned SQLite sessions with three-way, field-level stale-write merging."""
import copy
import json
import sqlite3
from collections import Counter

MISSING=object()
LIMITS={'history':20,'received_inputs':40,'turn_events':80,'assistant_positions':12,
        'candidate_attempts':100,'candidate_plans':100,'drawing_attempts':50,
        'delivery_receipts':50,'scene_changes':20,'image_requests':30,'drawing_sends':100,
        'sent_image_hashes':30,'image_events':10,'shot_history':12,'shot_attempts':4,'recent_visuals':5}

class SessionConflict(RuntimeError):pass

class Session(dict):
    def __init__(self,value,revision):
        super().__init__(copy.deepcopy(value))
        self.baseline=copy.deepcopy(value);self.revision=revision

def merge(base,proposed,current,path=()):
    if proposed==base:return copy.deepcopy(current)
    if current==base or current==proposed:return copy.deepcopy(proposed)
    if all(isinstance(value,dict) for value in (base,proposed,current)):
        result=copy.deepcopy(current)
        for key in base.keys()|proposed.keys():
            old=base.get(key,MISSING);new=proposed.get(key,MISSING);live=current.get(key,MISSING)
            if new==old:continue
            if new is MISSING:
                if live==old:result.pop(key,None)
                elif live is not MISSING:raise SessionConflict('Concurrent delete: '+'.'.join(path+(str(key),)))
            elif old is MISSING:
                if live is MISSING or live==new:result[key]=copy.deepcopy(new)
                elif isinstance(new,list) and isinstance(live,list):result[key]=merge([],new,live,path+(str(key),))
                elif isinstance(new,dict) and isinstance(live,dict):result[key]=merge({},new,live,path+(str(key),))
                else:raise SessionConflict('Concurrent addition: '+'.'.join(path+(str(key),)))
            elif live is MISSING:raise SessionConflict('Concurrent removal: '+'.'.join(path+(str(key),)))
            else:result[key]=merge(old,new,live,path+(str(key),))
        return result
    if all(isinstance(value,list) for value in (base,proposed,current)):
        # Lists with stable IDs support in-place segment acknowledgement updates.
        key=next((key for key in ('id','index') if base and all(isinstance(v,dict) and key in v for seq in (base,proposed,current) for v in seq)),None)
        if key:
            old={str(v[key]):v for v in base};new={str(v[key]):v for v in proposed};live={str(v[key]):v for v in current}
            values=merge(old,new,live,path)
            order=list(dict.fromkeys([str(v[key]) for v in current]+[str(v[key]) for v in proposed]))
            result=[values[k] for k in order if k in values]
            limit=LIMITS.get(path[-1] if path else '')
            return result[-limit:] if limit else result
        encode=lambda item:json.dumps(item,ensure_ascii=False,sort_keys=True)
        before=Counter(map(encode,base));after=Counter(map(encode,proposed))
        remove=before-after;add=after-before;result=[]
        for item in current:
            code=encode(item)
            if remove[code]:remove[code]-=1
            else:result.append(copy.deepcopy(item))
        for item in proposed:
            code=encode(item)
            if add[code]:result.append(copy.deepcopy(item));add[code]-=1
        limit=LIMITS.get(path[-1] if path else '')
        return result[-limit:] if limit else result
    raise SessionConflict('Concurrent field update: '+'.'.join(path))

class SessionStore:
    def __init__(self,path,default):
        self.default=default;self.db=sqlite3.connect(path,timeout=10)
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, value TEXT, revision INTEGER NOT NULL DEFAULT 0)')
        if 'revision' not in {row[1] for row in self.db.execute('PRAGMA table_info(sessions)')}:
            self.db.execute('ALTER TABLE sessions ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
        self.db.commit()

    def get(self,key):
        row=self.db.execute('SELECT value,revision FROM sessions WHERE key=?',(key,)).fetchone()
        return Session(json.loads(row[0]) if row else copy.deepcopy(self.default),row[1] if row else 0)

    def save(self,key,value):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.db.execute('SELECT value,revision FROM sessions WHERE key=?',(key,)).fetchone()
            current=json.loads(row[0]) if row else copy.deepcopy(self.default);revision=row[1] if row else 0
            updated=merge(value.baseline,dict(value),current) if isinstance(value,Session) else copy.deepcopy(value)
            self.db.execute('INSERT INTO sessions(key,value,revision) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,revision=excluded.revision',
                            (key,json.dumps(updated,ensure_ascii=False),revision+1))
            self.db.commit()
        except BaseException:self.db.rollback();raise
        if isinstance(value,Session):
            value.clear();value.update(copy.deepcopy(updated));value.baseline=copy.deepcopy(updated);value.revision=revision+1
