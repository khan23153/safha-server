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

    def list_instances(self, *a, **k):
        return ns(data=[self.existing] if self.existing else [], has_next_page=False, next_page=None)

    def list_images(self, *a, **k):
        return ns(data=[ns(id="img-1")])

    def launch_instance(self, details):
        if self.errors:
            raise self.errors.pop(0)
        self.launched.append(details)
        return ns(data=ns(id="inst-1"))

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
                 "OCI_PRIVATE_KEY": "k", "OCI_SSH_PUBLIC_KEY": "ssh-ed25519 AAA", "GITHUB_OUTPUT": str(out)}.items():
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
        return dict(line.split("=", 1) for line in out.read_text().split())
    return run


def test_out_of_capacity_is_a_quiet_retry(setup):
    compute = FakeCompute(errors=[Err(500, "Out of host capacity.")])
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
