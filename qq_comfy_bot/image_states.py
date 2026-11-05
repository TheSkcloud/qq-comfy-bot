"""One shared vocabulary for persisted image states and progress facts."""
ACTIVE={'resuming', 'recovering', 'degraded_notice', 'ready_to_send', 'sending', 'reconciling', 'quality_checked', 'gpu_cancel_requested', 'validating', 'generating', 'queued', 'downloading', 'accepted', 'received', 'submitted', 'generated', 'planning'}
TERMINAL={'duplicate_suppressed', 'cancelled', 'outcome_unknown', 'sent', 'expired', 'failed', 'rejected', 'qq_result_unknown', 'recovered_output', 'completed', 'withheld_quality', 'timeout'}
MESSAGES={'accepted': '已经接下这张，正在排队。',
 'cancelled': '这张已经取消，没有继续生成。',
 'completed': '这张已经发出了。',
 'degraded_notice': '基础检查已完成，正在准备发送。',
 'downloading': '画好了，正在下载原图。',
 'duplicate_suppressed': '这张被判定重复，未再次发送。',
 'expired': '这张等得太久，任务已经停止。',
 'failed': '这张任务失败了，已经停下。',
 'generated': '画好了，等待图片检查。',
 'generating': '正在画，还没完成。',
 'gpu_cancel_requested': '已请求停止自己的待执行项，运行中结果尚未确认。',
 'planning': '还在准备画面，没开始出图。',
 'quality_checked': '画好了，正在完成图片检查。',
 'queued': '已经排队，还没开始画。',
 'ready_to_send': '图片已准备好，正在发送。',
 'received': '还在准备画面，没开始出图。',
 'reconciling': '正在核对重启前的原任务，尚未确认结果，不会重复提交。',
 'recovered_output': '重启后已找到这张的生成结果，保存在本机；发送 /取图 可以检查并领取原图。',
 'recovering': '正在恢复原任务查询。',
 'rejected': '这张没有进入绘图队列。',
 'resuming': '正在核对原任务结果。',
 'sending': '图片正在发送，还没确认送达。',
 'sent': '这张已经发出了。',
 'submitted': '正在画，还没完成。',
 'timeout': '这张超时了，已经停下。',
 'validating': '画好了，正在检查。',
 'withheld_quality': '这张没有通过图片检查，已经停下，没有发送。'}
ALIASES={'submitted':'generating','quality_checked':'validating','withheld_quality':'validation_failed','completed':'sent','timeout':'failed','duplicate_suppressed':'rejected','qq_result_unknown':'outcome_unknown','degraded_notice':'ready_to_send','gpu_cancel_requested':'cancelling'}
def lifecycle(status):
    return ALIASES.get(status,status) if isinstance(status,str) and status in ACTIVE|TERMINAL else 'unknown'

for _legacy,_canonical in ALIASES.items():
    if _legacy in MESSAGES:MESSAGES.setdefault(_canonical,MESSAGES[_legacy])
    if _legacy in ACTIVE:ACTIVE.add(_canonical)
    if _legacy in TERMINAL:TERMINAL.add(_canonical)
MESSAGES.setdefault('outcome_unknown','这张的提交或发送结果还没确认，先不重复提交，避免重复。')
MESSAGES.setdefault('qq_result_unknown',MESSAGES['outcome_unknown'])
