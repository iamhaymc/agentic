# /// script
# requires-python = ">=3.10"
# dependencies = ["torch>=2.5", "transformers>=5", "peft>=0.17"]
# ///
"""Caveman SFT: LoRA on a small student, one script, any CUDA box.

Input: `mix/train.jsonl` of util/caveman_data.py (rows with `conversations` + `caveman.weight`),
or any JSONL of `{"messages": [...]}`.
- ChatML (Qwen, LFM2); loss on every assistant turn, `<think>` kept: Terminus-2 replays whole turns
- per-row weight scales its loss; rows past `--max-len` tokens are cut at the end
- adapter saved every `--save-steps` and at the end; `--merge` also writes a merged model for serving

Run (one 80 GB GPU):
    uv run util/caveman_train.py --model Qwen/Qwen3.5-4B --data data/caveman/mix/train.jsonl --out runs/caveman-4b
Smoke (CPU, ~1 min):
    uv run --with peft python util/caveman_train.py --model trl-internal-testing/tiny-Qwen2ForCausalLM-2.5 \
        --data data/caveman/full/train.jsonl --limit 4 --max-len 512 --out <scratch>/smoke
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sys
import time
from pathlib import Path
from typing import Callable, Iterator, Sequence

log = logging.getLogger("caveman.train")

IM_START, IM_END = "<|im_start|>", "<|im_end|>"
IGNORE = -100  # label of a token that is not trained on


# ---------------------------------------------------------------- data (pure, no torch)


def read_rows(path: Path, limit: int = 0) -> Iterator[tuple[list[dict], float]]:
    """`(messages, weight)` per JSONL row: caveman rows (`conversations`) or plain `messages`."""
    with path.open(encoding="utf-8") as handle:
        taken = 0
        for line in handle:
            if limit and taken >= limit:
                return
            if not line.strip():
                continue
            row = json.loads(line)
            messages = row.get("messages") or row.get("conversations") or []
            weight = float((row.get("caveman") or {}).get("weight", row.get("weight", 1.0)))
            if messages and weight > 0:
                taken += 1
                yield [{"role": m["role"], "content": m.get("content") or ""} for m in messages], weight


def segments(messages: list[dict]) -> list[tuple[str, bool]]:
    """ChatML pieces, each flagged when trained on: an assistant turn's content and its end mark."""
    pieces: list[tuple[str, bool]] = []
    for message in messages:
        trained = message["role"] == "assistant"
        pieces += [(f"{IM_START}{message['role']}\n", False), (f"{message['content']}{IM_END}", trained), ("\n", False)]
    return pieces


def encode(
    pieces: list[tuple[str, bool]], tokenize: Callable[[str], list[int]], max_len: int, prefix: Sequence[int] = ()
) -> tuple[list[int], list[int]]:
    """Token ids and labels (IGNORE outside trained pieces), cut at `max_len`."""
    ids, labels = list(prefix), [IGNORE] * len(prefix)
    for text, trained in pieces:
        tokens = tokenize(text)
        ids += tokens
        labels += tokens if trained else [IGNORE] * len(tokens)
        if len(ids) >= max_len:
            break
    return ids[:max_len], labels[:max_len]


def lr_at(step: int, total: int, warmup: int, peak: float) -> float:
    """Linear warmup, then cosine decay to a tenth of `peak`."""
    if step < warmup:
        return peak * (step + 1) / warmup
    progress = (step - warmup) / max(1, total - warmup)
    return peak * (0.1 + 0.45 * (1 + math.cos(math.pi * min(1.0, progress))))


def order(count: int, epochs: int, seed: int) -> list[int]:
    """Example indexes, reshuffled each epoch (seeded)."""
    rng, out = random.Random(seed), []
    for _ in range(epochs):
        batch = list(range(count))
        rng.shuffle(batch)
        out += batch
    return out


# ---------------------------------------------------------------- training (torch)


