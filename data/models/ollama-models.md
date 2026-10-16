# 🐬 Ollama — Uncensored / Creative / Distilled Models

_Curated best-in-class catalog scraped from [ollama.com/search](https://ollama.com/search)_

- [README](../README.md) — Summary and quickstart
- [DESIGN](../DESIGN.md) — Complete overview and reference
- [TODO](../TODO.md) — Open development tasks

> ❗ Snapshot: 2026-10-16. Ollama search pages, model cards and the registry API
> (`registry.ollama.ai/v2/<ns>/<name>/manifests/latest`) were read directly.
> `—` means the field is not published on the model card.

## ▶️ Read (Legend)

- **Variant** — how the model differs from its base: `abliterated` (refusal direction removed from weights), `heretic` (automated abliteration via [heretic](https://github.com/p-e-w/heretic)), `uncensored` (trained/merged without refusals), `finetune`, `merge`, `distilled`, `requant`.
- **Arch** — `model_family` from the Ollama registry config blob.
- **Params** — `model_type` from the registry config blob (total, not active).
- **Context** — native window in tokens; `K` = 1024.
- **In** — input modalities advertised on the model page.
- **Out** — expected outputs; `JSON` means structured tool-call / schema output.
- **Censor** — `Abliterated` (weights edited), `Uncensored` (trained without refusals), `Reduced` (partial), `Censored` (stock alignment).
- **Refusals** — `n/100` on `LLM-LAT/harmful-dataset` unless noted; **KL** is divergence from base (lower = less capability damage; <0.5 is the usual "no damage" threshold).

## ▶️ Pick (Top Tier)

Highest capability with refusals removed. Start here.

### `orcarouter/Qwen3.8-27B-Uncensored`

- **Variant** — abliterated (tensor-level, vision tower + MTP head untouched) · **Arch** — qwen35 · **Params** — 27.3B
- **Context** — 262,144 (256K) · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — MMLU 84.7 · MMLU-Pro 76.8 · GSM8K 88.7 · CMMLU 80.8 · XSTest-safe over-refusal 0.4%
- **Censor** — Abliterated · **Refusals** — 0–6% across AdvBench / JailbreakBench / StrongREJECT / HarmBench / MaliciousInstruct / SimpleSafetyTests / ForbiddenQuestions
- **URL** — https://ollama.com/orcarouter/Qwen3.8-27B-Uncensored

### `huihui_ai/Qwen3.8-abliterated`

- **Variant** — abliterated · **Arch** — qwen35 · **Params** — 27.3B
- **Context** — 256K · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — — (inherits Qwen3.8-27B)
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/huihui_ai/Qwen3.8-abliterated

### `mannix/Bonsai-2-27B-CRACK`

- **Variant** — abliterated (weight-level tensor edit) + ternary requant · **Arch** — qwen35 · **Params** — 27.3B
- **Context** — 262,144 (256K) · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — HarmBench-320 refusal 93.44% → **0.00%** · MMLU 40.53% → 39.91% (−0.62)
- **Censor** — Abliterated · **Refusals** — 0/320 HarmBench
- **URL** — https://ollama.com/mannix/Bonsai-2-27B-CRACK
- **Note** — PQ2_0 ternary (2.13 bpw, 7.2 GB) needs **xOllama** v0.40.3+; stock Ollama cannot load it.

### `aiconjured/Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q5_NVFP4`

- **Variant** — uncensored (HauhauCS Aggressive) + NVFP4 requant · **Arch** — qwen35 · **Params** — 27.3B
- **Context** — 256K · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — — (requant of Q5_K_P source, 14.74 GiB / 4.63 BPW)
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/aiconjured/Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q5_NVFP4
- **Note** — NVFP4 is natively accelerated on Blackwell (sm_120); loads on any current llama.cpp.

### `lucloner/qwen38-27b-turbofcfusion`

- **Variant** — uncensored (DavidAU multi-stage decensor) + merge · **Arch** — qwen35 · **Params** — 26.9B
- **Context** — 262,144 native, extensible to 1,000,000 · **In** — Text · **Out** — text, thinking, JSON tool calls
- **Bench** — ARC-C 735 (8-bit) / 718 (4-bit) · ARC-E 880 · decode 40.2 tok/s · MTP acceptance 0.695
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/lucloner/qwen38-27b-turbofcfusion

### `mgraffam/qwen3.8-turbo-fusion-heretic`

- **Variant** — heretic + distilled (Claude Fable) · **Arch** — qwen35 · **Params** — 27B
- **Context** — 256K · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/mgraffam/qwen3.8-turbo-fusion-heretic

### `rafw007/qwen3.8-27b-redteam`

- **Variant** — abliterated · **Arch** — qwen35 · **Params** — 27.3B
- **Context** — 256K · **In** — Text · **Out** — text, JSON tool calls
- **Bench** — AIME (see card) · documented live redteam/sysadmin agent results
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/rafw007/qwen3.8-27b-redteam

### `Ermzzz999/qwen3.6-35b-a3b-abliterated-nvfp4-mtp`

- **Variant** — abliterated (huihui-ai) + native MLX nvfp4 · **Arch** — qwen3.6 MoE · **Params** — 35B-A3B
- **Context** — 256K · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — ~56 tok/s on M3 Pro 36 GB · MTP draft acceptance ~75–80%
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/Ermzzz999/qwen3.6-35b-a3b-abliterated-nvfp4-mtp
- **Note** — Apple Silicon only (nvfp4 on Ollama is always native MLX).

### `ninjasatwork/deepseek-v4-flash-abliterated`

- **Variant** — abliterated · **Arch** — deepseek4 · **Params** — 284.3B
- **Context** — 1M · **In** — Text · **Out** — text, thinking
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/ninjasatwork/deepseek-v4-flash-abliterated
- **Note** — IQ2_XXS, 87 GB. Largest uncensored model on Ollama.

## ▶️ Pick (Heretic Series)

Automated abliteration with published refusal/KL numbers. Best capability-per-byte.

### `Maternion/spark-x2.5-heretic`

- **Variant** — heretic (trial 155/400, Pareto knee) · **Arch** — spark-x2.5 · **Params** — 4B
- **Context** — 1M · **In** — Text · **Out** — text, thinking
- **Bench** — BFCL-V4 65.1 · τ²-bench 75.1 · τ³-bench 30.4 · MCP-Atlas 54.6 · SWE-Bench Pro 44.4 · SWE-Bench Verified 41.6 · SWE-Bench Multilingual 53.3 · SciCode 34.7 · AIME 2026 90.7 · HMMT Feb 2026 81.2 · IMO-AnswerBench 74.2 · IFEval 93.0 · IFBench 75.0 · AA-LCR 56.3 · HLE 12.3 · GPQA 67.4
- **Censor** — Abliterated · **Refusals** — 7/100 (from 100/100) · **KL** — 0.0490
- **URL** — https://ollama.com/Maternion/spark-x2.5-heretic

### `Maternion/mimo-v2.6-heretic`

- **Variant** — heretic (trial 174/200) · **Arch** — qwen3.5 · **Params** — 9B
- **Context** — 256K · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — MiMo-V2.6 technical report (coding, agent, visual coding, cybersecurity)
- **Censor** — Abliterated · **Refusals** — 20/100 · **KL** — 0.0171
- **URL** — https://ollama.com/Maternion/mimo-v2.6-heretic
- **Note** — Distilled: `XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`. Thinking + instruct tags.

### `Maternion/minicpm5-heretic`

- **Variant** — heretic (200 trials, TPE) · **Arch** — llama · **Params** — 2.5B
- **Context** — 128K · **In** — Text · **Out** — text, thinking
- **Bench** — 2B-class SOTA, competitive with 4B models
- **Censor** — Abliterated · **Refusals** — 10/100 (from ~99/100) · **KL** — 0.0349
- **URL** — https://ollama.com/Maternion/minicpm5-heretic
- **Note** — Apache-2.0. Residual refusals are SFT-baked, not direction-based.

### `richardyoung/agents-a1-4b-heretic`

- **Variant** — heretic (trial 126) · **Arch** — qwen35 · **Params** — 4.2B
- **Context** — 256K · **In** — Text, Image · **Out** — text, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — 11/100 (from 99/100, −89%) · **KL** — 0.060
- **URL** — https://ollama.com/richardyoung/agents-a1-4b-heretic
- **Note** — Apache-2.0. Agent/tool-use tuned.

### `richardyoung/deepseek-r1-0528-qwen3-8b-heretic`

- **Variant** — heretic (trial 173) + distilled · **Arch** — qwen3 · **Params** — 8.2B
- **Context** — 128K · **In** — Text · **Out** — text, ` thinking` reasoning
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — 4/100 (from 89/100, −96%) · **KL** — 0.041
- **URL** — https://ollama.com/richardyoung/deepseek-r1-0528-qwen3-8b-heretic
- **Note** — MIT. R1-0528 reasoning distilled into Qwen3-8B.

### `richardyoung/granite-4.2-8b-heretic`

- **Variant** — heretic · **Arch** — granite · **Params** — 8.8B
- **Context** — 128K · **In** — Text · **Out** — text, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — 12/100 (from 99/100) · **KL** — 0.080
- **URL** — https://ollama.com/richardyoung/granite-4.2-8b-heretic
- **Note** — Apache-2.0, multilingual.

### `richardyoung/phi-4-mini-instruct-heretic`

- **Variant** — heretic · **Arch** — phi3 · **Params** — 3.8B
- **Context** — 128K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — 5/100 (from 99/100) · **KL** — 0.038
- **URL** — https://ollama.com/richardyoung/phi-4-mini-instruct-heretic
- **Note** — MIT.

### `richardyoung/muse-glimmer-30b-heretic`

- **Variant** — heretic · **Arch** — muse-glimmer · **Params** — 27.9B
- **Context** — 128K · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated (partial) · **Refusals** — 52/100 (from 99/100) · **KL** — 0.063
- **URL** — https://ollama.com/richardyoung/muse-glimmer-30b-heretic
- **Note** — Apache-2.0. Deliberately conservative: lower-refusal trials exceeded KL 0.10.

### `richardyoung/frognano-4b-2609-heretic`

- **Variant** — heretic (trial 161) · **Arch** — qwen35 · **Params** — 4.2B
- **Context** — 256K · **In** — Text, Image · **Out** — text, thinking
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — 12/100 (from 99/100) · **KL** — 0.040
- **URL** — https://ollama.com/richardyoung/frognano-4b-2609-heretic
- **Note** — MIT. Qwen3.5-based coding agent that thinks.

### `richardyoung/lfm2.5-1.2b-instruct-heretic`

- **Variant** — heretic · **Arch** — lfm2 · **Params** — 1.2B
- **Context** — 125K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — 3/100 (from 98/100) · **KL** — 0.059
- **URL** — https://ollama.com/richardyoung/lfm2.5-1.2b-instruct-heretic
- **Note** — 567 MB at Q4. Runs on almost anything.

### `richardyoung/granite-4.2-3b-heretic`

- **Variant** — heretic · **Arch** — granite · **Params** — 3.7B
- **Context** — 128K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — 27/100 (from 98/100) · **KL** — 0.085
- **URL** — https://ollama.com/richardyoung/granite-4.2-3b-heretic
- **Note** — Apache-2.0, multilingual.

### `R4C3R/qwen3-8b-heretic`

- **Variant** — heretic · **Arch** — qwen3 · **Params** — 8B
- **Context** — 40K · **In** — Text · **Out** — text, ` thinking` reasoning, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — 10/100 (from 100/100) · **KL** — 0.036
- **URL** — https://ollama.com/R4C3R/qwen3-8b-heretic
- **Note** — Apache-2.0. Dual thinking / non-thinking mode.

### `R4C3R/qwen2.5-14b-instruct-heretic`

- **Variant** — heretic · **Arch** — qwen2 · **Params** — 14B
- **Context** — 32K · **In** — Text · **Out** — text, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — 14/100 (from 98/100) · **KL** — 0.06
- **URL** — https://ollama.com/R4C3R/qwen2.5-14b-instruct-heretic
- **Note** — qwen-research license. Strongest general reasoning in the R4C3R series.

### `R4C3R/gemma-3-12b-it-heretic`

- **Variant** — heretic · **Arch** — gemma3 · **Params** — 12B
- **Context** — 128K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/R4C3R/gemma-3-12b-it-heretic
- **Note** — Gemma license. Tuned for creative writing and roleplay.

### `R4C3R/mistral-7b-instruct-v0.3-heretic`

- **Variant** — heretic · **Arch** — mistral · **Params** — 7B
- **Context** — 32K · **In** — Text · **Out** — text, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/R4C3R/mistral-7b-instruct-v0.3-heretic
- **Note** — Apache-2.0. Mistral prose style; strong at 7B for creative writing.

### `R4C3R/qwen2.5-7b-instruct-heretic`

- **Variant** — heretic · **Arch** — qwen2 · **Params** — 7B
- **Context** — 32K · **In** — Text · **Out** — text, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/R4C3R/qwen2.5-7b-instruct-heretic

### `R4C3R/gemma-3-4b-it-heretic`

- **Variant** — heretic · **Arch** — gemma3 · **Params** — 4B
- **Context** — 128K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/R4C3R/gemma-3-4b-it-heretic

### `R4C3R/minicpm5-1b-fable5-heretic`

- **Variant** — heretic (decensored mergeset) · **Arch** — llama · **Params** — 1.1B
- **Context** — 128K · **In** — Text · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/R4C3R/minicpm5-1b-fable5-heretic
- **Note** — 688 MB. Bilingual (zh/en) roleplay and thinking.

## ▶️ Pick (Gemma 4 Abliterated)

### `mgraffam/gemma4-agentic-fable-abliterated`

- **Variant** — abliterated + distilled (Fable 5) · **Arch** — gemma4 · **Params** — 11.9B
- **Context** — 256K · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/mgraffam/gemma4-agentic-fable-abliterated

### `mgraffam/gemma4-opus4.8-heretic`

- **Variant** — heretic + distilled (Opus 4.8 reasoning) · **Arch** — gemma4 · **Params** — 26B-A4B
- **Context** — 256K · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/mgraffam/gemma4-opus4.8-heretic

### `mgraffam/goetia-absolute-heretic`

- **Variant** — heretic (merge of several abliterated 26B-A4B) · **Arch** — gemma4 · **Params** — 26B-A4B
- **Context** — 256K · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/mgraffam/goetia-absolute-heretic

### `ebbotrobot/gemma4-heretic-ara-8k`

- **Variant** — heretic · **Arch** — gemma4 + clip · **Params** — 30.7B
- **Context** — 256K · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/ebbotrobot/gemma4-heretic-ara-8k

### `ttempvnn/OS-Software-gemma-4-31B-it-qat-q4-0-uncensored-heretic-Q4-0`

- **Variant** — heretic + QAT · **Arch** — gemma4 · **Params** — 30.7B
- **Context** — 256K · **In** — Text · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/ttempvnn/OS-Software-gemma-4-31B-it-qat-q4-0-uncensored-heretic-Q4-0

### `locbaccity/gemma4-31b-it-vision`

- **Variant** — uncensored · **Arch** — gemma4 · **Params** — 31B
- **Context** — 256K · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/locbaccity/gemma4-31b-it-vision

### `masska129/gemma-4-abliterated`

- **Variant** — abliterated · **Arch** — gemma4 · **Params** — —
- **Context** — 256K · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/masska129/gemma-4-abliterated

### `s1gnature/gemma4-uncensored-mtp`

- **Variant** — uncensored (HauhauCS Aggressive) + MTP drafter · **Arch** — gemma4 · **Params** — E4B / E2B
- **Context** — 32,000 · **In** — Text, Image · **Out** — text
- **Bench** — up to 80 TPS on RTX 3050 8 GB OC
- **Censor** — Uncensored · **Refusals** — **0/465**
- **URL** — https://ollama.com/s1gnature/gemma4-uncensored-mtp

### `ninjasatwork/gemma4_RP_Companion_abliterated`

- **Variant** — abliterated + LoRA (r=128, 524M params) · **Arch** — gemma4 · **Params** — 11.9B
- **Context** — 8K · **In** — Text · **Out** — text, `<model_think>` / `<char_think>` traces, screenplay lines
- **Bench** — format adherence 92.5% → 100% · thinking traces 0% → 100% · RoleBench 4.4/10 in-character, 4.2/10 accuracy · loss 3.07 → 0.30
- **Censor** — Abliterated · **Refusals** — none observed
- **URL** — https://ollama.com/ninjasatwork/gemma4_RP_Companion_abliterated
- **Note** — Persona simulator, not a fandom-knowledge model. English-dominant.

## ▶️ Pick (Creative Writing & Roleplay)

### `n0404n0404/Qwen3.8-27B-Heretic-JP-Roleplay`

- **Variant** — heretic + QLoRA (18.8k JP roleplay convs) · **Arch** — qwen35 · **Params** — 27B
- **Context** — 256K · **In** — Text, Image · **Out** — text, ` thinking` reasoning
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — none (explicitly does not refuse)
- **URL** — https://ollama.com/n0404n0404/Qwen3.8-27B-Heretic-JP-Roleplay
- **Note** — Adult content. Every training target carries a full reasoning trace, so thinking survives the fine-tune. Vision projector ships with every tag.

### `LESSTHANSUPER/RP-INK-Qwen2.5-32b`

- **Variant** — finetune (allura-org) + i-matrix quant · **Arch** — qwen2 · **Params** — 32B
- **Context** — 32K · **In** — Text · **Out** — text, JSON tool calls
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/LESSTHANSUPER/RP-INK-Qwen2.5-32b
- **Note** — Praised for following the prompt's own writing style; rated above Magnum V4.

### `LESSTHANSUPER/THE_OMEGA_DIRECTIVE-Mistral_Small3.2-24b`

- **Variant** — finetune (ReadyArt) + i-matrix quant · **Arch** — mistral · **Params** — 24B
- **Context** — 128K · **In** — Text · **Out** — text, JSON tool calls
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/LESSTHANSUPER/THE_OMEGA_DIRECTIVE-Mistral_Small3.2-24b
- **Note** — "Unslop" — cliché LLM mannerisms trained out; keeps characters distinct in group chats.

### `LESSTHANSUPER/CLOUDY_MIXTRAL-22b`

- **Variant** — finetune (Cloudyu) + i-matrix quant · **Arch** — mixtral MoE · **Params** — 22B (2×13B, ~26B effective)
- **Context** — 4K · **In** — Text · **Out** — text, JSON tool calls
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/LESSTHANSUPER/CLOUDY_MIXTRAL-22b
- **Note** — Dual-expert roleplay. MoE speed with 22–24B prose quality.

### `HammerAI/omega-darker-gaslight-fever-dream`

- **Variant** — merge (ReadyArt) · **Arch** — llama · **Params** — 23.6B
- **Context** — 32K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/HammerAI/omega-darker-gaslight-fever-dream

### `nchapman/l3.1-70b-hanami-x1`

- **Variant** — finetune (Sao10K, over Euryale v2.2) · **Arch** — llama · **Params** — 70.6B
- **Context** — 128K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/nchapman/l3.1-70b-hanami-x1
- **Note** — `min_p` ≥ 0.1 recommended for Llama 3 types.

### `RoseRudolph/rudy-nemo-12b-v1`

- **Variant** — SLERP merge (Mistral NeMo + Rocinante) · **Arch** — llama · **Params** — 12.2B
- **Context** — 128K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/RoseRudolph/rudy-nemo-12b-v1
- **Note** — Optimized for long-context creative roleplay.

### `anthony-maio/remnantinstruct-8b`

- **Variant** — SLERP merge (Qwen3-8B + remnant-qwen3-8b) · **Arch** — qwen3 · **Params** — 8.2B
- **Context** — 40K · **In** — Text · **Out** — text, ` thinking` reasoning
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/anthony-maio/remnantinstruct-8b
- **Note** — Attention layers favor the creative tune in deeper layers; MLP layers anchor facts from base. Thinking mode preserved.

### `sayitfast/CreativeWriting`

- **Variant** — finetune + RL (grammar/writing datasets) · **Arch** — qwen3 · **Params** — 4.4B
- **Context** — 256K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/sayitfast/CreativeWriting
- **Note** — Q8_0 only. Recommended: temp 1.0, repeat_penalty 1.1, top_k 40, top_p 0.95, min_p 0.05.

### `ehartford/samantha-1.1-westlake-7b`

- **Variant** — finetune (Samantha-1.1 dataset on Westlake-7b) · **Arch** — llama · **Params** — 7B
- **Context** — 32K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Reduced · **Refusals** — partial (trained not to, but drifts)
- **URL** — https://ollama.com/ehartford/samantha-1.1-westlake-7b
- **Note** — Companion AI persona. Prompt template on the card.

### `aroonv/dolphin31`

- **Variant** — finetune (Dolphin 3.0 for roleplay) · **Arch** — llama · **Params** — 8.0B
- **Context** — 128K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/aroonv/dolphin31

## ▶️ Pick (NSFW / Erotica)

> ❗ Adult content. Listed for catalog completeness; each card carries its own usage warnings.

### `jimscard/new-adult-writer`

- **Variant** — finetune + system prompt (successor to `adult-film-screenwriter-nsfw`) · **Arch** — gemma4 · **Params** — 8.0B
- **Context** — 128K · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/jimscard/new-adult-writer

### `jimscard/adult-film-screenwriter-nsfw`

- **Variant** — system prompt over dolphin-mistral · **Arch** — llama · **Params** — 7B
- **Context** — 32K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/jimscard/adult-film-screenwriter-nsfw
- **Note** — 29.2K downloads. Original purpose: verify uncensored models use adult language.

### `goonsai/qwen2.5-3B-goonsai-nsfw-100k`

- **Variant** — abliterated + finetune (100K prompt examples) · **Arch** — qwen2 · **Params** — 3.1B
- **Context** — 32K · **In** — Text · **Out** — text (comma-separated prompt keywords)
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — none ("will refuse nothing")
- **URL** — https://ollama.com/goonsai/qwen2.5-3B-goonsai-nsfw-100k
- **Note** — Completion-style. Emits image/video generation prompts. 38.7K downloads.

### `goonsai/qwen2.5-1.5B-goonsai-nsfw-10k`

- **Variant** — abliterated + finetune (10K, Civitai top uncensored prompts) · **Arch** — qwen2 · **Params** — 1.5B
- **Context** — 32K · **In** — Text · **Out** — text (prompt keywords)
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — none
- **URL** — https://ollama.com/goonsai/qwen2.5-1.5B-goonsai-nsfw-10k

### `windffooxxyang1130764/mesugaki-R18G`

- **Variant** — abliterated base + character finetune · **Arch** — qwen3 · **Params** — 4.0B
- **Context** — 40K · **In** — Text · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — none
- **URL** — https://ollama.com/windffooxxyang1130764/mesugaki-R18G
- **Note** — Yuri / R18G roleplay persona. Recommended temp 1.4, repeat penalty 1.35.

### `draganis/vanessa`

- **Variant** — backup of `benevolentjoker/nsfwvanessa` · **Arch** — llama · **Params** — 8.0B
- **Context** — 8K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/draganis/vanessa
- **Note** — 11.3K downloads.

## ▶️ Pick (Distilled)

Reasoning distilled from frontier models. Not all are uncensored — noted per entry.

### `mgraffam/qwen3.8-coder390`

- **Variant** — distilled (Opus 5.5, GPT 6 Astra, Grok 4.7) · **Arch** — qwen35 · **Params** — 27B
- **Context** — 256K · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Censored · **Refusals** — —
- **URL** — https://ollama.com/mgraffam/qwen3.8-coder390

### `kwangsuklee/Qwen3.5-9B-Claude-4.6-Opus-Reasoning-Distilled-GGUF`

- **Variant** — distilled (Claude 4.6 Opus CoT) · **Arch** — qwen35 · **Params** — 9.0B
- **Context** — 256K · **In** — Text · **Out** — text, thinking
- **Bench** — —
- **Censor** — Censored · **Refusals** — —
- **URL** — https://ollama.com/kwangsuklee/Qwen3.5-9B-Claude-4.6-Opus-Reasoning-Distilled-GGUF
- **Note** — 25K downloads. Higher-quality reasoning trajectories in science, instruction-following, math.

### `ImpurestClub/qwen3.5-27B-claude-4.6-opus-reasoning-distilled-v2-q3km`

- **Variant** — distilled (Claude 4.6 Opus) · **Arch** — qwen35 · **Params** — 26.9B
- **Context** — 256K · **In** — Text · **Out** — text, thinking
- **Bench** — —
- **Censor** — Censored · **Refusals** — —
- **URL** — https://ollama.com/ImpurestClub/qwen3.5-27B-claude-4.6-opus-reasoning-distilled-v2-q3km

### `yanjia/Qwen3.6-35B-A3B-Opus4.7-Reasoning-Distilled`

- **Variant** — distilled (Claude Opus 4.7 reasoning) · **Arch** — qwen3.6 MoE · **Params** — 35B-A3B
- **Context** — 256K · **In** — Text · **Out** — text, thinking
- **Bench** — —
- **Censor** — Censored · **Refusals** — —
- **URL** — https://ollama.com/yanjia/Qwen3.6-35B-A3B-Opus4.7-Reasoning-Distilled
- **Note** — Q4_K_M ~21 GB, ≥24 GB VRAM. Strong zh/en QA, reasoning, writing, code, long context.

### `cryptidbleh/gemma4-claude-opus-4.6`

- **Variant** — distilled (Claude Opus 4.6 reasoning dataset) · **Arch** — gemma4 · **Params** — 4.6B
- **Context** — 128K · **In** — Text · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Censored · **Refusals** — —
- **URL** — https://ollama.com/cryptidbleh/gemma4-claude-opus-4.6
- **Note** — Gemma 4 E2B base. No vision.

### `cryptidbleh/gemma4-claude-sonnet-4.6`

- **Variant** — distilled (Claude Sonnet 4.6) · **Arch** — gemma4 · **Params** — E2B
- **Context** — 128K · **In** — Text · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Censored · **Refusals** — —
- **URL** — https://ollama.com/cryptidbleh/gemma4-claude-sonnet-4.6

### `evanollama/qwen3.5-opus-4.7-distilled`

- **Variant** — distilled (Opus 4.7) · **Arch** — qwen35 · **Params** — —
- **Context** — 256K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Censored · **Refusals** — —
- **URL** — https://ollama.com/evanollama/qwen3.5-opus-4.7-distilled
- **Note** — 529 MB.

### `pdurugyan/qwen3.5-9b-deepseek-v4-flash-Q4_K_M-v_2`

- **Variant** — distilled (DeepSeek V4 Flash) + vision · **Arch** — qwen35 · **Params** — 9B
- **Context** — 256K · **In** — Text, Image · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Censored · **Refusals** — —
- **URL** — https://ollama.com/pdurugyan/qwen3.5-9b-deepseek-v4-flash-Q4_K_M-v_2
- **Note** — 7.2K downloads. Needs Ollama v0.30.0-rc20+.

### `reaperdoesntrun/Qwen3-Distilled-Coder-1.7B`

- **Variant** — distilled (coder teacher → STEM → logical inference SFT) · **Arch** — qwen3 · **Params** — 1.7B
- **Context** — 40K · **In** — Text · **Out** — text, thinking, JSON tool calls
- **Bench** — —
- **Censor** — Censored · **Refusals** — —
- **URL** — https://ollama.com/reaperdoesntrun/Qwen3-Distilled-Coder-1.7B
- **Note** — 1.3 GB. Structured reasoning in ~1.2 GB.

## ▶️ Pick (Dolphin & Library Uncensored)

The `library/*` namespace is Ollama's own curated set — highest download counts, oldest snapshots.

### `library/dolphin3`

- **Variant** — finetune (Eric Hartford, Dolphin 3.0) · **Arch** — llama · **Params** — 8.0B
- **Context** — 128K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/library/dolphin3
- **Note** — 4.2M downloads. Coding, math, agentic, function calling.

### `library/megadolphin`

- **Variant** — merge (Dolphin-2.2-70b interleaved with itself) · **Arch** — llama · **Params** — 120B
- **Context** — 4K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/library/megadolphin
- **Note** — 583.3K downloads. 68 GB at Q4_0.

### `library/dolphin-mixtral`

- **Variant** — finetune (Eric Hartford) · **Arch** — mixtral MoE · **Params** — 46.7B (8x7b / 8x22b)
- **Context** — 32K / 64K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/library/dolphin-mixtral
- **Note** — 1.9M downloads.

### `library/dolphin-mistral`

- **Variant** — finetune (Eric Hartford, v2.8) · **Arch** — llama · **Params** — 7B
- **Context** — 32K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/library/dolphin-mistral
- **Note** — 1.8M downloads. Excels at coding.

### `library/dolphin-llama3`

- **Variant** — finetune (Eric Hartford, Dolphin 2.9) · **Arch** — llama · **Params** — 8B / 70B
- **Context** — 8K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/library/dolphin-llama3
- **Note** — 2.2M downloads.

### `library/dolphincoder`

- **Variant** — finetune (Dolphin family, StarCoder2 base) · **Arch** — starcoder2 · **Params** — 7B / 15B
- **Context** — 16K · **In** — Text · **Out** — text (code)
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/library/dolphincoder
- **Note** — 1M downloads.

### `library/dolphin-phi`

- **Variant** — finetune (Eric Hartford, Phi base) · **Arch** — phi2 · **Params** — 3B
- **Context** — 2K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/library/dolphin-phi
- **Note** — 1.6M downloads.

### `library/llama2-uncensored`

- **Variant** — finetune (George Sung, Jarrad Hope) · **Arch** — llama · **Params** — 7B / 70B
- **Context** — 2K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/library/llama2-uncensored
- **Note** — 2.7M downloads. Legacy; 2K context.

### `library/wizard-vicuna-uncensored`

- **Variant** — finetune (Eric Hartford, Llama 2 base) · **Arch** — llama · **Params** — 7B / 13B / 30B
- **Context** — 2K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/library/wizard-vicuna-uncensored
- **Note** — 1.3M downloads.

### `library/wizardlm-uncensored`

- **Variant** — finetune (Wizard LM, uncensored) · **Arch** — llama · **Params** — 13B
- **Context** — 4K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/library/wizardlm-uncensored
- **Note** — 661.2K downloads.

### `library/everythinglm`

- **Variant** — finetune (Llama 2 base) · **Arch** — llama · **Params** — 13B
- **Context** — 16K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/library/everythinglm
- **Note** — 581.3K downloads.

### `library/r1-1776`

- **Variant** — post-trained (Perplexity, DeepSeek-R1 base) · **Arch** — llama · **Params** — 70.6B / 671B
- **Context** — 128K · **In** — Text · **Out** — text, ` thinking` reasoning
- **Bench** — —
- **Censor** — Reduced (unbiased/factual post-training, not abliteration) · **Refusals** — —
- **URL** — https://ollama.com/library/r1-1776
- **Note** — 430.5K downloads. Post-trained for unbiased, accurate, factual output.

### `library/nous-hermes2`

- **Variant** — finetune (Nous Research) · **Arch** — llama · **Params** — 10.7B / 34B
- **Context** — 4K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Reduced · **Refusals** — —
- **URL** — https://ollama.com/library/nous-hermes2
- **Note** — 1.1M downloads. Scientific discussion and coding.

### `thomboe/dolphin3.0-llama3.1-8b-abliterated`

- **Variant** — abliterated (Dolphin 3.0 base) · **Arch** — llama · **Params** — 8.0B
- **Context** — 128K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/thomboe/dolphin3.0-llama3.1-8b-abliterated

### `mparvin/dolphin-qwen3-4b-abliterated`

- **Variant** — abliterated (Dolphin + Qwen3) · **Arch** — qwen3 · **Params** — 4.0B
- **Context** — 32K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/mparvin/dolphin-qwen3-4b-abliterated

### `mparvin/dolphin3-cyber-8b-gguf`

- **Variant** — finetune (Dolphin 3.0 cyber) · **Arch** — llama · **Params** — 8B
- **Context** — 128K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/mparvin/dolphin3-cyber-8b-gguf

### `altalus/my-uncensored-dolphin-70b`

- **Variant** — finetune (Dolphin 70B) · **Arch** — llama · **Params** — 70.6B
- **Context** — 8K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/altalus/my-uncensored-dolphin-70b

### `altalus/my-uncensored-dolphin-8b`

- **Variant** — finetune (Dolphin 8B) · **Arch** — llama · **Params** — 8.0B
- **Context** — 8K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/altalus/my-uncensored-dolphin-8b

## ▶️ Pick (Small & Edge)

### `vatistasdim/Cipher-Abliterated`

- **Variant** — abliterated · **Arch** — llama · **Params** — 3.2B
- **Context** — 128K · **In** — Text · **Out** — text, JSON tool calls
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/vatistasdim/Cipher-Abliterated
- **Note** — 278.4K downloads. Adaptive conversational AI, structured output preserved.

### `s1gnature/qwen3.5-uncensored-low-end`

- **Variant** — uncensored · **Arch** — qwen35 · **Params** — —
- **Context** — 256K (32K effective for low-end) · **In** — Text, Image · **Out** — text
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/s1gnature/qwen3.5-uncensored-low-end

### `edtorre/hermes-qwen3.5-9b-abliterated`

- **Variant** — abliterated (Hermes + Qwen3.5) · **Arch** — qwen35 · **Params** — 8.95B
- **Context** — 256K · **In** — Text, Image · **Out** — text
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/edtorre/hermes-qwen3.5-9b-abliterated

### `sachasayan/interimlabs-huihui-qwen3.5-9b-abliterated`

- **Variant** — abliterated (huihui) · **Arch** — qwen35 · **Params** — 9B
- **Context** — 256K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/sachasayan/interimlabs-huihui-qwen3.5-9b-abliterated
- **Note** — Standalone Q4_K_M packaging by Interim Labs.

### `richardyoung/qwen3-8b-abliterated`

- **Variant** — abliterated · **Arch** — qwen3 · **Params** — 8B
- **Context** — 40K · **In** — Text · **Out** — text, ` thinking` reasoning
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/richardyoung/qwen3-8b-abliterated

### `richardyoung/gemma-7b-it-abliterated`

- **Variant** — abliterated · **Arch** — gemma · **Params** — 7B
- **Context** — 8K · **In** — Text · **Out** — text
- **Bench** — —
- **Censor** — Abliterated · **Refusals** — —
- **URL** — https://ollama.com/richardyoung/gemma-7b-it-abliterated

### `codysrromero/Dolphin-uncensored`

- **Variant** — finetune (Dolphin) · **Arch** — llama · **Params** — 3.2B
- **Context** — 128K · **In** — Text · **Out** — text, JSON tool calls
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/codysrromero/Dolphin-uncensored

### `uncensoredai/diddy`

- **Variant** — finetune (coding assistant) · **Arch** — llama · **Params** — 1.5B
- **Context** — 128K · **In** — Text · **Out** — text, JSON tool calls
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/uncensoredai/diddy
- **Note** — 955 MB. Lightweight coding assistant.

### `zarigata/unfiltered-llama3`

- **Variant** — finetune (unfiltered Llama 3) · **Arch** — llama · **Params** — 3.61B
- **Context** — 128K · **In** — Text · **Out** — text, JSON tool calls
- **Bench** — —
- **Censor** — Uncensored · **Refusals** — —
- **URL** — https://ollama.com/zarigata/unfiltered-llama3

## ▶️ Compare (Quick Matrix)

| Model                                            | Params  | Context | In  | Censor      | Refusals | KL     |
| ------------------------------------------------ | ------- | ------- | --- | ----------- | -------- | ------ |
| `orcarouter/Qwen3.8-27B-Uncensored`              | 27.3B   | 256K    | T+I | Abliterated | 0–6%     | —      |
| `huihui_ai/Qwen3.8-abliterated`                  | 27.3B   | 256K    | T+I | Abliterated | —        | —      |
| `mannix/Bonsai-2-27B-CRACK`                      | 27.3B   | 256K    | T+I | Abliterated | 0/320    | —      |
| `lucloner/qwen38-27b-turbofcfusion`              | 26.9B   | 262K    | T   | Uncensored  | —        | —      |
| `Maternion/spark-x2.5-heretic`                   | 4B      | 1M      | T   | Abliterated | 7/100    | 0.0490 |
| `Maternion/mimo-v2.6-heretic`                    | 9B      | 256K    | T+I | Abliterated | 20/100   | 0.0171 |
| `Maternion/minicpm5-heretic`                     | 2.5B    | 128K    | T   | Abliterated | 10/100   | 0.0349 |
| `richardyoung/agents-a1-4b-heretic`              | 4.2B    | 256K    | T+I | Abliterated | 11/100   | 0.060  |
| `richardyoung/deepseek-r1-0528-qwen3-8b-heretic` | 8.2B    | 128K    | T   | Abliterated | 4/100    | 0.041  |
| `richardyoung/granite-4.2-8b-heretic`            | 8.8B    | 128K    | T   | Abliterated | 12/100   | 0.080  |
| `richardyoung/phi-4-mini-instruct-heretic`       | 3.8B    | 128K    | T   | Abliterated | 5/100    | 0.038  |
| `richardyoung/muse-glimmer-30b-heretic`          | 27.9B   | 128K    | T+I | Abliterated | 52/100   | 0.063  |
| `richardyoung/frognano-4b-2609-heretic`          | 4.2B    | 256K    | T+I | Abliterated | 12/100   | 0.040  |
| `richardyoung/lfm2.5-1.2b-instruct-heretic`      | 1.2B    | 125K    | T   | Abliterated | 3/100    | 0.059  |
| `R4C3R/qwen3-8b-heretic`                         | 8B      | 40K     | T   | Abliterated | 10/100   | 0.036  |
| `R4C3R/qwen2.5-14b-instruct-heretic`             | 14B     | 32K     | T   | Abliterated | 14/100   | 0.06   |
| `s1gnature/gemma4-uncensored-mtp`                | E4B/E2B | 32K     | T+I | Uncensored  | 0/465    | —      |
| `ninjasatwork/gemma4_RP_Companion_abliterated`   | 11.9B   | 8K      | T   | Abliterated | none     | —      |
| `n0404n0404/Qwen3.8-27B-Heretic-JP-Roleplay`     | 27B     | 256K    | T+I | Abliterated | none     | —      |
| `library/dolphin3`                               | 8.0B    | 128K    | T   | Uncensored  | —        | —      |
| `library/megadolphin`                            | 120B    | 4K      | T   | Uncensored  | —        | —      |
| `library/r1-1776`                                | 70.6B   | 128K    | T   | Reduced     | —        | —      |
| `vatistasdim/Cipher-Abliterated`                 | 3.2B    | 128K    | T   | Abliterated | —        | —      |

## ▶️ Note (Caveats)

- **Refusal numbers are not comparable across publishers.** `Maternion` and `richardyoung` score on `LLM-LAT/harmful-dataset` with the reasoning block skipped; `orcarouter` uses a rule-based opening-phrase classifier; `mannix` uses HarmBench-320. Read each card's methodology.
- **KL divergence is the capability-damage proxy.** <0.10 is excellent, <0.5 is the usual "no damage" threshold. `muse-glimmer-30b-heretic` deliberately keeps 52/100 refusals rather than exceed KL 0.10.
- **No model is fully uncensored.** Residual refusals baked into SFT (not the refusal direction) survive abliteration — `minicpm5-heretic` documents a hard floor at 5–6/100 across three independent builds.
- **Quantization degrades uncensoring.** `orcarouter` notes extra capability loss at q2_K / q3; `minicpm5-heretic` recommends Q8_0 or higher.
- **Non-stock runtimes.** `mannix/Bonsai-2-27B-CRACK` (PQ2_0 ternary) needs xOllama v0.40.3+. `Ermzzz999/qwen3.6-35b-a3b-abliterated-nvfp4-mtp` (nvfp4) is Apple Silicon only.
- **Registry metadata gaps.** Ollama's registry returns 404 for some community namespaces, so `Arch` / `Params` fall back to the model card or `—`.
- **Legal.** Abliterated and NSFW models ship with no safety layer. Each card places deployment, moderation and legal compliance on the operator.
