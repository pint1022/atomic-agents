"""Lightweight RoCE device C-model.

This module implements a minimal behavioral simulation of a RoCE device that
supports queue pair management, memory registration, and a basic send/receive
pipeline. The goal is to provide a small, dependency-free model that can be
extended to match implementation details from vendor specifications.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, Optional, Tuple


class QueuePairState(str, Enum):
    """Operational states for a queue pair."""

    RESET = "reset"
    INIT = "init"
    RTR = "ready_to_receive"
    RTS = "ready_to_send"
    ERROR = "error"


class OpCode(str, Enum):
    """Supported operation codes."""

    SEND = "send"
    RECV = "recv"
    WRITE = "write"
    READ = "read"


class CompletionStatus(str, Enum):
    """Completion status codes."""

    SUCCESS = "success"
    NO_PEER = "no_peer"
    NO_RECV = "no_recv"
    INVALID_MR = "invalid_mr"
    CQ_OVERFLOW = "cq_overflow"


@dataclass
class MemoryRegion:
    """Registered memory buffer."""

    key: int
    length: int
    buffer: bytearray = field(init=False)

    def __post_init__(self) -> None:
        self.buffer = bytearray(self.length)

    def write(self, offset: int, data: bytes) -> None:
        end = offset + len(data)
        if end > self.length:
            raise ValueError("write exceeds memory region length")
        self.buffer[offset:end] = data

    def read(self, offset: int, length: int) -> bytes:
        end = offset + length
        if end > self.length:
            raise ValueError("read exceeds memory region length")
        return bytes(self.buffer[offset:end])


@dataclass
class WorkRequest:
    """Queued work request."""

    wr_id: int
    opcode: OpCode
    length: int
    mr: Optional[int] = None
    offset: int = 0
    payload: bytes | None = None
    immediate: int | None = None


@dataclass
class RoceCompletion:
    """Completion queue entry."""

    wr_id: int
    opcode: OpCode
    status: CompletionStatus
    length: int
    immediate: int | None = None
    payload: bytes | None = None


class CompletionQueue:
    """Simple bounded completion queue."""

    def __init__(self, depth: int = 256) -> None:
        self.depth = depth
        self.entries: Deque[RoceCompletion] = deque()

    def push(self, entry: RoceCompletion) -> None:
        if len(self.entries) >= self.depth:
            raise OverflowError("completion queue overflow")
        self.entries.append(entry)

    def poll(self) -> Optional[RoceCompletion]:
        if self.entries:
            return self.entries.popleft()
        return None

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self.entries)


class QueuePair:
    """Queue pair with send/receive queues and completion queue."""

    def __init__(self, qp_num: int, cq_depth: int = 256) -> None:
        self.qp_num = qp_num
        self.state = QueuePairState.RTS
        self.peer: Optional[Tuple["RoceDevice", int]] = None
        self.send_queue: Deque[WorkRequest] = deque()
        self.recv_queue: Deque[WorkRequest] = deque()
        self.cq = CompletionQueue(depth=cq_depth)

    def post_send(self, wr: WorkRequest) -> None:
        self.send_queue.append(wr)

    def post_recv(self, wr: WorkRequest) -> None:
        if wr.opcode != OpCode.RECV:
            raise ValueError("Receive queue accepts only RECV operations")
        self.recv_queue.append(wr)


class RoceDevice:
    """Behavioral RoCE device model supporting loopback connections."""

    def __init__(
        self,
        max_qp: int = 128,
        cq_depth: int = 256,
        mtu: int = 1024,
    ) -> None:
        self.max_qp = max_qp
        self.cq_depth = cq_depth
        self.mtu = mtu
        self.qps: Dict[int, QueuePair] = {}
        self.mrs: Dict[int, MemoryRegion] = {}
        self._next_qp = 1
        self._next_mr = 1

    def register_memory(self, length: int) -> MemoryRegion:
        key = self._next_mr
        self._next_mr += 1
        mr = MemoryRegion(key=key, length=length)
        self.mrs[key] = mr
        return mr

    def create_qp(self) -> QueuePair:
        if len(self.qps) >= self.max_qp:
            raise RuntimeError("maximum number of queue pairs reached")
        qp_num = self._next_qp
        self._next_qp += 1
        qp = QueuePair(qp_num=qp_num, cq_depth=self.cq_depth)
        self.qps[qp_num] = qp
        return qp

    def connect(self, local_qp: int, remote: "RoceDevice", remote_qp: int) -> None:
        local = self.qps[local_qp]
        peer = remote.qps[remote_qp]
        local.peer = (remote, remote_qp)
        peer.peer = (self, local_qp)

    def post_send(self, qp_num: int, wr: WorkRequest) -> None:
        self._assert_qp_ready(qp_num)
        self.qps[qp_num].post_send(wr)

    def post_recv(self, qp_num: int, wr: WorkRequest) -> None:
        self._assert_qp_ready(qp_num)
        self.qps[qp_num].post_recv(wr)

    def progress(self) -> None:
        """Drive the simulation forward for all queue pairs."""

        for qp_num, qp in list(self.qps.items()):
            if qp.state != QueuePairState.RTS or not qp.send_queue:
                continue

            peer = qp.peer
            if not peer:
                self._complete_local(qp, qp.send_queue.popleft(), CompletionStatus.NO_PEER)
                continue

            remote_dev, remote_qp_num = peer
            remote_qp = remote_dev.qps[remote_qp_num]

            wr = qp.send_queue.popleft()
            try:
                if wr.opcode == OpCode.SEND:
                    self._handle_send(qp, remote_dev, remote_qp, wr)
                elif wr.opcode == OpCode.WRITE:
                    self._handle_write(qp, remote_dev, wr)
                elif wr.opcode == OpCode.READ:
                    self._handle_read(qp, remote_dev, wr)
                else:
                    self._complete_local(qp, wr, CompletionStatus.SUCCESS)
            except OverflowError:
                self._complete_local(qp, wr, CompletionStatus.CQ_OVERFLOW)

    def _handle_send(
        self,
        local_qp: QueuePair,
        remote_dev: "RoceDevice",
        remote_qp: QueuePair,
        wr: WorkRequest,
    ) -> None:
        if not remote_qp.recv_queue:
            self._complete_local(local_qp, wr, CompletionStatus.NO_RECV)
            return

        recv_wr = remote_qp.recv_queue.popleft()
        remote_mr = remote_dev.mrs.get(recv_wr.mr or 0)
        if remote_mr is None:
            self._complete_local(local_qp, wr, CompletionStatus.INVALID_MR)
            return

        payload = wr.payload or bytes(wr.length)
        remote_mr.write(recv_wr.offset, payload[: recv_wr.length])

        remote_completion = RoceCompletion(
            wr_id=recv_wr.wr_id,
            opcode=OpCode.RECV,
            status=CompletionStatus.SUCCESS,
            length=min(len(payload), recv_wr.length),
            immediate=wr.immediate,
        )
        remote_qp.cq.push(remote_completion)

        self._complete_local(local_qp, wr, CompletionStatus.SUCCESS)

    def _handle_write(self, local_qp: QueuePair, remote_dev: "RoceDevice", wr: WorkRequest) -> None:
        remote_mr = remote_dev.mrs.get(wr.mr or 0)
        if remote_mr is None:
            self._complete_local(local_qp, wr, CompletionStatus.INVALID_MR)
            return

        payload = wr.payload or bytes(wr.length)
        remote_mr.write(wr.offset, payload[: wr.length])
        self._complete_local(local_qp, wr, CompletionStatus.SUCCESS)

    def _handle_read(self, local_qp: QueuePair, remote_dev: "RoceDevice", wr: WorkRequest) -> None:
        remote_mr = remote_dev.mrs.get(wr.mr or 0)
        if remote_mr is None:
            self._complete_local(local_qp, wr, CompletionStatus.INVALID_MR)
            return

        payload = remote_mr.read(wr.offset, wr.length)
        self._complete_local(local_qp, wr, CompletionStatus.SUCCESS, payload=payload)

    def _complete_local(
        self,
        qp: QueuePair,
        wr: WorkRequest,
        status: CompletionStatus,
        payload: bytes | None = None,
    ) -> None:
        completion = RoceCompletion(
            wr_id=wr.wr_id,
            opcode=wr.opcode,
            status=status,
            length=wr.length,
            immediate=wr.immediate,
            payload=payload,
        )
        qp.cq.push(completion)

    def _assert_qp_ready(self, qp_num: int) -> None:
        qp = self.qps.get(qp_num)
        if qp is None:
            raise KeyError(f"queue pair {qp_num} not found")
        if qp.state not in {QueuePairState.RTS, QueuePairState.RTR}:
            raise RuntimeError(f"queue pair {qp_num} not ready: {qp.state}")
