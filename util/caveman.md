# Caveman

## ▶️ Run (Data)

Shrink agent reasoning into SFT data, caveman voice, every action kept:

```bash
uv run python util/caveman_data.py fetch --limit 50    # seeded sample -> data/caveman/source.jsonl
uv run python util/caveman_data.py shrink --level full # -> data/caveman/full/train.jsonl
uv run python util/caveman_data.py stats --level full  # re-summarize turns.jsonl
```

- levels `lite` / `full` / `ultra`; hard turns get one level gentler; each trace tagged `Reasoning budget: <level>`
- a turn that loses a fact, a negation or its commands (replay check) keeps its original text
- `fetch --parquet --limit 0` downloads the whole dataset (1.75 GB) instead of sampling the viewer

> Compressor: the `.env` inference config (OpenRouter), or `CAVEMAN_URL` + `CAVEMAN_LLM` ([OPTIONS](OPTIONS.md#caveman-data)); depth in [DESIGN](DESIGN.md#24-caveman-sft-data)

## ▶️ Run (Student)

Mix levels into one SFT set, train a student remotely, eval it:

```bash
uv run python util/caveman_data.py mix                                          # plan + mix/train.jsonl + mix/eval.jsonl
uv run python util/caveman_data.py shrink --planned --level ultra --traces 8    # each trace once, at its planned level
uv run util/caveman_train.py --model Qwen/Qwen3.5-4B --out runs/caveman-4b      # LoRA, any CUDA box (80 GB)
uv run python util/caveman_data.py eval --url http://gpu:8000/v1 --model caveman  # similarity vs reasoning tokens per tag
```

- plan: 5% held out, 10% original and untagged, the rest one level each (`lite` / `full` / `ultra`)
- `replay --verifier qwen/qwen3.5-9b` re-checks shrunk turns with a student-sized verifier
- Terminal-Bench (Harbor) and BFCL runs against the served student: [DESIGN](DESIGN.md#eval)

## ▶️ Run (GPU)

Preview a private AWS GPU inference stack; no resources created without `--apply`:

```bash
uv run --no-project python util/ec2_setup.py --region us-east-1 --vpc-id vpc-REPLACE --subnet-id subnet-REPLACE --allow-cidr 10.0.0.0/8
```

- Existing target VPC/subnet required; Ubuntu 22.04 GPU AMI, encrypted EBS, minimal SSM role, vLLM and pinned Qwen3.8 FP8 model configured automatically
- `--check` validates target resources read-only; `--apply` creates a new billed stack and waits for inference readiness
- [DESIGN](DESIGN.md#25-ec2-inference-deployment) — Shared Resource Arguments, Source Inventory And Operations
