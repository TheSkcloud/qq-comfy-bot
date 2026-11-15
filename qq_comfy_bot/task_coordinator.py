"""Bounded independent execution lanes. No network operations or domain mutations."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from collections import deque
import time
from image_director import PrioritySlots

class LaneBusy(RuntimeError):pass

class TaskCoordinator:
    def __init__(self):
        self.gpu=PrioritySlots()
        self.limits={'control':4,'text':4,'background':2}
        self.semaphores={name:asyncio.Semaphore(n) for name,n in self.limits.items()}
        self.pending={name:0 for name in self.limits}
        self.active={name:0 for name in (*self.limits,'state')}
        self.events=deque(maxlen=200)

    def record(self,lane,status):
        self.events.append({'lane':lane,'status':status,'at':time.monotonic()})

    async def run(self,lane,factory):
        if self.pending[lane]>=64:raise LaneBusy(lane+'_QUEUE_FULL')
        self.pending[lane]+=1
        self.record(lane,'queued')
        acquired=False
        try:
            await asyncio.wait_for(self.semaphores[lane].acquire(),120)
            acquired=True;self.pending[lane]-=1;self.active[lane]+=1
            self.record(lane,'running')
            result=await factory()
            self.record(lane,'completed');return result
        except BaseException:
            self.record(lane,'interrupted');raise
        finally:
            if acquired:
                self.active[lane]-=1;self.semaphores[lane].release()
            else:self.pending[lane]-=1

    @contextmanager
    def state(self):
        # Session writes are synchronous: no await can interleave a transaction.
        if self.active['state']:raise RuntimeError('REENTRANT_STATE_COMMIT')
        self.active['state']=1
        try:yield
        finally:self.active['state']=0

    def diagnostics(self):
        return {'active':{**self.active,'gpu':int(self.gpu.active)},
                'waiting':{**self.pending,'gpu':len(self.gpu.waiters)},'events':list(self.events)}

def lane(name):
    """Route every invocation, including proactive timer calls, through its lane."""
    from functools import wraps
    def decorate(method):
        @wraps(method)
        async def routed(self,*args,**kwargs):
            return await self.coordinator.run(name,lambda:method(self,*args,**kwargs))
        return routed
    return decorate