def train(args: argparse.Namespace) -> dict:
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer

    torch.manual_seed(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    vocab = tokenizer.get_vocab()
    if IM_START not in vocab or IM_END not in vocab:
        raise SystemExit(f"{args.model}: no {IM_START} / {IM_END} tokens; this script trains ChatML models (Qwen, LFM2)")
    bos = tokenizer.bos_token
    prefix = [tokenizer.bos_token_id] if bos and bos in (tokenizer.chat_template or "") else []
    tokenize = lambda text: tokenizer(text, add_special_tokens=False)["input_ids"]  # noqa: E731

    examples, cut, empty, started = [], 0, 0, time.perf_counter()
    for messages, weight in read_rows(Path(args.data), args.limit):
        pieces = segments(messages)
        ids, labels = encode(pieces, tokenize, args.max_len, prefix)
        cut += len(ids) >= args.max_len
        if all(label == IGNORE for label in labels[1:]):
            empty += 1
            continue
        examples.append((ids, labels, weight))
    tokens = sum(len(ids) for ids, _, _ in examples)
    log.info(
        "data: %d rows, %d tokens (%d cut at %d, %d without assistant tokens) in %.0fs",
        len(examples), tokens, cut, args.max_len, empty, time.perf_counter() - started,
    )
    if not examples:
        raise SystemExit(f"{args.data}: no trainable rows (a task prompt longer than --max-len leaves none)")

    cuda = torch.cuda.is_available()
    dtype = torch.bfloat16 if cuda and torch.cuda.is_bf16_supported() else torch.float32
    device = torch.device("cuda" if cuda else "cpu")
    model = AutoModelForCausalLM.from_pretrained(args.model, dtype=dtype, attn_implementation=args.attn).to(device)
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    lora = LoraConfig(
        r=args.rank, lora_alpha=2 * args.rank, lora_dropout=0.05, target_modules="all-linear", task_type="CAUSAL_LM"
    )
    model = get_peft_model(model, lora)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    log.info("model: %s on %s (%s), LoRA r=%d, %.1fM trainable", args.model, device, dtype, args.rank, trainable / 1e6)

    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(params, lr=args.lr, weight_decay=0.0)
    sequence = order(len(examples), args.epochs, args.seed)
    total = math.ceil(len(sequence) / args.accum)
    warmup = max(1, int(total * args.warmup))
    history = out / "train_log.jsonl"
    history.unlink(missing_ok=True)
    step, losses, seen, clock = 0, [], 0, time.perf_counter()
    model.train()
    for number, index in enumerate(sequence, 1):
        ids, labels, weight = examples[index]
        batch = torch.tensor([ids], device=device)
        loss = model(input_ids=batch, labels=torch.tensor([labels], device=device)).loss
        (loss * weight / args.accum).backward()
        losses.append(loss.item())
        seen += len(ids)
        if number % args.accum and number != len(sequence):
            continue
        torch.nn.utils.clip_grad_norm_(params, 1.0)
        lr = lr_at(step, total, warmup, args.lr)
        for group in optimizer.param_groups:
            group["lr"] = lr
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
        step += 1
        if step % args.log_steps == 0 or step == total:
            secs = time.perf_counter() - clock
            entry = {"step": step, "of": total, "loss": round(sum(losses) / len(losses), 4), "lr": lr,
                     "tokens_per_sec": round(seen / secs), "secs": round(secs)}
            log.info("step %d/%d loss %.4f lr %.2e %d tok/s", step, total, entry["loss"], lr, entry["tokens_per_sec"])
            with history.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
            losses = []
        if args.save_steps and step % args.save_steps == 0 and step != total:
            model.save_pretrained(out / "adapter")
    model.save_pretrained(out / "adapter")
    tokenizer.save_pretrained(out / "adapter")
    summary = {"model": args.model, "data": args.data, "rows": len(examples), "tokens": tokens, "steps": total,
               "secs": round(time.perf_counter() - clock), "args": vars(args)}
    if args.merge:
        merged = model.merge_and_unload()
        merged.save_pretrained(out / "merged")
        tokenizer.save_pretrained(out / "merged")
    (out / "train.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    log.info("saved %s (%d steps, %ds)", out / "adapter", total, summary["secs"])
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--model", required=True, help="ChatML student, e.g. Qwen/Qwen3.5-4B or Qwen/Qwen3.5-9B")
    parser.add_argument("--data", default="data/caveman/mix/train.jsonl")
    parser.add_argument("--out", required=True, help="adapter, train_log.jsonl, train.json (and merged/)")
    parser.add_argument("--limit", type=int, default=0, help="rows (0 = all)")
    parser.add_argument("--max-len", type=int, default=16384, help="tokens per row; memory grows with it")
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--warmup", type=float, default=0.03, help="share of steps")
    parser.add_argument("--accum", type=int, default=16, help="rows per optimizer step")
    parser.add_argument("--rank", type=int, default=32, help="LoRA rank (alpha = 2 x rank)")
    parser.add_argument("--attn", default="sdpa", help="attn_implementation (sdpa, flash_attention_2)")
    parser.add_argument("--log-steps", type=int, default=10)
    parser.add_argument("--save-steps", type=int, default=200, help="adapter checkpoint every N steps (0 = end only)")
    parser.add_argument("--merge", action="store_true", help="also write merged/ (base + adapter) for serving")
    parser.add_argument("--seed", type=int, default=0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for name in ("httpx", "huggingface_hub"):  # one line per file download drowns the log
        logging.getLogger(name).setLevel(logging.WARNING)
    train(parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
