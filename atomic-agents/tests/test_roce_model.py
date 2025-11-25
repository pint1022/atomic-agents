import pytest

from atomic_agents.roce import (
    CompletionStatus,
    MemoryRegion,
    OpCode,
    RoceDevice,
    WorkRequest,
)


@pytest.fixture
def roce_pair():
    local = RoceDevice()
    remote = RoceDevice()

    l_qp = local.create_qp()
    r_qp = remote.create_qp()

    local.connect(l_qp.qp_num, remote, r_qp.qp_num)
    return local, remote, l_qp, r_qp


def test_memory_region_bounds():
    mr = MemoryRegion(key=1, length=8)
    mr.write(0, b"abcdefg")
    assert mr.read(0, 7) == b"abcdefg"
    with pytest.raises(ValueError):
        mr.write(4, b"toolong")
    with pytest.raises(ValueError):
        mr.read(4, 8)


def test_send_and_receive(roce_pair):
    local, remote, l_qp, r_qp = roce_pair
    recv_mr = remote.register_memory(64)

    remote.post_recv(
        r_qp.qp_num,
        WorkRequest(wr_id=1, opcode=OpCode.RECV, length=16, mr=recv_mr.key),
    )

    payload = b"hello, roce!"
    local.post_send(
        l_qp.qp_num,
        WorkRequest(wr_id=2, opcode=OpCode.SEND, length=len(payload), payload=payload),
    )

    local.progress()

    recv_cqe = r_qp.cq.poll()
    assert recv_cqe is not None
    assert recv_cqe.opcode is OpCode.RECV
    assert recv_cqe.status is CompletionStatus.SUCCESS
    assert recv_mr.read(0, recv_cqe.length).rstrip(b"\x00") == payload

    send_cqe = l_qp.cq.poll()
    assert send_cqe is not None
    assert send_cqe.status is CompletionStatus.SUCCESS


def test_write_and_read(roce_pair):
    local, remote, l_qp, r_qp = roce_pair
    target_mr = remote.register_memory(32)
    remote_data = b"remote data payload"
    target_mr.write(0, remote_data)

    # Write new content
    local.post_send(
        l_qp.qp_num,
        WorkRequest(
            wr_id=10,
            opcode=OpCode.WRITE,
            length=len(b"new"),
            mr=target_mr.key,
            payload=b"new",
        ),
    )

    # Read back
    local.post_send(
        l_qp.qp_num,
        WorkRequest(
            wr_id=11,
            opcode=OpCode.READ,
            length=len(remote_data),
            mr=target_mr.key,
            offset=0,
        ),
    )

    local.progress()

    write_cqe = l_qp.cq.poll()
    assert write_cqe is not None
    assert write_cqe.opcode is OpCode.WRITE
    assert write_cqe.status is CompletionStatus.SUCCESS

    read_cqe = l_qp.cq.poll()
    assert read_cqe is not None
    assert read_cqe.opcode is OpCode.READ
    assert read_cqe.status is CompletionStatus.SUCCESS
    assert read_cqe.payload.startswith(b"new")


def test_missing_receive_produces_error(roce_pair):
    local, remote, l_qp, r_qp = roce_pair
    payload = b"data"

    local.post_send(
        l_qp.qp_num,
        WorkRequest(wr_id=20, opcode=OpCode.SEND, length=len(payload), payload=payload),
    )

    local.progress()

    send_cqe = l_qp.cq.poll()
    assert send_cqe is not None
    assert send_cqe.status is CompletionStatus.NO_RECV


