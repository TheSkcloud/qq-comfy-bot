"""Single effective capability resolver; legacy IMAGE_MODE=off means MANUAL."""
from dataclasses import dataclass
import hashlib,json,os
MODES={'OFF','MANUAL','ASSISTED','AUTO'}

@dataclass(frozen=True)
class Capability:
    capability_id: str
    owner: str
    effective_mode: str
    config_revision: str
    reason: str

    def permits(self, origin, confirmed=False):
        if self.effective_mode=='OFF':return False
        if origin=='user_explicit':return True
        return self.effective_mode=='AUTO' or (self.effective_mode=='ASSISTED' and confirmed)


def resolve(settings, capability_id='image', legacy_mode=None):
    legacy=(legacy_mode if legacy_mode is not None else os.getenv('IMAGE_MODE','off')).strip().lower()
    config=settings.get('capabilities',{})
    configured=config.get(capability_id) if isinstance(config,dict) else 'OFF'
    if configured is not None and (not isinstance(configured,str) or configured not in MODES):
        configured='OFF'  # Invalid persisted configuration must fail closed.
    if capability_id=='image':
        mode=configured or ('AUTO' if legacy=='auto' and settings.get('spontaneous_images',True) else 'MANUAL')
        reason='configured_mode' if configured else 'global_mode_off' if legacy!='auto' else 'spontaneous_preference_off' if not settings.get('spontaneous_images',True) else 'active'
    elif capability_id=='proactive_image':
        parent=resolve(settings,'image',legacy)
        mode=parent.effective_mode if settings.get('proactive_enabled',False) else 'OFF'
        if configured:mode=configured if parent.effective_mode=='AUTO' and settings.get('proactive_enabled',False) else 'OFF'
        reason='parent_and_preference'
    else:
        default='AUTO' if capability_id=='reaction' and settings.get('reactions',True) else 'OFF'
        mode=configured or default
        reason='configured_mode' if configured else 'default'
    revision=hashlib.sha256(json.dumps([settings,legacy],sort_keys=True).encode()).hexdigest()[:16]
    return Capability(capability_id,capability_id,mode,revision,reason)
