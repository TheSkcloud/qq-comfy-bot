"""Per-conversation revision, cancellation and delivery clocks. No model calls."""
import asyncio
import contextvars
from dataclasses import dataclass, field
import time
import random
import uuid

CURRENT = contextvars.ContextVar('scheduled_reply', default=None)


class Superseded(Exception):
    pass


@dataclass
class Ticket:
    key: str
    version: int
    read_length: int
    started: float = field(default_factory=time.monotonic)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    packets: int = 0
    in_flight: bool = False
    pace: str = "normal"
    collection_ms: float = 0
    message_count: int = 1
    reading_ms: float = 0
    model_ms: float = 0
    typing_ms: float = 0
    reply_type: str = 'casual_chat'
    segment_count: int = 1
    input_batch_id: str = field(default_factory=lambda:uuid.uuid4().hex)



class ReplyScheduler:
    def __init__(self):
        self.current = {}
        self.timing = timing_config()
        self.send_locks = {}

    def begin(self, key, read_length):
        old = self.current.get(key)
        if old:
            old.cancelled.set()
        ticket = Ticket(key, old.version+1 if old else 1, read_length)
        self.current[key] = ticket
        return ticket

    def check(self, ticket):
        if ticket and (ticket.cancelled.is_set() or self.current.get(ticket.key) is not ticket):
            raise Superseded()

    async def run(self, ticket, coroutine):
        task = asyncio.create_task(coroutine)
        stopped = asyncio.create_task(ticket.cancelled.wait())
        try:
            done, _ = await asyncio.wait([task, stopped], return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return await task
            if ticket.in_flight:
                # A packet already handed to QQ cannot be unsent. Finish its acknowledgement
                # and history commit, then the next revision check stops all remaining segments.
                return await task
            raise Superseded()
        finally:
            for pending in (task, stopped):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, stopped, return_exceptions=True)

    async def wait(self, ticket, seconds):
        self.check(ticket)
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(ticket.cancelled.wait(), seconds)
        except asyncio.TimeoutError:
            self.check(ticket)
            return
        raise Superseded()

    def remaining(self, ticket, reply_length, segment=0):
        cfg=self.timing
        fast=ticket.pace=='fast'
        reading=min(cfg['reading_max'],.4+ticket.read_length*.035)
        reading+=2. if ticket.pace=='complex' else .8 if ticket.pace=='emotion' else 0
        if fast:reading=min(reading,.4)
        typing=min(cfg['typing_max'],max(cfg['typing_min'],reply_length/cfg['characters_per_second']))
        if not segment:
            # Model processing already supplied a natural pause; credit a bounded part of it.
            typing=max(.25,typing-min(ticket.model_ms/1000*.6,typing*.8))
            if reply_length<=12:typing=min(typing,.9)
        if fast:typing=min(typing,1.2)
        if segment:
            typing=min(cfg['segment_max'],max(.25,typing*.65))+random.uniform(cfg['segment_gap_min'],cfg['segment_gap_max'])
        reading_wait=0 if segment else max(0,reading-(time.monotonic()-ticket.started))
        typing*=random.uniform(1-cfg['jitter'],1+cfg['jitter'])
        ticket.reading_ms+=reading_wait*1000
        ticket.typing_ms+=typing*1000
        return reading_wait+typing

    def lock(self, key):
        return self.send_locks.setdefault(key, asyncio.Lock())


def timing_config(values=None):
    cfg={'characters_per_second':7.,'typing_min':.65,'typing_max':14.,'reading_max':7.,'jitter':.15,'collection_min':.35,'collection_max':.85,'segment_max':5.,'segment_gap_min':.35,'segment_gap_max':1.1}
    for key,value in (values or {}).items():
        if key in cfg:
            number=float(value)
            low,high={'characters_per_second':(3,30),'typing_min':(.1,2),'typing_max':(2,20),'reading_max':(0,12),'jitter':(0,.3),'collection_min':(.1,1),'collection_max':(.2,1.5),'segment_max':(1,10),'segment_gap_min':(.1,2),'segment_gap_max':(.2,3)}[key]
            cfg[key]=max(low,min(high,number))
    cfg['collection_max']=max(cfg['collection_max'],cfg['collection_min'])
    return cfg
