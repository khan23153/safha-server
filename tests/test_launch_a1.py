"""Offline checks of the A1 grabber against a fake Oracle API."""
import types

import oci
import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launch_a1 as grab  # noqa: E402


class Err(oci.exceptions.ServiceError):
    def __init__(self, status, message):
        super().__init__(status, "InternalError", {}, message)


def ns(**kw):
    return types.SimpleNamespace(**kw)


class FakeCompute:
    def __init__(self, existing=None, errors=()):
        self.existing, self.errors, self.launched = existing, list(errors), []
        self.resize_errors, self.resized = [], []

    def list_instances(self, *a, **k):
        return ns(data=[self.existing] if self.existing else [], has_next_page=False, next_page=None)

    def list_images(self, *a, **k):
        return ns(data=[ns(id="img-1")])

    def launch_instance(self, details):
        if self.errors:
            raise self.errors.pop(0)
        self.launched.append(details)
        return ns(data=ns(id="inst-1"))

    def update_instance(self, _id, details):
        if self.resize_errors:
            raise self.resize_errors.pop(0)
        self.resized.append(details)
        return ns(data=ns(id=_id))

    def get_instance(self, _id):
        return ns(data=ns(lifecycle_state="RUNNING"))

    def list_vnic_attachments(self, *a, **k):
        return ns(data=[ns(lifecycle_state="ATTACHED", vnic_id="vnic-1")])


class FakeVnet:
    def __init__(self):
        self.created = []

    def list_vcns(self, *a, **k):
        return ns(data=[ns(id="vcn-1", lifecycle_state="AVAILABLE",
                           default_route_table_id="rt-1", default_security_list_id="sl-1")])

    def list_internet_gateways(self, *a, **k):
        return ns(data=[ns(id="igw-1")])

    def get_route_table(self, _id):
        return ns(data=ns(id="rt-1", route_rules=[ns(network_entity_id="igw-1")]))

    def list_subnets(self, *a, **k):
        return ns(data=[ns(id="sub-1", lifecycle_state="AVAILABLE", prohibit_public_ip_on_vnic=False)])

    def get_vnic(self, _id):
        return ns(data=ns(public_ip="203.0.113.7"))


@pytest.fixture
def setup(monkeypatch, tmp_path):
    out = tmp_path / "out"
    for k, v in {"OCI_USER_OCID": "u", "OCI_TENANCY_OCID": "t", "OCI_FINGERPRINT": "f",
                 "OCI_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nk\n-----END PRIVATE KEY-----\nOCI_API_KEY", "OCI_SSH_PUBLIC_KEY": "ssh-ed25519 AAA", "GITHUB_OUTPUT": str(out)}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(grab.oci.config, "validate_config", lambda cfg: None)
    monkeypatch.setattr(grab.oci, "wait_until", lambda *a, **k: None)
    monkeypatch.setattr(grab.oci.identity, "IdentityClient",
                        lambda cfg: ns(list_availability_domains=lambda c: ns(data=[ns(name="AD-1")])))
    monkeypatch.setattr(grab.oci.core, "VirtualNetworkClient", lambda cfg: FakeVnet())
    monkeypatch.setattr(grab.time, "sleep", lambda s: None)

    def run(compute):
        monkeypatch.setattr(grab.oci.core, "ComputeClient", lambda cfg: compute)
        grab.main()
        result = dict(line.split("=", 1) for line in out.read_text().split())
        result.pop("attempts", None)
        result.pop("created", None)
        return result
    return run


def test_out_of_capacity_is_a_quiet_retry(setup):
    compute = FakeCompute(errors=[Err(500, "Out of host capacity.")] * 2)
    assert setup(compute) == {"status": "no_capacity", "public_ip": ""}
    assert not compute.launched


def test_launch_uses_free_a1_shape_and_reports_ip(setup):
    compute = FakeCompute()
    assert setup(compute) == {"status": "created", "public_ip": "203.0.113.7"}
    d = compute.launched[0]
    assert d.shape == "VM.Standard.A1.Flex"
    assert (d.shape_config.ocpus, d.shape_config.memory_in_gbs) == (2, 12)
    assert d.create_vnic_details.assign_public_ip is True
    assert d.metadata["user_data"]


def test_existing_server_is_not_duplicated(setup):
    compute = FakeCompute(existing=ns(id="inst-0", lifecycle_state="RUNNING"))
    assert setup(compute)["status"] == "exists"
    assert not compute.launched


def test_other_errors_fail_loudly(setup):
    with pytest.raises(oci.exceptions.ServiceError):
        setup(FakeCompute(errors=[Err(400, "LimitExceeded: service limit")]))


def test_missing_secrets_do_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("OCI_USER_OCID", raising=False)
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    grab.main()
    assert "status=not_configured" in out.read_text()


def test_several_attempts_per_run_stop_at_success(setup, monkeypatch):
    monkeypatch.setenv("ATTEMPTS", "3")
    compute = FakeCompute(errors=[Err(500, "Out of host capacity.")] * 2)
    assert setup(compute)["status"] == "created"
    assert len(compute.launched) == 1


def test_oracle_label_after_the_key_is_ignored():
    key = "-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----\nOCI_API_KEY\n"
    assert grab.pem_only(key) == "-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----\n"
    assert grab.pem_only("  " + key.replace("\n", "\\n")).startswith("-----BEGIN PRIVATE KEY-----\nABC")


