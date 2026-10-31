#!/usr/bin/env python3
"""Grab an Oracle Cloud Always Free Ampere A1 server for Safha.

Free A1 capacity in a region is a shared pool, so creating the instance often
fails with "Out of host capacity". This script is safe to run every few
minutes: it exits successfully without doing anything when the server already
exists, treats capacity/rate-limit errors as "try again later", and creates
the network (VCN, internet gateway, public subnet) on first use.

Configuration comes from environment variables (GitHub secrets):
  OCI_USER_OCID, OCI_TENANCY_OCID, OCI_FINGERPRINT, OCI_PRIVATE_KEY,
  OCI_REGION (default ap-mumbai-1), OCI_SSH_PUBLIC_KEY,
  optional KAGGLE_API_TOKEN (lets the server download the private models),
  A1_OCPUS (default 2), A1_MEMORY_GB (default 12), BOOT_GB (default 100).

Outputs (GITHUB_OUTPUT): status=exists|created|no_capacity, public_ip=...
"""
from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path

import oci

NAME = "safha-server"
VCN_NAME = "safha-vcn"
SHAPE = "VM.Standard.A1.Flex"
CAPACITY_MARKERS = ("out of host capacity", "out of capacity", "capacity")
HERE = Path(__file__).resolve().parent


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        sys.exit(f"Missing {name}")
    return value


def output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    for key, value in values.items():
        print(f"{key}={value}")
        if path:
            with open(path, "a") as f:
                f.write(f"{key}={value}\n")


def pem_only(text: str) -> str:
    """Keep just the PEM block; Oracle's download appends an 'OCI_API_KEY' label."""
    text = text.replace("\\n", "\n").strip()
    begin, end = text.find("-----BEGIN"), text.rfind("-----END")
    if begin < 0 or end < 0:
        sys.exit("OCI_PRIVATE_KEY is not a PEM private key")
    end = text.find("-----", end + 8) + 5
    return text[begin:end] + "\n"


def config() -> dict:
    cfg = {
        "user": env("OCI_USER_OCID").strip(),
        "tenancy": env("OCI_TENANCY_OCID").strip(),
        "fingerprint": env("OCI_FINGERPRINT").strip(),
        "key_content": pem_only(env("OCI_PRIVATE_KEY")),
        "region": os.environ.get("OCI_REGION") or "ap-mumbai-1",
    }
    oci.config.validate_config(cfg)
    return cfg


def is_capacity_error(err: oci.exceptions.ServiceError) -> bool:
    message = (err.message or "").lower()
    return err.status in (429, 500, 503) and (
        err.status == 429 or any(marker in message for marker in CAPACITY_MARKERS)
    )


def existing_instance(compute, compartment):
    for inst in compute.list_instances(compartment, display_name=NAME).data:
        if inst.lifecycle_state not in ("TERMINATED", "TERMINATING"):
            return inst
    return None


def ensure_network(vnet, compartment) -> str:
    """Return a public subnet id, creating VCN + gateway + subnet when absent."""
    vcns = [v for v in vnet.list_vcns(compartment, display_name=VCN_NAME).data
            if v.lifecycle_state == "AVAILABLE"]
    if vcns:
        vcn = vcns[0]
    else:
        vcn = vnet.create_vcn(oci.core.models.CreateVcnDetails(
            compartment_id=compartment, display_name=VCN_NAME,
            cidr_blocks=["10.0.0.0/16"], dns_label="safha")).data
        oci.wait_until(vnet, vnet.get_vcn(vcn.id), "lifecycle_state", "AVAILABLE")
        print("created VCN", vcn.id)
    gateways = vnet.list_internet_gateways(compartment, vcn_id=vcn.id).data
    if gateways:
        gateway = gateways[0]
    else:
        gateway = vnet.create_internet_gateway(oci.core.models.CreateInternetGatewayDetails(
            compartment_id=compartment, vcn_id=vcn.id, is_enabled=True,
            display_name="safha-igw")).data
        print("created internet gateway", gateway.id)
    table = vnet.get_route_table(vcn.default_route_table_id).data
    if not any(r.network_entity_id == gateway.id for r in table.route_rules):
        rules = list(table.route_rules) + [oci.core.models.RouteRule(
            destination="0.0.0.0/0", destination_type="CIDR_BLOCK",
            network_entity_id=gateway.id)]
        vnet.update_route_table(table.id, oci.core.models.UpdateRouteTableDetails(route_rules=rules))
        print("added default route")
    subnets = [s for s in vnet.list_subnets(compartment, vcn_id=vcn.id).data
               if s.lifecycle_state == "AVAILABLE" and not s.prohibit_public_ip_on_vnic]
    if subnets:
        return subnets[0].id
    subnet = vnet.create_subnet(oci.core.models.CreateSubnetDetails(
        compartment_id=compartment, vcn_id=vcn.id, cidr_block="10.0.0.0/24",
        display_name="safha-public", dns_label="pub",
        route_table_id=vcn.default_route_table_id,
        security_list_ids=[vcn.default_security_list_id])).data
    oci.wait_until(vnet, vnet.get_subnet(subnet.id), "lifecycle_state", "AVAILABLE")
    print("created subnet", subnet.id)
    return subnet.id


