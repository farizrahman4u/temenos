"""Phase 1 — Policy tests. Pure Python; run anywhere."""
from __future__ import annotations

import pytest

from temenos import Policy, PolicyViolation, TrustLevel


# -- construction / defaults ----------------------------------------------------------

def test_default_policy_is_locked_down():
    p = Policy()
    assert p.read == () and p.write == () and p.network is False
    assert p.trust is TrustLevel.UNTRUSTED
    assert p.max_memory_mb == 256

def test_lists_are_coerced_to_tuples_and_deduped():
    p = Policy(read=["/a", "/b", "/a"])
    assert p.read == ("/a", "/b")
    assert isinstance(p.read, tuple)

def test_frozen_and_hashable():
    p = Policy(read=["/a"])
    with pytest.raises(Exception):
        p.read = ("/b",)            # type: ignore[misc]
    assert hash(p) == hash(Policy(read=["/a"]))
    assert p == Policy(read=["/a"])

def test_string_for_set_field_is_rejected():
    with pytest.raises(TypeError):
        Policy(read="/a")           # a bare string is almost never intended

def test_negative_limit_rejected():
    with pytest.raises(ValueError):
        Policy(max_memory_mb=-1)

@pytest.mark.parametrize("value,expected", [
    ("untrusted", TrustLevel.UNTRUSTED),
    ("HOST", TrustLevel.HOST),
    (2, TrustLevel.SANDBOXED),
    (TrustLevel.RESTRICTED, TrustLevel.RESTRICTED),
])
def test_trust_coercion(value, expected):
    assert Policy(trust=value).trust is expected

def test_bad_trust_rejected():
    with pytest.raises(ValueError):
        Policy(trust="nonsense")
    with pytest.raises(ValueError):
        Policy(trust=True)          # bool must not sneak through as int


# -- restrict() -----------------------------------------------------------------------

def test_restrict_narrows():
    parent = Policy(read=["/a", "/b"], network=True, max_memory_mb=512,
                    trust=TrustLevel.SANDBOXED)
    child = parent.restrict(read=["/a"], network=False, max_memory_mb=128,
                            trust=TrustLevel.UNTRUSTED)
    assert child.read == ("/a",)
    assert child.network is False
    assert child.max_memory_mb == 128
    assert child.trust is TrustLevel.UNTRUSTED

def test_restrict_inherits_unpassed_fields():
    parent = Policy(read=["/a"], write=["/w"], max_cpu_seconds=10)
    child = parent.restrict(read=["/a"])
    assert child.write == ("/w",)
    assert child.max_cpu_seconds == 10

def test_restrict_noargs_returns_equal_policy():
    parent = Policy(read=["/a"], max_memory_mb=300)
    assert parent.restrict() == parent

@pytest.mark.parametrize("kwargs", [
    {"read": ["/a", "/c"]},               # add a path not in parent
    {"network": True},                    # enable network (parent has none)
    {"max_memory_mb": 1024},              # raise a limit
    {"max_processes": 999},
    {"trust": TrustLevel.HOST},           # raise trust
])
def test_restrict_widening_raises(kwargs):
    parent = Policy(read=["/a"], network=False, max_memory_mb=512,
                    max_processes=16, trust=TrustLevel.SANDBOXED)
    with pytest.raises(PolicyViolation):
        parent.restrict(**kwargs)


def test_restrict_can_disable_network_not_enable():
    assert Policy(network=True).restrict(network=False).network is False
    with pytest.raises(PolicyViolation):
        Policy(network=False).restrict(network=True)


def test_network_toggle_defaults_off_and_coerces():
    assert Policy().network is False
    assert Policy(network=True).network is True
    assert Policy(network="host").network is True
    assert Policy(network="none").network is False
    with pytest.raises(ValueError):
        Policy(network="evil.com")        # old allowlist form is gone in v1
    with pytest.raises(ValueError):
        Policy(network=["a", "b"])         # a list is not a toggle

def test_restrict_unknown_field_raises_typeerror():
    with pytest.raises(TypeError):
        Policy().restrict(memory_mb=10)   # typo'd field name

def test_no_escalate_method():
    assert not hasattr(Policy(), "escalate")


# -- from_dict / to_dict round trip ---------------------------------------------------

def test_round_trip():
    p = Policy(read=["/p"], write=["/w"], network=True,
               max_memory_mb=384, trust=TrustLevel.RESTRICTED)
    assert Policy.from_dict(p.to_dict()) == p

def test_to_dict_serializes_trust_as_name():
    assert Policy(trust=TrustLevel.HOST).to_dict()["trust"] == "HOST"

def test_from_dict_accepts_trust_name_or_int():
    assert Policy.from_dict({"trust": "sandboxed"}).trust is TrustLevel.SANDBOXED
    assert Policy.from_dict({"trust": 1}).trust is TrustLevel.RESTRICTED

def test_from_dict_rejects_unknown_key():
    with pytest.raises(ValueError):
        Policy.from_dict({"reads": ["/a"]})


# -- semantic checks ------------------------------------------------------------------

def test_allows_path_read_and_write():
    p = Policy(read=["/project"], write=["/project/out"])
    assert p.allows_path_read("/project/src/main.py")
    assert p.allows_path_read("/project")              # exact base
    assert p.allows_path_read("/project/out/x")        # writable implies readable
    assert not p.allows_path_read("/etc/passwd")
    assert p.allows_path_write("/project/out/x")
    assert not p.allows_path_write("/project/src/main.py")

def test_path_prefix_is_not_fooled_by_sibling():
    p = Policy(read=["/foo"])
    assert p.allows_path_read("/foo/bar")
    assert not p.allows_path_read("/foobar")           # /foobar is not under /foo

def test_root_read_allows_everything():
    assert Policy(read=["/"]).allows_path_read("/etc/hosts")

def test_network_round_trips_as_bool():
    assert Policy(network=True).to_dict()["network"] is True
    assert Policy.from_dict({"network": "host"}).network is True
