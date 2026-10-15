"""Fast, offline deployment tests: uv run --no-project python -m unittest discover -s util -p ec2_setup_bdd.py -v."""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from util import ec2_setup as E


def config(*extra: str):
    return E.parse_args([
        "--vpc-id", "vpc-0123456789abcdef0",
        "--subnet-id", "subnet-0123456789abcdef0",
        "--allow-cidr", "10.20.0.0/16",
        *extra,
    ])


class Arguments(unittest.TestCase):
    def invalid(self, *extra):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            config(*extra)

    def test_defaults_match_active_source(self):
        args = config()
        self.assertEqual(args.instance_type, "g6e.2xlarge")
        self.assertEqual(args.volume_size, 200)
        self.assertFalse(args.apply)
        self.assertFalse(args.check)

    def test_rejects_public_or_invalid_ingress(self):
        for value in ("0.0.0.0/0", "::/0", "bad", "10.0.0.0/99"):
            with self.subTest(value=value):
                self.invalid("--allow-cidr", value)

    def test_normalizes_cidr(self):
        self.assertEqual(config("--allow-cidr", "192.168.1.10/24").allow_cidr[-1], "192.168.1.0/24")

    def test_rejects_bad_ids_sizes_names_and_conflicting_modes(self):
        for flags in (
            ("--vpc-id", "vpc-bad"),
            ("--subnet-id", "subnet-bad"),
            ("--volume-size", "20"),
            ("--stack-name", "bad_name"),
            ("--apply", "--check"),
            ("--timeout", "30"),
            ("--ssh-cidr", "10.0.0.0/8"),
            ("--permissions-boundary", "arn:aws:iam::123456789012:policy/boundary", "--instance-profile", "existing"),
            ("--tag", "missing-equals"),
            ("--tag", "aws:reserved=value"),
        ):
            with self.subTest(flags=flags):
                self.invalid(*flags)

    def test_existing_security_groups_do_not_accept_new_rules(self):
        self.invalid("--security-group-id", "sg-0123456789abcdef0")
        args = E.parse_args([
            "--vpc-id", "vpc-0123456789abcdef0",
            "--subnet-id", "subnet-0123456789abcdef0",
            "--security-group-id", "sg-0123456789abcdef0",
        ])
        self.assertEqual(args.security_group_id, ["sg-0123456789abcdef0"])

    def test_requires_deliberate_ingress_choice(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            E.parse_args(["--vpc-id", "vpc-0123456789abcdef0", "--subnet-id", "subnet-0123456789abcdef0"])

    def test_rejects_too_many_tags(self):
        flags = [item for index in range(50) for item in ("--tag", f"key{index}=value")]
        self.invalid(*flags)


class Template(unittest.TestCase):
    def test_portable_private_encrypted_managed_instance(self):
        template = E.build_template(config())
        resources = template["Resources"]
        instance = resources["Instance"]["Properties"]
        self.assertIn(E.AMI_PARAMETER, instance["ImageId"])
        self.assertEqual(instance["MetadataOptions"]["HttpTokens"], "required")
        self.assertFalse(instance["NetworkInterfaces"][0]["AssociatePublicIpAddress"])
        disk = instance["BlockDeviceMappings"][0]["Ebs"]
        self.assertEqual((disk["VolumeSize"], disk["VolumeType"], disk["Iops"], disk["Throughput"]), (200, "gp3", 3000, 125))
        self.assertTrue(disk["Encrypted"])
        policy = json.dumps(resources["Role"])
        self.assertIn("${AWS::Partition}", policy)
        self.assertIn("AmazonSSMManagedInstanceCore", policy)
        self.assertNotIn("611998158124", json.dumps(template))
        rules = resources["SecurityGroup"]["Properties"]["SecurityGroupIngress"]
        self.assertEqual([rule["FromPort"] for rule in rules], [8000])

    def test_existing_resources_and_customer_kms(self):
        args = E.parse_args([
            "--vpc-id", "vpc-0123456789abcdef0",
            "--subnet-id", "subnet-0123456789abcdef0",
            "--security-group-id", "sg-0123456789abcdef0",
            "--instance-profile", "target-profile",
            "--ami-id", "ami-0123456789abcdef0",
            "--kms-key-id", "alias/target-ebs",
        ])
        resources = E.build_template(args, root_device="/dev/xvda")["Resources"]
        for name in ("Role", "InstanceProfile", "SecurityGroup"):
            self.assertNotIn(name, resources)
        instance = resources["Instance"]["Properties"]
        self.assertEqual(instance["IamInstanceProfile"], "target-profile")
        self.assertEqual(instance["ImageId"], args.ami_id)
        self.assertEqual(instance["BlockDeviceMappings"][0]["DeviceName"], "/dev/xvda")
        self.assertEqual(instance["BlockDeviceMappings"][0]["Ebs"]["KmsKeyId"], "alias/target-ebs")

    def test_boundary_tags_and_optional_ssh(self):
        args = config("--permissions-boundary", "arn:aws:iam::123456789012:policy/boundary",
                      "--key-name", "target-key", "--ssh-cidr", "192.168.1.0/24",
                      "--tag", "Environment=test")
        resources = E.build_template(args)["Resources"]
        self.assertEqual(resources["Role"]["Properties"]["PermissionsBoundary"], args.permissions_boundary)
        instance = resources["Instance"]["Properties"]
        self.assertEqual(instance["KeyName"], "target-key")
        self.assertIn({"Key": "Environment", "Value": "test"}, instance["Tags"])
        self.assertEqual([r["FromPort"] for r in resources["SecurityGroup"]["Properties"]["SecurityGroupIngress"]], [8000, 22])

    def test_name_tag_override_is_not_duplicated(self):
        tags = E.build_template(config("--tag", "Name=custom"))["Resources"]["Instance"]["Properties"]["Tags"]
        self.assertEqual([tag for tag in tags if tag["Key"] == "Name"], [{"Key": "Name", "Value": "custom"}])

    def test_stack_readiness_depends_on_bootstrap_signal(self):
        resources = E.build_template(config())["Resources"]
        self.assertEqual(resources["Ready"]["Type"], "AWS::CloudFormation::WaitCondition")
        self.assertEqual(resources["Ready"]["DependsOn"], "Instance")
        self.assertEqual(resources["Ready"]["Properties"]["Handle"], {"Ref": "ReadyHandle"})
        self.assertEqual(resources["Ready"]["Properties"]["Timeout"], "7200")

    def test_user_data_has_valid_bash_within_ec2_limit(self):
        joins = E.build_template(config())["Resources"]["Instance"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Join"]
        script = "".join(part if isinstance(part, str) else "https://example.invalid/" + "x" * (3000 if "Ref" in part else 150) for part in joins[1])
        self.assertLessEqual(len(script.encode()), 16384)
        result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("trap finish EXIT", script)
        self.assertIn('signal_status=FAILURE', script)
        self.assertIn("017b9c7af6b5689d5dd426a76e0bc077eb5ca20a", script)
        self.assertIn("--max-model-len 98304", script)
        self.assertIn("--tool-call-parser qwen3_coder", script)
        self.assertIn("--kv-cache-dtype fp8", script)
        self.assertIn("torch==2.13.0+cu130", script)
        self.assertIn("User=vllm", script)
        self.assertNotIn("ornith", script.lower())
        self.assertNotIn("ollama pull", script)


class Execution(unittest.TestCase):
    def test_preview_never_contacts_aws(self):
        with patch.object(E, "Aws", side_effect=AssertionError("AWS must not be called")), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(E.main([
                "--vpc-id", "vpc-0123456789abcdef0",
                "--subnet-id", "subnet-0123456789abcdef0",
                "--allow-cidr", "10.0.0.0/8",
            ]), 0)
        self.assertIn("Resources", json.loads(out.getvalue()))

    def test_aws_errors_and_missing_cli_are_explicit(self):
        aws = E.Aws(config())
        failed = subprocess.CompletedProcess([], 254, "", "AccessDenied: denied")
        with patch.object(E.subprocess, "run", return_value=failed), self.assertRaisesRegex(RuntimeError, "AccessDenied"):
            aws.call("ec2", "describe-subnets")
        with patch.object(E.subprocess, "run", side_effect=FileNotFoundError), self.assertRaisesRegex(RuntimeError, "AWS CLI"):
            aws.call("sts", "get-caller-identity")

    def test_aws_arguments_preserve_profile_region_and_json(self):
        aws = E.Aws(config("--profile", "target", "--region", "eu-west-1"))
        with patch.object(E.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"Account":"123456789012"}', "")) as run:
            self.assertEqual(aws.call("sts", "get-caller-identity")["Account"], "123456789012")
        command = run.call_args.args[0]
        self.assertIn("target", command)
        self.assertIn("eu-west-1", command)
        self.assertIn("--no-cli-pager", command)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_failed_stack_never_reports_success(self):
        aws = unittest.mock.Mock()
        aws.call.side_effect = [
            {"Stacks": [{"StackStatus": "CREATE_FAILED"}]},
            {"StackEvents": [{"LogicalResourceId": "Ready", "ResourceStatus": "CREATE_FAILED", "ResourceStatusReason": "bootstrap failed"}]},
        ]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(RuntimeError, "CREATE_FAILED"):
            E.wait_stack(aws, "test-stack", 600)

    def test_complete_stack_returns_outputs(self):
        aws = unittest.mock.Mock()
        aws.call.return_value = {"Stacks": [{"StackStatus": "CREATE_COMPLETE", "Outputs": [{"OutputKey": "InstanceId", "OutputValue": "i-target"}]}]}
        self.assertEqual(E.wait_stack(aws, "test-stack", 600), {"InstanceId": "i-target"})

    def test_apply_only_creates_and_waits_for_ssm(self):
        aws = unittest.mock.Mock()
        aws.call.return_value = {"StackId": "stack-target"}
        with patch.object(E, "Aws", return_value=aws), patch.object(E, "preflight", return_value=("ami-0123456789abcdef0", "/dev/sda1")), \
             patch.object(E, "wait_stack", return_value={"InstanceId": "i-target"}) as wait, \
             patch.object(E, "verify_ssm") as ssm, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = E.main(["--apply", "--vpc-id", "vpc-0123456789abcdef0",
                             "--subnet-id", "subnet-0123456789abcdef0", "--allow-cidr", "10.0.0.0/8"])
        self.assertEqual(result, 0)
        command = aws.call.call_args.args
        self.assertEqual(command[:2], ("cloudformation", "create-stack"))
        self.assertIn("DO_NOTHING", command)
        self.assertIn("file://", command[command.index("--template-body") + 1])
        wait.assert_called_once()
        ssm.assert_called_once_with(aws, "i-target")

    def test_ssm_offline_is_not_success(self):
        aws = unittest.mock.Mock()
        aws.call.return_value = {"InstanceInformationList": []}
        with patch.object(E.time, "monotonic", side_effect=[0, 0, 301]), patch.object(E.time, "sleep"), self.assertRaisesRegex(RuntimeError, "SSM did not come online"):
            E.verify_ssm(aws, "i-target")


class Preflight(unittest.TestCase):
    def setUp(self):
        self.routes = {
            ("sts", "get-caller-identity"): {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/operator"},
            ("ec2", "describe-subnets"): {"Subnets": [{"VpcId": "vpc-0123456789abcdef0", "State": "available", "AvailabilityZone": "us-east-1b"}]},
            ("ec2", "describe-instance-type-offerings"): {"InstanceTypeOfferings": [{"InstanceType": "g6e.2xlarge"}]},
            ("ec2", "describe-instance-types"): {"InstanceTypes": [{"GpuInfo": {"Gpus": [{"Count": 1, "MemoryInfo": {"SizeInMiB": 45776}}]}}]},
            ("ssm", "get-parameter"): {"Parameter": {"Value": "ami-0123456789abcdef0"}},
            ("ec2", "describe-images"): {"Images": [{"Architecture": "x86_64", "State": "available", "RootDeviceType": "ebs", "RootDeviceName": "/dev/xvda",
                                                   "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 75}}]}]},
        }
        self.aws = unittest.mock.Mock()
        self.aws.call.side_effect = lambda *operation: self.routes[operation[:2]]

    def preflight(self, args=None):
        with contextlib.redirect_stderr(io.StringIO()):
            return E.preflight(self.aws, args or config())

    def test_official_ami_is_resolved_in_target_and_root_is_discovered(self):
        self.assertEqual(self.preflight(), ("ami-0123456789abcdef0", "/dev/xvda"))
        self.assertTrue(all("describe-" in call.args[1] or call.args[1] in ("get-parameter", "get-caller-identity") for call in self.aws.call.call_args_list))

    def test_subnet_wrong_vpc_is_rejected(self):
        self.routes[("ec2", "describe-subnets")]["Subnets"][0]["VpcId"] = "vpc-other"
        with self.assertRaisesRegex(RuntimeError, "belong"):
            self.preflight()

    def test_insufficient_gpu_is_rejected(self):
        self.routes[("ec2", "describe-instance-types")]["InstanceTypes"][0]["GpuInfo"]["Gpus"][0]["MemoryInfo"]["SizeInMiB"] = 24576
        with self.assertRaisesRegex(RuntimeError, "L40S"):
            self.preflight()

    def test_missing_zonal_offering_is_rejected(self):
        self.routes[("ec2", "describe-instance-type-offerings")] = {"InstanceTypeOfferings": []}
        with self.assertRaisesRegex(RuntimeError, "no InstanceTypeOfferings"):
            self.preflight()

    def test_root_snapshot_cannot_be_shrunk(self):
        self.routes[("ec2", "describe-images")]["Images"][0]["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"] = 500
        with self.assertRaisesRegex(RuntimeError, "smaller"):
            self.preflight()

    def test_custom_ami_does_not_query_public_parameter(self):
        self.preflight(config("--ami-id", "ami-0123456789abcdef0"))
        self.assertNotIn(("ssm", "get-parameter", "--name", E.AMI_PARAMETER), [call.args for call in self.aws.call.call_args_list])


class Bootstrap(unittest.TestCase):
    def setUp(self):
        self.snippets = re.findall(r"<<'PY'\n(.*?)\nPY", E.BOOTSTRAP, re.DOTALL)
        self.assertEqual(len(self.snippets), 2)

    def test_embedded_python_compiles(self):
        for snippet in self.snippets:
            compile(snippet, "<bootstrap>", "exec")

    def run_health(self, models=None, content="OK", arguments='{"status":"ok"}'):
        if models is None:
            models = [{"id": name, "max_model_len": 98304} for name in ("qwen3.8", "qwen3.8-27b")]
        responses = [
            {"data": models},
            {"choices": [{"message": {"content": content}}]},
            {"choices": [{"message": {"tool_calls": [{"function": {"name": "deployment_probe", "arguments": arguments}}]}}]},
        ]
        bodies = [io.BytesIO(json.dumps(response).encode()) for response in responses]
        with patch("urllib.request.urlopen", side_effect=bodies) as request, contextlib.redirect_stdout(io.StringIO()):
            exec(compile(self.snippets[-1], "<health-check>", "exec"), {})
        return request

    def test_readiness_checks_models_text_and_tool_calls(self):
        request = self.run_health()
        self.assertEqual(request.call_count, 3)

    def test_incorrect_actual_context_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "incorrect context"):
            self.run_health(models=[{"id": "qwen3.8", "max_model_len": 8192}])

    def test_empty_generation_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "no content"):
            self.run_health(content="")

    def test_invalid_tool_arguments_are_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "invalid arguments"):
            self.run_health(arguments='{"status":"wrong"}')


if __name__ == "__main__":
    unittest.main()