def ubuntu_image(compute, compartment) -> str:
    images = compute.list_images(
        compartment, operating_system="Canonical Ubuntu", operating_system_version="24.04",
        shape=SHAPE, sort_by="TIMECREATED", sort_order="DESC").data
    if not images:
        sys.exit("No Ubuntu 24.04 image for A1 in this region")
    return images[0].id


def cloud_init() -> str:
    script = (HERE / "cloud-init.sh").read_text()
    token = os.environ.get("KAGGLE_API_TOKEN", "")
    script = script.replace("__KAGGLE_API_TOKEN__", token)
    return base64.b64encode(script.encode()).decode()


def public_ip(compute, vnet, compartment, instance_id) -> str:
    for _ in range(30):
        attachments = compute.list_vnic_attachments(compartment, instance_id=instance_id).data
        for att in attachments:
            if att.lifecycle_state == "ATTACHED":
                ip = vnet.get_vnic(att.vnic_id).data.public_ip
                if ip:
                    return ip
        time.sleep(10)
    return ""


def main() -> None:
    """Try up to ATTEMPTS times, ATTEMPT_GAP_S apart; stop as soon as the server exists."""
    if not os.environ.get("OCI_USER_OCID"):
        print("Oracle secrets are not set yet; nothing to do.")
        output(status="not_configured", public_ip="", attempts="0")
        return
    attempts = max(1, int(os.environ.get("ATTEMPTS") or 1))
    gap = float(os.environ.get("ATTEMPT_GAP_S") or 150)
    status, ip = "no_capacity", ""
    for n in range(1, attempts + 1):
        status, ip = attempt()
        print(f"attempt {n}/{attempts}: {status}")
        if status != "no_capacity":
            break
        if n < attempts:
            time.sleep(gap)
    output(status=status, public_ip=ip, attempts=str(n))


def attempt() -> tuple[str, str]:
    """One try. Returns (status, public_ip)."""
    cfg = config()
    compartment = cfg["tenancy"]
    compute = oci.core.ComputeClient(cfg)
    vnet = oci.core.VirtualNetworkClient(cfg)
    identity = oci.identity.IdentityClient(cfg)

    found = existing_instance(compute, compartment)
    if found:
        return "exists", public_ip(compute, vnet, compartment, found.id)

    subnet_id = ensure_network(vnet, compartment)
    image_id = ubuntu_image(compute, compartment)
    ocpus = float(os.environ.get("A1_OCPUS") or 2)
    memory = float(os.environ.get("A1_MEMORY_GB") or 12)
    boot = int(os.environ.get("BOOT_GB") or 100)
    ssh_key = env("OCI_SSH_PUBLIC_KEY")

    for ad in identity.list_availability_domains(compartment).data:
        details = oci.core.models.LaunchInstanceDetails(
            availability_domain=ad.name, compartment_id=compartment, display_name=NAME,
            shape=SHAPE,
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=ocpus, memory_in_gbs=memory),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=image_id, boot_volume_size_in_gbs=boot),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id, assign_public_ip=True),
            metadata={"ssh_authorized_keys": ssh_key, "user_data": cloud_init()},
        )
        try:
            inst = compute.launch_instance(details).data
        except oci.exceptions.ServiceError as err:
            if is_capacity_error(err):
                print(f"{ad.name}: no capacity yet ({err.status} {err.message})")
                continue
            raise
        print("launched", inst.id, "in", ad.name)
        oci.wait_until(compute, compute.get_instance(inst.id), "lifecycle_state", "RUNNING",
                       max_wait_seconds=900)
        return "created", public_ip(compute, vnet, compartment, inst.id)
    return "no_capacity", ""


def annotate(err: BaseException) -> None:
    """Surface a readable cause as a GitHub annotation (never secrets)."""
    if isinstance(err, oci.exceptions.ServiceError):
        text = f"Oracle API {err.status} {err.code}: {err.message}"
    else:
        text = f"{type(err).__name__}: {err}"
    text = text.replace("\n", " ")[:900]
    print(f"::error title=Oracle A1 grab failed::{text}", flush=True)
    output(status="error", public_ip="", error=text.replace("=", ":"))


if __name__ == "__main__":
    try:
        main()
    except SystemExit as err:
        if err.code not in (0, None):
            annotate(RuntimeError(str(err.code)))
        raise
    except Exception as err:  # noqa: BLE001 - report then fail the run
        annotate(err)
        raise
