"""Ingest contracts + registry (#35). The pipeline itself imports the redis/DB
stack (CI-only); the channel abstraction and registry are pure."""
from beacon_core.ingest.base import (BaseInboundChannel, InboundMessage,
                                     IngestResult)
from beacon_core.ingest.registry import register_channel, get_channel


def test_inbound_message_defaults():
    m = InboundMessage(source_id=7, kind="telegram", text="BUY XAUUSD ...")
    assert m.is_live is True and m.persist_message is False
    assert m.parsed is None and m.from_freetext is False
    # structured path can pre-fill parsed + skip AI
    m2 = InboundMessage(source_id=1, kind="api", parsed=object(), from_freetext=False)
    assert m2.parsed is not None


def test_ingest_result_shape():
    r = IngestResult(signal_id=5, status="parsed", accepted=True, reason=None, published=True)
    assert (r.signal_id, r.status, r.accepted, r.published) == (5, "parsed", True, True)


def test_registry_builds_by_kind():
    class _Fake(BaseInboundChannel):
        kind = "fake"
        def __init__(self, config=None):
            self.config = config
        async def run(self):
            return None

    register_channel("fake", _Fake)
    ch = get_channel("fake")
    assert isinstance(ch, _Fake) and ch.kind == "fake"
    ch2 = get_channel("fake", {"x": 1})
    assert ch2.config == {"x": 1}


def test_registry_unknown_kind_raises():
    try:
        get_channel("nope-not-registered")
    except KeyError as e:
        assert "nope-not-registered" in str(e)
    else:
        raise AssertionError("expected KeyError for unknown kind")


def test_base_channel_is_abstract():
    try:
        BaseInboundChannel()
    except TypeError:
        pass
    else:
        raise AssertionError("BaseInboundChannel should not be instantiable")


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            f(); print("ok ", n)
    print("ALL PASS")
