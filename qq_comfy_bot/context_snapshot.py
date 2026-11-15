"""Immutable per-turn model inputs. Slices return detached mutable copies."""
from dataclasses import dataclass
import hashlib
import json

@dataclass(frozen=True)
class ContextSnapshot:
    snapshot_id: str
    payload: str

    @classmethod
    def capture(cls, key, input_id, session, history, context, discussion, capabilities=None, dialogue_plan=None):
        value = {'conversation_key': key, 'input_id': input_id,
                 'versions': {'session': getattr(session, 'revision', 0),
                              'scene': session.get('scene_version', 0),
                              'conversation': session.get('dialogue_state',{}).get('revision',0)},
                 'state': session['state'], 'history': history,
                 'context': context, 'discussion': discussion,
                 'capabilities': capabilities or {}, 'dialogue_plan': dialogue_plan or {}}
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return cls(hashlib.sha256(payload.encode()).hexdigest()[:24], payload)

    def slice(self, profile='reply'):
        value = json.loads(self.payload)
        fields = ('history', 'context', 'discussion') if profile == 'analysis' else ('capabilities','dialogue_plan','versions') if profile == 'planning' else ('state', 'history', 'context')
        return {key: value[key] for key in fields}
