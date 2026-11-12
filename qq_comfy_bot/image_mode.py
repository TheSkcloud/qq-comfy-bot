"""Report the actual intersection of environment policy and user preferences."""
import os
def effective(settings,mode=None):
    mode=(mode if mode is not None else os.getenv('IMAGE_MODE','off')).strip().lower()
    automatic=mode=='auto'
    spontaneous=automatic and settings.get('spontaneous_images',True)
    proactive=spontaneous and settings.get('proactive_enabled',False)
    return {'mode':mode if mode in ('off','auto') else 'off','explicit_requests':True,
        'spontaneous_enabled':bool(spontaneous),'proactive_enabled':bool(proactive),
        'reason':'global_mode_off' if not automatic else 'spontaneous_preference_off' if not spontaneous else 'active'}

def description(settings):
    value=effective(settings)
    if value['reason']=='global_mode_off':return '偏好已保存；当前全局自动绘图关闭，随手图和主动分享暂不生效，仍可明确要图。'
    return '当前实际生效：聊天随手图'+('开启' if value['spontaneous_enabled'] else '关闭')+'，空闲主动分享'+('开启' if value['proactive_enabled'] else '关闭')+'。'
