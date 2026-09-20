#!/usr/bin/env python3
"""Pin models from the second official Vision page and explicit Instruct variants.

No model download/inference. Preserve the initial catalog; produce a separate addendum.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from replay_vlm_frames import save
from run_ollama_fall_suite import registry_model


ADDITIONS = [
    'qwen3-vl:2b-instruct', 'qwen3-vl:4b-instruct', 'qwen3-vl:8b-instruct',
    'qwen3-vl:30b-a3b-instruct', 'qwen3-vl:32b-instruct',
    'devstral-small-2:24b', 'translategemma:4b', 'translategemma:12b',
    'translategemma:27b', 'deepseek-ocr:3b', 'llama4:scout', 'llama4:maverick',
    'qwen3.8-flash-next:125b-a6b-q4_K_M',
]


def main():
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with ThreadPoolExecutor(max_workers=4) as pool:
        models = list(pool.map(registry_model, ADDITIONS))
    save(args.output, dict(
        created_utc=datetime.now(timezone.utc).isoformat(), models=models,
        scope='Vision page 2 additions and explicitly named Qwen3-VL Instruct variants; '
              'OCR/translation models remain labeled special-purpose comparisons',
        sources=['https://ollama.com/search?c=vision&page=2',
                 'https://ollama.com/library/qwen3-vl/tags']))
    for m in models:
        print(m['name'], m['status'], flush=True)


if __name__ == '__main__':
    main()