def test_a_transient_error_does_not_end_a_long_loop(setup, monkeypatch):
    monkeypatch.setenv("ATTEMPTS", "3")
    monkeypatch.setenv("SHAPES_PER_ATTEMPT", "all")
    compute = FakeCompute(errors=[Err(502, "Bad Gateway"), Err(500, "Out of host capacity.")] * 1 + [Err(500, "Out of host capacity.")])
    assert setup(compute)["status"] in ("created", "created_small")


def test_three_errors_in_a_row_stop_the_loop(setup, monkeypatch):
    monkeypatch.setenv("ATTEMPTS", "5")
    with pytest.raises(oci.exceptions.ServiceError):
        setup(FakeCompute(errors=[Err(401, "NotAuthenticated")] * 3))


def test_smaller_server_is_tried_when_the_big_one_has_no_room(setup, monkeypatch):
    monkeypatch.setenv("SHAPES_PER_ATTEMPT", "all")
    compute = FakeCompute(errors=[Err(500, "Out of host capacity.")])
    assert setup(compute)["status"] == "created_small"
    d = compute.launched[0]
    assert (d.shape_config.ocpus, d.shape_config.memory_in_gbs) == (1, 6)


def test_rate_limit_waits_for_the_next_attempt(setup):
    compute = FakeCompute(errors=[Err(429, "Too many requests for the user")])
    assert setup(compute) == {"status": "no_capacity", "public_ip": ""}
    assert not compute.launched and not compute.errors


def test_attempt_count_continues_across_runs(setup, monkeypatch, tmp_path):
    monkeypatch.setenv("ATTEMPT_OFFSET", "120")
    monkeypatch.setenv("ATTEMPTS", "2")
    sent = []
    monkeypatch.setattr(grab, "notify", lambda status, ip, number, error=None: sent.append(number))
    out = tmp_path / "out"
    monkeypatch.setattr(grab.oci.core, "ComputeClient", lambda cfg: FakeCompute(errors=[Err(500, "Out of host capacity.")] * 4))
    grab.main()
    assert sent == [121, 122]
    assert "attempts=122" in out.read_text()


def small_server():
    return ns(id="inst-0", lifecycle_state="RUNNING", shape_config=ns(ocpus=1.0, memory_in_gbs=6.0))


def test_small_server_is_grown_when_oracle_has_room(setup):
    compute = FakeCompute(existing=small_server())
    assert setup(compute)["status"] == "upgraded"
    c = compute.resized[0].shape_config
    assert (c.ocpus, c.memory_in_gbs) == (2, 12)
    assert not compute.launched


def test_small_server_waits_for_room_to_grow(setup):
    compute = FakeCompute(existing=small_server())
    compute.resize_errors = [Err(500, "Out of host capacity.")]
    assert setup(compute) == {"status": "resizing", "public_ip": "203.0.113.7"}


def test_full_size_server_is_left_alone(setup):
    compute = FakeCompute(existing=ns(id="inst-0", lifecycle_state="RUNNING", shape_config=ns(ocpus=2.0, memory_in_gbs=12.0)))
    assert setup(compute)["status"] == "exists"
    assert not compute.resized


def test_after_a_small_server_the_loop_keeps_growing_it(setup, monkeypatch, tmp_path):
    monkeypatch.setenv("ATTEMPTS", "3")
    monkeypatch.setenv("SHAPES_PER_ATTEMPT", "all")
    compute = FakeCompute(errors=[Err(500, "Out of host capacity.")])
    real_launch = compute.launch_instance
    def launch(details):
        r = real_launch(details)
        compute.existing = ns(id="inst-1", lifecycle_state="RUNNING", shape_config=details.shape_config)
        return r
    compute.launch_instance = launch
    compute.resize_errors = [Err(500, "Out of host capacity.")]
    monkeypatch.setattr(grab.oci.core, "ComputeClient", lambda cfg: compute)
    grab.main()
    out = (tmp_path / "out").read_text()
    assert "status=upgraded" in out and "created=1" in out


def test_fallback_shape_waits_before_asking_again(setup, monkeypatch):
    """Two launch requests back to back get a 429; the smaller shape is asked after a pause."""
    pauses = []
    monkeypatch.setattr(grab.time, "sleep", lambda s: pauses.append(s))
    monkeypatch.setenv("SHAPE_GAP_S", "30")
    monkeypatch.setenv("SHAPES_PER_ATTEMPT", "all")
    compute = FakeCompute(errors=[Err(500, "Out of host capacity.")])
    assert setup(compute)["status"] == "created_small"
    assert pauses[:1] == [30.0]


def test_sizes_take_turns_one_launch_request_per_attempt(setup, monkeypatch):
    """Default: attempt 1 asks for the big server only, attempt 2 for the small one only."""
    monkeypatch.setenv("ATTEMPTS", "2")
    compute = FakeCompute(errors=[Err(500, "Out of host capacity.")])
    assert setup(compute)["status"] == "created_small"
    d = compute.launched[0]
    assert (d.shape_config.ocpus, d.shape_config.memory_in_gbs) == (1, 6)
    assert not compute.errors  # exactly one failed big request before the small one
