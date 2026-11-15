"""Report the actual intersection of environment policy and user preferences."""
import os
def effective(settings,mode=None):
    mode=(mode if mode is not None else os.getenv('IMAGE_MODE','off')).strip().lower()
    from capability_registry import resolve
    image=resolve(settings,'image',mode)
    proactive=resolve(settings,'proactive_image',mode)
    return {'mode':mode if mode in ('off','auto') else 'off',
        'effective_mode':image.effective_mode,'config_revision':image.config_revision,
        'explicit_requests':image.permits('user_explicit'),
        'spontaneous_enabled':image.permits('character_auto'),
        'proactive_enabled':proactive.permits('character_auto'),
        'reason':image.reason}

def description(settings):
    value=effective(settings)
    if value['reason']=='global_mode_off':return '偏好已保存；当前全局自动绘图关闭，随手图和主动分享暂不生效，仍可明确要图。'
    return '当前实际生效：聊天随手图'+('开启' if value['spontaneous_enabled'] else '关闭')+'，空闲主动分享'+('开启' if value['proactive_enabled'] else '关闭')+'。'
