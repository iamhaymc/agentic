#!/usr/bin/env python3
"""Preview, check, or create a private GPU inference stack using Python stdlib and AWS CLI v2."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]
type Object = dict[str, Json]

AMI_PARAMETER = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id"

BOOTSTRAP = r'''
set -Eeuo pipefail
umask 022
export HOME=/root
export PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin:/snap/bin
export DEBIAN_FRONTEND=noninteractive
export UV_CACHE_DIR=/var/cache/uv
export UV_PYTHON_INSTALL_DIR=/opt/uv/python
exec > >(tee -a /var/log/agentic-ec2-setup.log) 2>&1

finish() {
  code=$?
  trap - EXIT
  signal_status=FAILURE
  if [ "$code" -eq 0 ]; then
    signal_status=SUCCESS
  else
    printf 'ERROR: bootstrap failed (exit %s); see /var/log/agentic-ec2-setup.log\n' "$code" >&2
    if ! journalctl -u vllm.service --no-pager -n 60; then
      printf 'ERROR: could not read the service journal\n' >&2
    fi
  fi
  if ! curl --fail --silent --show-error --connect-timeout 15 --max-time 120 \
    --retry 5 --retry-delay 5 -X PUT -H 'Content-Type:' \
    --data-binary "{\"Status\":\"$signal_status\",\"Reason\":\"See /var/log/agentic-ec2-setup.log\",\"UniqueId\":\"bootstrap\",\"Data\":\"$signal_status\"}" \
    "$SIGNAL_URL"; then
    printf 'ERROR: could not signal CloudFormation bootstrap status\n' >&2
    exit 1
  fi
  exit "$code"
}
trap finish EXIT
trap 'printf "ERROR: bootstrap command failed at line %s\n" "$LINENO" >&2' ERR

. /etc/os-release
if [ "$ID" != ubuntu ] || [ "$VERSION_ID" != 22.04 ] || [ "$(uname -m)" != x86_64 ]; then
  printf 'ERROR: use an Ubuntu 22.04 x86-64 GPU AMI\n' >&2
  exit 1
fi
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
apt-get -o DPkg::Lock::Timeout=300 update
apt-get -o DPkg::Lock::Timeout=300 install -y ca-certificates curl python3 libgl1 libglib2.0-0

if systemctl cat snap.amazon-ssm-agent.amazon-ssm-agent.service >/dev/null 2>&1; then
  systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service
elif systemctl cat amazon-ssm-agent.service >/dev/null 2>&1; then
  systemctl enable --now amazon-ssm-agent.service
else
  curl --fail --silent --show-error --retry 5 "$SSM_AGENT_URL" \
    -o /var/tmp/agentic-ssm-agent.deb
  dpkg -i /var/tmp/agentic-ssm-agent.deb
  rm /var/tmp/agentic-ssm-agent.deb
  systemctl enable --now amazon-ssm-agent.service
fi

if systemctl cat ollama.service >/dev/null 2>&1; then
  systemctl disable --now ollama.service
fi
if [ -d /etc/systemd/system/vllm.service.d ] && \
  [ -n "$(find /etc/systemd/system/vllm.service.d -type f -name '*.conf' -print -quit)" ]; then
  printf 'ERROR: base AMI contains vLLM overrides; use a clean approved GPU base image\n' >&2
  exit 1
fi

curl --fail --silent --show-error --retry 5 https://astral.sh/uv/0.12.14/install.sh \
  -o /var/tmp/agentic-uv-install.sh
UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh /var/tmp/agentic-uv-install.sh
rm /var/tmp/agentic-uv-install.sh
uv python install 3.12.14
uv venv --python 3.12.14 /opt/vllm
uv pip install --python /opt/vllm/bin/python \
  --index-url https://download.pytorch.org/whl/cu130 'torch==2.13.0+cu130'
uv pip install --python /opt/vllm/bin/python \
  'vllm==0.29.0' 'torch==2.13.0+cu130' 'triton==3.7.1' \
  'transformers==5.17.0' 'huggingface-hub==1.31.0' 'flashinfer-python==0.6.18'
uv pip check --python /opt/vllm/bin/python

/opt/vllm/bin/python - <<'PY'
import torch
if not torch.cuda.is_available():
    raise RuntimeError("CUDA unavailable; select a GPU AMI with a driver supporting CUDA 13.0")
if torch.cuda.device_count() != 1:
    raise RuntimeError("This deployment reproduces a single-GPU service")
if torch.cuda.get_device_capability(0) < (8, 9):
    raise RuntimeError("FP8 inference requires an Ada-or-newer GPU")
if torch.cuda.get_device_properties(0).total_memory < 44 * 1024**3:
    raise RuntimeError("This model configuration requires an L40S-class GPU with at least 44 GiB")
print("CUDA ready:", torch.__version__, torch.version.cuda, torch.cuda.get_device_name(0))
PY

if ! getent passwd vllm >/dev/null; then
  useradd --system --create-home --home-dir /var/lib/vllm --shell /usr/sbin/nologin vllm
fi
install -d -o vllm -g vllm /var/lib/vllm /opt/models /opt/models/Qwen3.8-27B-FP8
runuser -u vllm -- env HOME=/var/lib/vllm /opt/vllm/bin/hf download \
  Qwen/Qwen3.8-27B-FP8 --revision 017b9c7af6b5689d5dd426a76e0bc077eb5ca20a \
  --local-dir /opt/models/Qwen3.8-27B-FP8

cat > /etc/systemd/system/vllm.service <<'UNIT'
[Unit]
Description=vLLM OpenAI-compatible Qwen3.8-27B FP8 inference
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=vllm
Group=vllm
Environment=HOME=/var/lib/vllm
Environment=PATH=/opt/vllm/bin:/usr/local/cuda/bin:/usr/local/bin:/usr/bin:/bin
WorkingDirectory=/var/lib/vllm
ExecStart=/opt/vllm/bin/vllm serve /opt/models/Qwen3.8-27B-FP8 \
  --served-model-name qwen3.8-27b qwen3.8 \
  --host 0.0.0.0 --port 8000 \
  --max-model-len 98304 \
  --kv-cache-dtype fp8 \
  --gpu-memory-utilization 0.92 \
  --max-num-seqs 16 \
  --enable-prefix-caching \
  --reasoning-parser qwen3 \
  --default-chat-template-kwargs '{"enable_thinking":false}' \
  --limit-mm-per-prompt '{"image":2,"video":0}' \
  --mm-processor-kwargs '{"size":{"longest_edge":4194304,"shortest_edge":65536}}' \
  --generation-config vllm \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder
Restart=always
RestartSec=10
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now vllm.service
ready=false
for ((attempt=0; attempt<720; attempt++)); do
  if curl --fail --silent --max-time 5 http://127.0.0.1:8000/health >/dev/null; then
    ready=true
    break
  fi
  sleep 5
done
if [ "$ready" != true ]; then
  printf 'ERROR: vLLM did not become healthy within one hour\n' >&2
  exit 1
fi

/opt/vllm/bin/python - <<'PY'
import json
from urllib.request import Request, urlopen

base = "http://127.0.0.1:8000"
with urlopen(base + "/v1/models", timeout=30) as response:
    models = json.load(response)["data"]
for name in ("qwen3.8-27b", "qwen3.8"):
    if not any(model["id"] == name and model.get("max_model_len") == 98304 for model in models):
        raise RuntimeError(f"Missing alias or incorrect context: {name}")

def chat(payload):
    request = Request(base + "/v1/chat/completions", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=180) as response:
        return json.load(response)["choices"][0]["message"]

payload = {"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "Reply with OK."}],
           "temperature": 0, "max_tokens": 32}
message = chat(payload)
if not message.get("content", "").strip():
    raise RuntimeError("Text generation smoke test returned no content")
payload["messages"] = [{"role": "user", "content": "Call deployment_probe with status ok."}]
payload["tools"] = [{"type": "function", "function": {"name": "deployment_probe",
    "description": "Report deployment status", "parameters": {"type": "object",
    "properties": {"status": {"type": "string", "enum": ["ok"]}}, "required": ["status"]}}}]
payload["tool_choice"] = {"type": "function", "function": {"name": "deployment_probe"}}
payload["max_tokens"] = 64
calls = chat(payload).get("tool_calls", [])
if not calls or calls[0]["function"]["name"] != "deployment_probe":
    raise RuntimeError("Tool calling smoke test returned no expected tool call")
if json.loads(calls[0]["function"]["arguments"]) != {"status": "ok"}:
    raise RuntimeError("Tool calling smoke test returned invalid arguments")
print("Verified model aliases, 98304-token context, text generation and tool calling")
PY
printf 'Inference bootstrap complete\n'
'''


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack-name", default="agentic-inference")
    parser.add_argument("--region", default=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1")
    parser.add_argument("--profile")
    parser.add_argument("--vpc-id", required=True)
    parser.add_argument("--subnet-id", required=True)
    parser.add_argument("--allow-cidr", action="append", default=[], help="Trusted IPv4 clients allowed to reach port 8000; repeatable")
    parser.add_argument("--security-group-id", action="append", default=[], help="Reuse target security groups instead of creating one")
    parser.add_argument("--instance-profile", help="Reuse a target IAM instance profile with SSM permissions")
    parser.add_argument("--permissions-boundary", help="Boundary policy ARN for the new IAM role")
    parser.add_argument("--ami-id", help="Override the official Ubuntu 22.04 GPU AMI in the target region")
    parser.add_argument("--instance-type", default="g6e.2xlarge")
    parser.add_argument("--volume-size", type=int, default=200, help="Encrypted gp3 root volume GiB")
    parser.add_argument("--kms-key-id", help="Target EBS KMS key ARN/ID/alias; default uses the target account EBS key")
    parser.add_argument("--key-name", help="Existing target EC2 SSH key pair; not needed for SSM")
    parser.add_argument("--ssh-cidr", action="append", default=[], help="Optional trusted IPv4 SSH clients; requires --key-name")
    parser.add_argument("--tag", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--timeout", type=int, default=7200, help="CloudFormation bootstrap timeout seconds (600–43200)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Run read-only target preflight and emit resolved template")
    mode.add_argument("--apply", action="store_true", help="Create a new stack and wait for verified bootstrap; incurs charges")
    args = parser.parse_args(argv)
    for option, prefix in (("vpc_id", "vpc"), ("subnet_id", "subnet")):
        if not re.fullmatch(prefix + r"-(?:[0-9a-f]{8}|[0-9a-f]{17})", getattr(args, option)):
            parser.error(f"--{option.replace('_', '-')} must be a valid {prefix} ID")
    for group in args.security_group_id:
        if not re.fullmatch(r"sg-(?:[0-9a-f]{8}|[0-9a-f]{17})", group):
            parser.error("--security-group-id must be a valid security group ID")
    if args.ami_id and not re.fullmatch(r"ami-(?:[0-9a-f]{8}|[0-9a-f]{17})", args.ami_id):
        parser.error("--ami-id must be a valid AMI ID")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,127}", args.stack_name):
        parser.error("--stack-name must start with a letter and contain only letters, digits and hyphens (128 characters max)")
    if args.volume_size < 100 or args.volume_size > 16384:
        parser.error("--volume-size must be between 100 and 16384 GiB")
    if not 600 <= args.timeout <= 43200:
        parser.error("--timeout must be between 600 and 43200 seconds")
    if args.security_group_id and (args.allow_cidr or args.ssh_cidr):
        parser.error("Existing security groups cannot be combined with --allow-cidr or --ssh-cidr; their rules are not modified")
    if not args.security_group_id and not args.allow_cidr:
        parser.error("Provide --allow-cidr or existing --security-group-id; public unauthenticated inference is not enabled")
    if args.ssh_cidr and not args.key_name:
        parser.error("--ssh-cidr requires an existing target --key-name")
    if args.instance_profile and args.permissions_boundary:
        parser.error("--permissions-boundary only applies when creating a new instance role")
    for option in ("allow_cidr", "ssh_cidr"):
        normalized = []
        for value in getattr(args, option):
            try:
                network = ipaddress.ip_network(value, strict=False)
            except ValueError:
                parser.error(f"Invalid CIDR: {value}")
            if not isinstance(network, ipaddress.IPv4Network) or network.prefixlen == 0:
                parser.error("Ingress must be an explicit trusted IPv4 range, never a world-open CIDR")
            normalized.append(str(network))
        setattr(args, option, list(dict.fromkeys(normalized)))
    tags = {"Name": args.stack_name}
    for tag in args.tag:
        key, separator, value = tag.partition("=")
        if not separator or not key or len(key) > 128 or len(value) > 256 or key.lower().startswith("aws:"):
            parser.error("--tag must be KEY=VALUE (key 1–128, value 0–256 characters; no aws: prefix)")
        tags[key] = value
    if len(tags) > 50:
        parser.error("At most 50 tags are supported, including the default Name tag")
    args.tags = [{"Key": key, "Value": value} for key, value in tags.items()]
    return args


def build_template(args: argparse.Namespace, image_id: str | None = None, root_device: str = "/dev/sda1") -> Object:
    resources: Object = {
        "ReadyHandle": {"Type": "AWS::CloudFormation::WaitConditionHandle"},
        "Ready": {
            "Type": "AWS::CloudFormation::WaitCondition",
            "DependsOn": "Instance",
            "Properties": {"Handle": {"Ref": "ReadyHandle"}, "Timeout": str(args.timeout), "Count": 1},
        },
    }
    tags = args.tags
    if not args.instance_profile:
        role: Object = {
            "AssumeRolePolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}],
            },
            "ManagedPolicyArns": [{"Fn::Sub": "arn:${AWS::Partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"}],
            "Tags": tags,
        }
        if args.permissions_boundary:
            role["PermissionsBoundary"] = args.permissions_boundary
        resources["Role"] = {"Type": "AWS::IAM::Role", "Properties": role}
        resources["InstanceProfile"] = {"Type": "AWS::IAM::InstanceProfile", "Properties": {"Roles": [{"Ref": "Role"}]}}
    if not args.security_group_id:
        rules: list[Json] = [
            {"IpProtocol": "tcp", "FromPort": port, "ToPort": port, "CidrIp": cidr, "Description": description}
            for port, cidrs, description in ((8000, args.allow_cidr, "Trusted inference clients"), (22, args.ssh_cidr, "Optional SSH clients"))
            for cidr in cidrs
        ]
        resources["SecurityGroup"] = {
            "Type": "AWS::EC2::SecurityGroup",
            "Properties": {
                "VpcId": args.vpc_id, "GroupDescription": "Private GPU inference and optional SSH",
                "SecurityGroupIngress": rules,
                "SecurityGroupEgress": [{"IpProtocol": "-1", "CidrIp": "0.0.0.0/0", "Description": "Outbound installation, model downloads and AWS management"}],
                "Tags": tags,
            },
        }
    disk: Object = {"VolumeSize": args.volume_size, "VolumeType": "gp3", "Iops": 3000, "Throughput": 125, "Encrypted": True, "DeleteOnTermination": True}
    if args.kms_key_id:
        disk["KmsKeyId"] = args.kms_key_id
    instance: Object = {
        "ImageId": image_id or args.ami_id or "{{resolve:ssm:" + AMI_PARAMETER + "}}",
        "InstanceType": args.instance_type,
        "IamInstanceProfile": args.instance_profile or {"Ref": "InstanceProfile"},
        "NetworkInterfaces": [{
            "DeviceIndex": "0", "AssociatePublicIpAddress": False, "DeleteOnTermination": True,
            "SubnetId": args.subnet_id, "GroupSet": args.security_group_id or [{"Ref": "SecurityGroup"}],
        }],
        "MetadataOptions": {"HttpTokens": "required", "HttpEndpoint": "enabled", "HttpPutResponseHopLimit": 1},
        "BlockDeviceMappings": [{"DeviceName": root_device, "Ebs": disk}],
        "PropagateTagsToVolumeOnCreation": True,
        "UserData": {"Fn::Base64": {"Fn::Join": ["", [
            "#!/bin/bash\nSIGNAL_URL='", {"Ref": "ReadyHandle"},
            "'\nSSM_AGENT_URL='",
            {"Fn::Sub": "https://s3.${AWS::Region}.${AWS::URLSuffix}/amazon-ssm-${AWS::Region}/latest/debian_amd64/amazon-ssm-agent.deb"},
            "'\n", BOOTSTRAP,
        ]]}},
        "Tags": tags,
    }
    if args.key_name:
        instance["KeyName"] = args.key_name
    resources["Instance"] = {"Type": "AWS::EC2::Instance", "Properties": instance}
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Private Qwen3.8 FP8 vLLM inference; bootstrap readiness is required for stack completion",
        "Resources": resources,
        "Outputs": {
            "InstanceId": {"Value": {"Ref": "Instance"}},
            "PrivateIp": {"Value": {"Fn::GetAtt": ["Instance", "PrivateIp"]}},
            "InferenceUrl": {"Value": {"Fn::Join": ["", ["http://", {"Fn::GetAtt": ["Instance", "PrivateIp"]}, ":8000/v1"]]}},
        },
    }


class Aws:
    def __init__(self, args: argparse.Namespace):
        self.command = ["aws", "--no-cli-pager", "--region", args.region, "--output", "json",
                        "--cli-connect-timeout", "10", "--cli-read-timeout", "60"]
        if args.profile:
            self.command.extend(["--profile", args.profile])

    def call(self, *operation: str) -> Object:
        try:
            result = subprocess.run([*self.command, *operation], capture_output=True, text=True, timeout=120)
        except FileNotFoundError as error:
            raise RuntimeError("AWS CLI v2 is required; install aws and authenticate in the target account") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"AWS command timed out: {' '.join(operation[:2])}") from error
        if result.returncode:
            raise RuntimeError(f"AWS {' '.join(operation[:2])} failed: {result.stderr.strip() or result.stdout.strip()}")
        try:
            payload: Json = json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError as error:
            raise RuntimeError(f"AWS returned invalid JSON for {' '.join(operation[:2])}") from error
        return mapping(payload, "AWS response")


def mapping(value: Json, label: str) -> Object:
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected an object for {label}")
    return value


def objects(payload: Object, key: str) -> list[Object]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise RuntimeError(f"Expected an AWS result list: {key}")
    return [mapping(item, key) for item in value]


def first(payload: Object, key: str) -> Object:
    values = objects(payload, key)
    if not values:
        raise RuntimeError(f"AWS returned no {key}; check target account, region and resource arguments")
    return values[0]


def preflight(aws: Aws, args: argparse.Namespace) -> tuple[str, str]:
    identity = aws.call("sts", "get-caller-identity")
    print(f"Target account: {identity.get('Account')} | region: {args.region} | caller: {identity.get('Arn')}", file=sys.stderr)
    subnet = first(aws.call("ec2", "describe-subnets", "--subnet-ids", args.subnet_id), "Subnets")
    if subnet.get("VpcId") != args.vpc_id or subnet.get("State") != "available":
        raise RuntimeError("Target subnet must be available and belong to --vpc-id")
    offering = aws.call("ec2", "describe-instance-type-offerings", "--location-type", "availability-zone",
                        "--filters", f"Name=location,Values={subnet.get('AvailabilityZone')}", f"Name=instance-type,Values={args.instance_type}")
    first(offering, "InstanceTypeOfferings")
    hardware = first(aws.call("ec2", "describe-instance-types", "--instance-types", args.instance_type), "InstanceTypes")
    gpu = mapping(hardware.get("GpuInfo"), "GPU info")
    devices = objects(gpu, "Gpus")
    if len(devices) != 1 or devices[0].get("Count") != 1:
        raise RuntimeError("Select a single-GPU instance type, such as g6e.2xlarge")
    memory = mapping(devices[0].get("MemoryInfo"), "GPU memory").get("SizeInMiB")
    if not isinstance(memory, int) or memory < 45000:
        raise RuntimeError("This exact FP8/context configuration needs an L40S-class GPU with at least 45000 MiB")
    image_id = args.ami_id
    if not image_id:
        parameter = mapping(aws.call("ssm", "get-parameter", "--name", AMI_PARAMETER).get("Parameter"), "GPU AMI parameter")
        image_id = parameter.get("Value")
    if not isinstance(image_id, str) or not image_id.startswith("ami-"):
        raise RuntimeError("No official regional GPU AMI found; supply a target-region --ami-id")
    image = first(aws.call("ec2", "describe-images", "--image-ids", image_id), "Images")
    if image.get("Architecture") != "x86_64" or image.get("State") != "available" or image.get("RootDeviceType") != "ebs":
        raise RuntimeError("AMI must be available, x86-64 and EBS-backed; use an Ubuntu 22.04 GPU base image")
    root = image.get("RootDeviceName")
    if not isinstance(root, str) or not root:
        raise RuntimeError("AMI has no root device name")
    for block in objects(image, "BlockDeviceMappings"):
        if block.get("DeviceName") == root:
            minimum = mapping(block.get("Ebs"), "AMI root volume").get("VolumeSize")
            if not isinstance(minimum, int) or minimum > args.volume_size:
                raise RuntimeError("Requested root volume is smaller than the AMI root snapshot")
    if args.security_group_id:
        groups = objects(aws.call("ec2", "describe-security-groups", "--group-ids", *args.security_group_id), "SecurityGroups")
        if len(groups) != len(set(args.security_group_id)) or any(group.get("VpcId") != args.vpc_id for group in groups):
            raise RuntimeError("Every security group must belong to the target VPC")
    if args.instance_profile:
        profile = mapping(aws.call("iam", "get-instance-profile", "--instance-profile-name", args.instance_profile).get("InstanceProfile"), "instance profile")
        first(profile, "Roles")
    if args.key_name:
        first(aws.call("ec2", "describe-key-pairs", "--key-names", args.key_name), "KeyPairs")
    if args.kms_key_id:
        key = mapping(aws.call("kms", "describe-key", "--key-id", args.kms_key_id).get("KeyMetadata"), "KMS key")
        if key.get("KeyState") != "Enabled" or key.get("KeyUsage") != "ENCRYPT_DECRYPT":
            raise RuntimeError("EBS KMS key must be enabled for encryption")
    print(f"Preflight passed: {image_id}, {args.instance_type}, {subnet.get('AvailabilityZone')}. Network egress and GPU quota/capacity remain deployment prerequisites.", file=sys.stderr)
    return image_id, root


def wait_stack(aws: Aws, stack_id: str, timeout: int) -> dict[str, str]:
    deadline = time.monotonic() + timeout + 600
    previous = ""
    while time.monotonic() < deadline:
        stack = first(aws.call("cloudformation", "describe-stacks", "--stack-name", stack_id), "Stacks")
        status = stack.get("StackStatus")
        if not isinstance(status, str):
            raise RuntimeError("CloudFormation returned no stack status")
        if status != previous:
            print(f"Stack: {status}", file=sys.stderr)
            previous = status
        if status == "CREATE_COMPLETE":
            outputs = {}
            for output in objects(stack, "Outputs"):
                key, value = output.get("OutputKey"), output.get("OutputValue")
                if not isinstance(key, str) or not isinstance(value, str):
                    raise RuntimeError("CloudFormation returned invalid stack outputs")
                outputs[key] = value
            return outputs
        if status != "CREATE_IN_PROGRESS":
            events = objects(aws.call("cloudformation", "describe-stack-events", "--stack-name", stack_id), "StackEvents")
            for event in events[:15]:
                if str(event.get("ResourceStatus", "")).endswith("FAILED"):
                    print(f"{event.get('LogicalResourceId')}: {event.get('ResourceStatusReason')}", file=sys.stderr)
            raise RuntimeError(f"Stack ended in {status}; resources are retained and may incur charges. Inspect stack events and bootstrap logs.")
        time.sleep(15)
    raise RuntimeError(f"Timed out waiting for stack {stack_id}; it may still be running and incurring charges")


def verify_ssm(aws: Aws, instance_id: str) -> None:
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        instances = objects(aws.call("ssm", "describe-instance-information", "--filters", f"Key=InstanceIds,Values={instance_id}"), "InstanceInformationList")
        if instances and instances[0].get("PingStatus") == "Online":
            return
        time.sleep(15)
    raise RuntimeError(f"{instance_id} bootstrapped, but SSM did not come online; check the instance role, endpoints, DNS and outbound HTTPS")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if not args.apply and not args.check:
            print(json.dumps(build_template(args), indent=2))
            return 0
        aws = Aws(args)
        image_id, root = preflight(aws, args)
        template = build_template(args, image_id, root)
        if args.check:
            print(json.dumps(template, indent=2))
            return 0
        print("Creating a NEW billed stack; existing stacks are never updated. Failed resources are retained for diagnosis.", file=sys.stderr)
        with tempfile.TemporaryDirectory(prefix="agentic-ec2-") as directory:
            path = Path(directory) / "template.json"
            path.write_text(json.dumps(template), encoding="utf-8")
            created = aws.call("cloudformation", "create-stack", "--stack-name", args.stack_name,
                               "--template-body", path.as_uri(), "--capabilities", "CAPABILITY_IAM",
                               "--on-failure", "DO_NOTHING", "--tags", json.dumps(args.tags))
        stack_id = created.get("StackId")
        if not isinstance(stack_id, str) or not stack_id:
            raise RuntimeError("CloudFormation did not return a stack ID; inspect the requested stack name before retrying")
        print(f"Created stack: {stack_id}", file=sys.stderr)
        outputs = wait_stack(aws, stack_id, args.timeout)
        instance_id = outputs.get("InstanceId")
        if not instance_id:
            raise RuntimeError("Completed stack is missing its InstanceId output")
        verify_ssm(aws, instance_id)
        print(json.dumps(outputs, indent=2))
        return 0
    except (RuntimeError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"Interrupted; stack {args.stack_name} is NOT deleted and may still be provisioning or incurring charges.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
